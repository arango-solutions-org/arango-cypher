"""Pydantic request / response models + ``_MAX_*`` length constants.

Every model in this module is re-exported from
:mod:`arango_cypher.service` so the historical
``from arango_cypher.service import TranslateRequest`` style imports
keep working across the audit-v2 #8 split. The route modules under
:mod:`arango_cypher.service.routes` import them via the package init
to keep cross-module dependencies one-directional (routes depend on
models, never the other way round).

The ``_MAX_*`` constants live as module-level attributes (intentionally
underscore-prefixed) so an operator who needs to raise one for a
specific deployment can grep, monkeypatch, and rebuild the affected
model. Closes audit-v2 finding #5 — see that audit's section 5
close-out for the per-field rationale.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator

# Stricter-than-type field bounds for the user-controlled string fields on
# every request model below. Closes audit-v2 finding #5 — without these,
# a single 10 MB POST body can wedge the ANTLR parser thread, push novel-
# length prompts at an LLM, or balloon the corrections SQLite store.
# These constants are intentionally generous (a real Cypher query is
# almost never above ~10 KB) so they bound an attack rather than restrict
# normal use, and they live as module-level constants so an operator who
# needs to raise one for a specific deployment can grep + monkeypatch.
_MAX_CYPHER_LENGTH = 100_000  # ~100 KB; a real interactive query is < 10 KB
_MAX_AQL_LENGTH = 100_000  # raw AQL on /execute-aql; same envelope
_MAX_NL_QUESTION_LENGTH = 4_000  # bounds LLM context-window cost
_MAX_RETRY_HINT_LENGTH = 8_000  # WP-29 retry context (parser/EXPLAIN error blob)
_MAX_TURTLE_LENGTH = 1_000_000  # OWL ontologies can be sizeable; 1 MB cap
_MAX_NOTE_LENGTH = 4_000  # correction notes — keep them human-readable
_MAX_FIELD_LENGTH = 256  # urls, usernames, db names, tool names, tenant fields
# Most rows a caller may ask /execute and /execute-aql for (``max_rows``). A
# response is built in memory and sent as one JSON document: 156,861 whole
# IAM nodes came to 257 MB and ten minutes on the wire.
_MAX_RESULT_ROWS = 1_000_000


class ConnectRequest(BaseModel):
    url: str = Field(default="http://localhost:8529", max_length=_MAX_FIELD_LENGTH)
    database: str = Field(default="_system", max_length=_MAX_FIELD_LENGTH)
    username: str = Field(default="root", max_length=_MAX_FIELD_LENGTH)
    password: str = Field(default="", max_length=_MAX_FIELD_LENGTH)
    # Multi-tenant session binding (PRD docs/multitenant_prd.md §4).
    # When supplied at /connect, the tenant identifier is server-bound
    # on the session and Layer 5 (EXPLAIN-plan validator) enforces it
    # against every query — independent of any subsequent request body.
    # Optional so workbench / single-tenant deployments keep working.
    tenantId: str | None = Field(default=None, max_length=_MAX_FIELD_LENGTH)
    tenantKey: str | None = Field(default=None, max_length=_MAX_FIELD_LENGTH)
    isAdmin: bool = Field(default=False)

    @field_validator("url")
    @classmethod
    def _url_shape(cls, v: str) -> str:
        # Defensive shape-check before the SSRF guard at /connect runs.
        # The /connect endpoint already rejects bad targets at runtime via
        # the SSRF allowlist (PR #7); this validator catches the cheaper,
        # more obviously-broken cases at request-validation time so the
        # caller gets a 422 with a clear "url is malformed" hint instead
        # of a deeper 4xx/5xx after the connect machinery has spun up.
        if not v:
            return v
        lowered = v.lower()
        if not (lowered.startswith("http://") or lowered.startswith("https://")):
            raise ValueError("url must start with http:// or https://")
        return v


class ConnectResponse(BaseModel):
    token: str
    databases: list[str]
    # The database the session opened. ``/connect/platform`` may choose it
    # (the caller can name none); ``/connect`` echoes the requested one.
    database: str | None = None
    # ``/connect/platform`` only: the platform user the session runs as, as
    # the platform's integration sidecar names them (``None`` when unknown).
    user: str | None = None
    # Echoed for UI transparency: which tenant the session is bound to,
    # if any. UI surfaces this in the connect status badge so the user
    # always sees the active tenant alongside the active database.
    tenant_id: str | None = None
    tenant_key: str | None = None
    is_admin: bool = False


class PlatformConnectRequest(BaseModel):
    """Body for ``POST /connect/platform``.

    No credentials: the session authenticates as the platform JWT the
    gateway forwards. ``database`` of ``None`` opens the instance's mount
    database (see :func:`arango_cypher.service.platform_auth.default_database`).
    """

    database: str | None = Field(default=None, max_length=_MAX_FIELD_LENGTH)


class PlatformStatus(BaseModel):
    """``GET /connect/platform`` — can this request open a platform session?"""

    available: bool
    database: str
    #: Why ``available`` is false; ``None`` when it is true.
    reason: str | None = None


class TenantDiscoverRequest(BaseModel):
    """Body for ``POST /tenants/discover``.

    Carries the introspected mapping so the server can build the
    tenant-scope manifest and decide how to enumerate tenants:
    from a ``Tenant`` collection when one exists, or by sampling the
    distinct values of the denormalised tenant field otherwise.
    """

    mapping: dict[str, Any] | None = None


class BindTenantRequest(BaseModel):
    """Body for ``POST /session/tenant`` — re-bind the active session's
    tenant after schema analysis without re-authenticating.

    ``tenantId`` is ``None`` to clear the binding ("all tenants" /
    reference-only mode). ``tenantKey`` defaults to ``tenantId`` when
    omitted (denormalised-tenant schemas key on the id itself).
    """

    tenantId: str | None = Field(default=None, max_length=_MAX_FIELD_LENGTH)
    tenantKey: str | None = Field(default=None, max_length=_MAX_FIELD_LENGTH)


class BindGraphRequest(BaseModel):
    """Body for ``POST /session/graph`` — bind (or clear) the active session's
    named-graph scope after schema analysis (PRD §17).

    ``graphName`` is ``None`` to clear the binding ("all collections" mode);
    otherwise it must name an existing graph in the connected database (the
    endpoint validates existence and returns HTTP 404 on a miss).
    """

    graphName: str | None = Field(default=None, max_length=_MAX_FIELD_LENGTH)


class TranslateRequest(BaseModel):
    cypher: str = Field(..., max_length=_MAX_CYPHER_LENGTH)
    mapping: dict[str, Any] | None = None
    params: dict[str, Any] | None = None
    extensions_enabled: bool = True


class TranslateResponse(BaseModel):
    aql: str
    bind_vars: dict[str, Any]
    warnings: list[dict[str, Any]]
    elapsed_ms: float | None = None
    # Wave 8a / MT-4 — Layer 4 (AQL AST tenant-injection) audit trail.
    # One human-readable string per rewrite site (e.g. "Added FILTER
    # e.TENANT_HEX_ID == @tenantId after FOR e IN Employee"). The UI
    # renders the list as an annotation strip above the AQL editor
    # so the user can see *what* Layer 4 changed and *why*. Empty
    # list when no rewrites were needed (satellite-only or already-
    # constrained query); absent / null on pre-Wave-8a service builds
    # so older UI bundles handle it gracefully (UI treats `null` and
    # `[]` identically — no strip rendered).
    tenantRewritesAql: list[str] = []


class ExecuteRequest(BaseModel):
    cypher: str = Field(..., max_length=_MAX_CYPHER_LENGTH)
    mapping: dict[str, Any] | None = None
    params: dict[str, Any] | None = None
    extensions_enabled: bool = True
    # MT-7 — admin cross-tenant bypass. Honoured only for an admin-flagged
    # session with a non-empty ``bypass_reason``; Layer 5 then skips its
    # tenant-scope enforcement (structural EXPLAIN check still runs) and
    # records the event on the audit stream. Ignored for non-admins.
    cross_tenant: bool = False
    bypass_reason: str | None = Field(default=None, max_length=_MAX_FIELD_LENGTH)
    # Return at most this many rows; ``truncated`` in the response says
    # whether there were more. ``None`` (the default) returns every row.
    max_rows: int | None = Field(default=None, ge=1, le=_MAX_RESULT_ROWS)


class ExecuteResponse(BaseModel):
    results: list[Any]
    aql: str
    bind_vars: dict[str, Any]
    warnings: list[dict[str, Any]] = []
    exec_ms: float | None = None
    # Wall-clock time spent in the Cypher → AQL transpiler on this
    # request. Surfaced separately from `exec_ms` so the UI can show
    # both badges side-by-side after a Run; otherwise users lose
    # visibility into translation cost the moment they execute.
    translate_ms: float | None = None
    # Wave 8a / MT-4 — see TranslateResponse.tenantRewritesAql.
    tenantRewritesAql: list[str] = []
    # True when the request's ``max_rows`` cut the result short; ``row_limit``
    # echoes that cap (``None`` when the request set none).
    truncated: bool = False
    row_limit: int | None = None
    # A query that needs several AQL statements runs as a plan
    # (arango_cypher.translate_plan): each step's aql, bind_vars and the bind
    # parameters it reads / hands on its rows as. ``None`` for one statement.
    plan_steps: list[dict[str, Any]] | None = None


class ValidateRequest(BaseModel):
    cypher: str = Field(..., max_length=_MAX_CYPHER_LENGTH)
    mapping: dict[str, Any] | None = None
    params: dict[str, Any] | None = None


class ValidateResponse(BaseModel):
    ok: bool
    errors: list[dict[str, str]]


class ErrorResponse(BaseModel):
    error: str
    code: str


class ExecuteAqlRequest(BaseModel):
    aql: str = Field(..., max_length=_MAX_AQL_LENGTH)
    bind_vars: dict[str, Any] = Field(default_factory=dict)
    # Mapping bundle for Layer 5 (EXPLAIN-plan tenant-scope validator).
    # Optional — pre-Wave-7 callers may omit it. With a tenant-bound
    # session and no mapping, /execute-aql is refused fail-closed.
    mapping: dict[str, Any] | None = None
    # MT-7 — admin cross-tenant bypass; see ExecuteRequest.cross_tenant.
    cross_tenant: bool = False
    bypass_reason: str | None = Field(default=None, max_length=_MAX_FIELD_LENGTH)
    # See ExecuteRequest.max_rows.
    max_rows: int | None = Field(default=None, ge=1, le=_MAX_RESULT_ROWS)


class ToolCallRequest(BaseModel):
    name: str = Field(..., max_length=_MAX_FIELD_LENGTH)
    arguments: dict[str, Any] = {}


class SuggestIndexesRequest(BaseModel):
    mapping: dict[str, Any]


class TenantContextPayload(BaseModel):
    """Ambient tenant scope applied to NL translations in a session.

    Mirrors :class:`arango_cypher.nl2cypher.tenant_guardrail.TenantContext`
    on the wire. See ``/tenants`` for how the UI sources this.
    """

    property: str = Field(
        ...,
        max_length=_MAX_FIELD_LENGTH,
        description=(
            "Physical property name on the Tenant entity (e.g. 'TENANT_HEX_ID', 'NAME', 'SUBDOMAIN')."
        ),
    )
    value: str = Field(..., max_length=_MAX_FIELD_LENGTH, description="Exact value to match.")
    display: str | None = Field(
        default=None,
        max_length=_MAX_FIELD_LENGTH,
        description="Optional human-readable label for prompts / UI.",
    )


class NL2CypherRequest(BaseModel):
    question: str = Field(..., max_length=_MAX_NL_QUESTION_LENGTH)
    mapping: dict[str, Any] | None = None
    use_llm: bool = True
    use_fewshot: bool = True
    use_entity_resolution: bool = True
    session_token: str | None = Field(default=None, max_length=_MAX_FIELD_LENGTH)
    tenant_context: TenantContextPayload | None = None
    # WP-29 Part 4: WP-30 hand-off contract. When supplied, the NL
    # retry loop seeds ``PromptBuilder.retry_context`` on the very
    # first attempt with the caller-provided hint (typically the
    # parse / EXPLAIN error from a prior translate). WP-30 wires
    # this from the UI's "Regenerate from NL with error hint"
    # button; without a caller it stays ``None`` and the prompt is
    # byte-identical to the pre-WP-29 shape for zero-shot bare-name
    # schemas.
    retry_context: str | None = Field(default=None, max_length=_MAX_RETRY_HINT_LENGTH)


class NLSuggestRequest(BaseModel):
    mapping: dict[str, Any] | None = None
    count: int = Field(default=8, ge=1, le=20)
    use_llm: bool = True


class NL2AqlRequest(BaseModel):
    question: str = Field(..., max_length=_MAX_NL_QUESTION_LENGTH)
    mapping: dict[str, Any] | None = None
    tenant_context: TenantContextPayload | None = None
    # When set, the LLM translates this Cypher query to AQL instead of
    # answering ``question``. Powers the workbench "Generate AQL with AI"
    # fallback for Cypher the deterministic transpiler cannot handle.
    cypher: str | None = Field(default=None, max_length=_MAX_FIELD_LENGTH)


class CreateIndexRequest(BaseModel):
    """WP-S3c: one-click creation of an inverted index for fuzzy matching.

    Consumes the structured fields of an ``IndexAdvisory`` (collection + field)
    emitted by the NL entity resolver. The index spec is reconstructed
    server-side from these fields rather than accepting a free-form spec dict
    from the client, so a session cannot create an arbitrary index shape on its
    database through this endpoint.
    """

    collection: str = Field(..., max_length=_MAX_FIELD_LENGTH)
    field: str = Field(..., max_length=_MAX_FIELD_LENGTH)
    analyzer: str = Field(default="text_en", max_length=_MAX_FIELD_LENGTH)
    name: str | None = Field(default=None, max_length=_MAX_FIELD_LENGTH)


class OwlExportRequest(BaseModel):
    mapping: dict[str, Any]


class OwlImportRequest(BaseModel):
    turtle: str = Field(..., max_length=_MAX_TURTLE_LENGTH)


class CorrectionRequest(BaseModel):
    cypher: str = Field(..., max_length=_MAX_CYPHER_LENGTH)
    mapping: dict[str, Any] = Field(default_factory=dict)
    database: str = Field(default="", max_length=_MAX_FIELD_LENGTH)
    original_aql: str = Field(..., max_length=_MAX_AQL_LENGTH)
    corrected_aql: str = Field(..., max_length=_MAX_AQL_LENGTH)
    bind_vars: dict[str, Any] = Field(default_factory=dict)
    note: str = Field(default="", max_length=_MAX_NOTE_LENGTH)


class NLCorrectionRequest(BaseModel):
    question: str = Field(..., max_length=_MAX_NL_QUESTION_LENGTH)
    cypher: str = Field(..., max_length=_MAX_CYPHER_LENGTH)
    mapping: dict[str, Any] = Field(default_factory=dict)
    database: str = Field(default="", max_length=_MAX_FIELD_LENGTH)
    note: str = Field(default="", max_length=_MAX_NOTE_LENGTH)
