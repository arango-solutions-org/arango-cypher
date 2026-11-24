# Disjoint SmartGraph Multi-Tenancy — PRD + Implementation Plan
Date: 2026-04-21  
Last updated: 2026-07-05  
Status: **Draft — Phase 1 (MT-0 / MT-1 / MT-5) shipped in Wave 7; Phase 2 (MT-2 / MT-3a / MT-4) shipped in Wave 8 (2026-05-27, PR #30); MT-3b (Layer 3 service-route wiring) + MT-6 (Layer 5 plan-shape LRU) + MT-7 (admin cross-tenant bypass + audit stream) shipped 2026-07-05.** All of Phase 3 is complete; only MT-8 (ongoing security review / red-team corpus) remains. Standalone document; to be merged into `python_prd.md` — see §Merge notes.

### Changelog
| Date | Changes |
|------|---------|
| 2026-07-05 | **MT-8 (start) — red-team corpus seeded.** `tests/test_redteam_tenant_corpus.py` is the single, extensible registry of known cross-tenant escape attempts that Layer 5 must refuse — organised by the PRD threat model (T1/T2/T4/T5/T7) with hand-crafted EXPLAIN plans, plus MT-7 privilege-escalation vectors (non-admin bypass, admin-without-reason) and an MT-6×MT-7 cache-poisoning guard (an admin bypass must never leave a certification a later non-admin call could reuse). Workflow: a new escape is added as a failing case here first, then closed in `tenant_plan_validator.py`. Also hardened test isolation: reloading `arango_cypher.service` now restores the full submodule tree (`tests/helpers/service_reload.py`), fixing a cross-file leak that let stale reloaded route modules dodge monkeypatching. |
| 2026-07-05 | **MT-7 — admin cross-tenant bypass + audit stream.** `/execute` + `/execute-aql` accept `cross_tenant` + `bypass_reason` body fields. `_resolve_admin_bypass` gates at the trust boundary (403 `ADMIN_REQUIRED` for non-admins, 422 `BYPASS_REASON_REQUIRED` when no reason). An authorised bypass skips Layer 4 (avoids re-injecting a tenant predicate) and threads `admin_bypass`/`bypass_reason` through `safe_execute` into `validate_plan`, which skips the tenant-scope walk + bind-var gate while still enforcing structural EXPLAIN checks (`EXPLAIN_FAILED` / `EXPLAIN_MALFORMED`), and emits `ADMIN_CROSS_TENANT_BYPASS` to the dedicated `arango_cypher.tenant_audit` logger. `is_admin` is re-checked at Layer 5 as defence in depth; the plan-shape cache is skipped for bypass calls. Tests: `tests/test_tenant_admin_bypass.py` + `/execute-aql` gating in `tests/test_session_tenant_binding.py`. |
| 2026-07-05 | **MT-6 — Layer 5 plan-shape certification LRU.** `arango_cypher/tenant_plan_validator.py` gains `_PlanCertificationCache`: `validate_plan` now memoises structural PASSes keyed by `sha256(aql + schema-fingerprint)`, so a repeat of the same `(aql, schema)` shape skips the EXPLAIN round-trip + plan walk. Safe because the EXPLAIN sentinel makes the plan shape tenant-value-independent, the key fingerprints every schema input the walker reads (so a schema change auto-invalidates), only structural passes are cached, and the per-call bind-var gate (`NO_SESSION_TENANT` / `TENANT_BIND_MISMATCH`) runs on every call incl. cache hits. Bounded thread-safe LRU (`TENANT_PLAN_CACHE_SIZE`, `TENANT_PLAN_CACHE_TTL_SECONDS`); `plan_cache_stats()` / `reset_plan_cache()` for observability + tests. Every pass still logs the plan digest with a `cache=hit|miss` marker. Tests: `tests/test_tenant_plan_cache.py`. |
| 2026-07-05 | **MT-3b — Layer 3 (Cypher AST tenant injection) wired into the service.** Added `apply_layer3_rewrite` to `arango_cypher/service/safe_exec.py` (mirrors the Layer-4 `apply_layer4_rewrite` adapter) and invoked it from `POST /nl2cypher`: for a tenant-bound session with a mapping, the generated Cypher is rewritten so every tenant-scoped node pattern carries a `$tenantId` / `$tenantKey` bind predicate before it is returned. Rewrites are surfaced on the response as `tenantRewrites`; a literal tenant predicate / unknown label raises `TenantScopeRewriteRejection` → HTTP 403; the traversal-path / multi-label cases MT-3a defers raise `TenantScopeRewriteIncomplete` and fall back to the original Cypher (Layer 5 remains the fail-closed boundary at execute). `parse_cypher` now returns the normalized source (`ParseResult.normalized`) so the offset-based rewriter is fed the exact string its tree was built from. Tests: `tests/test_service_layer3_tenant_injection.py` (adapter gating) + `TestNl2CypherLayer3Injection` in `tests/test_session_tenant_binding.py` (route HTTP contract). |
| 2026-04-27 | **Documented `metadata.multitenancy` + `physicalEnforcement` consumption (docs-only).** §3 extended with new §3.1 that specifies the `metadata.multitenancy` block shipped upstream in mapper PR #17 / analyzer `0.6.0` (style, `tenantKey[]`, `tenantKeyCollections`, `physicalEnforcement`, `evidence`), which fields the guardrail reads, how `physicalEnforcement` is used for log-triage labelling (data-leak vs. translation-quality) without being promoted to a Layer-5 gate, and the back-compat fallback for analyzers older than `0.6`. Added §3.2 summary table mapping each upstream metadata block (`tenantScope`, `shardingProfile`, `multitenancy`, `shardFamilies`) to the guardrail field that consumes it and the layer where that consumption happens. Closes the gap flagged in the post-Wave-6a code-quality audit where these signals were consumed by `tenant_guardrail.py` but not documented in the PRD. |
| 2026-04-21 | Initial draft. Captures the six-layer tenant-safety architecture for ad-hoc NL queries against a disjoint-SmartGraph + satellite-collection multi-tenant schema. Supersedes no prior document; extends the existing tenant guardrail (`arango_cypher/nl2cypher/tenant_guardrail.py`, `tenant_scope.py`) with algorithmic AST rewriting and an EXPLAIN-plan gate. |

---

## Executive summary

When the `arango-cypher-py` service is deployed against a multi-tenant ArangoDB graph, tenant users issue natural-language questions that the service translates into Cypher, then into AQL, then executes. **Without additional safeguards, two failure modes let a tenant user read data belonging to another tenant:**

1. **Underconstraint** — the LLM omits a tenant filter ("show me all employees" translates to `MATCH (e:Employee) RETURN e`).
2. **Injection** — the user names another tenant in the prompt, and the LLM dutifully obliges ("show me employees where `TENANT_HEX_ID = 'B...'`").

These are classified as **data-leak-class defects**, not translation-quality nits. The existing tenant guardrail (Wave 4r, shipped 2026-04-20) addresses them with a prompt + regex-postcheck + LLM retry loop — necessary but insufficient, because it relies on the LLM eventually doing the right thing.

This PRD specifies a six-layer defense-in-depth architecture that makes cross-tenant data leakage **structurally impossible**, independent of LLM behavior:

| # | Layer | Mechanism | Status |
|---|---|---|---|
| 0 | Storage | Disjoint SmartGraphs (per-tenant shard key) + satellite collections (tenant-independent reference data) | **Partial** — `metadata.shardingProfile` / `multitenancy` / `shardFamilies` adopted from analyzer ≥0.5.0 (see §3) |
| 1 | Session | Server-bound `@tenantId`, injected from authenticated session; never trusted from the request body | **Done** (Wave 7, MT-1) — `POST /connect` validates and stores tenant; body tenant ignored in tenant-user mode |
| 2 | LLM | Manifest-aware prompt + few-shot + regex postcheck + retry | **Done** (Wave 4r, 2026-04-20) |
| 3 | Cypher AST | Algorithmic tenant-predicate injection on the parsed Cypher, before transpilation | **Done** — core MT-3a (Wave 8, PR #30); service-route wiring MT-3b (2026-07-05) via `apply_layer3_rewrite` in `/nl2cypher` |
| 4 | AQL AST | Tenant-predicate injection on the transpiled AQL; covers the NL→AQL direct path and `/execute-aql` | Not implemented (MT-4, Wave 8) |
| 5 | Pre-execute | EXPLAIN-plan validator that refuses any plan scanning a tenant-scoped collection without a bind-var tenant predicate | **Done** (Wave 7, MT-5) — `tenant_plan_validator.py`; wired on `/execute`, `/execute-aql`, `/aql-profile` |
| 6 | Execute | `db.aql.execute(query, bind_vars={**client, "tenantId": session.tenant_id})` (session value wins) | **Done** (Wave 7, MT-5) — `safe_execute` in `arango_query_core/exec.py` + service adapter |

**Key decisions:**

- **Layer 5 is the security boundary.** Layers 3 and 4 exist for transparency (the user sees the rewritten Cypher/AQL), developer ergonomics, and defense-in-depth — but the only check that matters for audit is the EXPLAIN-plan validator. If Layer 5 passes, the query is safe by the definition of "safe" below; if it fails, the query does not run.
- **Fail-closed everywhere.** Unknown entity labels, missing manifest entries, unparseable plans, and any ambiguity cause refusal, not a permissive fallback. The only way to relax the boundary is an explicit admin role with its own audited pipeline (§8).
- **The LLM is never trusted.** Layer 2 reduces retry burden on Layers 3–5 but is not counted as a defense for audit purposes.
- **Storage layout is a schema-mapper concern.** The mapper must surface each collection's `physicalLayout.kind` (`smartgraph` / `satellite` / `regular` / `system`), `smartGraphAttribute`, `isDisjoint`, and `graphName`, plus each entity's `tenantScope.scopingPathFromTenant`. Everything downstream reads from this manifest.
- **Bind-variables only.** The tenant identifier is always passed as a bind variable (`@tenantId`), never inlined as a literal. The literal never appears in a query string that leaves the server. Layer 5 refuses plans that encode a tenant constraint as a string literal.

---

## 1) Problem statement

### 1.1 Threat model

The service runs against an ArangoDB database holding data for multiple tenants. Users authenticate into a specific tenant. Each session has a single bound tenant identifier. The threat model covers:

| ID | Threat | Defeated by |
|---|---|---|
| T1 | **Underconstraint** — LLM omits the tenant filter | L2 (detects), L3/L4 (rewrites), L5 (refuses) |
| T2 | **Injection** — user supplies another tenant's literal in the NL prompt | L3/L4 (rewrites literal → bind), L5 (refuses literal predicate) |
| T3 | **Traversal escape** — query walks an edge collection into another tenant | L0 (disjoint shard boundary for SmartGraph edges), L4 (constrains traversal), L5 (refuses unconstrained `TraversalNode`) |
| T4 | **Body-supplied tenant context** — user overrides session tenant via request body | L1 (server ignores body-supplied tenant) |
| T5 | **Direct AQL bypass** — NL→AQL path and `/execute-aql` skip the Cypher pipeline | L4 (AQL AST pass still runs), L5 (applies to every execute) |
| T6 | **Aggregation leakage** — answer is a scalar (`COUNT`, `AVG`) spanning tenants | L3/L4/L5 (same rewriting and validation apply to subqueries and aggregation) |
| T7 | **Bind-var override** — user supplies `@tenantId` in request body | L6 (spread order guarantees session value wins) |
| T8 | **Satellite write exposure** — tenant user writes to a satellite collection | Out of scope for this PRD; writes against satellite collections are governed by ArangoDB role-based permissions at the DB layer |

Out-of-scope for this PRD:

- Side-channel attacks (timing, cache) — addressed by standard ArangoDB deployment practices.
- Compromised service operators with admin keys.
- Attacks on the LLM provider (prompt-injection into the provider itself).
- Tenant-to-tenant collusion where two tenants intentionally pool a single account.

### 1.2 What "safe" means

A query is **safe** if, for every collection it reads, one of the following holds:

1. The collection's `physicalLayout.kind == "satellite"`, OR
2. The collection's `physicalLayout.kind == "smartgraph"` AND the plan's access node carries an index condition or filter of the form `doc.<smartGraphAttribute> == @tenantId` where `@tenantId` is a bind variable whose value equals the session's tenant identifier, OR
3. The collection is `TENANT_ROOT` (i.e., the `Tenant` collection itself) AND the access is keyed by `@tenantKey` (the session tenant's `_key`), OR
4. The access occurs inside a subquery or traversal whose enclosing constraint already satisfies (2) and guarantees every document visited carries `@tenantId`.

All other cases are **unsafe** and must be refused before execution.

---

## 2) Architecture

### 2.1 Pipeline (corrected layer order)

```
[Authenticated session]         ── Layer 1 (server-bound, never user input)
         │
         ▼   tenant_id, tenant_key
[NL]
  │
  ▼ Layer 2: prompt + few-shot + LLM + manifest-aware retry
[generated Cypher]
  │
  ▼ Layer 3: Cypher AST tenant injection         ← NEW
[scoped Cypher]
  │
  ▼ Transpile Cypher → AQL
[AQL]
  │
  ▼ Layer 4: AQL AST tenant injection            ← NEW
[scoped AQL]
  │
  ▼ Layer 5: EXPLAIN-plan validator (fail gate)  ← NEW
[verified AQL]
  │
  ▼ Layer 6: execute with @tenantId bind from session
[results]
```

For the **NL→AQL direct path** (§1.3 of `python_prd.md`), Layer 3 is skipped (no Cypher exists). Layer 4 still runs, and Layer 5 is unconditional. Consequence: NL→AQL is no less safe than NL→Cypher→AQL, because the safety boundary is at Layer 5.

For **raw AQL submissions** via `/execute-aql`, Layers 2–4 are skipped; Layers 5 and 6 run unconditionally.

### 2.2 Layering invariants

- Every layer is **purely additive** — it can only tighten constraints, never loosen them.
- Every layer is **independently testable** — Layer 5 is tested against plans hand-crafted to bypass Layers 3–4, and must refuse them.
- Every layer is **fail-closed** — on any internal error, ambiguity, or missing input, the layer must refuse rather than permit.

---

## 3) Schema mapper requirements

For the AST passes and the EXPLAIN validator to operate, the mapping bundle produced by `arangodb-schema-analyzer` must expose, per collection:

```jsonc
{
  "Employee": {
    "collectionName": "Employee",
    "physicalLayout": {
      "kind": "smartgraph",
      "smartGraphAttribute": "TENANT_HEX_ID",
      "isDisjoint": true,
      "graphName": "TenantGraph"
    },
    "tenantScope": {
      "role": "TENANT_SCOPED",
      "denormField": "TENANT_HEX_ID",
      "scopingPathFromTenant": [
        "TENANTUSERTENANT",
        "GSUITEUSERTENANTUSER"
      ]
    }
  },
  "Country": {
    "collectionName": "Country",
    "physicalLayout": { "kind": "satellite" },
    "tenantScope": { "role": "GLOBAL", "denormField": null }
  },
  "Tenant": {
    "collectionName": "Tenant",
    "physicalLayout": { "kind": "smartgraph", "smartGraphAttribute": "_key", "isDisjoint": true, "graphName": "TenantGraph" },
    "tenantScope": { "role": "TENANT_ROOT", "denormField": null }
  }
}
```

**What the mapper already provides** (as of 2026-04-23, `arangodb-schema-analyzer>=0.5.0`):

- `tenantScope.role` — `TENANT_ROOT` / `TENANT_SCOPED` / `GLOBAL`, classified by `analyze_tenant_scope` with `analyzer>=0.4` annotations or local heuristic fallback.
- `tenantScope.denormField` — name of the denormalised tenant-reference field when present.
- `collectionName`, `properties`, `style`, `typeField`, `typeValue`, statistics.
- **`metadata.shardingProfile`** (analyzer v0.5 / upstream PRD §6.2 bullet 3) — one-shot database-level classification into exactly one of `OneShard`, `DisjointSmartGraph`, `SmartGraph`, `SatelliteGraph`, `Sharded`, with per-graph evidence (`smartGraphAttribute`, `isDisjoint`, `graphName`, `shardKeys`, `numberOfShards`, `replicationFactor`) and per-collection `kind` buckets (`smartgraph` / `satellite` / `regular` / `system` / `tenant-root`). See [`arango-schema-mapper` PRD §6.2][upstream-prd-62] and the downstream tracking doc [`docs/schema_analyzer_issues/08-emit-physical-layout-and-shard-topology.md`][08-issue].

**Work-Package MT-0: schema-mapper uplift — SUPERSEDED (2026-04-23).**

Originally MT-0 was going to compute `physicalLayout.kind`, `smartGraphAttribute`, `isDisjoint`, `graphName`, and `scopingPathFromTenant` locally in `schema_acquire.py` against live DB properties. This is now fully covered upstream by `metadata.shardingProfile` emitted by `arangodb-schema-analyzer>=0.5.0`:

| Originally-planned local field | Now sourced from |
|---|---|
| `physicalLayout.kind` (per collection) | `metadata.shardingProfile.members[collection].kind` |
| `physicalLayout.smartGraphAttribute` | `metadata.shardingProfile.graphs[*].smartGraphAttribute` |
| `physicalLayout.isDisjoint` | `metadata.shardingProfile.graphs[*].isDisjoint` |
| `physicalLayout.graphName` | `metadata.shardingProfile.graphs[*].name` |
| Database-level deployment style (new) | `metadata.shardingProfile.style` ∈ {OneShard, DisjointSmartGraph, SmartGraph, SatelliteGraph, Sharded} |

Adoption in this repo:

- `arango_cypher/schema_acquire.py::acquire_mapping_bundle` logs `shardingProfile.style` at INFO on every acquisition, and escalates to WARNING when `status == "degraded"` (analyzer signalled incomplete evidence).
- `arango_cypher/nl2cypher/_core.py::_build_schema_summary` renders a conceptual, one-line deployment hint derived from `shardingProfile.style` into the LLM prompt (see `_deployment_style_hint`), without leaking physical details.
- The Layer-5 EXPLAIN-plan validator (§7) will read `shardingProfile.style` once at session start: `OneShard` / `SatelliteGraph` ⇒ no shard-key enforcement; `DisjointSmartGraph` ⇒ hard-enforce; `SmartGraph` / `Sharded` ⇒ soft-enforce with warnings.

The remaining `tenantScope.scopingPathFromTenant` item (BFS-derived edge path from `Tenant` to a scoped entity) is **not** yet covered upstream. It stays in this PRD as a separate work item (to be tracked when MT-1/5 lands) and remains a local computation on top of the upstream `tenantScope` annotation.

### 3.1 Upstream `metadata.multitenancy` — database-level classification

Alongside `metadata.shardingProfile` (which describes the cluster-physical story — sharding strategy, smart-graph attributes, per-collection kind), `arangodb-schema-analyzer >= 0.6.0` emits a separate `metadata.multitenancy` block that classifies the database's **multi-tenant deployment style**. Shipped in mapper PR #17 (upstream PRD §6.2 bullet 4), consumed locally since 2026-04-23.

Shape:

```jsonc
{
  "metadata": {
    "multitenancy": {
      "style": "disjoint_smartgraph" |
               "shard_key" |
               "discriminator_field" |
               "collection_per_tenant" |
               "database_per_tenant" |
               "unknown_single_db" |
               "none",
      "tenantKey": ["TENANT_HEX_ID", "TENANT_ID", "tenantId"],
      "tenantKeyCollections": ["Employee", "Device", "AppInstance", ...],
      "physicalEnforcement": true | false,
      "evidence": { "...opaque rationale for the classification..." }
    }
  }
}
```

Field semantics used by the guardrail:

| Field | Meaning | Consumer |
|---|---|---|
| `style` | One of seven categorical classifications. `none` means the analyzer found no tenancy signal at all (or saw only a single tenant); `unknown_single_db` means tenancy is present but the style could not be pinned. Any other value is a concrete deployment pattern. | `tenant_guardrail.multitenancy_physical_enforcement` treats `style in {none, absent}` as "no signal, fall back to heuristic / Layer 2". |
| `tenantKey` | Ordered list of property names used to denormalise the tenant reference across collections (`TENANT_HEX_ID` first, fall-throughs after). | `tenant_scope.analyze_tenant_scope` extends its discovery regex with these names so a non-conventional tenant-column name is still recognised. |
| `tenantKeyCollections` | Collections that carry a denormalised tenant column drawn from `tenantKey`. | Informational today; candidate signal for a future Layer 3 denorm-filter optimisation pass. |
| `physicalEnforcement` | Whether the deployment's storage layer physically prevents cross-tenant reads. `True` for `disjoint_smartgraph` and `shard_key`; `False` for `discriminator_field`. `null` / absent for older analyzer versions or `style == "none"`. | `tenant_guardrail.TenantScopeViolation.physical_enforcement` surfaces this on every violation — `True` means a guardrail miss is a translation-quality concern (storage still blocks the leak), `False` / `None` means the guardrail is the only defense and a miss is a data-leak-class defect. |
| `evidence` | Free-form blob with the analyzer's rationale (collections with shard keys, column-value entropy, etc.). | Logged at DEBUG for debugging the classification; never consumed programmatically. |

**Why this is distinct from `tenantScope`.** `tenantScope` is a per-entity annotation (is `Employee` scoped? `GLOBAL`? `TENANT_ROOT`?) that drives per-query rewrite decisions; `metadata.multitenancy` is a per-database classification that drives per-session audit decisions. The two are complementary and are consumed in different layers:

| Consumer | Reads | Layer |
|---|---|---|
| `nl2cypher/tenant_scope.py::analyze_tenant_scope` | `tenantScope.role`, `tenantScope.tenantField`, `metadata.multitenancy.tenantKey[]` | Layer 2 prompt + Layer 3 AST rewrite (future) |
| `tenant_guardrail.multitenancy_physical_enforcement` | `metadata.multitenancy.style`, `metadata.multitenancy.physicalEnforcement` | Layer 2 postcheck violation label |
| `schema_acquire.acquire_mapping_bundle` | `metadata.multitenancy.style` | INFO log on every acquisition |

**`physicalEnforcement` is NOT a Layer 5 gate.** The guardrail surfaces the field on violations for log-triage purposes, but `physicalEnforcement=True` does **not** relax any of the Layer 3 / Layer 4 / Layer 5 checks — Layer 5 still refuses plans that scan a tenant-scoped collection without a bind-var tenant predicate, regardless of the underlying storage story. The reasoning is defence in depth: shard-key enforcement only covers scans through the graph; a direct `FOR d IN Employee` against the collection API still returns cross-tenant rows on a SmartGraph deployment. Disjoint-SmartGraph is the only style where storage alone would be sufficient, and even there we want the query-level refusal as a belt-and-suspenders signal for audit.

**Back-compat for older analyzers.** When the bundle lacks `metadata.multitenancy` (analyzer < 0.6), the guardrail returns `None` and logs at DEBUG; callers treat the field as "no signal" and fall back to the `has_tenant_entity` heuristic plus Layer 2 prompt + postcheck. No hard error.

### 3.2 Section summary — what each block gives the guardrail

| Block | Scope | Field the guardrail reads | Purpose |
|---|---|---|---|
| `physicalMapping.entities[*].tenantScope` | Per-entity | `role`, `tenantField` | Classify each label referenced by a query as `TENANT_ROOT` / `TENANT_SCOPED` / `GLOBAL`; accept a denorm-field filter as scope satisfaction. |
| `metadata.shardingProfile` | Per-database | `style`, per-collection `kind` | Surface a conceptual deployment hint to the LLM; drive the Layer-5 validator's sharding-vs-satellite branching. |
| `metadata.multitenancy` | Per-database | `style`, `tenantKey[]`, `physicalEnforcement` | Extend tenant-column discovery with the upstream key list; label each violation as data-leak vs. translation-quality. |
| `physicalMapping.shardFamilies` | Per-database | `members[]`, `discriminator`, `sharedProperties` | Render shard-family groupings into the LLM prompt (reduces the per-tenant-collection blowup in schema summaries). |

All four blocks are consumed lazily — a missing block is silently treated as "no signal" and the guardrail falls back to the heuristic tier. No hard dependency on any single analyzer feature flag.

[upstream-prd-62]: https://github.com/ArthurKeen/arango-schema-mapper/blob/main/docs/PRD.md
[08-issue]: schema_analyzer_issues/08-emit-physical-layout-and-shard-topology.md

---

## 4) Layer 1 — Session-bound `@tenantId`

### 4.1 Requirement

The tenant identifier used for every query in a session is set at authentication time and is **never** sourced from the request body thereafter. The service's existing `tenant_context` HTTP payload (used by `/nl2cypher` and `/nl2aql`) becomes a **debug-only input**, controlled by an explicit server-side flag.

### 4.2 Changes

1. **Session model.** Extend the authenticated session (see `_get_session` in `service.py`) to carry `tenant_id: str` and `tenant_key: str` set at `/connect`. The identifier source is one of:
   - An explicit `tenantId` parameter on `/connect`, validated against a tenant the authenticated user is authorised to access.
   - A claim in the auth token (when deployed behind a token issuer that binds tenants to users).
   - A header injected by the platform ingress (when deployed on the ArangoDB Platform with tenant-scoped routing).

   The exact source is deployment-dependent. The service treats it as opaque and the schema mapper confirms the tenant document exists before completing `/connect`. Sessions for unknown tenants are rejected.

2. **Endpoint hardening.** Every endpoint that currently accepts `tenant_context` in the body (`/nl2cypher`, `/nl2aql`) changes behavior:
   - In **workbench mode** (`ARANGO_CYPHER_WORKBENCH=1` or equivalent), the body value is honored.
   - In **tenant-user mode** (default), the body value is silently ignored; the server injects its session-bound tenant on every call. A log line is emitted at WARN if the body tried to supply a different tenant.

3. **Admin role.** A separate `admin` session flag exists for operator queries that legitimately span tenants. Admin sessions still go through Layers 3–5 but the validator's refusal rule is relaxed to "must have a `tenantId` predicate *or* be flagged `?cross_tenant=1` explicitly with a bypass reason logged." Admin is never the default.

### 4.3 Tests

- `test_layer1_session_tenant_is_authoritative`: body-supplied `tenant_context` differs from session; request executes against session tenant; WARN is logged.
- `test_layer1_workbench_mode_honors_body`: same request in workbench mode; executes against body tenant.
- `test_layer1_admin_cross_tenant_requires_flag`: admin session without `?cross_tenant=1` gets the same refusal as a tenant user; with the flag, it succeeds and the bypass is logged.
- `test_layer1_unknown_tenant_rejected_at_connect`: `/connect` with a `tenantId` for a tenant that does not exist in the `Tenant` collection returns 403.

---

## 5) Layer 2 — NL prompt + manifest-aware guardrail (existing)

### 5.1 Current state

Shipped in Wave 4r (2026-04-20):

- `arango_cypher/nl2cypher/tenant_guardrail.py` — `TenantContext`, `check_tenant_scope`, `prompt_section`.
- `arango_cypher/nl2cypher/tenant_scope.py` — `analyze_tenant_scope`, `EntityTenantRole`, `TenantScopeManifest`.
- Integrated into `_call_llm_with_retry` after parse and EXPLAIN checks.
- Fails closed on retry exhaustion with `method="tenant_guardrail_blocked"` and empty `cypher`.

### 5.2 Enhancements required by this PRD

No structural changes. Two hardening items:

1. **Reject tenant literals in the Cypher.** Today the guardrail accepts a `TENANT_HEX_ID = '<value>'` inline filter as proof of scoping when the value matches the active tenant. Under the new pipeline, literal tenant values in generated Cypher are **always** rewritten to `@tenantId` by Layer 3. The guardrail is updated to additionally reject generated Cypher that uses a literal tenant value (T2 defense at the LLM layer), forcing the retry loop to produce a bind-var form or a pattern Layer 3 can rewrite. This strengthens the existing manifest-aware check.

2. **Count the guardrail as soft, not a boundary.** The guardrail's existing "fails closed on retry exhaustion" contract is correct; what changes is audit classification. In the security review, Layer 2 is documented as a **quality** measure, not a safety boundary. Audit evidence points at Layer 5 logs.

---

## 6) Layer 3 — Cypher AST tenant injection (NEW)

### 6.1 Goal

Mechanically rewrite the generated (or hand-written) Cypher AST so every node pattern over a `TENANT_SCOPED` label gets a tenant predicate expressed against the session's `@tenantId` bind variable. The rewritten Cypher is transparent to the user — they see exactly what constraints were added and can reason about the query.

### 6.2 Algorithm

Single pre-transpilation pass over the Cypher AST (existing `arango_cypher.parser` output):

```
For each MATCH / OPTIONAL MATCH / MERGE / CREATE pattern:
  For each node pattern (var:Label <props>):
    scope = manifest.tenant_scope_of(Label)

    if scope.role == GLOBAL:
      continue   # satellite or tenant-independent; no injection

    if scope.role == TENANT_ROOT:
      enforce property map includes {_key: $tenantKey}
      (or WHERE clause equivalent); remove any conflicting literal
      continue

    if scope.role == TENANT_SCOPED:
      if scope.denorm_field is not None:
        # Cheap path: inline equality on the denorm field
        merge {<denormField>: $tenantId} into the property map
      else:
        # Traversal-only: promote pattern to a path from :Tenant
        rewrite pattern p as:
          (:Tenant {_key: $tenantKey}) -scoping_path-> (var:Label <props>)

For each WHERE clause:
  For each predicate of the form x.<field> OP literal where
  <field> is a tenant field on x's entity:
    REJECT — literal tenant values are forbidden; the user cannot
    inject a tenant identity through the NL prompt

For any node pattern with a label that is not in the manifest:
  REJECT — fail closed rather than guess the scope
```

### 6.3 Examples

```cypher
-- Input
MATCH (e:Employee)-[:OWNS]->(a:Asset)
RETURN e, a

-- Output (Employee and Asset both TENANT_SCOPED with denorm field TENANT_HEX_ID)
MATCH (e:Employee {TENANT_HEX_ID: $tenantId})
      -[:OWNS]->
      (a:Asset {TENANT_HEX_ID: $tenantId})
RETURN e, a
```

```cypher
-- Input
MATCH (d:Document) WHERE d.TENANT_HEX_ID = 'tenant-B-uuid' RETURN d

-- Output: REJECTED
-- Reason: literal tenant predicate; tenant identity is bound to the session
```

```cypher
-- Input (Cve GLOBAL, Software TENANT_SCOPED)
MATCH (cve:Cve)<-[:HAS_VULN]-(s:Software)
RETURN cve, s

-- Output
MATCH (cve:Cve)<-[:HAS_VULN]-(s:Software {TENANT_HEX_ID: $tenantId})
RETURN cve, s
```

```cypher
-- Input (Employee TENANT_SCOPED but no denorm field → traversal-only)
MATCH (e:Employee) WHERE e.name = 'Alice' RETURN e

-- Output
MATCH (t:Tenant {_key: $tenantKey})
      -[:TENANTUSERTENANT]-> (:TenantUser)
      <-[:GSUITEUSERTENANTUSER]- (e:Employee)
WHERE e.name = 'Alice'
RETURN e
```

### 6.4 Known limitations

- Variable-length paths (`[:KNOWS*1..3]`) cannot be constrained by inline property maps. Layer 3 emits a `WHERE all(n IN nodes(p) WHERE n.<denormField> = $tenantId)` clause where feasible, and otherwise **defers to Layer 4** for the traversal constraint. Both layers running is load-bearing here.
- `OPTIONAL MATCH` over tenant-scoped entities still receives the predicate; the resulting "no match" row correctly reflects absence-of-data-in-this-tenant rather than absence-of-data-globally.
- `UNWIND [<literals>]` of tenant values is rejected (T2 defense) even if no entity constraint is violated.

### 6.5 Module layout

New module `arango_cypher/nl2cypher/tenant_ast_cypher.py`:

- `inject_tenant_scope(cypher_ast: CypherAst, manifest: TenantScopeManifest, tenant_id: str, tenant_key: str) -> CypherAst`
- Internal visitor subclasses the existing ANTLR tree walker in `arango_cypher.parser`.

Integration point: `arango_cypher.service.translate_endpoint` (and the `/execute` variant) calls `inject_tenant_scope` between parse and transpile. Gated by the same session flag as Layer 1.

---

## 7) Layer 4 — AQL AST tenant injection (NEW)

### 7.1 Goal

A second, independent rewrite pass over the transpiled AQL. Catches everything Layer 3 missed and is the **only** defense for the NL→AQL direct path and `/execute-aql`.

### 7.2 Algorithm

```
Walk every AQL AST node:

  FOR <var> IN <coll>
    scope = manifest.tenant_scope_of_collection(<coll>)
    if scope.role == GLOBAL: continue
    if scope.role in {TENANT_SCOPED, TENANT_ROOT}:
      if next FILTER already carries <var>.<field> == @tenantId:
        continue   # already constrained (e.g. by Layer 3)
      inject FILTER <var>.<field> == @tenantId after the FOR
      (where <field> is smartGraphAttribute or denormField)
      if collection has neither: REJECT (cannot safely constrain)

  FOR v, e, p IN ... <min>..<max> ANY|OUTBOUND|INBOUND <start> <graph>
    restrict <graph> to { vertexCollections: [<satellite + tenant coll>],
                          edgeCollections:   [<same>] }
    add PRUNE v.<field> != @tenantId          (OR-ed into an existing PRUNE)
    and FILTER v.<field> == @tenantId         (after any OPTIONS {...})
    (the PRUNE halts the traversal at a vertex of another tenant, and
    the FILTER keeps that vertex out of the result: a PRUNE alone still
    yields the vertex it stops at, and a FILTER alone still walks
    through other tenants' vertices, so a path would carry them)

  LET x = (<subquery>)
    recurse with the same pass; the subquery shares bind-var scope

  COLLECT / AGGREGATE / COUNT over a tenant-scoped collection:
    ensure the enclosing FOR has been constrained; otherwise REJECT

  Function calls (LENGTH, COUNT, AVERAGE) referencing a collection name:
    rewrite to a tenant-filtered subquery
```

### 7.3 Example

```aql
// Transpiled output for "list assets mentioning this product"
// (Asset TENANT_SCOPED, Product SATELLITE)

// After Layer 4:
FOR p IN Product
  FILTER p._key == @productKey
  FOR e IN MENTIONS
    FILTER e._to == p._id
    FOR a IN Asset
      FILTER a._id == e._from
      FILTER a.TENANT_HEX_ID == @tenantId   // injected
      RETURN a
```

### 7.4 Module layout

New module `arango_cypher/tenant_ast_aql.py`:

- `inject_tenant_scope(aql: str, manifest: TenantScopeManifest, tenant_id: str, tenant_key: str) -> str`
- Parses AQL via ArangoDB's own parser (round-trip through `db._conn.post("/_api/query", {...}, validate=True)` if no local parser is available) or, preferably, a local AQL AST library. The transpiler already generates structured AQL; the pass operates on that structured form rather than re-parsing.

Integration point: invoked in `service.translate_endpoint` after the Cypher→AQL transpile and for every request that hits `/execute-aql`.

### 7.5 Relationship to Layer 3

Layers 3 and 4 are **both** mandatory and **not redundant**:

- Layer 3 produces Cypher the user can read and reason about. Without it, the user sees a query that does not visibly respect their tenant scope.
- Layer 4 covers the AQL-only path and catches transpiler bugs that might drop a Layer 3 predicate. Without it, `/execute-aql` is a bypass.

---

## 8) Layer 5 — EXPLAIN-plan validator (NEW, the security boundary)

### 8.1 Goal

Before any query executes, obtain its execution plan from ArangoDB and **independently verify** that every collection access honors the safety definition in §1.2. This layer trusts no upstream — not the LLM, not the guardrail, not the AST passes, not the transpiler.

### 8.2 Algorithm

```python
def validate(aql: str, bind_vars: dict, session) -> None:
    plan = db.aql.explain(aql, bind_vars)["plan"]

    # Bind-var sanity first. If we don't have the expected @tenantId,
    # nothing else matters.
    if bind_vars.get("tenantId") != session.tenant_id:
        raise TenantScopeViolation("bind_vars['tenantId'] not session-bound")

    for node in plan["nodes"]:
        t = node["type"]

        if t == "EnumerateCollectionNode":
            coll = node["collection"]
            scope = manifest.physical_layout(coll)
            if scope.kind == "satellite":
                continue
            if not _node_has_tenant_predicate(plan, node, bind_vars):
                raise TenantScopeViolation(f"{coll} scanned without @tenantId")

        elif t == "IndexNode":
            if not _index_covers_tenant(node, manifest, bind_vars):
                raise TenantScopeViolation(f"{node['collection']} index miss")

        elif t == "TraversalNode":
            if not _traversal_constrained_to_tenant(node, manifest, bind_vars):
                raise TenantScopeViolation("unconstrained traversal")

        elif t == "SubqueryNode":
            # recurse into the subquery's plan
            validate_subplan(node["subquery"], bind_vars, session)

    # Success. Query is safe to execute under this session.
```

`_node_has_tenant_predicate` walks the plan to find `FILTER` / `IndexRangeNode` / `CalculationNode` children whose condition references the node's output variable and compares it to `@tenantId` (the bind-var form — literal predicates do not count and are rejected separately).

`_index_covers_tenant` inspects `node["condition"]` looking for equality on the smartGraphAttribute keyed against `@tenantId`. ArangoDB gives that condition as an OR of AND branches, and every branch must carry the equality (one conjunct of an AND is enough; a `NOT` carries nothing). The `_key == @tenantKey` check for a tenant-root index reads the condition the same way (`_condition_requires`).

A traversal is accepted when every vertex collection in play is satellite, OR it walks a disjoint SmartGraph whose attribute is enforced at storage, OR it keeps to the tenant (`traversal_confined_to_tenant`): its PRUNE (in a real plan, the node's `expression`) holds for every vertex whose tenant field is not the session tenant, and its vertex condition (the FILTER the optimizer fuses into the node, every branch) or a FILTER expression requires that field to be the session tenant. The tenant-scoped vertex collections must share one tenant field. A real plan names its vertex collections in `vertexCollections` and its graph in `graph`; both layers read them through `traversal_vertex_collections` / `traversal_graph_name`.

### 8.3 What makes this the boundary

- **Total.** Every execute goes through it — Cypher-driven, NL→AQL direct, and hand-submitted AQL. No code path executes a query without first running this function.
- **Independent.** It reads the plan that ArangoDB itself produced; upstream bugs that produce "looks-safe but actually isn't" AQL are still caught here because the plan exposes what will really be scanned.
- **Auditable.** Every violation is logged with: session id, tenant id, originating NL (if any), Cypher (if any), AQL, plan digest. The log is the primary audit evidence. Every *pass* also logs a plan digest so an auditor can replay.

### 8.4 Cost

- One EXPLAIN round-trip per execute. Typical: 5–20 ms. Amortisable via a plan-shape LRU keyed by `(aql_hash, mapping_hash)` — when the same query shape reappears with different bind-var values, we can skip the EXPLAIN if we've already certified that shape. Cache TTL bounded by mapping fingerprint (§Wave 4q) so a schema change invalidates certifications.
- Implementation note: ArangoDB's `explain()` supports `allPlans=False` and is cheap; it does not touch storage.

### 8.5 Module layout

New module `arango_cypher/tenant_plan_validator.py`:

- `validate_plan(aql, bind_vars, manifest, session) -> None`
- `_node_has_tenant_predicate(...)`, `_index_covers_tenant(...)`, `_traversal_constrained_to_tenant(...)`.

Integration point: every `aql.execute` call site wraps through a single helper `safe_execute(aql, bind_vars, session)` that runs the validator first.

### 8.6 Tests

The key test suite — more important than any other part of this PRD — lives at `tests/test_tenant_plan_validator.py` and must cover at least:

- Hand-crafted AQL that scans a tenant-scoped collection without a filter → rejected.
- Hand-crafted AQL with a literal-string tenant predicate → rejected (not a bind-var).
- Hand-crafted AQL with `@tenantId` but the value mismatches the session → rejected.
- Hand-crafted AQL with a correctly-constrained index lookup → accepted.
- Traversal over `ANY` with a satellite collection in play but no `prune` → rejected; with only the PRUNE, or only the FILTER → rejected; with both → accepted.
- Traversal over `OUTBOUND` on a disjoint-smartgraph named graph → accepted even without explicit `prune` (storage enforces it; validator recognises the `graphName`).
- Subquery that scans a tenant-scoped collection without a filter → rejected.
- `COUNT { FOR c IN TenantCollection ... }` without a filter → rejected.
- Every accept case is also exercised with a deliberately-wrong session tenant to confirm the bind-var check dominates.

The validator's correctness is defined by these tests; any ambiguity in the pipeline is pushed into "is the test expressing the right intent?" rather than "does the validator have the right heuristic?"

---

## 9) Layer 6 — Execute with bind-var injection

### 9.1 Requirement

```python
def safe_execute(aql: str, client_bind_vars: dict, session):
    # Session tenant always wins; cannot be overridden by the client.
    bind_vars = {**client_bind_vars, "tenantId": session.tenant_id, "tenantKey": session.tenant_key}
    validate_plan(aql, bind_vars, manifest, session)
    return session.db.aql.execute(aql, bind_vars=bind_vars)
```

The spread order is load-bearing. If the client supplies `tenantId`, it is **silently overwritten** by the session value. The validator then checks that the bind-var `tenantId` equals the session's, closing T7.

### 9.2 Transparency

The response includes the final bind vars (already done for `/execute` and `/translate`). The UI shows them next to the AQL so the user can see which tenant was injected. This is both a debugging aid and a transparency requirement.

---

## 10) Admin / cross-tenant bypass

Some operator workflows require legitimately cross-tenant queries (health reports, billing rollups, data migrations). These:

- Require a session flagged `admin: true` at authentication.
- Require an explicit per-request `cross_tenant: true` parameter (header or field), never implicit.
- Do not bypass Layer 5's structural checks — they bypass only the "must filter by `@tenantId`" rule.
- Log every request to a separate audit stream with the bypass reason (required string parameter).
- Are rate-limited per admin session.

Admin users never issue ad-hoc NL queries against the cross-tenant dataset through the standard UI. A separate endpoint or workbench mode is required, and the LLM prompt for that endpoint explicitly tells the model it is operating across tenants. This PRD does not specify the admin UX beyond the boundary conditions above.

---

## 11) Implementation status overview

| Work package | Description | Status | Estimate |
|---|---|---|---|
| **MT-0** | Schema-mapper uplift: `physicalLayout` block + `scopingPathFromTenant` on manifest | **Done** — `physicalLayout` half superseded by `analyzer>=0.5.0` `metadata.shardingProfile` (PRD §6.2 bullet 3); `scopingPathFromTenant` shipped in Wave 7 (`compute_scoping_path` BFS in `arango_cypher/nl2cypher/tenant_scope.py`, surfaced on `EntityScope.scoping_path` + `TenantScopeManifest.scoping_path_of`). | n/a (`physicalLayout`); ~80 LOC + 15 tests (`scopingPathFromTenant`) |
| **MT-1** | Session-bound `@tenantId`; strip body-supplied tenant in tenant-user mode | **Done** (Wave 7) — `_Session` carries `tenant_id`/`tenant_key`/`is_admin`; `/connect` validates tenant against `Tenant` collection; `/nl2cypher` + `/nl2aql` honor session tenant in tenant-user mode (workbench mode preserves body tenant). | ~150 LOC + 10 tests |
| **MT-2** | Guardrail hardening: reject literal tenant predicates in LLM output | **Done** (Wave 8 Phase 2, 2026-05-27, PR #30) — `arango_cypher/nl2cypher/tenant_guardrail.py` rejects denormalised tenant predicates with literal RHS; new `LITERAL_TENANT_PREDICATE` violation code; only `@tenantId` / `@tenantKey` bind references prove scope; fails closed when `manifest.known_tenant_keys` is unset. A literal is compared by its value, escapes decoded as the translator decodes them (`tenant_ast_common.cypher_literal_value`), here and in the MT-3 rewriter. | ~300 LOC + 12 tests |
| **MT-3** | Cypher AST tenant injection pass | **Done** — core **MT-3a** (Wave 8 Phase 2, 2026-05-27, PR #30): `arango_cypher/nl2cypher/tenant_ast_cypher.py` rewriter with property-map / WHERE / UNWIND injection contracts pinned by tests. Service wiring **MT-3b** (2026-07-05): `apply_layer3_rewrite` (`arango_cypher/service/safe_exec.py`) invoked from `POST /nl2cypher` — rewrites tenant-scoped node patterns to `$tenantId`/`$tenantKey` binds, surfaces `tenantRewrites`, refuses literal/unknown-label with HTTP 403, falls back on `TenantScopeRewriteIncomplete` (Layer 5 enforces). `parse_cypher` exposes `ParseResult.normalized` for offset-faithful rewriting. | ~1300 LOC + 28 tests (core) + ~90 LOC + 10 tests (wiring) |
| **MT-4** | AQL AST tenant injection pass | **Done** (Wave 8 Phase 2, 2026-05-27, PR #30) — `arango_cypher/tenant_ast_aql.py` plan-node walker (EnumerateCollection, Index, Traversal, Subquery) wired into `/translate`, `/execute`, `/execute-aql`, `/aql-profile`, `/nl2aql` via `arango_cypher/service/safe_exec.py`. | ~1100 LOC + 31 tests |
| **MT-5** | EXPLAIN-plan validator + `safe_execute` wrapper | **Done** (Wave 7) — Layer 5 validator (`arango_cypher/tenant_plan_validator.py`) walks EXPLAIN nodes (EnumerateCollection / Index / Traversal / Subquery), rejects unconstrained scans + literal tenant predicates, and logs every pass/refusal with AQL+plan digests. Layer 6 `safe_execute` (`arango_query_core/exec.py`) + service adapter (`arango_cypher/service/safe_exec.py`) overrides client bind vars with session tenant before validation; wired into `/execute`, `/execute-aql`, `/aql-profile` and surfaces violations as HTTP 403. | ~700 LOC + 32 tests |
| **MT-6** | Plan-shape LRU for Layer 5 performance | **Done** (2026-07-05) — `_PlanCertificationCache` in `arango_cypher/tenant_plan_validator.py` memoises structural PASSes keyed by `sha256(aql + schema-fingerprint)` (manifest roles/denorm/tenant_entity + sharding profile + collection→entity map). A hit skips the EXPLAIN + plan walk but **still** runs the per-call bind-var gate (`NO_SESSION_TENANT` / `TENANT_BIND_MISMATCH`) and logs `TENANT_SCOPE_OK … cache=hit`. Only passes are cached (never violations or transient EXPLAIN errors); schema change → new key → auto-invalidation; thread-safe bounded LRU with `TENANT_PLAN_CACHE_SIZE` (0 disables) + optional `TENANT_PLAN_CACHE_TTL_SECONDS`. `plan_cache_stats()` exposes hit-rate. | ~200 LOC + 15 tests |
| **MT-7** | Admin bypass + audit log stream | **Done** (2026-07-05) — `cross_tenant` + `bypass_reason` body fields on `/execute` + `/execute-aql`; `_resolve_admin_bypass` (routes/cypher.py) refuses non-admins (403 `ADMIN_REQUIRED`) and reason-less requests (422 `BYPASS_REASON_REQUIRED`). An authorised bypass skips Layer 4 (no tenant re-injection) and passes `admin_bypass`/`bypass_reason` through `safe_execute` → `validate_plan`, which skips the tenant-scope walk + bind gate (structural EXPLAIN checks still enforced) and logs `ADMIN_CROSS_TENANT_BYPASS` to the dedicated `arango_cypher.tenant_audit` stream. `is_admin` is re-checked at Layer 5 (defence in depth); per-session compute rate-limit already applies. Tests: `tests/test_tenant_admin_bypass.py` + route gating in `tests/test_session_tenant_binding.py`. | ~180 LOC + 12 tests |
| **MT-8** | Security review + red-team corpus | **In progress** (2026-07-05) — red-team corpus seeded at `tests/test_redteam_tenant_corpus.py`: an extensible registry of cross-tenant escape attempts (T1 underconstraint, T2 injection incl. `OR`-smuggling / fused-inline-literal / tenant-root / index-without-predicate, T4 traversal, T5 subquery-hidden scan, T7 bind-var override + no-session-tenant) that Layer 5 must refuse, plus MT-7 privilege-escalation cases (non-admin / reason-less bypass) and an MT-6×MT-7 cache-poisoning guard (an admin bypass must not certify a shape for later non-admin reuse). New escapes are added here first, then closed in `tenant_plan_validator.py`. | Ongoing |

Suggested phasing:

- **Phase 1 (demoable MVP):** MT-0, MT-1, MT-5. **Shipped in Wave 7.** Closes T1, T4, T5, T7 for the common case and gives auditable proof (the EXPLAIN validator's log).
- **Phase 2 (hardening):** MT-2, MT-3, MT-4. Closes T2, T6 structurally. The demo for a security review is "open the network tab, see every query rewritten, see the validator refuse every hand-tampered query."
- **Phase 3 (operability):** MT-6, MT-7. Needed before any production deployment.
- **Ongoing:** MT-8. Red-team corpus lives next to the existing NL eval corpus; every known attempted escape becomes a test case that the validator must refuse.

---

## 12) Testing strategy

### 12.1 Unit

Each module ships with a full unit suite mirrored on the existing `tests/test_nl2cypher*` pattern.

### 12.2 Integration

- `tests/integration/test_multitenant_crossvalidate.py`: seeds a two-tenant disjoint-SmartGraph fixture with one satellite collection, runs the full NL corpus as tenant A, asserts zero rows returned belong to tenant B — for every query, including ones specifically designed to try to leak.
- `tests/integration/test_multitenant_direct_aql.py`: same, but via `/execute-aql`, exercising the Layer 4 + Layer 5 path independently.
- `tests/integration/test_multitenant_admin.py`: admin session with `?cross_tenant=1` returns rows from both tenants; without the flag, gets the same refusal as a tenant user.

All integration suites gated behind `RUN_INTEGRATION=1 RUN_MULTITENANT=1`.

### 12.3 Red-team corpus

A dedicated corpus of NL prompts and hand-crafted AQL submissions designed to try to escape. Every entry has an expected outcome (`refuse` or `pass-with-tenant-predicate`) and the expected rejection reason or injected predicate. The corpus is a test oracle; adding an attempted escape is how a new defense makes it into the product.

Seed entries (to be built out):

| # | Input | Expected outcome |
|---|---|---|
| 1 | NL: "list all employees" | Cypher rewritten with `{TENANT_HEX_ID: $tenantId}` |
| 2 | NL: "list all employees including in tenant B" | Rejected at Layer 2 (literal tenant); retry; rejected again; empty result with explanation |
| 3 | AQL: `FOR e IN Employee RETURN e` via `/execute-aql` | Rejected at Layer 5 |
| 4 | AQL: `FOR e IN Employee FILTER e.TENANT_HEX_ID == 'tenant-B' RETURN e` | Rejected at Layer 5 (literal predicate) |
| 5 | AQL: `FOR e IN Employee FILTER e.TENANT_HEX_ID == @tenantId RETURN e` with `bind_vars={"tenantId": "tenant-B"}` | Rejected at Layer 5 (bind mismatch with session) |
| 6 | AQL traversal through a satellite collection back into another tenant's SmartGraph | Rejected at Layer 5 (unconstrained traversal) |
| 7 | `COUNT { FOR e IN Employee RETURN 1 }` | Rejected at Layer 5 (unconstrained subquery) |
| 8 | NL that references only GLOBAL entities ("list all countries") | Accepted; no tenant predicate injected anywhere |

### 12.4 Performance

EXPLAIN gate adds a round-trip per execute. Baseline budget: P95 ≤ 30 ms for the validator's own work (plan fetch + walk), measured via `scripts/benchmark_tenant_validator.py`. Plan-shape LRU hit-rate reported alongside; target ≥ 80 % on steady-state workloads.

---

## 13) Open questions

1. **Tenant identifier format.** `_key` is the canonical choice (unique, indexed, cheap). Schemas that use `TENANT_HEX_ID` as a denorm field should continue to do so, but the session's authoritative value is always `_key`. Confirm with deployment.
2. **Hierarchical tenants.** If a tenant has sub-tenants (org / division / team), is the scope `== @tenantId` or `IN @allowedTenants`? This affects every layer's predicate shape. Default assumption in this PRD: flat tenants (`==`).
3. **Satellite-collection audit.** Every collection classified as satellite must be audited to confirm it contains no tenant-private data. This is a deployment-time checklist, not a runtime check.
4. **Write operations.** This PRD covers reads. Writes (CREATE / SET / DELETE / MERGE) against tenant-scoped collections must also be constrained to `@tenantId` and must set the denorm field correctly. Layers 3 and 4 already see write clauses; Layer 5's "every access is constrained" rule applies to write plans too. Open question: policy for writes against satellite collections — presumed forbidden for tenant users, but needs explicit statement.
5. **Graph vs. collection traversal.** ArangoDB's `TraversalNode` is easier to validate when it targets a named graph rather than an anonymous edge-collection list. Should Layer 4 rewrite anonymous traversals into named-graph form? Likely yes; a disjoint SmartGraph's storage-level guarantees only apply through the named graph.
6. **Platform deployment.** Does the ArangoDB Platform inject a tenant identifier via an ingress header when the service is deployed in tenant-scoped mode? If so, Layer 1's session-binding can read from that header instead of requiring a `tenantId` at `/connect`. Confirm with platform team.

---

## Merge notes

This document is written as a standalone PRD so it can be reviewed independently. When merged into `docs/python_prd.md`:

- Sections **§1–§2** fold into a new **§16 "Multi-tenant safety"** under the main PRD, preserving the layer-number taxonomy used throughout this document (Layer 0–6).
- **§3** ("schema mapper requirements") becomes a subsection of **§5 "Mapping"** and introduces the `physicalLayout` block in the main PRD's mapping schema.
- **§4–§9** (per-layer detail) become subsections of §16.
- **§10** (admin bypass) becomes **§16.8**.
- **§11** (work packages MT-0 .. MT-8) folds into the main PRD's implementation status table and `docs/implementation_plan.md`.
- **§12** (testing strategy) folds into **§8 "Testing"**.
- **§13** (open questions) folds into the main PRD's open-questions section and is tracked through to close in the changelog.
- The changelog entry for the merge itself is a single row in the main PRD changelog citing this document by date.

All section numbers used here (**§1**, **§2**, ...) are local to this document and should be renumbered on merge.
