"""Shared tenant-rewrite vocabulary used by Layers 2, 3, and 4.

Why this module exists
----------------------
Wave 8a fans out three parallel sub-agents that all need to agree on:

* What a "tenant predicate" looks like (property-map injection,
  WHERE-clause equality, prune-options injection, scoping-path
  expansion).
* How to read the :class:`TenantScopeManifest` for a given conceptual
  label or physical collection.
* How to distinguish a bind-variable reference (acceptable) from a
  literal tenant value (a T2-class data-leak attempt).

Without a shared module, each agent would invent slightly different
predicate shapes and the merge would be miserable. This module is
the **single source of truth** the three rewriters extend; it is
deliberately small and pure-Python (no AST library, no DB access,
no I/O) so it can be imported from anywhere without dragging dependencies.

PRD source-of-truth
-------------------
* ``docs/multitenant_prd.md`` §5.2 (Layer 2 hardening / MT-2).
* ``docs/multitenant_prd.md`` §6.2 (Cypher AST rewrite / MT-3).
* ``docs/multitenant_prd.md`` §7.2 (AQL AST rewrite / MT-4).
* ``docs/agent_prompts_multitenant.md`` "Wave 8-pre" section.

Public surface
--------------
* :data:`TENANT_ID_BIND`, :data:`TENANT_KEY_BIND` — canonical
  bind-variable names. **Never** spell them as string literals in
  the rewriters; always import these constants.
* :class:`TenantPredicateShape` — the only way a tenant predicate
  is described after this module lands. Rewriters render the
  shape into Cypher/AQL fragments using their own emitters.
* :func:`predicate_for_entity` — manifest lookup keyed by
  conceptual label (used by Layer 3 / MT-3).
* :func:`predicate_for_collection` — manifest lookup keyed by
  physical collection name (used by Layer 4 / MT-4).
* :func:`is_bindvar_reference` — match an ArangoDB EXPLAIN-plan
  ``parameter`` node by name. Used by Layer 5 today and by Layer 4
  preflight (already-injected predicates).
* :func:`is_literal_tenant_value` — return True if a plan/AST node
  is a literal whose value is in the manifest's known tenant-key
  set. **Fails closed** when the key set is unset.
* :class:`UnknownEntityScope` — raised when a label/collection has
  no manifest entry. Caller decides refuse-vs-pass; convention is
  REFUSE for Layer 3 always, REFUSE for Layer 4 on tenant-scoped
  collections, PASS for satellite/system collections.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from arango_query_core import CoreError

from arango_cypher._translate_v0.literals import cypher_string_value

from .tenant_scope import (
    EntityTenantRole,
    TenantScopeManifest,
)

__all__ = [
    "TENANT_ID_BIND",
    "TENANT_KEY_BIND",
    "TenantPredicateShape",
    "UnknownEntityScope",
    "is_bindvar_reference",
    "is_literal_tenant_value",
    "predicate_for_collection",
    "predicate_for_entity",
]

# ---------------------------------------------------------------------------
# Canonical bind-variable names — single source of truth
# ---------------------------------------------------------------------------

#: Bind variable carrying the active tenant's stable identifier
#: (denormalised field value, e.g. ``TENANT_HEX_ID``). Layer 1
#: (``arango_cypher/service/security.py::_Session.tenant_id``) is
#: the only producer; every rewriter consumes this constant rather
#: than spelling the string literal.
TENANT_ID_BIND = "tenantId"

#: Bind variable carrying the tenant collection ``_key`` (the
#: ArangoDB document key of the tenant root). Used by predicates
#: targeting ``Tenant._key`` directly and by traversal-path
#: anchors (``(t:Tenant {_key: $tenantKey})``). Layer 1
#: (``_Session.tenant_key``) is the only producer.
TENANT_KEY_BIND = "tenantKey"


# ---------------------------------------------------------------------------
# Predicate shape
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TenantPredicateShape:
    """Canonical description of *how* a tenant predicate is rendered.

    A rewriter (Layer 3 for Cypher, Layer 4 for AQL) consumes this
    shape and emits the layer-appropriate syntax. The shape itself
    is layer-agnostic: it records the *intent* (which field on
    which kind of entity should be constrained to which bind
    variable, or which scoping path should be expanded), not the
    serialisation.

    Attributes
    ----------
    style:
        One of:

        * ``"property_map"`` — inject ``{field: $bind_name}`` into a
          node pattern's property map (Cypher) or attach a ``FILTER
          <var>.<field> == @<bind_name>`` to a ``FOR <var> IN <coll>``
          (AQL). Used for TENANT_ROOT (`_key` = ``$tenantKey``) and
          for TENANT_SCOPED entities that carry a denormalised tenant
          column.
        * ``"where_eq"`` — emit a free-standing equality predicate
          ``<var>.<field> == $<bind_name>``. Used when the entity is
          referenced without an inline property map and the rewriter
          must add a ``WHERE`` clause instead.
        * ``"traversal_path"`` — replace a bare pattern over a
          TENANT_SCOPED entity with a scoping-path expansion from
          ``Tenant``. Used when the entity has no denormalised tenant
          column but is reachable via the relationship graph (see
          :attr:`EntityScope.scoping_path`).
        * ``"prune"`` — for AQL ``TraversalNode`` rewrites: attach an
          ``OPTIONS {prune: v.<field> != @<bind_name>}`` rather than a
          per-vertex FILTER. Used only by Layer 4 (MT-4).
    field:
        The conceptual property or document attribute the predicate
        targets (``"_key"`` for TENANT_ROOT, the denormalised field
        name for TENANT_SCOPED). ``None`` when ``style ==
        "traversal_path"`` — that style is expressed entirely via
        :attr:`scoping_path`.
    bind_name:
        The bind variable the predicate compares against. **Always**
        one of :data:`TENANT_ID_BIND` or :data:`TENANT_KEY_BIND`;
        never a literal value. Encoded as the bare name (no ``$``
        prefix); the rewriter adds the prefix in its emitter.
    scoping_path:
        Ordered list of relationship-type names that connect
        ``Tenant`` to the target entity, populated only when
        ``style == "traversal_path"``. ``None`` otherwise.
        Sourced from :attr:`EntityScope.scoping_path` (Wave 7,
        Part 2).
    """

    style: str
    field: str | None
    bind_name: str
    scoping_path: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        # Defence-in-depth: catch construction bugs early so they
        # don't bubble through to a rewriter producing a malformed
        # predicate. The rewriters themselves should never construct
        # a shape that fails these checks — they all come from the
        # `predicate_for_*` factories below — but if someone hand-
        # builds one in a test, we'd rather raise immediately.
        valid_styles = {"property_map", "where_eq", "traversal_path", "prune"}
        if self.style not in valid_styles:
            raise ValueError(
                f"TenantPredicateShape.style must be one of {sorted(valid_styles)!r}, got {self.style!r}"
            )
        if self.bind_name not in (TENANT_ID_BIND, TENANT_KEY_BIND):
            raise ValueError(
                f"TenantPredicateShape.bind_name must be {TENANT_ID_BIND!r} or "
                f"{TENANT_KEY_BIND!r}, got {self.bind_name!r}"
            )
        if self.style == "traversal_path":
            if self.field is not None:
                raise ValueError("TenantPredicateShape.style='traversal_path' requires field=None")
            if not self.scoping_path:
                raise ValueError(
                    "TenantPredicateShape.style='traversal_path' requires a non-empty scoping_path"
                )
        else:
            if not self.field:
                raise ValueError(f"TenantPredicateShape.style={self.style!r} requires a non-empty field")
            if self.scoping_path is not None:
                raise ValueError(f"TenantPredicateShape.style={self.style!r} must not carry a scoping_path")


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class UnknownEntityScope(Exception):
    """Raised when a label or collection has no manifest entry.

    The Layer 3 / Layer 4 rewriters catch this and decide whether to
    refuse the query or fall through. Convention:

    * Layer 3 (MT-3) — **REFUSE always.** A Cypher pattern over an
      unmapped label is a translation bug or an injection probe;
      either way, refusing is the safe answer.
    * Layer 4 (MT-4) — **REFUSE on tenant-scoped collections.**
      Satellite / system collections (``_users``, ``_jobs``, etc.)
      pass through because they are tenant-independent by design;
      tenant-scoped collections that the manifest doesn't know about
      cannot be classified and must be refused.

    The exception carries the offending name so callers can produce
    a useful 4xx response.
    """

    def __init__(self, name: str) -> None:
        super().__init__(f"no tenant-scope manifest entry for {name!r}")
        self.name = name


# ---------------------------------------------------------------------------
# Manifest lookups
# ---------------------------------------------------------------------------


def predicate_for_entity(
    label: str,
    manifest: TenantScopeManifest,
) -> TenantPredicateShape | None:
    """Return the canonical predicate shape for a conceptual ``label``.

    This is the lookup used by Layer 3 (MT-3 Cypher AST rewriter).
    The decision tree is exactly that in PRD §6.2:

    +-----------------+----------------------+------------------------------------------+
    | role            | denorm_field set?    | result                                   |
    +=================+======================+==========================================+
    | GLOBAL          | n/a                  | ``None`` (no rewrite needed)             |
    | TENANT_ROOT     | n/a                  | ``property_map`` / ``_key`` /            |
    |                 |                      | :data:`TENANT_KEY_BIND`                  |
    | TENANT_SCOPED   | yes                  | ``property_map`` / ``<denorm_field>`` /  |
    |                 |                      | :data:`TENANT_ID_BIND`                   |
    | TENANT_SCOPED   | no, but scoping_path | ``traversal_path`` / ``None`` /          |
    |                 | populated            | :data:`TENANT_KEY_BIND` / scoping_path   |
    | TENANT_SCOPED   | no, no path          | raises :class:`UnknownEntityScope`       |
    |                 |                      | (cannot scope safely)                    |
    | label unmapped  | n/a                  | raises :class:`UnknownEntityScope`       |
    +-----------------+----------------------+------------------------------------------+

    Parameters
    ----------
    label:
        Conceptual entity name as it appears in ``MATCH (var:Label)``.
    manifest:
        Built by :func:`tenant_scope.analyze_tenant_scope`.

    Returns
    -------
    TenantPredicateShape | None
        ``None`` only for GLOBAL entities (which need no rewrite).
        For every other tenant-relevant role, returns the shape the
        rewriter should emit.

    Raises
    ------
    UnknownEntityScope
        When ``label`` is not present in ``manifest.entities``, or
        is TENANT_SCOPED without either a denormalised tenant field
        **or** a derived scoping path. In both cases the rewriter
        cannot produce a safe predicate and must refuse the query.
    """
    if not isinstance(label, str) or not label:
        raise UnknownEntityScope(label or "<empty>")

    entry = manifest.entities.get(label)
    if entry is None:
        raise UnknownEntityScope(label)

    role = entry.role
    if role is EntityTenantRole.GLOBAL:
        return None
    if role is EntityTenantRole.TENANT_ROOT:
        return TenantPredicateShape(
            style="property_map",
            field="_key",
            bind_name=TENANT_KEY_BIND,
        )
    # role is TENANT_SCOPED at this point.
    if entry.denorm_field:
        return TenantPredicateShape(
            style="property_map",
            field=entry.denorm_field,
            bind_name=TENANT_ID_BIND,
        )
    if entry.scoping_path:
        return TenantPredicateShape(
            style="traversal_path",
            field=None,
            bind_name=TENANT_KEY_BIND,
            scoping_path=tuple(entry.scoping_path),
        )
    # TENANT_SCOPED without either a denorm field or a scoping path
    # cannot be scoped safely — the manifest is incomplete. Surface
    # the ambiguity rather than silently producing a no-op rewrite.
    raise UnknownEntityScope(label)


def predicate_for_collection(
    collection: str,
    manifest: TenantScopeManifest,
    sharding_profile: dict[str, Any] | None,
) -> TenantPredicateShape | None:
    """Return the canonical predicate shape for a physical ``collection``.

    Used by Layer 4 (MT-4 AQL AST rewriter), which sees collection
    names (``FOR e IN Employee``) rather than conceptual labels.

    Resolution proceeds in three steps:

    1. **Satellite / system short-circuit.** If ``sharding_profile``
       classifies the collection as ``satellite`` or ``system``, the
       collection is tenant-independent by design and no predicate is
       needed (``return None``). This is the *only* path that returns
       ``None`` for a non-GLOBAL collection — every other tenant-
       relevant role must produce a predicate or raise.
    2. **Collection-to-entity mapping.** ``sharding_profile`` carries
       an optional ``collectionToEntity`` map produced by the schema
       analyzer; we use it to translate a collection name into the
       conceptual label that :func:`predicate_for_entity` consumes.
       Falls back to assuming the collection name equals the entity
       name when the map is absent (common in heuristic mode).
    3. **Delegation.** Forward the resolved label to
       :func:`predicate_for_entity`.

    Parameters
    ----------
    collection:
        Physical collection name as it appears in AQL ``FOR x IN <coll>``.
    manifest:
        Tenant-scope manifest as built by ``analyze_tenant_scope``.
    sharding_profile:
        ``metadata.shardingProfile`` from the schema bundle, used to
        identify satellite/system collections and to map collection
        names to entity labels. ``None`` is accepted (heuristic mode
        / older bundles) — fall through to step 2 with no
        collection-to-entity map.

    Returns
    -------
    TenantPredicateShape | None
        ``None`` for satellite/system collections and for collections
        that map to GLOBAL entities. Otherwise the predicate shape
        the AQL rewriter should emit.

    Raises
    ------
    UnknownEntityScope
        When the collection maps to a label that is missing from the
        manifest or is TENANT_SCOPED-without-path. Caller (Layer 4)
        refuses the query.
    """
    if not isinstance(collection, str) or not collection:
        raise UnknownEntityScope(collection or "<empty>")

    profile = sharding_profile if isinstance(sharding_profile, dict) else {}

    # Step 1: satellite / system short-circuit. The collection kinds
    # come from `metadata.shardingProfile.collections[*].{name,kind}`
    # (PRD §6.2 bullet 3). We tolerate both the list shape and a flat
    # name → kind dict shape because both have been observed in the
    # wild.
    kind = _collection_kind(collection, profile)
    if kind in {"satellite", "system"}:
        return None

    # Step 2: collection → entity label.
    label = _label_for_collection(collection, profile)

    # Step 3: delegate. predicate_for_entity raises UnknownEntityScope
    # when the label is unmapped; we let it propagate.
    return predicate_for_entity(label, manifest)


# ---------------------------------------------------------------------------
# Plan-node helpers — operate on ArangoDB EXPLAIN-plan dict nodes
# ---------------------------------------------------------------------------


def is_bindvar_reference(node: object, *, name: str) -> bool:
    """Return ``True`` iff ``node`` is an ArangoDB EXPLAIN-plan
    ``parameter`` node referencing bind variable ``name``.

    ArangoDB serialises bind-variable references in EXPLAIN plans as
    dict nodes ``{"type": "parameter", "name": "<bind_name>"}``. This
    helper is the canonical matcher used by Layer 5
    (``arango_cypher/tenant_plan_validator.py``) and by Layer 4
    (MT-4 AQL rewriter) when checking whether an injected predicate
    already exists at a given site (idempotency: don't double-add).

    Parameters
    ----------
    node:
        Anything; the helper is intentionally permissive about
        non-dict input so callers don't need to wrap every call in
        ``isinstance(...)``.
    name:
        Bind variable name *without* the leading ``@`` (e.g.
        ``"tenantId"`` not ``"@tenantId"``).
    """
    if not isinstance(node, dict):
        return False
    return node.get("type") == "parameter" and node.get("name") == name


def is_literal_tenant_value(
    node: object,
    manifest: TenantScopeManifest,
) -> bool:
    """Return ``True`` iff ``node`` is a literal whose value matches a
    known tenant key.

    This is the T2-defence primitive shared across Layers 2/3/4. A
    rewriter sees a ``e.TENANT_HEX_ID == 'tenant-A-uuid'`` predicate
    and asks "is the right-hand side a literal tenant?" — if yes,
    refuse: the only legitimate way to compare against a tenant is
    via the session-bound bind variable (:data:`TENANT_ID_BIND` /
    :data:`TENANT_KEY_BIND`).

    The helper recognises two literal shapes:

    * ArangoDB EXPLAIN-plan ``{"type": "value", "value": "..."}``
      (used by Layer 4 / MT-4 over plan nodes).
    * Bare Python strings (used by Layer 2 / MT-2 over regex matches
      from the Cypher source and by Layer 3 / MT-3 over ANTLR token
      literals once the rewriter has stripped surrounding quotes).

    Fail-closed contract
    --------------------
    When ``manifest.known_tenant_keys`` is ``None`` (the manifest was
    built without sampling the Tenant collection — common in tests,
    heuristic-mode mappings, older bundles), this function returns
    ``True`` for **any** string-valued literal. The reasoning:

    * The alternative ("can't tell, allow") is the original T2 bug.
    * A test or heuristic-mode caller can pass an explicitly-empty
      ``frozenset()`` to opt out of the fail-closed path; that
      signals "I have sampled and zero keys exist", which is a
      different statement from "I haven't sampled".

    Non-string literals (numbers, booleans, ``None``) are always
    ``False`` — tenant identifiers are strings by definition (PRD
    §1.2 / §3, tenantKey is a UUID-shaped string).

    Parameters
    ----------
    node:
        An EXPLAIN-plan dict, a bare string, or anything else
        (returns ``False`` for unrecognised shapes that are clearly
        not literals — e.g. attribute-access nodes, references).
    manifest:
        The tenant-scope manifest. ``known_tenant_keys`` is the only
        field read.
    """
    value = _literal_value(node)
    if value is None:
        return False
    if not isinstance(value, str):
        return False
    if manifest.known_tenant_keys is None:
        # Fail-closed: we cannot tell whether this string is a tenant
        # key, so we refuse the comparison. See the docstring's
        # fail-closed contract section.
        return True
    return value in manifest.known_tenant_keys


# ---------------------------------------------------------------------------
# Internal helpers — not part of the public surface
# ---------------------------------------------------------------------------


def cypher_literal_value(literal: str) -> str:
    """The value of the Cypher string literal *literal*, quotes included.

    Escapes are decoded as the translator decodes them
    (:func:`arango_cypher._translate_v0.literals.cypher_string_value`), so a
    check compares the value the query will compare. A literal the
    translator refuses comes back without its quotes; its translation
    fails anyway.
    """
    try:
        return cypher_string_value(literal)
    except CoreError:
        return literal[1:-1]


def _literal_value(node: object) -> Any | None:
    """Extract the scalar value from a literal node, or return None.

    Accepts:

    * Bare ``str`` / ``int`` / ``float`` / ``bool`` (returned as-is).
    * ArangoDB EXPLAIN-plan dict ``{"type": "value", "value": ...}``.

    Returns ``None`` for anything else, including references,
    attribute accesses, function calls, and dicts without a
    ``"type": "value"`` tag. ``None`` here is the sentinel for "not
    a literal"; it is **not** a valid literal value in its own right
    because ArangoDB serialises ``null`` literals as ``{"type":
    "value", "value": null}``, which would round-trip through this
    helper as a real ``None`` (but callers of
    :func:`is_literal_tenant_value` already short-circuit on
    non-string values).
    """
    if isinstance(node, (str, int, float, bool)):
        return node
    if isinstance(node, dict) and node.get("type") == "value":
        return node.get("value")
    return None


def _collection_kind(collection: str, sharding_profile: dict[str, Any]) -> str | None:
    """Look up a collection's physical kind in ``sharding_profile``.

    Tolerates two shapes the analyzer has emitted:

    * ``"collections": [{"name": "<c>", "kind": "satellite"}, ...]``
    * ``"collectionKinds": {"<c>": "satellite", ...}``

    Returns ``None`` when the collection is not present in either.
    """
    cols = sharding_profile.get("collections")
    if isinstance(cols, list):
        for entry in cols:
            if not isinstance(entry, dict):
                continue
            if entry.get("name") == collection:
                kind = entry.get("kind")
                if isinstance(kind, str):
                    return kind
                break
    kinds = sharding_profile.get("collectionKinds")
    if isinstance(kinds, dict):
        v = kinds.get(collection)
        if isinstance(v, str):
            return v
    return None


def _label_for_collection(collection: str, sharding_profile: dict[str, Any]) -> str:
    """Resolve a collection name to its conceptual entity label.

    Looks at ``sharding_profile.collectionToEntity`` (a name → label
    map produced by the analyzer). When the map is missing or the
    collection isn't in it, falls back to assuming the collection
    name equals the label name — true for ``COLLECTION`` style
    physical mappings (the common case) and the only safe default.
    """
    c2e = sharding_profile.get("collectionToEntity")
    if isinstance(c2e, dict):
        v = c2e.get(collection)
        if isinstance(v, str) and v:
            return v
    return collection
