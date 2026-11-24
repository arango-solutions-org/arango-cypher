"""Layer 5 — EXPLAIN-plan tenant-scope validator (the security boundary).

This module implements the safety boundary defined by
``docs/multitenant_prd.md`` §1.2 / §8 and ``docs/agent_prompts_multitenant.md``
Wave 7 part 3:

  A query is *safe* if, for every collection it reads, one of these
  holds:
    1. The collection's physical layout kind is ``satellite``.
    2. The collection's physical layout kind is ``smartgraph`` AND the
       plan's access node carries a bind-var-based filter / index
       predicate of the form ``doc.<smartGraphAttribute> == @tenantId``
       whose ``@tenantId`` value equals the session's tenant id.
    3. The collection is ``Tenant`` (TENANT_ROOT) AND the access is
       keyed by ``@tenantKey`` (the session tenant's ``_key``).
    4. The access occurs inside a subquery or traversal whose
       enclosing constraint already guarantees the per-document
       tenant id matches ``@tenantId``.

``validate_plan`` calls ArangoDB ``EXPLAIN`` once per query and walks
the resulting plan, refusing anything that does not satisfy the above
**regardless of which upstream layer produced the AQL**. It trusts no
LLM, no guardrail, no AST pass, no transpiler — if Layer 5 passes, the
query is safe; if it refuses, the query does not execute.

This is the security boundary. Every refusal emits a structured
``TENANT_SCOPE_VIOLATION`` audit log line with both the AQL and plan
digests; every pass emits a ``TENANT_SCOPE_OK`` line with the same
digests. Both are required for audit replay — do not silence the OK
line for performance.

Wired through :func:`arango_query_core.exec.safe_execute` (Wave 7
part 4), which spreads ``{tenantId, tenantKey}`` from the session
over the client-supplied bind vars **last** so the session value
silently wins.

Layer 5 is independent of Wave 8a's Layers 3 / 4 (AST rewrites). The
predicates Layer 3 / 4 inject (``doc.<field> == @tenantId``) are the
shapes Layer 5 recognises here.

Bind-resolution sentinel (real-ArangoDB EXPLAIN)
------------------------------------------------
ArangoDB's EXPLAIN **resolves value bind parameters into literal
``value`` nodes** in the returned plan — a ``@tenantId`` reference
becomes ``{"type": "value", "value": "<the tenant id>"}``, and the
optimiser frequently fuses the injected ``FILTER`` *into* the
``EnumerateCollectionNode`` (``node["filter"]``) rather than leaving a
standalone ``CalculationNode``. A naïve "only accept a ``parameter``
node named tenantId" check therefore fails on every real query, while
a "accept any literal on the tenant field" check would re-open the T2
literal-smuggling hole (a caller could hard-code another tenant's id).

To get both safety and real-DB compatibility, :func:`validate_plan`
EXPLAINs with a fresh, unguessable **sentinel** substituted for
``tenantId`` (``bind_vars`` for execution are untouched). The plan
walker then accepts a tenant-field equality only when it compares
against (a) a ``parameter`` named ``tenantId`` — the hand-crafted /
``plan_override`` shape used by tests and any ArangoDB build that does
not fold the bind — or (b) a ``value`` literal **equal to the
per-call sentinel**. Because the sentinel is random per validation, a
caller cannot smuggle a matching literal, so a literal that is *not*
the sentinel is still refused as ``LITERAL_TENANT_PREDICATE``. The
walker also descends through ``AND`` conjunctions (never ``OR``) and
inspects the inline ``EnumerateCollectionNode.filter`` so a fused
predicate is recognised.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from threading import Lock
from typing import Any
from uuid import uuid4

from .nl2cypher.tenant_scope import EntityTenantRole, TenantScopeManifest

logger = logging.getLogger(__name__)

# MT-7 — dedicated admin cross-tenant bypass audit stream. Kept separate
# from the module logger (which carries the routine TENANT_SCOPE_OK /
# TENANT_SCOPE_VIOLATION lines) so operators can route, retain, and alert
# on privilege-escalation events independently (PRD §10: "Log every
# request to a separate audit stream with the bypass reason").
audit_logger = logging.getLogger("arango_cypher.tenant_audit")


# ---------------------------------------------------------------------------
# Public exceptions
# ---------------------------------------------------------------------------


class TenantScopeViolation(Exception):
    """Layer 5 refused the query as unsafe.

    Carries machine-actionable diagnostic fields:

    * ``code`` — short reason code (e.g. ``UNCONSTRAINED_COLLECTION_SCAN``,
      ``LITERAL_TENANT_PREDICATE``, ``TENANT_BIND_MISMATCH``). Used for
      log aggregation and red-team-corpus assertions.
    * ``message`` — human-readable description of the violation.
    * ``aql_digest`` — sha256 of the AQL plus its sorted bind vars; lets
      auditors replay the exact submission without storing the raw text.
    * ``plan_digest`` — sha256 of the EXPLAIN plan as JSON with
      ``sort_keys=True``; lets auditors recover the exact refused plan
      shape.
    """

    def __init__(
        self,
        *,
        code: str,
        message: str,
        aql_digest: str = "",
        plan_digest: str = "",
    ):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.aql_digest = aql_digest
        self.plan_digest = plan_digest


# ---------------------------------------------------------------------------
# MT-6 — plan-shape certification LRU (Layer 5 performance)
# ---------------------------------------------------------------------------
#
# The EXPLAIN round-trip + plan walk is the dominant cost of Layer 5
# (PRD §9: "One EXPLAIN round-trip per execute. Typical: 5–20 ms.").
# It is amortisable because the structural pass/fail decision depends
# ONLY on the query text and the schema — NOT on the tenant's bind-var
# *value*: :func:`validate_plan` EXPLAINs with a random per-call
# sentinel substituted for ``@tenantId``, so the resulting plan shape
# (and therefore whether the walk accepts it) is identical for every
# session running the same AQL against the same schema. We can thus
# cache "this (aql, schema) shape is structurally safe" and reuse it
# across sessions and across bind-var values.
#
# Safety invariants (why this cannot leak):
#   * The cache key fingerprints the AQL text **and** the full schema
#     inputs the walker reads (manifest roles / denorm fields /
#     tenant_entity, sharding profile, collection→entity map). Any
#     schema change yields a different key — a stale certification is
#     never consulted (PRD §9: "TTL bounded by mapping fingerprint so a
#     schema change invalidates certifications").
#   * Only structural PASSes are cached. Violations and transient
#     EXPLAIN failures are never cached (a transient DB error must not
#     poison the shape; a real violation is cheap to re-derive).
#   * The per-call bind-var checks (session has a tenant; a present
#     ``@tenantId`` bind equals the session tenant) ALWAYS run, even on
#     a cache hit — the cache only skips the EXPLAIN + structural walk,
#     never the session-identity gate.
#   * Every pass is still logged (with the certified plan digest and a
#     ``cache=hit`` marker) so the audit trail from PRD §9 is intact.
#
# The cache is thread-safe (FastAPI runs sync routes on a threadpool)
# and bounded (LRU eviction). ``TENANT_PLAN_CACHE_SIZE=0`` disables it.


@dataclass(frozen=True)
class _CachedCertification:
    """A cached Layer-5 structural PASS for one (aql, schema) shape.

    ``touches_tenant_data`` is memoised so the per-call bind-var gate
    can run on a cache hit without re-walking the plan. ``plan_digest``
    is the digest of the plan that was certified, echoed into the
    ``TENANT_SCOPE_OK`` audit line on subsequent hits.
    """

    touches_tenant_data: bool
    plan_digest: str


class _PlanCertificationCache:
    """Bounded, thread-safe LRU of structural Layer-5 certifications.

    Keyed by an ``(aql_hash, schema_hash)`` digest (see
    :func:`_plan_cache_key`). ``maxsize <= 0`` disables caching
    entirely (env kill-switch). ``ttl_seconds > 0`` adds a wall-clock
    freshness bound on top of the fingerprint-based invalidation
    (defence in depth against optimiser plan drift on an unchanged
    schema); ``0`` relies solely on the schema fingerprint in the key.
    """

    def __init__(self, *, maxsize: int, ttl_seconds: float) -> None:
        self._maxsize = maxsize
        self._ttl = ttl_seconds
        self._data: OrderedDict[str, tuple[float, _CachedCertification]] = OrderedDict()
        self._lock = Lock()
        self._hits = 0
        self._misses = 0

    @property
    def enabled(self) -> bool:
        return self._maxsize > 0

    def get(self, key: str) -> _CachedCertification | None:
        if not self.enabled:
            return None
        with self._lock:
            item = self._data.get(key)
            if item is None:
                self._misses += 1
                return None
            ts, cert = item
            if self._ttl > 0 and (time.monotonic() - ts) > self._ttl:
                # Expired — treat as a miss and drop the stale entry.
                del self._data[key]
                self._misses += 1
                return None
            self._data.move_to_end(key)
            self._hits += 1
            return cert

    def put(self, key: str, cert: _CachedCertification) -> None:
        if not self.enabled:
            return
        with self._lock:
            self._data[key] = (time.monotonic(), cert)
            self._data.move_to_end(key)
            while len(self._data) > self._maxsize:
                self._data.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()
            self._hits = 0
            self._misses = 0

    def stats(self) -> dict[str, Any]:
        with self._lock:
            total = self._hits + self._misses
            return {
                "enabled": self.enabled,
                "size": len(self._data),
                "maxsize": self._maxsize,
                "ttl_seconds": self._ttl,
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate": (self._hits / total) if total else 0.0,
            }


def _cache_maxsize_from_env() -> int:
    try:
        return int(os.getenv("TENANT_PLAN_CACHE_SIZE", "512"))
    except ValueError:
        return 512


def _cache_ttl_from_env() -> float:
    try:
        return max(0.0, float(os.getenv("TENANT_PLAN_CACHE_TTL_SECONDS", "0")))
    except ValueError:
        return 0.0


_PLAN_CACHE = _PlanCertificationCache(
    maxsize=_cache_maxsize_from_env(),
    ttl_seconds=_cache_ttl_from_env(),
)


def plan_cache_stats() -> dict[str, Any]:
    """Return Layer-5 plan-cache counters (PRD §9 hit-rate reporting)."""
    return _PLAN_CACHE.stats()


def reset_plan_cache() -> None:
    """Clear the plan-cache and its counters.

    Intended for tests and for an operator-triggered schema-refresh
    hook; production correctness never depends on it because the schema
    fingerprint is part of the cache key.
    """
    _PLAN_CACHE.clear()


def _schema_fingerprint(
    manifest: TenantScopeManifest,
    sharding_profile: dict[str, Any] | None,
    collection_to_entity: dict[str, str] | None,
) -> str:
    """Canonical digest of every schema input the plan walker reads.

    Captures the manifest fields that drive per-collection role /
    tenant-field decisions (``tenant_entity`` and each entity's role,
    denorm field, reachability and scoping path), the sharding profile
    (layout kinds + smartgraph attributes), and the collection→entity
    map. Anything that could change a structural pass/fail decision is
    in here, so a differing schema always yields a differing key.
    """
    entities = {}
    for name, scope in manifest.entities.items():
        role = getattr(scope, "role", None)
        entities[name] = {
            "role": role.value if isinstance(role, EntityTenantRole) else str(role),
            "denorm_field": getattr(scope, "denorm_field", None),
            "reachable_from_tenant": getattr(scope, "reachable_from_tenant", None),
            "scoping_path": list(getattr(scope, "scoping_path", ()) or ()),
        }
    canonical = json.dumps(
        {
            "tenant_entity": manifest.tenant_entity,
            "entities": entities,
            "sharding_profile": sharding_profile or {},
            "collection_to_entity": collection_to_entity or {},
        },
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _plan_cache_key(
    aql: str,
    manifest: TenantScopeManifest,
    sharding_profile: dict[str, Any] | None,
    collection_to_entity: dict[str, str] | None,
) -> str:
    payload = aql + "\0" + _schema_fingerprint(manifest, sharding_profile, collection_to_entity)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def validate_plan(
    *,
    db: Any,
    aql: str,
    bind_vars: dict[str, Any],
    manifest: TenantScopeManifest | None,
    sharding_profile: dict[str, Any] | None,
    collection_to_entity: dict[str, str] | None = None,
    session: Any,
    plan_override: dict[str, Any] | None = None,
    admin_bypass: bool = False,
    bypass_reason: str = "",
) -> None:
    """Refuse the query if its EXPLAIN plan violates §1.2.

    MT-7 — admin cross-tenant bypass. When ``admin_bypass`` is set AND
    the session is flagged ``is_admin`` AND a non-empty ``bypass_reason``
    is supplied, the tenant-scope enforcement (the bind-var identity gate
    and the per-node tenant-predicate walk) is skipped so an operator may
    legitimately span tenants (PRD §10). The **structural** checks are
    NOT bypassed — the query is still EXPLAINed and a failed / malformed
    plan is still refused. Every honoured bypass emits an
    ``ADMIN_CROSS_TENANT_BYPASS`` line to the dedicated
    ``arango_cypher.tenant_audit`` stream with the reason and digests. A
    bypass requested without admin rights or without a reason is ignored
    (falls through to normal enforcement) — defence in depth behind the
    route's own gate. The plan-shape cache is skipped for bypass calls
    (they never run the structural walk, so must not populate or read the
    certification LRU).

    Algorithm (PRD §8.2):

    1. Fetch ``plan = db.aql.explain(aql, bind_vars=bind_vars)["plan"]``.
       (When ``plan_override`` is supplied — used by unit tests — skip
       the round-trip and use it directly. The override path is only
       activated when explicitly passed; it never substitutes for a
       missing ``db`` in production.)
    2. Walk the plan nodes and classify each into ``satellite``,
       ``tenant_root`` (Tenant collection), ``tenant_scoped``, or
       ``unknown``. If any node is ``tenant_scoped`` (i.e. the plan
       reads a smartgraph collection or a manifest-tagged
       ``TENANT_SCOPED`` entity), the query is *tenant-touching* and
       the bind-var sanity check must pass:

       * ``session.tenant_id`` must not be ``None``.
       * **If** ``bind_vars`` carries a ``tenantId``, it must equal
         ``session.tenant_id``. When ``tenantId`` is absent (the AQL
         never references ``@tenantId`` — e.g. an unconstrained scan, or
         a storage-isolated disjoint-smartgraph traversal) the precise
         per-node walk below decides: it refuses an unconstrained scan
         with ``UNCONSTRAINED_COLLECTION_SCAN`` and accepts a disjoint
         smartgraph. Layer 6 (``safe_execute``) only injects the
         ``@tenantId`` bind when the query references it, so ``tenantId``
         being present here already implies it equals the session value
         — the equality check is retained as defence in depth.

       If the plan touches **only** satellite / global / Tenant-by-key
       collections, the bind-var check is **not** required — pure
       reference-data queries (e.g. ``FOR c IN Country RETURN c``)
       remain executable without a tenant binding.

    3. Per node type, refuse anything not covered by §1.2 (see helpers
       below).

    4. Emit a structured audit log line (pass *and* refuse).

    Raises :class:`TenantScopeViolation` on refusal.
    """
    # Bind-resolution sentinel: real ArangoDB EXPLAIN folds the
    # ``@tenantId`` value bind into literal ``value`` nodes, so we
    # cannot tell a session-injected ``@tenantId`` from a caller-
    # smuggled literal by node *type* alone. We EXPLAIN with a fresh
    # unguessable sentinel substituted for ``tenantId`` (execution
    # bind_vars are NOT touched — Layer 6 executes with the real
    # value); the walker then accepts a tenant-field equality against
    # the sentinel as proof the predicate is the resolved session
    # bind, and refuses any *other* literal. ``plan_override`` callers
    # (unit tests) skip EXPLAIN entirely and so have no sentinel — the
    # walker falls back to accepting only ``parameter``-typed
    # ``@tenantId`` nodes, exactly as before.
    # MT-7 — is the caller an authorised cross-tenant admin? Honoured
    # only for a session flagged is_admin with a non-empty reason; an
    # unauthorised bypass request falls through to normal enforcement.
    bypass_active = bool(admin_bypass) and bool(getattr(session, "is_admin", False)) and bool(bypass_reason)

    # MT-6 — plan-shape cache. When the same (aql, schema) shape has
    # already been certified we skip the EXPLAIN + structural walk, but
    # still run the per-call bind-var gate and emit the audit line. The
    # cache is only consulted on the real-EXPLAIN path; ``plan_override``
    # (unit tests) always walks the supplied plan so tests stay
    # deterministic and never populate the cache. Bypass calls skip the
    # cache entirely — they don't run the structural walk, so a cached
    # verdict is neither produced nor consulted for them.
    cache_key: str | None = None
    if plan_override is None and not bypass_active and manifest is not None:
        cache_key = _plan_cache_key(aql, manifest, sharding_profile, collection_to_entity)
        cached = _PLAN_CACHE.get(cache_key)
        if cached is not None:
            digests = _digests(aql=aql, bind_vars=bind_vars, plan=None)
            digests["plan_digest"] = cached.plan_digest
            _enforce_tenant_bindvars(
                touches_tenant_data=cached.touches_tenant_data,
                bind_vars=bind_vars,
                session=session,
                digests=digests,
            )
            _log_pass(session=session, cached=True, **digests)
            return

    tenant_sentinel: str | None = None
    if plan_override is not None:
        plan = plan_override
    else:
        explain_bind_vars = dict(bind_vars or {})
        if "tenantId" in explain_bind_vars:
            tenant_sentinel = f"__tenant_sentinel_{uuid4().hex}__"
            explain_bind_vars["tenantId"] = tenant_sentinel
        try:
            result = db.aql.explain(aql, bind_vars=explain_bind_vars)
        except Exception as exc:
            digests = _digests(aql=aql, bind_vars=bind_vars, plan=None)
            violation = TenantScopeViolation(
                code="EXPLAIN_FAILED",
                message=f"EXPLAIN failed: {type(exc).__name__}: {exc}",
                **digests,
            )
            _log_violation(violation, session=session)
            raise violation from exc
        plan = _coerce_plan(result)

    if not isinstance(plan, dict):
        digests = _digests(aql=aql, bind_vars=bind_vars, plan=None)
        violation = TenantScopeViolation(
            code="EXPLAIN_MALFORMED",
            message=f"EXPLAIN returned non-dict plan: {type(plan).__name__}",
            **digests,
        )
        _log_violation(violation, session=session)
        raise violation

    # MT-7 — authorised admin cross-tenant bypass. The plan is structurally
    # sound (it EXPLAINed and coerced to a dict); skip the tenant-scope
    # enforcement and record the privilege-escalation event on the audit
    # stream. Reached only for is_admin sessions with a reason.
    if bypass_active:
        digests = _digests(aql=aql, bind_vars=bind_vars, plan=plan)
        _log_admin_bypass(session=session, reason=bypass_reason, **digests)
        return

    if manifest is None:
        # Every current caller refuses before reaching here without a
        # manifest, but this is a public security entry point: without one
        # there is nothing to enforce against, so fail closed with a
        # violation — never an AttributeError from inside the walk.
        digests = _digests(aql=aql, bind_vars=bind_vars, plan=plan)
        violation = TenantScopeViolation(
            code="NO_TENANT_MANIFEST",
            message="no tenant-scope manifest to validate against; refusing",
            **digests,
        )
        _log_violation(violation, session=session)
        raise violation

    raw_nodes = plan.get("nodes")
    nodes: list[dict[str, Any]] = raw_nodes if isinstance(raw_nodes, list) else []

    walker = _PlanWalker(
        plan=plan,
        bind_vars=bind_vars,
        manifest=manifest,
        sharding_profile=sharding_profile or {},
        collection_to_entity=collection_to_entity or {},
        tenant_sentinel=tenant_sentinel,
    )
    touches_tenant_data = walker.classify_touches_tenant_data(nodes)

    digests = _digests(aql=aql, bind_vars=bind_vars, plan=plan)

    _enforce_tenant_bindvars(
        touches_tenant_data=touches_tenant_data,
        bind_vars=bind_vars,
        session=session,
        digests=digests,
    )

    walker.walk(nodes, digests=digests, session=session)

    # Certification succeeded — memoise the structural verdict for this
    # (aql, schema) shape so future calls skip the EXPLAIN + walk. Only
    # reached on the real-EXPLAIN path (``cache_key`` is ``None`` for
    # ``plan_override`` callers).
    if cache_key is not None:
        _PLAN_CACHE.put(
            cache_key,
            _CachedCertification(
                touches_tenant_data=touches_tenant_data,
                plan_digest=digests["plan_digest"],
            ),
        )

    _log_pass(
        session=session,
        aql_digest=digests["aql_digest"],
        plan_digest=digests["plan_digest"],
    )


def _enforce_tenant_bindvars(
    *,
    touches_tenant_data: bool,
    bind_vars: dict[str, Any],
    session: Any,
    digests: dict[str, str],
) -> None:
    """Per-call tenant-identity gate (runs on every validate, incl. cache hits).

    Refuses a tenant-touching query when the session has no bound tenant
    (``NO_SESSION_TENANT``) or when a *present* ``@tenantId`` bind
    disagrees with the session tenant (``TENANT_BIND_MISMATCH``). An
    absent ``@tenantId`` bind is not a mismatch — the per-node walk (or
    the cached structural verdict) is what decides those shapes. This is
    intentionally independent of the plan so it stays correct behind the
    MT-6 plan-shape cache, which skips the walk but never this gate.
    """
    if not touches_tenant_data:
        return
    session_tenant = getattr(session, "tenant_id", None)
    if session_tenant is None:
        violation = TenantScopeViolation(
            code="NO_SESSION_TENANT",
            message=("session has no tenant_id; cannot validate tenant-scoped query under tenant-user mode"),
            **digests,
        )
        _log_violation(violation, session=session)
        raise violation

    if "tenantId" in bind_vars and bind_vars["tenantId"] != session_tenant:
        bv_tenant = bind_vars["tenantId"]
        violation = TenantScopeViolation(
            code="TENANT_BIND_MISMATCH",
            message=(
                f"bind_vars['tenantId']={bv_tenant!r} does not match session.tenant_id={session_tenant!r}"
            ),
            **digests,
        )
        _log_violation(violation, session=session)
        raise violation


# ---------------------------------------------------------------------------
# Plan walker (the heart of Layer 5)
# ---------------------------------------------------------------------------


@dataclass
class _PlanWalker:
    plan: dict[str, Any]
    bind_vars: dict[str, Any]
    manifest: TenantScopeManifest
    sharding_profile: dict[str, Any]
    collection_to_entity: dict[str, str]
    # Per-call random sentinel substituted for ``tenantId`` at EXPLAIN
    # time so a resolved bind literal can be told apart from a smuggled
    # one. ``None`` on the ``plan_override`` path (no EXPLAIN ran) — the
    # matchers then accept only ``parameter``-typed ``@tenantId`` nodes.
    tenant_sentinel: str | None = None

    _nodes_by_id: dict[Any, dict[str, Any]] = field(default_factory=dict)
    _calc_by_outvar: dict[str, dict[str, Any]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        nodes = self.plan.get("nodes") or []
        if isinstance(nodes, list):
            for n in nodes:
                if not isinstance(n, dict):
                    continue
                nid = n.get("id")
                if nid is not None:
                    self._nodes_by_id[nid] = n
                if n.get("type") == "CalculationNode":
                    outvar = _outvar_name(n)
                    if outvar:
                        self._calc_by_outvar[outvar] = n

    # ---- Layout / role lookups ---------------------------------------------

    def _layout_kind(self, collection: str) -> str:
        """Return ``satellite`` / ``smartgraph`` / ``regular`` / ``system`` /
        ``tenant-root`` / ``unknown`` for *collection*."""
        members = self.sharding_profile.get("members") or {}
        if isinstance(members, dict):
            member = members.get(collection)
            if isinstance(member, dict):
                kind = member.get("kind")
                if isinstance(kind, str) and kind:
                    return kind.lower()
        return "unknown"

    def _entity_of(self, collection: str) -> str | None:
        return self.collection_to_entity.get(collection) or (
            collection if collection in self.manifest.entities else None
        )

    def _tenant_field_for(self, collection: str) -> str | None:
        """Return the entity's denormalised tenant column, or the
        graph-level ``smartGraphAttribute`` when no denorm field is
        declared but the collection is sharded by the attribute."""
        entity = self._entity_of(collection)
        if entity is not None:
            scope = self.manifest.entities.get(entity)
            if scope is not None and scope.denorm_field:
                return scope.denorm_field
        attr = self._smartgraph_attribute(collection)
        return attr

    def _smartgraph_attribute(self, collection: str) -> str | None:
        graphs = self.sharding_profile.get("graphs") or []
        if not isinstance(graphs, list):
            return None
        for g in graphs:
            if not isinstance(g, dict):
                continue
            verts = g.get("vertexCollections") or g.get("vertex_collections") or []
            edges = g.get("edgeCollections") or g.get("edge_collections") or []
            if collection in (verts or []) or collection in (edges or []):
                attr = g.get("smartGraphAttribute") or g.get("smart_graph_attribute")
                if isinstance(attr, str) and attr:
                    return attr
        return None

    def _is_disjoint_smartgraph(self, graph_name: str) -> bool:
        for g in self.sharding_profile.get("graphs") or []:
            if not isinstance(g, dict):
                continue
            if g.get("name") == graph_name and bool(g.get("isDisjoint")):
                return True
        return False

    def _role_of_collection(self, collection: str) -> EntityTenantRole | None:
        entity = self._entity_of(collection)
        if entity is None:
            return None
        return self.manifest.role_of(entity)

    def _is_tenant_touching_collection(self, collection: str) -> bool:
        """Whether reading *collection* requires a tenant predicate.

        Returns True iff the collection's physical layout is
        ``smartgraph`` / ``regular`` and the manifest classifies it as
        ``TENANT_SCOPED`` or ``TENANT_ROOT``, **or** the layout kind is
        unknown and the manifest tags the entity as tenant-touching
        (defence-in-depth: if we don't know the physical kind, we
        defer to the conceptual role).
        """
        kind = self._layout_kind(collection)
        if kind in {"satellite", "system"}:
            return False
        role = self._role_of_collection(collection)
        if role in {EntityTenantRole.TENANT_SCOPED, EntityTenantRole.TENANT_ROOT}:
            return True
        # Unknown collection on a smartgraph deployment — fail-closed.
        if kind in {"smartgraph", "regular"}:
            return True
        return False

    # ---- Classification pre-pass ------------------------------------------

    def classify_touches_tenant_data(self, nodes: list[dict[str, Any]]) -> bool:
        """Return True iff ANY node accesses a tenant-touching collection.

        Drives the bind-var sanity check in :func:`validate_plan`.
        Walks subquery bodies too so the check is plan-wide.
        """
        for node in nodes:
            if not isinstance(node, dict):
                continue
            t = node.get("type")
            if t in {"EnumerateCollectionNode", "IndexNode"}:
                coll = node.get("collection")
                if isinstance(coll, str) and self._is_tenant_touching_collection(coll):
                    return True
            elif t == "TraversalNode":
                for coll in traversal_vertex_collections(node, self.sharding_profile):
                    if self._is_tenant_touching_collection(coll):
                        return True
            elif t == "SubqueryNode":
                sub_nodes = ((node.get("subquery") or {}).get("nodes")) or []
                if isinstance(sub_nodes, list) and self.classify_touches_tenant_data(sub_nodes):
                    return True
        return False

    # ---- Main walk --------------------------------------------------------

    def walk(
        self,
        nodes: list[dict[str, Any]],
        *,
        digests: dict[str, str],
        session: Any,
    ) -> None:
        for node in nodes:
            if not isinstance(node, dict):
                continue
            self._check_node(node, digests=digests, session=session)

    def _check_node(
        self,
        node: dict[str, Any],
        *,
        digests: dict[str, str],
        session: Any,
    ) -> None:
        t = node.get("type")
        if t == "EnumerateCollectionNode":
            self._check_enumerate(node, digests=digests, session=session)
        elif t == "IndexNode":
            self._check_index(node, digests=digests, session=session)
        elif t == "TraversalNode":
            self._check_traversal(node, digests=digests, session=session)
        elif t == "SubqueryNode":
            sub_plan = node.get("subquery") or {}
            sub_nodes = sub_plan.get("nodes") if isinstance(sub_plan, dict) else []
            if isinstance(sub_nodes, list):
                # Build a fresh walker so the inner ``CalculationNode``
                # index sees only the subquery's calcs — accepting a
                # filter from the outer scope would be a tenant-scope
                # leak vector.
                inner = _PlanWalker(
                    plan=sub_plan,
                    bind_vars=self.bind_vars,
                    manifest=self.manifest,
                    sharding_profile=self.sharding_profile,
                    collection_to_entity=self.collection_to_entity,
                    tenant_sentinel=self.tenant_sentinel,
                )
                inner.walk(sub_nodes, digests=digests, session=session)
        # CalculationNode / FilterNode / ReturnNode / LimitNode / SortNode /
        # CollectNode / SingletonNode / GatherNode / RemoteNode / ScatterNode
        # / DistributeNode: not collection accesses; pass through.

    def _check_enumerate(
        self,
        node: dict[str, Any],
        *,
        digests: dict[str, str],
        session: Any,
    ) -> None:
        coll = node.get("collection")
        if not isinstance(coll, str):
            return
        kind = self._layout_kind(coll)
        if kind in {"satellite", "system"}:
            return
        # Non-tenant-touching collections (global/unknown role on a
        # non-smartgraph layout, including edge collections absent from
        # the conceptual entity map) hold no tenant data to isolate —
        # Layer 5 has nothing to enforce. Mirrors the Layer 4
        # short-circuit for schemas with no tenant model and fixes false
        # UNCONSTRAINED_* refusals on single-tenant / non-tenant DBs.
        if not self._is_tenant_touching_collection(coll):
            return
        role = self._role_of_collection(coll)
        if role is EntityTenantRole.TENANT_ROOT:
            if self._has_tenant_root_predicate(node):
                return
            violation = TenantScopeViolation(
                code="TENANT_ROOT_UNCONSTRAINED",
                message=(f"Tenant-root collection {coll!r} scanned without @tenantKey predicate"),
                **digests,
            )
            _log_violation(violation, session=session)
            raise violation

        # Accept first: a tenant-field equality against ``@tenantId``
        # (parameter form) or the per-call sentinel (resolved-bind
        # form) — checking both the inline ``node["filter"]`` and any
        # standalone CalculationNode, descending only through AND. When
        # such a conjunct is present the scan is safe even if the query
        # *also* carries a mismatching literal (the AND with the
        # session value makes that conjunct return nothing — no leak).
        if self._has_tenant_predicate(node, coll):
            return

        literal_hit = self._has_mismatching_tenant_literal(node, coll)
        if literal_hit is not None:
            violation = TenantScopeViolation(
                code="LITERAL_TENANT_PREDICATE",
                message=(
                    f"{coll!r} scanned with a literal tenant predicate "
                    f"({literal_hit!r}); only the session @tenantId bind is "
                    "accepted"
                ),
                **digests,
            )
            _log_violation(violation, session=session)
            raise violation

        violation = TenantScopeViolation(
            code="UNCONSTRAINED_COLLECTION_SCAN",
            message=(f"{coll!r} scanned without @tenantId predicate (physical kind=" + kind + ")"),
            **digests,
        )
        _log_violation(violation, session=session)
        raise violation

    def _check_index(
        self,
        node: dict[str, Any],
        *,
        digests: dict[str, str],
        session: Any,
    ) -> None:
        coll = node.get("collection")
        if not isinstance(coll, str):
            return
        kind = self._layout_kind(coll)
        if kind in {"satellite", "system"}:
            return
        # See _check_enumerate: skip collections with no tenant data to
        # isolate so single-tenant / non-tenant edge indexes aren't
        # falsely refused with INDEX_MISSING_TENANT_PREDICATE.
        if not self._is_tenant_touching_collection(coll):
            return
        role = self._role_of_collection(coll)
        if role is EntityTenantRole.TENANT_ROOT:
            if _index_keyed_by_tenant_key(node):
                return
            violation = TenantScopeViolation(
                code="TENANT_ROOT_UNCONSTRAINED",
                message=(f"Tenant-root index lookup on {coll!r} not keyed by @tenantKey"),
                **digests,
            )
            _log_violation(violation, session=session)
            raise violation

        if _index_keyed_by_tenant_key(node):
            # Smartgraph collections may legitimately accept a _key
            # equality if the smart-graph attribute is part of the
            # composite _key. Accept the same _key=@tenantKey shape
            # we use for TENANT_ROOT — Layer 5 still rejects scans
            # that don't carry this predicate via _check_enumerate.
            return

        tenant_field = self._tenant_field_for(coll)
        if not _index_covers_tenant(node, tenant_field, sentinel=self.tenant_sentinel):
            violation = TenantScopeViolation(
                code="INDEX_MISSING_TENANT_PREDICATE",
                message=(f"IndexNode on {coll!r} does not equality-match {tenant_field!r} == @tenantId"),
                **digests,
            )
            _log_violation(violation, session=session)
            raise violation

    def _check_traversal(
        self,
        node: dict[str, Any],
        *,
        digests: dict[str, str],
        session: Any,
    ) -> None:
        # 0) If none of the collections this traversal touches are
        # tenant-touching, there is nothing to isolate (single-tenant /
        # non-tenant schema, or a purely global subgraph). Without this
        # gate every traversal on a non-multi-tenant DB is falsely
        # refused: edge collections have no conceptual entity and thus
        # no GLOBAL role to short-circuit on, and the per-node enumerate/
        # index checks never see the traversal's collections.
        involved = self._traversal_all_collections(node)
        if involved and not any(self._is_tenant_touching_collection(c) for c in involved):
            return
        # 1) Every vertex collection in play is satellite → OK.
        vertex_colls = traversal_vertex_collections(node, self.sharding_profile)
        if vertex_colls and all(self._layout_kind(c) == "satellite" for c in vertex_colls):
            return
        # 2) The graph is a disjoint smartgraph → OK.
        graph_name = traversal_graph_name(node)
        if graph_name and self._is_disjoint_smartgraph(graph_name):
            return
        # 3) It prunes at, and filters out, every vertex of another tenant → OK.
        if self._traversal_confined(node, vertex_colls):
            return
        violation = TenantScopeViolation(
            code="UNCONSTRAINED_TRAVERSAL",
            message=(
                "TraversalNode "
                f"graph={graph_name!r} vertex_collections={vertex_colls!r}"
                " is not constrained to the session tenant (no satellite-only "
                "path, no disjoint smartgraph, no PRUNE <v>.<field> != @tenantId "
                "with FILTER <v>.<field> == @tenantId)"
            ),
            **digests,
        )
        _log_violation(violation, session=session)
        raise violation

    def _traversal_all_collections(self, node: dict[str, Any]) -> list[str]:
        """All vertex + edge collections a TraversalNode reads. Used only to
        decide whether the traversal touches any tenant-scoped collection."""
        out: list[str] = []
        edges = node.get("edgeCollections")
        if isinstance(edges, list):
            out.extend(c for c in edges if isinstance(c, str))
        out.extend(traversal_vertex_collections(node, self.sharding_profile))
        return list(dict.fromkeys(out))

    def _traversal_confined(self, node: dict[str, Any], vertex_colls: list[str]) -> bool:
        """Whether the traversal keeps to the session tenant
        (:func:`traversal_confined_to_tenant`) on the tenant field its
        tenant-scoped vertex collections share."""
        vertex_var = (node.get("vertexOutVariable") or {}).get("name")
        fields = {self._tenant_field_for(c) for c in vertex_colls if self._is_tenant_touching_collection(c)}
        if not isinstance(vertex_var, str) or len(fields) != 1:
            return False
        (field_name,) = fields
        if not field_name:
            return False
        return traversal_confined_to_tenant(
            node,
            vertex_var=vertex_var,
            tenant_field=field_name,
            sentinel=self.tenant_sentinel,
            filter_exprs=[_expr(calc) for calc in self._calc_by_outvar.values()],
        )

    # ---- Predicate detection ---------------------------------------------

    def _has_tenant_predicate(self, enum_node: dict[str, Any], collection: str) -> bool:
        """Whether the plan constrains the EnumerateCollectionNode's
        output variable to the session tenant on the collection's
        tenant field.

        Inspects both the inline ``EnumerateCollectionNode.filter`` (the
        fused shape real ArangoDB emits) and every standalone
        CalculationNode, descending only through ``AND`` conjunctions.
        A conjunct is accepted when the tenant field is compared ``==``
        to either the ``@tenantId`` *parameter* or a *value* literal
        equal to the per-call ``tenant_sentinel``.
        """
        outvar = _outvar_name(enum_node)
        if not outvar:
            return False
        tenant_field = self._tenant_field_for(collection)
        if not tenant_field:
            return False
        for expr in self._tenant_field_exprs(enum_node, outvar):
            if _expr_scopes_tenant(expr, var_name=outvar, attr=tenant_field, sentinel=self.tenant_sentinel):
                return True
        return False

    def _has_mismatching_tenant_literal(
        self,
        enum_node: dict[str, Any],
        collection: str,
    ) -> Any:
        """Return a literal value if the plan compares the enum's tenant
        field against a string literal that is **not** the per-call
        sentinel — i.e. a caller-smuggled tenant id Layer 5 refuses to
        honour. A literal equal to the sentinel is the resolved
        ``@tenantId`` bind and is not flagged (and is handled by
        :meth:`_has_tenant_predicate` anyway). Descends through both
        ``AND`` and ``OR`` since a mismatching literal anywhere is
        suspicious.
        """
        outvar = _outvar_name(enum_node)
        if not outvar:
            return None
        tenant_field = self._tenant_field_for(collection)
        if not tenant_field:
            return None
        for expr in self._tenant_field_exprs(enum_node, outvar):
            literal = _expr_mismatching_tenant_literal(
                expr, var_name=outvar, attr=tenant_field, sentinel=self.tenant_sentinel
            )
            if literal is not None:
                return literal
        return None

    def _tenant_field_exprs(self, enum_node: dict[str, Any], outvar: str) -> list[dict[str, Any]]:
        """Candidate expressions that may carry the tenant predicate for
        *enum_node*: the inline fused ``filter`` plus every standalone
        CalculationNode's expression.
        """
        exprs: list[dict[str, Any]] = []
        inline = enum_node.get("filter")
        if isinstance(inline, dict):
            exprs.append(inline)
        for calc in self._calc_by_outvar.values():
            e = _expr(calc)
            if e:
                exprs.append(e)
        return exprs

    def _has_tenant_root_predicate(self, enum_node: dict[str, Any]) -> bool:
        outvar = _outvar_name(enum_node)
        if not outvar:
            return False
        for calc in self._calc_by_outvar.values():
            if _calc_matches_key_eq_bindvar(calc, outvar, "tenantKey"):
                return True
            if _calc_matches_tenant_eq_bindvar(calc, outvar, "_key"):
                return True
        return False


# ---------------------------------------------------------------------------
# Pure expression-matchers (extracted so they're unit-testable)
# ---------------------------------------------------------------------------


def _outvar_name(node: dict[str, Any]) -> str | None:
    out = node.get("outVariable")
    if isinstance(out, dict):
        n = out.get("name")
        if isinstance(n, str) and n:
            return n
    return None


def _expr(node: dict[str, Any]) -> dict[str, Any]:
    e = node.get("expression")
    return e if isinstance(e, dict) else {}


def _is_attribute_access_on(
    expr: dict[str, Any],
    *,
    var_name: str,
    attr: str,
) -> bool:
    """Match ``<var_name>.<attr>`` — i.e. an ``attribute access`` node
    whose subNode is a ``reference`` to *var_name* and whose ``name``
    is *attr*."""
    if expr.get("type") != "attribute access":
        return False
    if expr.get("name") != attr:
        return False
    subs = expr.get("subNodes") or []
    if not subs or not isinstance(subs, list):
        return False
    inner = subs[0]
    if not isinstance(inner, dict):
        return False
    if inner.get("type") != "reference":
        return False
    return inner.get("name") == var_name


def _is_bindvar_named(expr: dict[str, Any], name: str) -> bool:
    """Match ``@<name>`` — a ``parameter`` node with ``name == name``."""
    return expr.get("type") == "parameter" and expr.get("name") == name


def _is_value_literal(expr: dict[str, Any]) -> bool:
    return expr.get("type") == "value"


def _value_of_literal(expr: dict[str, Any]) -> Any:
    return expr.get("value")


def _compare_eq_subnodes(expr: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """If *expr* is an equality compare, return its two operand subNodes."""
    if expr.get("type") not in {"compare ==", "n-ary compare"}:
        return None
    subs = expr.get("subNodes") or []
    if len(subs) != 2 or not all(isinstance(s, dict) for s in subs):
        return None
    return subs[0], subs[1]


def _calc_matches_tenant_eq_bindvar(
    calc: dict[str, Any],
    var_name: str,
    attr: str,
) -> bool:
    """``CalculationNode`` whose expression is
    ``<var_name>.<attr> == @tenantId`` (either operand order)."""
    expr = _expr(calc)
    sides = _compare_eq_subnodes(expr)
    if sides is None:
        return False
    lhs, rhs = sides
    return (
        _is_attribute_access_on(lhs, var_name=var_name, attr=attr) and _is_bindvar_named(rhs, "tenantId")
    ) or (_is_attribute_access_on(rhs, var_name=var_name, attr=attr) and _is_bindvar_named(lhs, "tenantId"))


def _calc_matches_tenant_eq_literal(
    calc: dict[str, Any],
    var_name: str,
    attr: str,
) -> Any:
    """If the calculation's expression is
    ``<var_name>.<attr> == <literal>``, return the literal. Else None.

    Treats the equality as symmetric and only fires when the literal
    operand is a ``value`` node — bind-var equality returns None and
    is the path :func:`_calc_matches_tenant_eq_bindvar` accepts.
    """
    expr = _expr(calc)
    sides = _compare_eq_subnodes(expr)
    if sides is None:
        return None
    lhs, rhs = sides
    if _is_attribute_access_on(lhs, var_name=var_name, attr=attr) and _is_value_literal(rhs):
        return _value_of_literal(rhs)
    if _is_attribute_access_on(rhs, var_name=var_name, attr=attr) and _is_value_literal(lhs):
        return _value_of_literal(lhs)
    return None


def _is_tenant_value_operand(expr: dict[str, Any], *, sentinel: str | None) -> bool:
    """Operand that ties a comparison to the session tenant.

    Accepts the ``@tenantId`` bind *parameter* (the hand-crafted /
    plan-override shape, and any ArangoDB build that does not fold the
    bind) or a ``value`` literal equal to the per-call *sentinel* (the
    resolved-bind shape real EXPLAIN produces). A literal that is not
    the sentinel — including ``None`` sentinel — is rejected so a
    caller cannot smuggle another tenant's id as a literal.
    """
    if _is_bindvar_named(expr, "tenantId"):
        return True
    if sentinel is not None and _is_value_literal(expr) and _value_of_literal(expr) == sentinel:
        return True
    return False


def _eq_scopes_tenant(
    expr: dict[str, Any],
    *,
    var_name: str,
    attr: str,
    sentinel: str | None,
) -> bool:
    """``<var>.<attr> == (@tenantId | sentinel-literal)`` in either order."""
    sides = _compare_eq_subnodes(expr)
    if sides is None:
        return False
    lhs, rhs = sides
    return (
        _is_attribute_access_on(lhs, var_name=var_name, attr=attr)
        and _is_tenant_value_operand(rhs, sentinel=sentinel)
    ) or (
        _is_attribute_access_on(rhs, var_name=var_name, attr=attr)
        and _is_tenant_value_operand(lhs, sentinel=sentinel)
    )


def _expr_scopes_tenant(
    expr: dict[str, Any],
    *,
    var_name: str,
    attr: str,
    sentinel: str | None,
) -> bool:
    """Whether *expr* constrains ``<var>.<attr>`` to the session tenant.

    A bare equality compare is checked directly. Conjunctions
    (``logical and`` / ``n-ary and``) are descended — a tenant
    predicate ANDed with anything still scopes the scan. ``OR`` is
    **never** descended: ``tenantId == @t OR x`` does not guarantee
    scoping, so it must not be accepted.
    """
    if not isinstance(expr, dict):
        return False
    if _eq_scopes_tenant(expr, var_name=var_name, attr=attr, sentinel=sentinel):
        return True
    if expr.get("type") in {"logical and", "n-ary and"}:
        for sub in expr.get("subNodes") or []:
            if isinstance(sub, dict) and _expr_scopes_tenant(
                sub, var_name=var_name, attr=attr, sentinel=sentinel
            ):
                return True
    return False


def _expr_mismatching_tenant_literal(
    expr: dict[str, Any],
    *,
    var_name: str,
    attr: str,
    sentinel: str | None,
) -> Any:
    """Return a literal if *expr* compares ``<var>.<attr>`` to a string
    literal that is not the *sentinel*, else ``None``.

    Descends through both ``AND`` and ``OR`` — a mismatching literal
    anywhere in the predicate tree is a smuggling attempt. A literal
    equal to the sentinel is the resolved ``@tenantId`` bind and is not
    flagged.
    """
    if not isinstance(expr, dict):
        return None
    sides = _compare_eq_subnodes(expr)
    if sides is not None:
        lhs, rhs = sides
        for field_side, val_side in ((lhs, rhs), (rhs, lhs)):
            if _is_attribute_access_on(field_side, var_name=var_name, attr=attr) and _is_value_literal(
                val_side
            ):
                value = _value_of_literal(val_side)
                if sentinel is None or value != sentinel:
                    return value
    for sub in expr.get("subNodes") or []:
        if isinstance(sub, dict):
            hit = _expr_mismatching_tenant_literal(sub, var_name=var_name, attr=attr, sentinel=sentinel)
            if hit is not None:
                return hit
    return None


def _calc_matches_key_eq_bindvar(
    calc: dict[str, Any],
    var_name: str,
    bindvar_name: str,
) -> bool:
    expr = _expr(calc)
    sides = _compare_eq_subnodes(expr)
    if sides is None:
        return False
    lhs, rhs = sides
    return (
        _is_attribute_access_on(lhs, var_name=var_name, attr="_key") and _is_bindvar_named(rhs, bindvar_name)
    ) or (
        _is_attribute_access_on(rhs, var_name=var_name, attr="_key") and _is_bindvar_named(lhs, bindvar_name)
    )


def _index_covers_tenant(
    node: dict[str, Any],
    tenant_field: str | None,
    *,
    sentinel: str | None = None,
) -> bool:
    """IndexNode condition references *tenant_field* == @tenantId.

    ArangoDB's IndexNode embeds the resolved index condition in
    ``node["condition"]["subNodes"]`` as an n-ary tree. Every row it
    selects must compare the ``attribute access`` against the
    ``@tenantId`` parameter or the resolved-bind *sentinel* literal
    (:func:`_condition_requires`).
    """
    if not tenant_field:
        return False
    outvar = _outvar_name(node)
    if not outvar:
        return False
    cond = node.get("condition")
    return _condition_covers_tenant(cond, outvar=outvar, tenant_field=tenant_field, sentinel=sentinel)


def _condition_covers_tenant(
    cond: Any,
    *,
    outvar: str,
    tenant_field: str,
    sentinel: str | None = None,
) -> bool:
    def tenant_eq(node: dict[str, Any]) -> bool:
        sides = _compare_eq_subnodes(node)
        if sides is None:
            return False
        lhs, rhs = sides
        return (
            _is_attribute_access_on(lhs, var_name=outvar, attr=tenant_field)
            and _is_tenant_value_operand(rhs, sentinel=sentinel)
        ) or (
            _is_attribute_access_on(rhs, var_name=outvar, attr=tenant_field)
            and _is_tenant_value_operand(lhs, sentinel=sentinel)
        )

    return _condition_requires(cond, tenant_eq)


def _condition_requires(cond: Any, leaf: Any) -> bool:
    """Whether every row the index condition *cond* selects satisfies one of
    its comparisons for which *leaf* holds.

    ArangoDB gives an IndexNode's condition in disjunctive form (an
    ``n-ary or`` of ``n-ary and`` branches). Under an AND one conjunct
    is enough; under an OR every branch needs one; any other node (a
    ``NOT``, a function call) proves nothing.
    """
    if not isinstance(cond, dict):
        return False
    if leaf(cond):
        return True
    subs = cond.get("subNodes") or []
    kind = cond.get("type")
    if kind in {"n-ary and", "logical and"}:
        return any(isinstance(sub, dict) and _condition_requires(sub, leaf) for sub in subs)
    if kind in {"n-ary or", "logical or"}:
        return bool(subs) and all(_condition_requires(sub, leaf) for sub in subs)
    return False


def _index_keyed_by_tenant_key(node: dict[str, Any]) -> bool:
    """IndexNode condition references ``_key == @tenantKey``."""
    outvar = _outvar_name(node)
    if not outvar:
        return False
    cond = node.get("condition")
    return _condition_keyed_by_tenant_key(cond, outvar=outvar)


def _condition_keyed_by_tenant_key(cond: Any, *, outvar: str) -> bool:
    def key_eq(node: dict[str, Any]) -> bool:
        sides = _compare_eq_subnodes(node)
        if sides is None:
            return False
        lhs, rhs = sides
        return (
            _is_attribute_access_on(lhs, var_name=outvar, attr="_key") and _is_bindvar_named(rhs, "tenantKey")
        ) or (
            _is_attribute_access_on(rhs, var_name=outvar, attr="_key") and _is_bindvar_named(lhs, "tenantKey")
        )

    return _condition_requires(cond, key_eq)


def traversal_graph_name(node: dict[str, Any]) -> str | None:
    """The named graph a TraversalNode walks: ``graph`` is its name in a real
    plan (a list of edge collections for an anonymous traversal)."""
    for key in ("graphName", "graph"):
        name = node.get(key)
        if isinstance(name, str) and name:
            return name
    return None


def traversal_vertex_collections(node: dict[str, Any], sharding_profile: dict[str, Any] | None) -> list[str]:
    """The vertex collections a TraversalNode may reach.

    A real plan lists them at the node's top level (``vertexCollections``,
    which for an anonymous traversal also names its edge collections; those
    are dropped). Otherwise they come from ``graph.vertexCollections``, from
    the named graph in *sharding_profile*, or from the profile's graphs that
    hold the traversal's edge collections.
    """
    raw_edges = node.get("edgeCollections")
    edges = {c for c in raw_edges if isinstance(c, str)} if isinstance(raw_edges, list) else set()
    top = node.get("vertexCollections")
    if isinstance(top, list) and top:
        return [c for c in top if isinstance(c, str) and c not in edges]
    graph = node.get("graph")
    if isinstance(graph, dict):
        verts = graph.get("vertexCollections") or graph.get("vertex_collections")
        if isinstance(verts, list):
            return [v for v in verts if isinstance(v, str)]
    graph_name = traversal_graph_name(node)
    out: list[str] = []
    for g in (sharding_profile or {}).get("graphs") or []:
        if not isinstance(g, dict):
            continue
        g_edges = g.get("edgeCollections") or g.get("edge_collections") or []
        if (graph_name and g.get("name") == graph_name) or (not graph_name and edges & set(g_edges)):
            verts = g.get("vertexCollections") or g.get("vertex_collections") or []
            out.extend(v for v in verts if isinstance(v, str))
    return list(dict.fromkeys(out))


def traversal_confined_to_tenant(
    node: dict[str, Any],
    *,
    vertex_var: str,
    tenant_field: str,
    sentinel: str | None,
    filter_exprs: list[dict[str, Any]],
) -> bool:
    """Whether a TraversalNode only reaches and yields the session tenant's
    vertices: its PRUNE holds for every vertex whose *tenant_field* is not
    the tenant, so the traversal never continues past one, and the vertex
    is required to be the tenant's, by the condition the optimizer fused
    into the node (every branch) or by a FILTER expression in the plan.

    A PRUNE alone still yields the vertex it stops at, and a FILTER alone
    still walks through other tenants' vertices to reach the tenant's, so
    a path would carry them: both are needed. A real plan holds the PRUNE
    in the node's ``expression``; ``options.prune`` is read for hand-built
    plans.
    """
    prune = node.get("expression")
    if not isinstance(prune, dict):
        options = node.get("options")
        prune = options.get("prune") if isinstance(options, dict) else None
    if not _holds_outside_tenant(prune, var_name=vertex_var, attr=tenant_field, sentinel=sentinel):
        return False

    def tenant_eq(cond: dict[str, Any]) -> bool:
        return _eq_scopes_tenant(cond, var_name=vertex_var, attr=tenant_field, sentinel=sentinel)

    if _condition_requires(node.get("condition"), tenant_eq):
        return True
    return any(
        _expr_scopes_tenant(expr, var_name=vertex_var, attr=tenant_field, sentinel=sentinel)
        for expr in filter_exprs
        if expr
    )


def _holds_outside_tenant(expr: Any, *, var_name: str, attr: str, sentinel: str | None) -> bool:
    """Whether *expr* is true for every vertex whose ``<var>.<attr>`` is not
    the session tenant: ``<var>.<attr> != @tenantId``, or an OR with such a
    branch, or an AND of such parts. Anything else proves nothing."""
    if not isinstance(expr, dict):
        return False
    if expr.get("type") == "compare !=":
        subs = expr.get("subNodes") or []
        if len(subs) == 2 and all(isinstance(sub, dict) for sub in subs):
            lhs, rhs = subs
            return (
                _is_attribute_access_on(lhs, var_name=var_name, attr=attr)
                and _is_tenant_value_operand(rhs, sentinel=sentinel)
            ) or (
                _is_attribute_access_on(rhs, var_name=var_name, attr=attr)
                and _is_tenant_value_operand(lhs, sentinel=sentinel)
            )
        return False
    subs = [sub for sub in expr.get("subNodes") or []]
    kind = expr.get("type")
    if kind in {"n-ary or", "logical or"}:
        return any(
            _holds_outside_tenant(sub, var_name=var_name, attr=attr, sentinel=sentinel) for sub in subs
        )
    if kind in {"n-ary and", "logical and"}:
        return bool(subs) and all(
            _holds_outside_tenant(sub, var_name=var_name, attr=attr, sentinel=sentinel) for sub in subs
        )
    return False


# ---------------------------------------------------------------------------
# Plan / bind-var coercion + digests + logging
# ---------------------------------------------------------------------------


def _coerce_plan(result: Any) -> Any:
    """``db.aql.explain`` returns ``{"plan": ..., "warnings": ...}`` in
    most python-arango versions; in older ones it returns the plan
    directly. Tolerate both shapes."""
    if isinstance(result, dict) and "plan" in result and isinstance(result["plan"], dict):
        return result["plan"]
    return result


def _digests(
    *,
    aql: str,
    bind_vars: dict[str, Any],
    plan: dict[str, Any] | None,
) -> dict[str, str]:
    aql_payload = aql + "\n" + json.dumps(bind_vars, sort_keys=True, default=str)
    aql_digest = hashlib.sha256(aql_payload.encode("utf-8")).hexdigest()
    if plan is None:
        plan_digest = ""
    else:
        plan_json = json.dumps(plan, sort_keys=True, default=str)
        plan_digest = hashlib.sha256(plan_json.encode("utf-8")).hexdigest()
    return {"aql_digest": aql_digest, "plan_digest": plan_digest}


def _log_violation(violation: TenantScopeViolation, *, session: Any) -> None:
    token_prefix = _session_token_prefix(session)
    logger.warning(
        "TENANT_SCOPE_VIOLATION code=%s session=%s tenant=%s aql_digest=%s plan_digest=%s message=%s",
        violation.code,
        token_prefix,
        getattr(session, "tenant_id", None),
        violation.aql_digest[:16],
        violation.plan_digest[:16],
        violation.message,
    )


def _log_pass(
    *,
    session: Any,
    aql_digest: str,
    plan_digest: str,
    cached: bool = False,
) -> None:
    token_prefix = _session_token_prefix(session)
    logger.info(
        "TENANT_SCOPE_OK session=%s tenant=%s aql_digest=%s plan_digest=%s cache=%s",
        token_prefix,
        getattr(session, "tenant_id", None),
        aql_digest[:16],
        plan_digest[:16],
        "hit" if cached else "miss",
    )


def _log_admin_bypass(
    *,
    session: Any,
    reason: str,
    aql_digest: str,
    plan_digest: str,
) -> None:
    """Record an honoured admin cross-tenant bypass (MT-7, PRD §10).

    Emitted at WARNING on the dedicated ``arango_cypher.tenant_audit``
    stream — a privilege-escalation event that must be independently
    alertable. Carries the mandatory bypass reason plus the same
    aql/plan digests as the normal audit lines so an auditor can replay
    exactly what ran.
    """
    audit_logger.warning(
        "ADMIN_CROSS_TENANT_BYPASS session=%s tenant=%s reason=%s aql_digest=%s plan_digest=%s",
        _session_token_prefix(session),
        getattr(session, "tenant_id", None),
        reason,
        aql_digest[:16],
        plan_digest[:16],
    )


def _session_token_prefix(session: Any) -> str:
    token = getattr(session, "token", "") or ""
    return token[:8] if token else "-"


__all__ = [
    "TenantScopeViolation",
    "plan_cache_stats",
    "reset_plan_cache",
    "validate_plan",
]
