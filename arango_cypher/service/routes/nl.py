"""NL-to-Cypher / NL-to-AQL endpoints + tenant catalog —
``/nl2cypher``, ``/nl-samples``, ``/nl2aql``, ``/tenants``.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from typing import Any

from arango.database import StandardDatabase
from arango.exceptions import AQLQueryExplainError
from arango_query_core import MappingBundle
from fastapi import Depends, HTTPException

from ..._arango_sync import sync
from ...tenant_ast_aql import AqlRewriteError
from ...tenant_plan_validator import TenantScopeViolation
from ..app import _PUBLIC_MODE, app
from ..models import NL2AqlRequest, NL2CypherRequest, NLSuggestRequest, TenantDiscoverRequest
from ..observability import (
    current_llm_provider_and_model,
    log_endpoint_timing,
    log_llm_call,
)
from ..safe_exec import (
    TenantScopeRewriteRejection,
    apply_layer3_rewrite,
    apply_layer4_rewrite,
)
from ..security import (
    _COLLECTION_NAME_RE,
    _check_nl_rate_limit,
    _get_session,
    _optional_session,
    _require_session_in_public_mode,
    _Session,
    _sessions,
    _translate_errors,
)

logger = logging.getLogger(__name__)


def _workbench_mode_enabled() -> bool:
    """Whether the service honors body-supplied ``tenant_context``.

    PRD ``docs/multitenant_prd.md`` §4.2 / Wave 7 part 1:

    * ``ARANGO_CYPHER_WORKBENCH`` ∈ {1, true, yes} — workbench mode.
      The request body's ``tenant_context`` is honored verbatim. This
      is the default for local development.
    * Anything else (including unset) — tenant-user mode. The body's
      ``tenant_context`` is silently overridden by the session-bound
      tenant if the session carries one; a WARN log records the
      override for audit.

    Resolved per-call (not module-load) so tests can flip the env via
    ``monkeypatch.setenv`` without re-importing the route module.
    """
    return os.getenv("ARANGO_CYPHER_WORKBENCH", "").lower() in {"1", "true", "yes"}


def _apply_session_tenant_to_context(
    body_ctx,
    *,
    session: _Session | None,
    endpoint: str,
):
    """Return the ``TenantContext`` that the NL pipeline should use.

    Imported locally to keep the route module free of the heavier
    ``nl2cypher`` import on every cold start.

    Behaviour matches PRD §4.2:

    * Workbench mode → return ``body_ctx`` untouched (may be ``None``).
    * Tenant-user mode with a session-bound tenant → return a
      ``TenantContext`` built from the session, ignoring any body value
      whose ``value`` differs from the session's ``tenant_id``. A
      WARN log records the override for audit.
    * Tenant-user mode without a session-bound tenant → return
      ``body_ctx`` (the legacy behaviour). Layer 5 will refuse any
      tenant-scoped read in this state, so leaving the body through is
      a usability win for global / satellite-only queries without
      compromising safety.
    """
    from ...nl2cypher.tenant_guardrail import TenantContext

    if _workbench_mode_enabled():
        return body_ctx

    tenant_id = getattr(session, "tenant_id", None) if session is not None else None
    if not isinstance(tenant_id, str) or not tenant_id:
        return body_ctx

    if body_ctx is not None and body_ctx.value != tenant_id:
        logger.warning(
            "%s: body-supplied tenant_context=%r ignored; session-bound tenant=%r",
            endpoint,
            body_ctx.value,
            tenant_id,
        )

    return TenantContext(
        property="_key",
        value=tenant_id,
        display=(body_ctx.display if body_ctx is not None else None),
    )


@app.post("/nl2cypher")
def nl2cypher_endpoint(
    req: NL2CypherRequest,
    _: None = Depends(_check_nl_rate_limit),
    auth_session: _Session | None = Depends(_require_session_in_public_mode),
    bound_session: _Session | None = Depends(_optional_session),
):
    """Translate a natural language question into Cypher.

    When ``session_token`` is supplied and entity resolution is enabled,
    the session's live ``StandardDatabase`` is passed through to
    :func:`nl_to_cypher` so mentions in the question can be rewritten to
    their database-correct form (WP-25.2).  Without a token the resolver
    is silently disabled and the prompt falls back to its pre-WP-25.2
    shape.

    In ``ARANGO_CYPHER_PUBLIC_MODE`` the request body's
    ``session_token`` field is ignored — the authenticated session
    (resolved from ``X-Arango-Session`` / ``Authorization``) is used
    instead, so a caller cannot point one user's NL request at another
    user's database by guessing the body field.
    """
    from ...nl2cypher import nl_to_cypher
    from ...nl2cypher.tenant_guardrail import TenantContext

    db = None
    if _PUBLIC_MODE:
        if auth_session is not None and req.use_entity_resolution:
            db = auth_session.db
            auth_session.touch()
    elif req.use_entity_resolution and req.session_token:
        sess = _sessions.get(req.session_token)
        # A platform session acts as the JWT on the request; only the
        # header-resolved ``bound_session`` has been re-bound to it, so a
        # body token alone must not reach its (possibly stale) handle.
        if sess is not None and sess.platform_token is not None and sess is not bound_session:
            sess = None
        if sess is not None:
            db = sess.db
            sess.touch()

    tenant_ctx = None
    if req.tenant_context is not None:
        tenant_ctx = TenantContext(
            property=req.tenant_context.property,
            value=req.tenant_context.value,
            display=req.tenant_context.display,
        )
    tenant_ctx = _apply_session_tenant_to_context(
        tenant_ctx,
        session=auth_session or bound_session,
        endpoint="/nl2cypher",
    )

    t0 = time.perf_counter()
    result = nl_to_cypher(
        req.question,
        mapping=req.mapping,
        use_llm=req.use_llm,
        use_fewshot=req.use_fewshot,
        use_entity_resolution=req.use_entity_resolution,
        db=db,
        tenant_context=tenant_ctx,
        retry_context=req.retry_context,
    )
    elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)

    # Layer 3 (Wave 8a / MT-3) — Cypher AST tenant-injection. For a
    # tenant-bound session with a mapping, rewrite the generated Cypher so
    # every tenant-scoped node pattern carries a bind-variable tenant
    # predicate before it is returned (and later transpiled + executed, where
    # Layer 4/5 re-enforce). Skipped for workbench/unbound sessions and when
    # no mapping is supplied (Layer 5 remains the fail-closed boundary).
    layer3_cypher = result.cypher
    layer3_changes: list[str] = []
    rewrite_session = auth_session or bound_session
    if (
        rewrite_session is not None
        and getattr(rewrite_session, "tenant_id", None)
        and result.cypher
        and req.mapping
    ):
        try:
            layer3_cypher, layer3_changes = apply_layer3_rewrite(
                cypher=result.cypher,
                mapping_dict=req.mapping,
                session=rewrite_session,
            )
        except TenantScopeRewriteRejection as exc:
            # Literal tenant predicate / unknown label → fail-closed. Same
            # 403 shape the AQL path uses for a tenant-safety refusal.
            raise HTTPException(
                status_code=403,
                detail={
                    "error": "tenant_scope_violation",
                    "code": exc.code,
                    "message": exc.message,
                    "where": exc.where,
                },
            ) from exc
        except TenantScopeViolation as exc:
            raise HTTPException(
                status_code=403,
                detail={
                    "error": "tenant_scope_violation",
                    "code": exc.code,
                    "message": exc.message,
                },
            ) from exc

    provider, model = current_llm_provider_and_model()
    log_llm_call(
        endpoint="/nl2cypher",
        provider=provider,
        model=model,
        prompt_tokens=result.prompt_tokens or 0,
        completion_tokens=result.completion_tokens or 0,
        cached_tokens=result.cached_tokens or 0,
        elapsed_ms=elapsed_ms,
        method=result.method,
        retries=result.retries or 0,
        confidence=result.confidence,
    )
    log_endpoint_timing(
        "/nl2cypher",
        elapsed_ms,
        method=result.method,
        confidence=result.confidence,
        cypher_len=len(layer3_cypher or ""),
        question_len=len(req.question or ""),
        used_entity_resolution=bool(req.use_entity_resolution and db is not None),
        retries=result.retries or 0,
        tenant_rewrites=len(layer3_changes),
    )
    return {
        "cypher": layer3_cypher,
        "explanation": result.explanation,
        "confidence": result.confidence,
        "method": result.method,
        "elapsed_ms": elapsed_ms,
        "prompt_tokens": result.prompt_tokens,
        "completion_tokens": result.completion_tokens,
        "total_tokens": result.total_tokens,
        "cached_tokens": result.cached_tokens,
        "retries": result.retries,
        # WP-S3c: inverted/ArangoSearch index advisories from the entity
        # resolver (fuzzy probes that fell back to a full scan). The UI offers
        # one-click creation via POST /schema/index/create. ``getattr`` keeps
        # older/mock result objects (and any non-LLM path) safe.
        "advisories": getattr(result, "advisories", None) or [],
        # Layer 3 (MT-3) tenant-scope injections applied to the Cypher, one
        # human-readable line per rewrite, for the UI annotation strip.
        "tenantRewrites": layer3_changes,
    }


@app.post("/nl-samples")
def nl_samples_endpoint(
    req: NLSuggestRequest,
    _: None = Depends(_check_nl_rate_limit),
    _auth: _Session | None = Depends(_require_session_in_public_mode),
):
    """Return a representative set of NL questions for the given schema.

    Used by the UI to seed the "Ask" history after schema mapping. Falls back
    to rule-based generation when no LLM provider is configured.
    """
    from ...nl2cypher import suggest_nl_queries

    t0 = time.perf_counter()
    queries = suggest_nl_queries(
        req.mapping,
        count=req.count,
        use_llm=req.use_llm,
    )
    elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
    log_endpoint_timing(
        "/nl-samples",
        elapsed_ms,
        count=len(queries),
        use_llm=bool(req.use_llm),
    )
    return {"queries": queries, "elapsed_ms": elapsed_ms}


def _server_explain(db: StandardDatabase) -> Callable[[str], str | None]:
    """``nl_to_aql``'s server check on the connected database: the EXPLAIN error, or ``None``.

    Only a refusal of the query itself (400: syntax, unknown function; 404: unknown
    collection) goes back to the model; anything else raises and is reported as itself.
    """

    def explain(aql: str) -> str | None:
        try:
            db.aql.explain(aql)
        except AQLQueryExplainError as e:
            if e.http_code in (400, 404):
                return str(e)
            raise
        return None

    return explain


def _maps_this_db(mapping: dict[str, Any] | None, db: StandardDatabase) -> bool:
    """Whether *db* holds every collection *mapping* names.

    A mapping for another database would make EXPLAIN refuse every candidate on a
    mapped collection, which the model cannot repair; the regex check is kept then.
    """
    from ...nl2cypher._aql import _collect_known_collections
    from ...nl2cypher.tenant_scope import _physical_mapping

    named = _collect_known_collections(
        MappingBundle(conceptual_schema={}, physical_mapping=_physical_mapping(mapping), metadata={})
    )
    if not named:
        return True
    try:
        held = {c["name"] for c in sync(db.collections())}
    except Exception:  # noqa: BLE001 - listing failed: keep main's regex check rather than fail the request
        return False
    return named <= held


@app.post("/nl2aql")
def nl2aql_endpoint(
    req: NL2AqlRequest,
    _: None = Depends(_check_nl_rate_limit),
    auth_session: _Session | None = Depends(_require_session_in_public_mode),
    bound_session: _Session | None = Depends(_optional_session),
):
    """Translate a natural language question directly into AQL (bypassing Cypher)."""
    from ...nl2cypher import nl_to_aql
    from ...nl2cypher.tenant_guardrail import TenantContext

    tenant_ctx = None
    if req.tenant_context is not None:
        tenant_ctx = TenantContext(
            property=req.tenant_context.property,
            value=req.tenant_context.value,
            display=req.tenant_context.display,
        )
    tenant_ctx = _apply_session_tenant_to_context(
        tenant_ctx,
        session=auth_session or bound_session,
        endpoint="/nl2aql",
    )

    session = auth_session or bound_session
    explain = None
    if session is not None and _maps_this_db(req.mapping, session.db):
        explain = _server_explain(session.db)
    t0 = time.perf_counter()
    result = nl_to_aql(
        req.question,
        mapping=req.mapping,
        tenant_context=tenant_ctx,
        cypher=req.cypher,
        explain=explain,
    )
    elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)

    # Layer 4 (Wave 8a / MT-4) — the *only* tenant-scope defence on
    # the NL→AQL direct path (Layer 3 / Cypher AST never runs because
    # no Cypher is generated). Rewrite every unscoped tenant-scoped
    # read before returning the AQL to the caller; the same AQL will
    # be re-validated by Layer 5 when the caller submits it via
    # `/execute-aql`.
    #
    # The rewriter requires a mapping bundle to know which collections
    # are tenant-scoped. ``/nl2aql`` historically accepts requests
    # without a mapping (the UI calls it before the user has loaded
    # a schema; the prompt-only flow doesn't need one), so we skip
    # Layer 4 in that case. Layer 5 / Layer 4-at-/execute-aql remain
    # the security boundaries — they refuse fail-closed when the
    # tenant-bound session submits raw AQL without a mapping.
    layer4_aql = result.aql
    layer4_bind = dict(result.bind_vars or {})
    layer4_changes: list[str] = []
    rewrite_session = auth_session or bound_session
    if (
        rewrite_session is not None
        and getattr(rewrite_session, "tenant_id", None)
        and result.aql
        and req.mapping  # see comment above re: mapping-optional contract
    ):
        try:
            layer4_aql, layer4_bind, layer4_changes = apply_layer4_rewrite(
                db=rewrite_session.db,
                aql=result.aql,
                bind_vars=result.bind_vars,
                session=rewrite_session,
                mapping_dict=req.mapping,
            )
        except AqlRewriteError as exc:
            # AMBIGUITY: NL output that the rewriter cannot safely
            # constrain is a translation-quality issue; we surface
            # 403 (same shape as a Layer-5 violation) so the UI
            # treats it as a tenant-safety refusal rather than a
            # transient retry candidate. A future MT-4.1 may route
            # this back into the NL retry loop with the violation
            # code as a hint, but doing so today would change the
            # response shape and require the UI to learn a new code.
            status = 403 if exc.code == "UNCONSTRAINED_COLLECTION_ACCESS" else 422
            raise HTTPException(
                status_code=status,
                detail={
                    "error": "aql_rewrite_failed",
                    "code": exc.code,
                    "message": exc.message,
                },
            ) from exc
        except TenantScopeViolation as exc:
            raise HTTPException(
                status_code=403,
                detail={
                    "error": "tenant_scope_violation",
                    "code": exc.code,
                    "message": exc.message,
                },
            ) from exc

    provider, model = current_llm_provider_and_model()
    log_llm_call(
        endpoint="/nl2aql",
        provider=provider,
        model=model,
        prompt_tokens=result.prompt_tokens or 0,
        completion_tokens=result.completion_tokens or 0,
        cached_tokens=result.cached_tokens or 0,
        elapsed_ms=elapsed_ms,
        method=result.method,
        confidence=result.confidence,
    )
    log_endpoint_timing(
        "/nl2aql",
        elapsed_ms,
        method=result.method,
        confidence=result.confidence,
        aql_len=len(layer4_aql or ""),
        question_len=len(req.question or ""),
        tenant_rewrites=len(layer4_changes),
    )
    return {
        "aql": layer4_aql,
        "bind_vars": layer4_bind,
        "explanation": result.explanation,
        "confidence": result.confidence,
        "method": result.method,
        "elapsed_ms": elapsed_ms,
        "prompt_tokens": result.prompt_tokens,
        "completion_tokens": result.completion_tokens,
        "total_tokens": result.total_tokens,
        "cached_tokens": result.cached_tokens,
        "tenantRewritesAql": layer4_changes,
    }


# ---------------------------------------------------------------------------
# Tenant catalog (multi-tenant graphs)
# ---------------------------------------------------------------------------

# Maximum number of tenants to surface in a single catalog response. 10k is
# ample headroom for the target schemas (Dagster-style graphs top out around
# 10³); clients that need more should paginate via a follow-up API.
_TENANT_CATALOG_LIMIT = 10000


@app.get("/tenants")
def tenants_endpoint(
    collection: str | None = None,
    session: _Session = Depends(_get_session),
):
    """Return the list of tenants in the connected database, if any.

    The optional ``collection`` query parameter lets the UI tell the
    server which ArangoDB collection backs the conceptual ``Tenant``
    entity (typically derived client-side from
    ``physical_mapping.entities.Tenant.collectionName``). When omitted,
    the endpoint falls back to the literal name ``Tenant`` — the
    pre-Wave-4r behaviour, kept for compatibility with stale UIs.

    Why a query param instead of POST-with-mapping? Three reasons:

    1. POST-with-body for a pure read trips CORS preflights in
       cross-origin deployments.
    2. A new UI bundle deployed against an older service (the common
       case during rolling deploys) would 405 on POST and silently
       hide the selector with no diagnostic.
    3. The mapping already lives in the UI's state; sending it back
       just so the server can pluck a single string out wastes a
       megabyte of payload per call on real schemas.

    The response includes ``collection`` (the resolved name we
    queried) and ``source`` (``"client"`` when the caller supplied
    the name, ``"heuristic"`` when we fell back to ``"Tenant"``)
    so the UI can show *why* detection succeeded or failed.
    """
    t0 = time.perf_counter()
    db = session.db
    if collection:
        resolved, source = collection, "client"
    else:
        resolved, source = "Tenant", "heuristic"

    # Defence-in-depth against AQL identifier injection: the resolved name is
    # interpolated into the AQL f-string below inside backticks, so anything
    # that isn't a valid ArangoDB collection identifier must be rejected at
    # the edge. `has_collection()` returns False for names that don't exist
    # but does *not* reject syntactically invalid names on all client
    # versions, hence the explicit gate.
    if not _COLLECTION_NAME_RE.fullmatch(resolved):
        raise HTTPException(
            status_code=400,
            detail=(
                "Invalid collection name: must be 1–256 characters, start "
                "with a letter or underscore, and contain only letters, "
                "digits, underscore, or hyphen."
            ),
        )

    with _translate_errors("Failed to inspect collections"):
        has_collection = db.has_collection(resolved)

    if not has_collection:
        log_endpoint_timing(
            "/tenants",
            round((time.perf_counter() - t0) * 1000, 1),
            detected=False,
            tenants=0,
            source=source,
        )
        return {
            "detected": False,
            "tenants": [],
            "collection": resolved,
            "source": source,
        }

    aql = (
        f"FOR t IN `{resolved}` "
        f"LIMIT {_TENANT_CATALOG_LIMIT} "
        "SORT t.NAME "
        "RETURN { "
        # `id` (full _id, e.g. 'Tenant/<uuid>') is the canonical
        # tenant identifier — what the guardrail uses to scope
        # generated Cypher. `key` is exposed too for tooltips and
        # for the Cypher `{_key: '...'}` shorthand. The schema-
        # specific NAME / SUBDOMAIN / TENANT_HEX_ID fields are
        # surfaced for human display and search but are not
        # required to exist; the LIMIT-projection tolerates nulls.
        "id: t._id, "
        "key: t._key, "
        "name: t.NAME, "
        "subdomain: t.SUBDOMAIN, "
        "hex_id: t.TENANT_HEX_ID "
        "}"
    )
    with _translate_errors("Tenant catalog query failed"):
        cursor = db.aql.execute(aql)
        tenants = list(sync(cursor))

    log_endpoint_timing(
        "/tenants",
        round((time.perf_counter() - t0) * 1000, 1),
        detected=True,
        tenants=len(tenants),
        source=source,
    )
    return {
        "detected": True,
        "tenants": tenants,
        "collection": resolved,
        "source": source,
    }


# Per (collection, field) pair, cap the distinct-value scan so a huge
# tenant-scoped collection can't wedge the discovery call. 5000 distinct
# tenants is far beyond any realistic interactive picker.
_TENANT_DISCOVER_LIMIT = 5000
# Cap how many scoped collections we probe so a wide schema with dozens
# of tenant-scoped entities doesn't fan out into dozens of COLLECT scans.
_TENANT_DISCOVER_MAX_COLLECTIONS = 8


@app.post("/tenants/discover")
def tenants_discover_endpoint(
    req: TenantDiscoverRequest,
    session: _Session = Depends(_get_session),
):
    """Discover selectable tenants for the connected database.

    Unlike ``GET /tenants`` (which only enumerates a literal/aliased
    ``Tenant`` collection), this builds the tenant-scope manifest from
    the supplied mapping and supports the common real-world shape where
    there is **no** ``Tenant`` collection — tenants exist only as
    denormalised values on scoped collections (e.g. ``Alert.tenantId``).

    Resolution order:

    1. ``Tenant`` root collection present → enumerate it (rich records).
    2. Otherwise → for each distinct ``(collection, denorm_field)`` of
       the manifest's ``TENANT_SCOPED`` entities (bounded), sample the
       distinct field values and union them into a tenant list.
    3. Neither → ``multiTenant: false`` (single-tenant / reference-only).

    Response::

        {
          "multiTenant": bool,
          "scope": "collection" | "denorm" | "none",
          "tenantField": str | null,   # denorm field, when scope=denorm
          "tenants": [{"id","key","name","subdomain","hex_id","docs"?}],
          "collections": [str],        # probed collections (denorm scope)
        }
    """
    from ...nl2cypher.tenant_scope import EntityTenantRole, analyze_tenant_scope
    from ..mapping import _mapping_from_dict

    t0 = time.perf_counter()
    db = session.db
    mapping = _mapping_from_dict(req.mapping) if req.mapping else None
    if mapping is None:
        log_endpoint_timing(
            "/tenants/discover",
            round((time.perf_counter() - t0) * 1000, 1),
            multi_tenant=False,
            scope="none",
        )
        return {
            "multiTenant": False,
            "scope": "none",
            "tenantField": None,
            "tenants": [],
            "collections": [],
        }

    manifest = analyze_tenant_scope(mapping)

    # ---- 1) Tenant-root collection path --------------------------------
    if manifest.tenant_entity is not None:
        coll = _collection_name_for(mapping, manifest.tenant_entity) or "Tenant"
        if _COLLECTION_NAME_RE.fullmatch(coll):
            with _translate_errors("Failed to inspect collections"):
                exists = db.has_collection(coll)
            if exists:
                aql = (
                    f"FOR t IN `{coll}` LIMIT {_TENANT_CATALOG_LIMIT} SORT t.NAME "
                    "RETURN {id: t._id, key: t._key, name: t.NAME, "
                    "subdomain: t.SUBDOMAIN, hex_id: t.TENANT_HEX_ID}"
                )
                with _translate_errors("Tenant catalog query failed"):
                    tenants = list(sync(db.aql.execute(aql)))
                log_endpoint_timing(
                    "/tenants/discover",
                    round((time.perf_counter() - t0) * 1000, 1),
                    multi_tenant=True,
                    scope="collection",
                    tenants=len(tenants),
                )
                return {
                    "multiTenant": True,
                    "scope": "collection",
                    "tenantField": "_key",
                    "tenants": tenants,
                    "collections": [coll],
                }

    # ---- 2) Denormalised-field discovery -------------------------------
    # Collect distinct (collection, denorm_field) pairs across scoped
    # entities. Several conceptual entities can share one physical
    # collection (severity-subtyped Alert → Critical/Info/Warning), so
    # we dedupe on the pair to avoid scanning the same collection twice.
    pairs: list[tuple[str, str]] = []
    seen_pairs: set[tuple[str, str]] = set()
    for entity_name, scope in manifest.entities.items():
        if scope.role is not EntityTenantRole.TENANT_SCOPED or not scope.denorm_field:
            continue
        coll = _collection_name_for(mapping, entity_name) or entity_name
        if not _COLLECTION_NAME_RE.fullmatch(coll):
            continue
        pair = (coll, scope.denorm_field)
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)
        pairs.append(pair)

    tenant_field = pairs[0][1] if pairs else None
    values: dict[str, int] = {}
    probed: list[str] = []
    for coll, field_name in pairs[:_TENANT_DISCOVER_MAX_COLLECTIONS]:
        with _translate_errors("Failed to inspect collections"):
            if not db.has_collection(coll):
                continue
        aql = (
            f"FOR d IN `{coll}` FILTER d.@field != null "
            f"COLLECT v = d.@field WITH COUNT INTO n "
            f"SORT n DESC LIMIT {_TENANT_DISCOVER_LIMIT} "
            "RETURN {value: v, docs: n}"
        )
        with _translate_errors("Tenant discovery query failed"):
            rows = list(sync(db.aql.execute(aql, bind_vars={"field": field_name})))
        probed.append(coll)
        for r in rows:
            v = r.get("value")
            if isinstance(v, str) and v:
                values[v] = values.get(v, 0) + int(r.get("docs") or 0)

    if values:
        tenants = [
            {"id": v, "key": v, "name": v, "subdomain": None, "hex_id": None, "docs": cnt}
            for v, cnt in sorted(values.items(), key=lambda kv: (-kv[1], kv[0]))
        ]
        log_endpoint_timing(
            "/tenants/discover",
            round((time.perf_counter() - t0) * 1000, 1),
            multi_tenant=True,
            scope="denorm",
            tenants=len(tenants),
            collections=len(probed),
        )
        return {
            "multiTenant": True,
            "scope": "denorm",
            "tenantField": tenant_field,
            "tenants": tenants,
            "collections": probed,
        }

    # ---- 3) Single-tenant / reference-only -----------------------------
    log_endpoint_timing(
        "/tenants/discover",
        round((time.perf_counter() - t0) * 1000, 1),
        multi_tenant=bool(pairs),
        scope="denorm" if pairs else "none",
        tenants=0,
    )
    return {
        # ``pairs`` non-empty but no values means the schema *is*
        # tenant-scoped but the probed collections were empty — still
        # multi-tenant, just nothing to pick yet.
        "multiTenant": bool(pairs),
        "scope": "denorm" if pairs else "none",
        "tenantField": tenant_field,
        "tenants": [],
        "collections": probed,
    }


def _collection_name_for(mapping: object, entity_name: str) -> str | None:
    """Physical collection name backing *entity_name*, or None.

    Reads ``physical_mapping.entities[entity].collectionName`` from a
    mapping bundle (object or dict) via the same extractor the
    tenant-scope analyzer uses, so collection-name resolution can't
    drift between classification and discovery.
    """
    from ...nl2cypher.tenant_scope import _physical_mapping

    ents = _physical_mapping(mapping).get("entities") or {}
    entry = ents.get(entity_name) if isinstance(ents, dict) else None
    if isinstance(entry, dict):
        coll = entry.get("collectionName") or entry.get("collection")
        if isinstance(coll, str) and coll:
            return coll
    return None
