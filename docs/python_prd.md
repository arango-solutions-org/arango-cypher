# Arango Cypher (Python) — PRD + Implementation Plan
Date: 2026-02-17  
Last updated: 2026-05-11  
Workspace: `arango-cypher-py`  
Related repos:
- `~/code/arango-cypher-foxx` (Foxx/JS implementation; renamed from `arango-cypher` on 2026-04-17 — see §11 naming resolution)
- `~/code/arango-schema-analyzer` (package `arangodb-schema-analyzer`, schema detection + mapping; formerly repo-named `arango-schema-mapper`)

### Changelog
| Date | Changes |
|------|---------|
| 2026-06-23 | **One-click inverted-index creation from NL advisories (WP-S3c).** End-to-end wiring of the ArangoSearch advisory the entity resolver records when a fuzzy name probe falls back to a full scan. `NL2CypherResult` gained `advisories: list[dict]`; `nl_to_cypher` reads `resolver.advisories` post-resolution (even if `resolve()` raised — partial probes can still flag a missing index) and attaches them to the LLM-path result. `POST /nl2cypher` now returns `advisories` (`getattr`-guarded for mock/non-LLM results). New session-authenticated `POST /schema/index/create` reconstructs the inverted-index spec **server-side** from validated `collection`/`field`/`analyzer` (no free-form spec from the client), is idempotent (`created:false` when an inverted index already covers the field), 404s on a missing collection and 400s on a bad name. UI: `client.ts` gains the `IndexAdvisory` type + `createIndex()`; `App.tsx` renders an amber "Slow fuzzy match detected" strip below the NL input with a per-advisory "Create index" button (creating/created/error inline states), mirroring `SchemaWarningBanner`. Tests: `tests/test_service_index_create.py` (create/idempotent/404/400/auth), advisory pass-through in `tests/test_service_nl.py`, pipeline threading in `tests/test_nl2cypher.py`. Closes the last item carved out of WP-S3. |
| 2026-06-23 | **Fix: 401 storm on same-cluster database switch (UI).** Switching databases produced `HTTP 401` on `/tenants/discover` and `/graphs` and could even tear down the freshly-minted session. Root cause was a client token-lifecycle race, not a backend auth bug: `CONNECT_START` kept the old (already server-invalidated) token while the `(token, database)`-keyed effects re-fired against the new DB. Fixes (all `ui/src`): `store.ts` `CONNECT_START` now nulls the token so DB-keyed effects bail (`if (!token) return`) until `CONNECT_SUCCESS`; the tenant + graph effects only act on a 401 when the run is still current (`!cancelled`), so a stale 401 can't disconnect the new session; `ConnectionDialog.handleSwitchDb` reuses the live `connection` credentials (not the possibly-empty form password) and connects before tearing down the old session. Reducer regression tests in `ui/src/api/store.test.ts`. (The separate "Schema not ready" banner is expected for a DB not yet in `configs/catalog.yml`; with the 401 fixed, its "analyze now" works.) |
| 2026-06-23 | **Label predicates on untyped variables (WP-S2c).** `MATCH (risk) … WHERE risk:RISK_FACTOR [OR risk:EVENT]` (corpus q10/q11/q20) previously dropped the `:Label` suffix in expression position, collapsing the predicate to a no-op that returned every row. `_compile_expression`'s `OC_PropertyOrLabelsExpression` branch now routes the pure-variable + labels case to a new `_compile_label_predicate(var, labels_ctx, bind_vars)`, which reads the active mapping via the existing `_active_resolver` ContextVar (no recursive-compiler parameter-threading refactor) and emits, per label, `var[@typeField] == @typeValue` for LABEL/GENERIC_WITH_TYPE or `IS_SAME_COLLECTION(@coll, var)` for COLLECTION. Multiple labels (`n:A:B`) AND together; `OR` composes through the normal boolean compiler; unknown labels raise `MAPPING_NOT_FOUND` rather than silently passing. Tests: `tests/test_translate_label_predicate.py` + corpus guard `test_label_predicate_on_untyped_var_emits_filter`. Closes the last semantic gap carved out of WP-S2. |
| 2026-06-23 | **Fuzzy/text `arango.*` compilers + ArangoSearch index advisory (WP-S3).** **(S3a)** Extended the *existing* registry-gated extension framework (`arango_cypher/extensions/search.py`, beside `arango.bm25`/`tfidf`/`analyzer`) with a fuzzy/text family — `arango.like`, `starts_with`, `in_range`, `levenshtein_distance`, `levenshtein_match`, `ngram_match`, `ngram_similarity`, `phrase`, `boost`, `min_match`, `tokens`, `soundex`, `regex_test`/`matches`/`replace` → identically-signatured AQL functions, arity-checked, via a shared `_passthrough` factory. Honors `ExtensionPolicy` gating (no registry → `EXTENSIONS_DISABLED`, allowlist/denylist respected) — *not* a parallel code path (an earlier draft that bypassed the registry was reverted after it broke the `EXTENSIONS_DISABLED` contract). `TestFuzzyTextFunctions` in `tests/test_extensions.py`. **(S3b)** `EntityResolver` now emits a structured, deduped `IndexAdvisory(collection, field)` whenever a fuzzy name probe scans a field lacking inverted/ArangoSearch coverage; `suggested_inverted_index()` returns a ready `add_index` spec (`inverted` + `text_en`) and `as_dict()` is UI-ready, exposed via `resolver.advisories` — mirroring the missing-VCI advisory so the UI can offer one-click index creation. `IndexAdvisory` exported from `arango_cypher.nl2cypher`; `TestArangoSearchAdvisory` in `tests/test_nl2cypher_entity_resolution.py`. **Carved out → WP-S3c:** the create-index endpoint + one-click UI affordance (the advisory + spec it needs now exist). |
| 2026-06-23 | **NL→Cypher value-shape signals + symbol/ticker resolution (WP-S2).** Two fixes so the LLM stops inventing legal names for token-shaped fields (the CINF defect). **(S2a) valueShape in the schema card.** `_property_quality_hint` now renders `shape: <…>` and `e.g. "…"` from analyzer property metadata (`valueShape`/`examples`/`exampleValues`/`sampleValues`), and `_build_schema_summary` appends a "Value-shape hints" block — only when such metadata is present, preserving byte-identity for shape-less mappings — instructing the model to match the real value form (`WHERE o.name = "cinf"`, not the full legal name) and to prefer case-insensitive/`CONTAINS` over brittle equality. **(S2b) symbol/ticker matching.** `EntityResolver.extract_candidates` captures a parenthesized symbol ("Cincinnati Financial **(CINF)**") *first* so an exact symbol hit outranks a fuzzy long-name hit, and the probe property set gained `ticker`/`symbol`/`code`/`id`. Tests: `TestValueShapeHints` (`tests/test_nl2cypher.py`), `TestExtractCandidates`/`TestIdentifierPropertyCandidates` (`tests/test_nl2cypher_entity_resolution.py`). **Carved out → WP-S2c:** label predicates on *untyped* variables (`WHERE risk:RISK_FACTOR` over `MATCH (risk)`) still compile to a no-op (`core.py` ~line 3956); fixing requires threading a var→{collection,typeField} resolver into the recursive expression compiler — deferred as a scoped follow-up to avoid a risky refactor (affects q10/q11/q20 semantics only; they already transpile). See `docs/cypher_coverage_plan.md`. |
| 2026-06-23 | **NL→Cypher "return a graph" intent (WP-S1).** A question that explicitly asks to *see a graph* ("Show me, **as a graph**, …", "visualize …", "draw the network of …", "graph of …") was being answered with a scalar projection (`RETURN x.name`) instead of a renderable subgraph. New `_detect_graph_intent` (conservative regex, avoids false positives like "how many graphs are there") sets `PromptBuilder.graph_intent`, which appends an **"Output shape: return a graph"** instruction + inline `MATCH p = (a)-[:REL]->(b) RETURN p` example to the system prompt — but only when the intent fires, so the cacheable prompt prefix is unchanged for everything else. Threaded through `_call_llm_with_retry` → `nl_to_cypher`. New `TestGraphIntentDetection` in `tests/test_nl2cypher.py` covers the detector (9 positives / 7 negatives), builder rendering, and a recording-provider integration that asserts the section reaches (and is omitted from) the real system prompt. The zero-shot `_SYSTEM_PROMPT` byte-identity invariant (`tests/test_nl2cypher_prompt_builder.py`) is preserved. |
| 2026-06-23 | **Cypher transpiler coverage → 22/22 on the FinReflectKG benchmark (WP-C1…C3).** Closed all three transpiler gaps from the coverage baseline. **WP-C1 — scalar fn aliases:** `upper`/`lower` accepted as aliases of `toUpper`/`toLower` → AQL `UPPER`/`LOWER` (`core.py`; promotes q03/q04/q06). **WP-C2 — list subscript & slice:** `x[i]` compiles to native AQL array access (negative indices supported); half-open slices `x[i..j]`/`[i..]`/`[..j]`/`[..]` → `SLICE(array, start[, length])` (`core.py`). This also closed the group-by-computed-key case (`labels(n)[0]`), so **WP-C4 was subsumed** — promotes q01. **WP-C3 — `collect()` aggregation:** new `_parse_collect` handles `collect(DISTINCT x)` (→ `UNIQUE`), a trailing slice `collect(x)[0..5]` (→ `SLICE`), and — crucially — lets `collect()` **coexist with `AGGREGATE` aggregates** (`count`/`sum`/…) in a single `COLLECT … AGGREGATE … INTO tmp = expr` + post-`LET`, in both `WITH` and `RETURN` (`core.py`; promotes q05/q09/q10). New goldens: `tests/test_translate_scalar_fn_aliases.py`, `tests/test_translate_list_operators.py`, `tests/test_translate_collect_aggregation.py`; corpus ratio test pinned at 22/22. **Note:** transpile-success ≠ full semantic correctness — label predicates on *untyped* variables (`WHERE risk:RISK_FACTOR` over `MATCH (risk)`, q10/q11/q20) still compile to a no-op and are tracked for the WP-S phase in `docs/cypher_coverage_plan.md`. Full unit suite green (pre-existing `test_session_tenant_binding` ordering failures and the network-gated Anthropic live smoke excepted). |
| 2026-06-23 | **NL literal-return guardrail, fresh per-scope NL samples, and a measured Cypher-coverage baseline + plan.** **(1) NL-side literal guardrail.** A question that only asks to echo a constant (`return "hello"`, `return 42`, `return true`, optionally `… as alias`) was being turned into a graph lookup by the LLM. `nl_to_cypher` now short-circuits these **before** the LLM/mapping path via `_detect_literal_return` (regex over string/number/boolean/null literals, tolerant of `please/just/…` prefixes and trailing punctuation), returning a bare `RETURN <literal>` with `method="literal_return"`, confidence 1.0 — which the transpiler's standalone-`RETURN` path renders verbatim (`RETURN "hello"`). Needs no schema and no network. New `TestLiteralReturnGuardrail` (21 cases) in `tests/test_nl2cypher.py`. **(2) UI sample queries regenerate on DB/graph switch.** The auto-generated NL suggestions were keyed only by `url||database`, gated "once seen" in `localStorage`, and **appended** to the shared `nl_history` — so switching database (or named-graph scope) never refreshed them and stale suggestions from the previous DB lingered forever. They now live in their own `nlSamples` state, keyed by `url||database||graphScope` via a per-session ref, and **replace** (never accumulate); a disconnect clears them. The Ask dropdown shows the user's typed history first, then the current scope's fresh samples, de-duplicated (`nlSuggestions`). Removed the obsolete `nl_samples_seen` machinery (`ui/src/App.tsx`). **(3) Cypher coverage — measured baseline + plan.** Ported a customer's 22-query NL/Cypher/AQL benchmark (anonymized, against the public FinReflectKG LPG) into `tests/test_translate_finreflectkg_corpus.py` with a new `finreflectkg.export.json` fixture (`Node` + `relations`, `type`-discriminated). Baseline: **15/22 transpile today**; the 7 failures collapse into three transpiler gaps — `collect()` aggregation, list subscript/slice `x[i]`/`x[i..j]`, and the `upper`/`lower` scalar aliases — plus the NL "as a graph" + approximate-match semantic issue. The test doubles as a regression guard (supported queries must keep transpiling) and a coverage ledger (each gap asserts the exact error, failing loudly when closed). Roadmap in new `docs/cypher_coverage_plan.md` (WP-C1…C4 transpiler, WP-S1…S3 semantics incl. value-shape signals, portable `CONTAINS`/`=~` fuzzy, and an ArangoSearch inverted-index advisory mirroring the VCI "offer to create" UX) — **for review before implementing.** |
| 2026-06-23 | **Three workbench fixes: structural VCI detection, bare `RETURN`, and labeled connection chips.** **(1) False-positive "no VCI" warning fixed.** A vertex-centric index is an edge-collection index keyed on `_from`/`_to` plus an edge property (e.g. the `type` discriminator), but ArangoDB reports these as ordinary `persistent` indexes with **no** `vci` flag. The transpiler's flag-only check (`idx.get("vci")`) therefore wrongly reported real VCIs (e.g. `["_from","type","_toType"]`) as missing — emitting a "Traversal performance may be degraded" warning **and** suppressing the traversal index hint. Added structural detection (`arango_query_core/mapping.py::_is_structural_vci`): a `persistent`/`edge` index whose leading field is `_from`/`_to` and that carries ≥1 non-system field is recognized as a VCI (the explicit flag is still honored; the bare `["_from","_to"]` edge index is correctly **not** a VCI). `_build_vci_options` now also picks the direction-appropriate index (`_from`-leading for OUTBOUND, `_to`-leading for INBOUND). Verified live: the warning is gone and traversals emit `OPTIONS {indexHint: {"relations": {"outbound": {"base": "vci_from_type_totype"}}}}`. New `tests/test_mapping_vci_detection.py` (11 cases); existing index-aware goldens unchanged. **(2) Bare `RETURN` now supported.** A query that is only a `RETURN` of constant/computed expressions (`RETURN "hello"`, `RETURN 1 + 1 AS x`) previously failed with "MATCH is required". New `arango_cypher/_translate_v0/returns.py::_translate_standalone_return` maps it to a top-level AQL `RETURN` (`RETURN "hello"` → `RETURN "hello"`; multiple/aliased items → a `RETURN {…}` object). Row-stream-only forms (`RETURN *`, `DISTINCT`, `ORDER BY`/`SKIP`/`LIMIT`, aggregations) are rejected with precise messages. New `tests/test_translate_standalone_return.py`; updated the obsolete `RETURN 1` fail-fast case in `tests/test_unit_helpers_and_errors.py`. **(3) UI:** the connected header now shows gray `CLUSTER` and `DATABASE` labels (matching the `GRAPH` selector style) instead of an unlabeled `host/db` string (`ui/src/components/ConnectionDialog.tsx`). |
| 2026-06-23 | **Entity-resolution probe hardening — NL→Cypher can no longer hang on large/free-text collections.** Pre-flight entity resolution (`arango_cypher/nl2cypher/entity_resolution.py`) runs a per-`(label × property)` AQL probe that full-scans a collection and computes `LEVENSHTEIN_DISTANCE` per row. On a real DB this stalled hard: a fuzzy probe against a `chunks.text` document store (1.38M rows of long text) is O(len²) per row, ran for minutes, and tripped the driver's 60s read timeout — surfacing to the user as a "stuck" NL→Cypher call (observed: a single question took **130s** then returned 0 rows because resolution had timed out). Four bounded, env-tunable safety limits now apply: **(1) per-probe `maxRuntime`** (`probe_timeout`, env `ARANGO_CYPHER_ER_PROBE_TIMEOUT_S`, default `5.0`s) — a slow probe is killed server-side and treated as "no match"; **(2) value-length filter** (`max_value_length`, env `ARANGO_CYPHER_ER_MAX_VALUE_LENGTH`, default `512`) — stored strings longer than this are pruned *inside* the probe before the Levenshtein step, so free-text fields can't blow up the scan (this is the actual root-cause fix); **(3) total wall-clock budget** (`total_budget`, env `ARANGO_CYPHER_ER_TOTAL_BUDGET_S`, default `8.0`s) — `resolve()` stops probing once exceeded and returns best-so-far, bounding aggregate latency when an unscoped schema fans out into dozens of probes (a non-exact 0.85 hit doesn't early-exit, so without a budget it scans every label); **(4) optional size+index gate** (`max_scan_collection_size`, env `ARANGO_CYPHER_ER_MAX_SCAN_COLLECTION_SIZE`, default `0`=off) — skips probes on collections above a row threshold that lack a supporting index, while unknown counts and indexed fields are never skipped so legitimate large entity collections still resolve. Live verification on a 3.1M-node / 17.5M-edge DB: the same question dropped from 130s (hang, 0 rows) to ~9s (bounded by budget + one in-flight probe) and now correctly resolves "Cincinnati Financial" → `COMP.name`. Named-graph scoping remains the recommended path for best latency; this hardening guarantees the request degrades gracefully even when unscoped. New `TestProbeHardening` (15 cases) in `tests/test_nl2cypher_entity_resolution.py`; full ER suite 40 passing. |
| 2026-06-22 | **Schema catalog sidecar — schema analysis moved off the request path (new section §19).** Schema analysis (LLM-grade conceptual extraction + statistics + fingerprinting) costs seconds-to-minutes on a remote cluster; running it inline on a user click (connect, graph-scope change) made the workbench feel unusable. This wave promotes the existing persistent schema cache into a **catalog** that an out-of-band **sidecar** keeps warm, so the service reads it **read-only** and never analyzes inline. **(1) Catalog sidecar** (`arango_cypher/catalog/`): `registry.py` loads a small declarative list of databases (and optional named-graph scopes) from `configs/catalog.yml` — credentials via env-var references (`password_env`), never inline — falling back to the standard `ARANGO_*` vars when no file exists; `sync.py` exposes `sync_once` / `sync_forever` / `sync_entry`, each calling the existing `get_mapping(force_refresh=True, graph_name=…)` which persists the analyzed bundle to that DB's shared `arango_cypher_schema_cache`. One target's failure (connect or analyze) is reported as a `SyncResult` and never aborts the pass. CLI entrypoint `scripts/catalog_sync.py` (`--once` for cron, default loop for a long-running sidecar, `--config` / `--interval` / `--log-level`). **(2) Read-only request path**: new `schema_acquire.read_cached_mapping(db, *, graph_name=…)` serves the in-memory tier then the persistent catalog and returns `None` on a true miss — it never fingerprints, never calls the analyzer, never writes. `GET /schema/introspect` (and `/schema/statistics`) now read via this path and return `{status: "ready", …}` on a hit or `{status: "pending", entities: [], …, warnings: [SCHEMA_PENDING]}` on a miss (the sidecar — or the explicit "Refresh schema", which still forces a synchronous rebuild — populates it). No inline analysis, no background threads spawned from the request path. **(3) UI**: `introspectSchemaUntilReady` polls briefly while the catalog reports `pending` and shows a clear "schema is being analyzed" notice (`SCHEMA_PENDING_MESSAGE`) instead of an error; connect and graph-scope rehydrate both use it. **Staleness** is bounded by the sidecar's `interval_seconds` (default 30 min), which matches the agreed "fresh until next scheduled sync" tolerance. New tests: `tests/test_catalog_registry.py` (YAML + env-fallback + env-ref secret resolution + validation errors), `tests/test_catalog_sync.py` (per-target sync, connect/analyze failure isolation, empty-registry guard — including the `registry or load_registry()` falsy-empty-registry footgun), and `read_cached_mapping` cases in `tests/test_schema_acquire.py` + a `pending` endpoint test. Full suite: +17 passing, zero new failures (two `test_session_tenant_binding` failures are a **pre-existing** full-suite test-ordering bug, confirmed at HEAD without these changes). |
| 2026-06-18 | **Untyped relationships + Cypher→AQL AI fallback.** Two changes that together close the gap between "what the deterministic transpiler covers" and "what the user can ask". **(1) Untyped relationships** (`MATCH ()-[r]->()`, `-->`, `-[]->`, `RETURN type(r)`): the transpiler previously raised `UNSUPPORTED` for any relationship without a `:TYPE`. It now traverses the single inferred edge collection (`_infer_unlabeled_edge_collection`: one edge collection → use it; several but one type-discriminated → use it with a warning; otherwise "specify a type") with **no** type-discriminator filter, returning all relationship types — the Cypher-path analogue of the NL→AQL relationship-type meta query. `_resolve_relationship_for_pattern` centralises resolution so every query shape (basic / multi-part / `WITH`-bound / `OPTIONAL MATCH`, `EXISTS`, pattern predicate/comprehension, `SET`/`DELETE`) works; `type(r)` uses the shared discriminator with a `PARSE_IDENTIFIER(...).collection` fallback. Also fixed a pre-existing bug: `RETURN type(r), count(*)` without an alias produced a malformed `COLLECT collection)` key (affected typed queries too) — the group key is now inferred from the original Cypher expression. New `tests/test_translate_untyped_rel_goldens.py` (21 cases). **(2) Cypher→AQL AI fallback** (new section **§18**): when a Cypher query fails to transpile with a *recoverable* code (`UNSUPPORTED`/`NOT_IMPLEMENTED`), the workbench offers a one-click "Generate AQL with AI" action that sends the failing Cypher to `POST /nl2aql` via a new optional `cypher` field; the LLM translates Cypher→AQL and the result runs through `/execute-aql` (Layer 4/5 still enforced). It is offered, never automatic. Also fixed the UI dropping the human `message` on tenant/Layer-4 refusals (`formatDetail` now prefers `message`; `ApiError` exposes `code`). New `tests/test_nl2aql_cypher_fallback.py`. |
| 2026-06-17 | **Optional named-graph scoping (graph awareness).** A database may hold collections from many unrelated sources — a GraphRAG import, GAE/Pregel result collections, application-control collections, the transpiler's own `arango_cypher_schema_cache`, benchmark/query corpora — that the operator does **not** want to query over. ArangoDB *named graphs* already declare the exact subgraph that matters (edge definitions → edge collections + from/to vertex collections, plus orphan vertex collections). This wave makes `arango-cypher-py` *optionally* graph-aware: after connecting, the operator may restrict the session to a single named graph so that schema introspection, the NL prompt context, transpilation, and execution all consider **only** that graph's collections. When no graph is selected the behaviour is byte-identical to today (all collections considered). New section **§17 "Named-graph scoping"** specifies the model. Implementation: (1) `schema_acquire.get_mapping(..., graph_name=None)` threads an optional graph name through `_build_fresh_bundle` → `acquire_mapping_bundle`; when set, the analyzer still introspects the whole DB once (it has no scoping hook) and the resulting `MappingBundle` is filtered to the graph's collection set via the new `graph_collections(db, name)` resolver + `_filter_bundle_to_graph(bundle, vertex_colls, edge_colls)` helper (filters `physical_mapping.entities`/`relationships`, `conceptual_schema`, and `metadata.statistics`). The cache key is made graph-aware (`mapping` vs `mapping::graph::<name>`) in **both** the in-process and persistent tiers so each graph — and "all collections" — gets its own cached bundle. (2) `_Session` (`service/security.py`) grows a `graph_name` slot. (3) Two endpoints: `GET /graphs` lists the database's named graphs with their vertex/edge/orphan collections and a `collectionCount`; `POST /session/graph` binds (or clears, `graphName=null`) the session's graph after validating it exists (HTTP 404 on miss) — mirroring the post-analysis `/session/tenant` pattern, because (like tenancy) you cannot pick a graph before you can see what's in the database. The schema introspect / statistics / status / invalidate / force-reacquire endpoints read `session.graph_name` and scope accordingly. (4) UI: a `GraphSelector` toolbar control populated from `/graphs`, persisted in `localStorage` keyed by `(url, database)`, that re-introspects on change; the default is "All collections". This also subsumes the earlier "schema hygiene" follow-up — operational/side collections simply aren't members of the chosen graph. New tests: `tests/test_schema_acquire_graph_scope.py` (graph-collection resolution, bundle filter, graph-aware cache isolation) and `tests/test_service_graphs.py` (`GET /graphs`, `POST /session/graph` bind/clear/unknown-graph). Default-path (no graph) behaviour is pinned unchanged. |
| 2026-05-11 | **Wave 7: multi-tenant Phase 1 — Layer 1 session binding, MT-0 residual scoping-path BFS, Layer 5 EXPLAIN-plan validator, and Layer 6 `safe_execute` wrapper.** Closes work packages MT-0 (residual), MT-1, and MT-5 from `docs/multitenant_prd.md` §11 — the smallest set that closes threats T1, T4, T5, T7 for the common case and produces an auditable refusal log. Four logical commits. **(1) Layer 1 — session-bound tenant.** `_Session` (`arango_cypher/service/security.py`) grows `tenant_id`, `tenant_key`, and `is_admin` slots; `POST /connect` (`arango_cypher/service/routes/connect.py`) now accepts optional `tenantId` / `tenantKey` / `isAdmin` in `ConnectRequest`, validates the bound key against the `Tenant` collection when present (HTTP 403 `unknown_tenant` on miss), and stores the validated tuple on the session so it cannot be overridden by request body downstream. `/connect` echoes the bound triple back to the caller in `ConnectResponse` for UI display. A new `_optional_session` dependency (returns `None` instead of 401 when no token is present) is added so `/nl2cypher` and `/nl2aql` can run in two modes: **workbench mode** (`ARANGO_CYPHER_WORKBENCH=1` or no session bound) honours body-supplied `tenant_context` for backward compatibility with the Wave-4r UI; **tenant-user mode** (session has `tenant_id` set) overrides body tenant with the session-bound value and logs a warning if the two disagree (`tenant_context_override`). Tests: `tests/test_session_tenant_binding.py` (10 cases) pins valid-tenant accept / unknown-tenant 403 / verbatim accept when no `Tenant` collection exists / `None` binding / admin flag persistence / `tenantKey` defaulting to `tenantId`. **(2) MT-0 residual — `scopingPathFromTenant` BFS.** `arango_cypher/nl2cypher/tenant_scope.py` grows `compute_scoping_path(mapping, *, from_label, to_label, max_hops=6)` — a shortest-path BFS from `:Tenant` to a `TENANT_SCOPED` entity that lacks a denorm tenant field, returning the lexicographically-first sequence of relationship type names. `EntityScope` gains `scoping_path: tuple[str, ...] | None`; `TenantScopeManifest` gains `scoping_path_of(entity_name)`; `analyze_tenant_scope` populates the path for every TS entity that needs it. Lexicographic tie-breaking is deterministic across re-runs (no `set` iteration in the critical loop). Tests: `tests/test_tenant_scoping_path.py` (15 cases) — direct / 2-hop / 3-hop / disconnected / cyclic / max-hops-exceeded / TS-without-denorm path-populated. **(3) Layer 5 — EXPLAIN-plan validator.** New module `arango_cypher/tenant_plan_validator.py` exposes `validate_plan(*, db, aql, bind_vars, manifest, sharding_profile, session, plan_override=None, ...)` and `TenantScopeViolation`. The validator (a) calls `db.aql.explain(aql, bind_vars=bind_vars)` (or accepts a `plan_override` for unit tests), (b) does a "touches tenant data" pre-walk to short-circuit pure satellite/global queries with zero overhead, (c) for queries that *do* touch tenant data, enforces two bind-var sanity checks (`NO_SESSION_TENANT` if the session is unbound; `TENANT_BIND_MISMATCH` if client bind vars contradict session values), and (d) recursively walks plan nodes (`EnumerateCollectionNode`, `IndexNode`, `TraversalNode`, `SubqueryNode`) applying per-node-kind rules: an `EnumerateCollectionNode` on a tenant-scoped non-satellite collection must be guarded by an upstream `CalculationNode` referencing `@tenantId` / `@tenantKey` on the scoped attribute; an `IndexNode` is accepted iff one of its `condition` clauses is a bind-var equality on the scoped attribute; a `TraversalNode` is rejected unless the underlying graph is a disjoint SmartGraph (whose smartGraphAttribute provides automatic isolation) or every edge collection in the traversal is a satellite; a `SubqueryNode` recurses with a *fresh* `_PlanWalker` so inner-plan calculations are isolated from outer scope. Literal tenant predicates (`doc.tenant == "tenant-a"`) are rejected with `LITERAL_TENANT_PREDICATE` to force bind-variable use. Every pass logs `TENANT_SCOPE_OK` and every refusal logs `TENANT_SCOPE_VIOLATION` with `code`, AQL digest, and plan digest for audit. Tests: `tests/test_tenant_plan_validator.py` (23 cases) — every node kind × pass/fail × bind-var-vs-literal × subquery recursion × audit logging. **(4) Layer 6 — `safe_execute` wrapper.** `arango_query_core/exec.py` grows `safe_execute(*, db, aql, client_bind_vars, session, validator, manifest, sharding_profile, ...)` — overrides `tenantId`/`tenantKey` in client-supplied bind vars with session values (session value wins, never the body), calls the injected validator (Layer 5) which short-circuits if validation fails, then executes the AQL query with the merged binds. Re-exported from the `arango_query_core` package root. Service adapter `arango_cypher/service/safe_exec.py` builds `TenantScopeManifest` / `sharding_profile` / `collection_to_entity` from the request mapping, injects `validate_plan` as the validator, and implements fail-closed: a tenant-bound session with **no mapping** raises `TenantScopeViolation(code="NO_MAPPING_FOR_VALIDATION")` rather than silently bypassing validation; an unbound/workbench session proceeds with a warning so the Wave-4r workbench keeps working during incremental adoption. `/execute`, `/execute-aql`, and `/aql-profile` (`arango_cypher/service/routes/cypher.py`) all route through `safe_execute_aql` and surface `TenantScopeViolation` as HTTP 403 with `{error: "tenant_scope_violation", code, message, aql_digest, plan_digest}` so a UI can render an actionable failure and an audit log can correlate the refusal back to a specific session+query. Tests: `tests/test_safe_execute.py` (9 cases) — session tenant overrides client bind vars, validator refusal blocks execution, `PermissionError` on no session, `execute_kwargs` passthrough, service adapter's fail-closed behaviour. **Test-hygiene fix carried in this PR.** `tests/test_service_hardening.py::TestConnectDefaultsRedaction` previously failed locally when `ARANGO_PASSWORD` was set in the developer's `.env` (which dotenv loads at process start) because the test set the legacy `ARANGO_PASS` via monkeypatch but didn't clear the canonical name — both `test_password_omitted_by_default` and `test_password_returned_when_opted_in` now `monkeypatch.delenv("ARANGO_PASSWORD", raising=False)` so the legacy probe is the only source of truth for the assertion. **Wave 7 totals:** 4 commits, ~1900 LOC + 57 new tests; full suite `pytest -m "not integration and not tck"` reports 1189 passed / 9 skipped / 0 failures (was 1188 passed / 9 skipped / 1 failure on the test-hygiene case above); ruff check clean; UI typecheck (`tsc --noEmit -p tsconfig.app.json` for app and node) clean; UI prod build green. **What's deferred to later waves:** MT-2 (LLM-output literal-tenant guardrail), MT-3 (Cypher AST tenant injection), MT-4 (AQL AST tenant injection), MT-6 (plan-shape LRU for Layer 5 performance), MT-7 (admin bypass + audit log stream), MT-8 (security review + red-team corpus). The schema-introspection AQL in `arango_cypher/service/routes/schema.py` (`_sample_properties`, `_infer_edge_endpoints`) is intentionally not routed through `safe_execute` — it executes service-issued metadata queries, not user input, and will be safeguarded under MT-7 (admin-bypass + service-system-account model) once that work-package lands. |
| 2026-05-04 | **Audit v2 batch 6 — closes the remaining half of L-severity finding #8 (`translate_v0.py` monolith split).** Refactor-only PR; intended zero behavior change. `arango_cypher/translate_v0.py` is now a small compatibility shim preserving the existing import surface (`TranslateOptions`, `translate_v0`, `_pick_fresh_var`, `_pick_bind_key`, `_aql_collection_ref`, `_compile_type_of_relationship`, `_strip_label_backticks`). The implementation moved behind the private package `arango_cypher/_translate_v0/`: `core.py` keeps top-level translation dispatch plus the still-tightly-coupled MATCH/expression pipeline; `state.py` owns contextvars, `_HopMeta`, and `TranslateOptions`; `formatting.py` owns `WITH` injection and AQL reindent; `hints.py` owns VCI/index-hint and warning helpers; `naming.py` owns fresh variable/bind-key selection and label normalization; `literals.py` owns string/type literal helpers; `calls.py` owns standalone and in-query `CALL`; `unwind.py` owns UNWIND compilation; `writes.py` owns CREATE/MERGE/SET/DELETE/REMOVE/FOREACH. The write module uses lazy helper wrappers back into `core.py` for the remaining shared MATCH/expression/projection helpers, avoiding an import-time cycle while still moving the write-clause code out of the main dispatch file. Verification is intentionally heavier than the refactor itself: direct-import tests, write/CALL/UNWIND goldens, all `tests/test_translate*.py`, extension/procedure tests, round-trip smoke, full pytest, `ruff check`, and `ruff format --check .`. Audit-v2 status after this batch: 3/3 **M**-findings closed, 6/6 **L**-findings closed. |
| 2026-05-03 | **Audit v2 batch 5 — closes L-severity finding #6 (structured logging is effectively absent).** Adds an observability spine to `arango_cypher/service/` without touching any existing log-emitting call site (the 7 pre-existing `_svc_logger.warning` lines for CORS / SSRF / 422 / connect-failure paths keep firing under the new formatter; only the *delivery* changes). New module `arango_cypher/service/observability.py` ships four building blocks: **(1) `CorrelationIdMiddleware`** — raw-ASGI middleware (not `BaseHTTPMiddleware`, which buffers response bodies and would break streaming endpoints). On every HTTP scope it reads `X-Request-Id`, runs the value through `_INBOUND_REQUEST_ID_RE = ^[A-Za-z0-9-]{1,128}$` (rejects newline-injection attempts and absurd lengths), accepts the value if it matches or mints a fresh `uuid.uuid4()` if it doesn't, sets it into `correlation_id_var: ContextVar[str]` for the request lifetime, and rewrites the response start message to echo the chosen value back as `X-Request-Id`. The contextvar token is reset in a `finally:` so a request that crashes still releases the slot — no leakage across requests. **(2) `CorrelationIdLogFilter`** — `logging.Filter` subclass that copies the contextvar value onto every `LogRecord` as `record.correlation_id`. Critically attached to the **handler** (`StreamHandler.addFilter(...)`) rather than the logger (`Logger.addFilter(...)`): Python's `logging` machinery only runs a logger's filters on records originating from that logger; records propagated up from child loggers (`arango_cypher.service.routes.cypher`, `arango_cypher.nl2cypher.*`, …) skip the parent's filter list and go straight to its handlers. Attaching at the handler level catches both kinds. The default contextvar value `"-"` keeps records emitted *outside* a request (startup guards, eviction prune) rendering cleanly. **(3) `_KeyValueFormatter` / `_JsonFormatter`** — twin output shapes selected at `configure_observability()` time by `ARANGO_CYPHER_LOG_JSON` env (default → key=value for `tail -f`-style triage; `=1` → single-line JSON for Datadog / Loki / Splunk). Both share an identical reserved-attribute filter so the structured surface is byte-equivalent across formatters; the choice is purely an output-shape decision. Timestamp is ISO-8601 UTC with millisecond precision; key=value renderer JSON-quotes string values so embedded spaces / quotes don't break a downstream regex split. **(4) `log_endpoint_timing(endpoint, elapsed_ms, *, status="ok", **extras)`** — single helper called by every endpoint on the success path. Extras are run through `arango_cypher.service.security._sanitize_error` before emit (lazy import via `sys.modules` so the security ↔ observability dance at package init doesn't deadlock); reserved keys (`correlation_id`, `msg`, `level`, …) are silently dropped to keep the formatter contract clean. **(5) `log_llm_call(...)`** — emitted on `/nl2cypher` and `/nl2aql` after every translation with `(provider, model, prompt_tokens, completion_tokens, cached_tokens, cost_usd, elapsed_ms, method, retries, confidence)`. Cost lookup against `_PRICING_PER_1K_TOKENS`, a manually-maintained table covering OpenAI (`gpt-4o`, `gpt-4o-mini`, `gpt-4-turbo`) and Anthropic (Claude 3.5 Sonnet / Haiku, Claude 3 Opus); unknown `(provider, model)` returns `0.0` (treat as "unpriced" not "free" — operators reading the log should reach for the provider's own usage API for precise figures on a new model name). The `current_llm_provider_and_model()` helper reads `LLM_PROVIDER` env or infers from whichever of `OPENAI_MODEL` / `ANTHROPIC_MODEL` / `OPENROUTER_MODEL` is set; the rule-based fallback path (no LLM hit) calls `log_llm_call(provider=None, model=None, prompt_tokens=0, ..., method="rule_based")` so log scans get a uniform grep target for "every NL request, regardless of LLM-or-not". **Wiring** (per the user's expanded scope): `log_endpoint_timing` is called from **all 35 endpoints** — the 12 that already computed `elapsed_ms` for the response body (`/translate`, `/execute`, `/execute-aql`, `/explain`, `/aql-profile`, `/validate`, `/schema/introspect`, `/schema/summary`, `/schema/statistics`, `/schema/status`, `/nl2cypher`, `/nl2aql`, `/nl-samples`) plus the 23 that previously hit the log stream zero times (`/connect`, `/disconnect`, `/connections`, `/connect/defaults`, `/cypher-profile`, `/schema/properties`, `/schema/invalidate-cache`, `/schema/force-reacquire`, `/sample-queries`, `/tools/schemas`, `/tools/call`, `/suggest-indexes`, `/mapping/export-owl`, `/mapping/import-owl`, `/tenants`, `/corrections` × 4 verbs, `/nl-corrections` × 4 verbs). **`/health` is intentionally *not* instrumented** — container orchestrators (Kubernetes, Container Manager, docker-compose healthchecks) poll it every few seconds and the operational signal from logging it is zero; the endpoint still carries `X-Request-Id` (the middleware doesn't discriminate) so probe-correlation works, just no `endpoint_timing` line per probe. **Configuration** read from `app.py` at import time via `configure_observability()` (idempotent — second and subsequent calls are no-ops unless `force=True`); installs the filter + handler before any route module's import-time `logger.*` call so even startup warnings carry `correlation_id=-`. `propagate=True` is left on so `pytest`'s `caplog` (which attaches at the root logger) keeps capturing — the prior draft set `propagate=False` and broke `tests/test_env_password.py::test_legacy_only_returns_value_and_warns`; production deploys typically have no root-handler so duplication is moot, and operators who *do* call `logging.basicConfig` can override per the inline docstring. **Two new env vars** documented in `.env.example`: `ARANGO_CYPHER_LOG_LEVEL` (default `INFO`; flipping to `WARNING` silences the per-request timing lines while keeping CORS / SSRF / 422 warnings) and `ARANGO_CYPHER_LOG_JSON` (default unset → key=value; `=1` → JSON). New `tests/test_observability.py` (20 cases) pins the contract: middleware mints UUID4 / accepts safe inbound / rejects unsafe inbound / resets between requests; `_normalise_request_id` accepts/rejects boundary cases (empty, too-long, special chars); the log filter injects `correlation_id` on records emitted from child loggers (`arango_cypher.service.endpoint`); `log_endpoint_timing` redacts string extras through `_sanitize_error`; `log_llm_call` zeroes the cost on unknown models without raising and computes correct cost on known models (`gpt-4o-mini` 1k input + 0.5k output → $0.00045); the JSON formatter emits parseable single-line JSON; `configure_observability` is idempotent and `force=True` resets handlers + filters cleanly. Tests use a private list-collecting handler (not `caplog`, which can miss handler-attached filters) and rely on `TestClient(app)` so the full middleware order (CORS → CorrelationId) is exercised end-to-end. Tests: 1118/1118 unit pass (was 1097 in batch 4, +21 new = 20 observability + 1 corner case from batch 4 follow-up); ruff check + format --check both clean. Audit-v2 status after this batch: 3/3 **M**-findings closed, 5.5/6 **L**-findings closed (#4, #5, #6, #7, #8-service-half, #9). Remaining L-backlog: #8-translate-half (`translate_v0.py` 5063 LOC — separate dedicated refactor). |
| 2026-05-02 | **Audit v2 batch 4 — partially closes L-severity finding #8 (`service.py` 2232 LOC monolith split into a `service/` package).** Refactor-only PR; zero behaviour change. **Two-commit shape per the audit recommendation** so review is mechanical: commit 1 is a pure `git mv arango_cypher/service.py arango_cypher/service/__init__.py` (the diff is ~6 lines of relative-import + UI-path adjustments to climb the new directory level), commit 2 moves symbols out into focused submodules and turns the `__init__.py` into a re-export shim. **Final layout** (`arango_cypher/service/`): `app.py` (FastAPI() instance, CORS startup guard, `ARANGO_CYPHER_PUBLIC_MODE` flag, `_require_analyzer_unless_opted_out` analyzer-presence guard), `security.py` (sessions, `_TokenBucket` / `_compute_bucket` / `_nl_bucket`, `_check_connect_target` SSRF guard, `_sanitize_error` / `_sanitize_pydantic_errors` / `_translate_errors` redaction utilities, `_validation_error_handler` Pydantic 422 handler), `models.py` (every request/response Pydantic model + the audit-v2 batch 3 `_MAX_*` length constants), `mapping.py` (`_mapping_from_dict` HTTP-shape adapter), `registry.py` (process-wide ExtensionRegistry singleton built at import time), `ui.py` (UI freshness check + `/ui` / `/frontend` / `/assets` / per-icon mounts; imported last so its WARNING lines surface after the route-registration log noise), and a `routes/` subpackage with one module per endpoint cluster: `health`, `connect`, `cypher` (`/translate`, `/validate`, `/execute*`, `/explain`, `/aql-profile`), `schema` (`/schema/*`, `/sample-queries`), `tools` (`/tools/*`, `/suggest-indexes`), `nl` (`/nl2cypher`, `/nl2aql`, `/nl-samples`, `/tenants`), `owl` (`/mapping/*-owl`), `corrections` (`/corrections`, `/nl-corrections`). **Re-export contract preserved.** `from arango_cypher.service import _X` keeps working byte-for-byte for every previously-importable symbol — every `_Session`, `_TokenBucket`, `_check_*`, `_sanitize_*`, `_MAX_*`, `_PUBLIC_MODE`, `SESSION_TTL_SECONDS`, `MAX_SESSIONS`, etc. is re-exported from `__init__.py`; `time` is re-exported as a module attribute too because `tests/test_service_middleware.py::TestTokenBucket::test_refills_over_time` does `monkeypatch.setattr(svc.time, "time", fake_time)`. **Three subtle preservations** that needed extra care: (1) the `__init__.py` purges every `arango_cypher.service.*` submodule from `sys.modules` at the top so the test fixtures that pop the parent module and re-import (`tests/test_service_hardening.py::reload_service_clean`, `tests/test_service_startup.py::TestImportTimeGuard`) see fresh CORS / analyzer / public-mode guards re-execute; (2) `_Session.expired` and `_evict_lru` look up `SESSION_TTL_SECONDS` / `MAX_SESSIONS` lazily via the `arango_cypher.service` package object (`_service_pkg_candidates()` walks both `sys.modules` and the parent-package attribute, since the reload-and-restore fixtures can leave them diverged) so `monkeypatch.setattr(service, "SESSION_TTL_SECONDS", 100)` keeps flowing through to the security submodule; (3) `_check_nl_rate_limit` / `_check_compute_rate_limit` use the same `_resolve_bucket` indirection so `monkeypatch.setattr(svc, "_nl_bucket", new_bucket)` keeps swapping a capacity-1 bucket for the rate-limit suite without tests needing to know about the post-split internal layout. The connect route uses a lazy `from arango_cypher import service as _svc` to read `_svc.ArangoClient` so the autouse `_block_real_connect` fixture's monkeypatch of `arango_cypher.service.ArangoClient` keeps blocking real connections. New ruff `per-file-ignores` entry for `arango_cypher/service/__init__.py` covering F401 (every `from .submodule import _X` is intentional re-export surface) and E402 (the `sys.modules` purge has to run before submodule imports). **Translate-v0 still pending** — the AST-level split needs golden re-baselines per slice and is at least a week of careful work, not the half-day this batch budgeted; tracked as the remaining half of finding #8. Tests: 1097/1097 unit pass (was 1097 in batch 3); ruff check + format --check both clean. Audit-v2 status after this batch: 3/3 **M**-findings closed, 4.5/6 **L**-findings closed (#4, #5, #7, #8-service-half, #9). Remaining L-backlog: #6 (structured logging — Wave-6b-class WP per the audit text), #8-translate-half (`translate_v0.py` 5063 LOC — separate dedicated refactor). |
| 2026-05-01 | **Audit v2 batch 3 — closes L-severity findings #5 (`@field_validator` on request models) and #7 (`ARANGO_PASS` / `ARANGO_PASSWORD` split).** **#5 (length-bound + URL-shape validators):** every user-facing `BaseModel` in `arango_cypher/service.py` now carries stricter-than-type bounds derived from a small set of module-level constants — `_MAX_CYPHER_LENGTH = 100_000` (Cypher / AQL bodies; ~100 KB envelope, a real interactive query is < 10 KB), `_MAX_NL_QUESTION_LENGTH = 4_000` (LLM context-cost cap), `_MAX_RETRY_HINT_LENGTH = 8_000` (WP-29 EXPLAIN-error blob is the largest legitimate retry-context shape), `_MAX_TURTLE_LENGTH = 1_000_000` (OWL ontologies can be sizeable), `_MAX_NOTE_LENGTH = 4_000`, `_MAX_FIELD_LENGTH = 256` (urls, usernames, db / tool names, tenant fields, session tokens). Concretely: `TranslateRequest` / `ExecuteRequest` / `ValidateRequest` / `ExecuteAqlRequest` / `CorrectionRequest` use the Cypher / AQL caps; `NL2CypherRequest` / `NL2AqlRequest` / `NLCorrectionRequest` use the NL caps; `OwlImportRequest.turtle` uses the 1 MB turtle cap; `ConnectRequest.{url,database,username,password}`, `TenantContextPayload.{property,value,display}`, `ToolCallRequest.name` use the small-field cap. `ConnectRequest.url` additionally carries a `@field_validator` that requires `http://` or `https://` (defence-in-depth in front of the SSRF guard added in PR #7 — caught at validation time so the caller gets a clear 422 instead of a deeper 4xx/5xx). Side-fix to `_sanitize_pydantic_errors`: stringify any `BaseException` instance left in `ctx[...]` before serialising — without this the raw `ValueError` from the URL validator crashed the 422 encoder. New `tests/test_request_model_validators.py` (12 cases) pins the at-limit-passes / one-byte-over-rejects contract for `/translate`, `/execute-aql`, `/nl2cypher`, `/corrections`, and `/connect`. **#7 (env-var canonicalisation):** new shared helper `arango_cypher/_env.py::read_arango_password(*, caller)`. Contract: `ARANGO_PASSWORD` (canonical, matches Postgres / Redis / MongoDB / MySQL / Cassandra) wins; `ARANGO_PASS` is a deprecated fallback that emits exactly one `DeprecationWarning` + one `logging.WARNING` per `(caller, fallback-name)` pair (no log spam in a long-running service that calls the helper per-request); neither set returns `""`; `ARANGO_PASSWORD=""` is treated as an intentional auth-less local dev value and does *not* fall through to the legacy name. Both call sites flipped — `arango_cypher/service.py` (`/connect/defaults` body assembler) and `arango_cypher/cli.py` (`_connect` factory). `.env.example` rewritten to flip canonical / legacy labels and document the deprecation timeline (legacy name removed at 1.0). New `tests/test_env_password.py` (8 cases) pins the contract: canonical wins, legacy warns once per caller, distinct callers each get their own warning, empty canonical short-circuits, `_reset_warning_state_for_tests()` re-arms for tests that exercise the fallback path more than once. Tests: 1043 unit pass (was 1077 in batch 2; the prior 34-test integration / TCK delta was deselected by `--ignore=tests/integration --ignore=tests/tck` in the local sweep — full CI matrix still runs them); ruff check + format --check both clean. Audit-v2 status after this batch: 3/3 **M**-findings closed, 4/6 **L**-findings closed (#4, #5, #7, #9). Remaining L-backlog: #6 (structured logging — Wave-6b-class WP per the audit text), #8 (`translate_v0.py` / `service.py` monoliths — dedicated refactor PR per the audit text). |
| 2026-04-30 | **Audit v2 batch 2 — closes M-severity finding #2 (rate-limit expansion) and L-severity finding #9 (CI format gate flip).** Two-commit PR. **Commit 1 (`ruff format .` mechanical pass):** clears the long-standing 102-file format-gate backlog originally deferred when the gate was first added (`.github/workflows/ci.yml:19-21` comment). Zero behaviour change — verified post-pass with `ruff check .` clean and 1074/1074 unit tests green. Commit isolated so reviewers can `git diff <prev>..<this>` and confirm pure-whitespace/wrap/quote normalisation. **Commit 2 (#2 rate-limit expansion + #9 CI gate flip):** adds a second, cheaper `_TokenBucket` (`_compute_bucket`) keyed off the new `COMPUTE_RATE_LIMIT_PER_MINUTE` env var (default 100/min — an order of magnitude above the LLM bucket per the audit recommendation) plus a `_check_compute_rate_limit` `Depends(...)` wired onto 14 endpoints: `/translate`, `/validate`, `/execute`, `/execute-aql`, `/explain`, `/aql-profile`, `/schema/introspect`, `/schema/summary`, `/schema/statistics`, `/schema/force-reacquire`, `/suggest-indexes`, `/mapping/export-owl`, `/mapping/import-owl`, `/tools/call`. The two buckets share a centralised `_client_key()` helper (Authorization header → remote IP → literal `"anon"`) so the same client is tracked under the same identity across both. Three new tests in `tests/test_service_middleware.py::TestComputeRateLimit`: (a) bucket-isolation — draining one bucket leaves the other intact, with the right 429 detail message in each case so callers can route their retry logic; (b) `_client_key` fall-through; (c) end-to-end dep wiring on `/validate` via FastAPI's official `app.dependency_overrides` (chosen over monkeypatch because the latter was flaky under broader test ordering — captured in the test docstring). Multi-worker shared-bucket redesign stays a separate Wave-6b WP per the audit text. **#9 CI gate flip:** `.github/workflows/ci.yml` now enforces `ruff format --check .` next to `ruff check .` in the `lint` job; comment swapped from "intentionally omitted" to a cross-reference to the format-pass commit + audit close-out. Tests: 1077 unit pass (was 1074, +3 new); ruff check + format --check both clean. Audit-v2 status after this batch: 3/3 **M**-findings closed (#1, #2, #3), 2/6 **L**-findings closed (#4, #9). Remaining L-backlog: #5 (`@field_validator`), #6 (structured logging), #7 (`ARANGO_PASS` / `ARANGO_PASSWORD` split), #8 (`translate_v0.py` / `service.py` monoliths). |
| 2026-04-29 | **Audit v2 batch 1 — closes M-severity findings #1 + #2 (quick-fix half) and L-severity finding #4; bonus: index-hint warning de-noised.** Closes the three audit items the user picked up first from `docs/audits/2026-04-28-post-hardening-audit.md`. **#1 (AQL identifier interpolation, `compute_statistics`)**: lifted the existing `_COLLECTION_NAME_RE` (originally service-only, introduced at the `/tenants?collection=` boundary) into `arango_query_core.mapping` as the public `COLLECTION_NAME_RE` + `is_valid_collection_name()` helper so non-service callers get the same defence-in-depth without re-importing service internals; re-exported from `arango_query_core/__init__.py`. The four AQL strings in `arango_cypher/schema_acquire.py` (`RETURN LENGTH({col_name})` × 2 and `FOR d IN {col_name} FILTER ...` × 2) now (a) regex-validate the collection name against the ArangoDB grammar before any DB call and (b) backtick-quote the identifier inside the AQL string. Invalid names short-circuit to `count=0` — same outcome the bare `except` branch produced before, just without the noisy server-side parse error. **#2 (rate-limit gap, quick-fix half)**: added the existing `_check_nl_rate_limit` `Depends(...)` to `/nl-samples` (it was the most concerning gap per the audit; the broader half-day expansion to the rest of the LLM-/CPU-heavy endpoints stays open as a separate item). **#4 (mapping-helper unit tests)**: new `tests/test_mapping_helpers.py` with 18 dedicated cases for `mapping_from_wire_dict` and `mapping_hash` covering snake/camel-case symmetry, snake-wins-when-both-present precedence, missing-keys → empty-dict normalisation, `MappingSource` pass-through, owl_turtle intentional-no-read, hash determinism across key insertion order, hash sensitivity to `conceptual_schema` / `physical_mapping`, hash insensitivity to `metadata` and `owl_turtle` (the documented `{cs, pm}`-only canonical contract), dict-vs-bundle-vs-stub equivalence, empty-input baseline, and the hasattr / unknown-type fallback branches. **Bonus A (index-hint warning de-noised)**: `_build_collection_index_hint` in `arango_cypher/translate_v0.py` now suppresses the "marked indexed but no named index found" warning when the entity has *zero* `IndexInfo` entries in the mapping (the common shape from the schema-analyzer export, which sets `PropertyInfo.indexed=true` from the catalog without round-tripping the named-index list) — there's no actionable advice to give in that case, every indexed field would trip the same warning, so the message only fires now when there's a real mapping gap (some named indexes exist, just none cover the filtered fields). User-reported via the `TenantUser` warning bubble. Tests: 1074 unit pass; 32 focused (new + golden) green; ruff clean. The remaining audit-v2 items (#3 CI path drift fix already shipped in PR #12 fix-up `84cf4c1`; #5–#9 L-severity backlog) stay open. |
| 2026-04-28 | **Audit v2 filed — post-hardening code-quality sweep ([`docs/audits/2026-04-28-post-hardening-audit.md`](./audits/2026-04-28-post-hardening-audit.md)).** First audit to land as a persisted doc (previous audits were in-chat only, tracked by PR number through this changelog). Nine findings, zero **H**-severity, three **M** (identifier interpolation in `schema_acquire.compute_statistics`, rate-limit coverage at 2-of-~35 endpoints with `/nl-samples` as the most concerning gap, CI path drift that silently masked 0-test-collected packaging runs from 2026-04-24 → 2026-04-28), six **L** (missing unit tests for the `arango_query_core` mapping helpers, no `@field_validator` declarations on request models, near-absent structured logging in `service.py`, `ARANGO_PASS` / `ARANGO_PASSWORD` split, `translate_v0.py` / `service.py` monoliths, un-flipped `ruff format --check` CI gate). Also establishes the new `docs/audits/` folder with a convention README (`YYYY-MM-DD-<slug>.md`, TL;DR → per-finding → what-stayed-the-same → next-actions). The "what stayed the same" back-reference table at the bottom of the audit confirms every Wave-6a TL;DR item was verified clean in this pass. Priority-ordered next-actions list; items 1–4 (~1 dev-day combined) close the three **M**-severity findings. |
| 2026-04-28 | **WP-19 packaging smoke test (closes WP-19 acceptance criteria #2 and #3).** New `tests/integration/test_packaging_smoke.py` with two cases. (a) `test_pyproject_extras_pin_published_versions_only` — unconditional (no gate); reads `pyproject.toml`, refuses any entry in `[analyzer]` / `[service]` / `[cli]` / `[owl]` / `[dev]` that starts with `file:` / `./` / `/` / `git+` / `hg+` / `svn+` / `bzr+`, or contains ` @ ` (PEP 508 direct reference). Self-referencing `arango-cypher-py[...]` entries (PR #10's DRY pass) are explicitly allowed — they're PEP 735 indirection, not a deployment blocker. ~50 ms, runs on every `pytest` invocation, standing regression guard against a dev accidentally running `pip install -e ../arangodb-schema-analyzer` and leaking the editable into the manifest. (b) `test_sdist_builds_and_imports_with_service_extras` — gated behind `RUN_PACKAGING=1`; runs the canonical WP-19 flow end-to-end via the portable stdlib toolchain: `python -m build --sdist --outdir <tmp>/dist <repo>` → `python -m venv <tmp>/venv` → `pip install '<sdist>[service,analyzer]'` → `python -c 'import arango_cypher.service'`. Uses `build` + `venv` + `pip` rather than `uv` so CI has no non-stdlib prerequisites; the runbook notes `uv build` / `uv sync` as equivalent for a dev running the flow locally. Verified end-to-end: 25 s on a warm PyPI cache, 1047/1047 existing tests untouched. Each subprocess step is wrapped in a `_run_or_fail` helper that inlines captured stdout/stderr into the assertion message so remote-CI log triage doesn't require re-running. `docs/arango_packaging_service/deployment_runbook.md` smoke-test section corrected (the old path `tests/test_packaging_smoke.py` was wrong; now points at `tests/integration/test_packaging_smoke.py`) and expanded to document both cases. `docs/implementation_plan.md` §WP-19 status flipped from "Ready" → "In progress (2/3 acceptance criteria met)"; the one outstanding criterion is the human-in-the-loop end-to-end staging-deploy walk-through (#1). The fix-up commit `84cf4c1` also corrected the CI `packaging` job path (\`\`.github/workflows/ci.yml:57\`\` referenced a never-existed `tests/test_packaging_smoke.py` — caught and documented as finding #3 in the 2026-04-28 audit v2 doc, fixed here before merge). |
| 2026-04-28 | **Small DRY pass + drop generated `CypherVisitor` (audit TL;DR items 5, 8, 9).** Net `-467 LOC` across 13 files (`146 insertions / 613 deletions`); no behaviour change; `1047 / 1047` tests still pass; no test-file edits required. (1) **Item 8 — drop `CypherVisitor`.** `arango_cypher/_antlr/CypherVisitor.py` (528 LOC of ANTLR-generated dead code) deleted; nothing in `arango_cypher/` ever imported it (the parse-tree `accept(visitor: ParseTreeVisitor)` methods on `CypherParser` ctx classes resolve against the antlr4 runtime's `ParseTreeVisitor`, not our generated subclass). `CONTRIBUTING.md`, `GEMINI.md`, and PRD §22 regen-command snippets updated to drop the `-visitor` flag (`antlr4 -Dlanguage=Python3 -o arango_cypher/_antlr grammar/Cypher.g4`); next regen produces a parser tree byte-equivalent to today minus the visitor file. README directory tree comment updated from "lexer/parser/visitor" to "lexer/parser". (2) **Item 5 — single canonical wire-dict helper.** New `arango_query_core.mapping_from_wire_dict(d, *, source=None)` and `arango_query_core.mapping_hash(mapping)`, both exported from the `arango_query_core` package root. Three previously-divergent helpers all now delegate: `service._mapping_from_dict` (HTTP path; preserves the `MappingSource("explicit", "supplied via HTTP")` tag and the `None`-short-circuit so `scripts/benchmark_translate` keeps working), `tools._dict_to_bundle` (LLM tool-calling path; no source — tool-calling payloads have no audit-worthy provenance), and `cli._load_mapping` (file / inline-JSON path; preserves the per-file source notes). Both correction stores' `_mapping_hash` now reduce to a one-line alias for `arango_query_core.mapping_hash` — the 30-line normalisation+hash body that was duplicated verbatim between `corrections.py` and `nl_corrections.py` since the 2026-04-26 hardening PR is now in one place; tests asserting on the per-module attribute (`corrections._mapping_hash` / `nl_corrections._mapping_hash` in `test_service_hardening.py`) still pass via the alias. The 30-LOC body lived in two places because of the symmetric correction-store split; the new home (`arango_query_core.mapping`) is the natural one — same place as the `MappingBundle` dataclass. (3) **Item 9 — single source for the analyzer pin.** `[service]` and `[dev]` extras in `pyproject.toml` now reference `arango-cypher-py[analyzer]` instead of re-pinning `arangodb-schema-analyzer>=0.6.1,<0.7.0` for the second and third time. PEP 621 / PEP 735 self-referencing extras are supported by pip, uv, and hatchling. The pin band only needs to be edited in one place when the next analyzer floor lands. |
| 2026-04-27 | **Docs refresh: post-0.6.1 narrative alignment (audit items 6, 10 + missing-facts cleanup).** One PR's worth of pure documentation edits — no code changes. (1) `docs/arango_packaging_service/deployment_runbook.md`: rewrote the "prerequisites" block so `arangodb-schema-analyzer` is no longer described as the packaging blocker (it's been on PyPI since 2026-04-23; the pin is now `>=0.6.1,<0.7`). Retained the "install without the `[analyzer]` extra" and `ARANGO_CYPHER_ALLOW_HEURISTIC=1` escape hatches for air-gapped / heuristic-only deploys, reframed as intentional fallbacks rather than workarounds. `requires-python` callout updated from `>=3.10` to `>=3.11`. (2) PRD §15.1 / §15.3 / §15.4 / §15.5: replaced "not published to any package index" with a dated resolution note; the two publication-related open questions flipped to resolved. The §15.2 rejection table is preserved unchanged (those alternatives are still rejected — they would now be redundant rather than blocked). (3) §Multi-tenant-safety Layer-0 row: status flipped from "Schema supports; mapper does not yet expose layout" to a partially-adopted note that enumerates what the analyzer emits today (`physicalLayout`, `shardFamilies`, `metadata.multitenancy.{style, tenantKey[], physicalEnforcement}`, `tenantScope.role/tenantField`) and what's still on the roadmap (`smartGraphAttribute`, `isDisjoint`, MT-0 fixtures, MT-5 EXPLAIN-plan validator). Matching edit in the `### Six-layer defense-in-depth architecture` bullets section further down. (4) `docs/implementation_plan.md` WP-19: status flipped from "Blocked (upstream)" to "Ready" with a dated dependency-resolved note; line-411 motivation paragraph rewritten so a reader who lands on WP-19 from the summary table no longer sees stale "not published to any package index" language. (5) `docs/multitenant_prd.md`: added §16a "Schema-analyzer signals consumed by the guardrail" documenting `metadata.multitenancy.{style, tenantKey[], physicalEnforcement}` (and the corresponding `tenantScope.{role, tenantField, scopingPathFromTenant}` per-entity annotation) so a reader of the PRD can trace "how does the guardrail know the DB is physically-enforced?" without grepping `tenant_guardrail.py`. (6) `docs/schema_analyzer_issues/README.md`: added issue #08 row; status banner updated to "all eight issues resolved upstream (2026-04-23)". (7) `docs/schema_analyzer_issues/06-emit-tenant-scope-annotations.md`: added a "Downstream adoption (2026-04-23)" addendum noting `tenantScope.role` / `tenantScope.tenantField` are now consumed by `nl2cypher/tenant_scope.py`. (8) `README.md`: "Python 3.10+" → "Python 3.11+"; CI matrix `3.10/3.11/3.12` → `3.11/3.12`; added a one-liner under Install noting the analyzer is pulled from PyPI by the `[service]` / `[analyzer]` extras and does not need any dev-checkout setup. (9) `GEMINI.md`: "Python 3.13 (Managed by `uv`)" → "Python 3.11+ (managed by `uv`; CI matrix 3.11 / 3.12)" — the 3.13 claim was never reality. (10) `arango_cypher/schema_acquire.py`: the `ImportError` message shown when the analyzer is not installed updated from the dual-hint form ("`pip install arangodb-schema-analyzer` or: `pip install -e ~/code/arango-schema-mapper`") to the canonical pinned form (`pip install 'arangodb-schema-analyzer>=0.6.1,<0.7'`), since the editable-install path is no longer the recommended route for any consumer. No tests touch this string. |
| 2026-04-26 | **Hardened NL + connect endpoints (TL;DR items 1–4 and 7 from the post-Wave-6a code-quality audit).** Six changes wrapped in a single PR with regression coverage in `tests/test_service_hardening.py` (37 cases): (1) `_validation_error_handler` now redacts every `errors()` entry's `input` recursively through `_sanitize_pydantic_errors` *before* logging or returning to the client, so a payload that embedded `password=…` no longer leaks via either the 422 response body *or* the WARNING log line; in `ARANGO_CYPHER_PUBLIC_MODE` the trailing body fragment that previously trailed the log is dropped entirely. (2) `GET /connect/defaults` returns the `password` field as the empty string by default — the field stays in the response (the UI's connect dialog binds against it) but the value of `ARANGO_PASS` is no longer dumped to anonymous callers; the legacy "auto-fill the password on a trusted laptop" affordance is gated behind the new `ARANGO_CYPHER_EXPOSE_DEFAULTS_PASSWORD` opt-in. (3) `_check_connect_target` added: `POST /connect` parses the supplied URL, refuses literal cloud-metadata IPs / hostnames (`169.254.169.254`, `100.100.100.200`, `fd00:ec2::254`, `metadata.google.internal`, etc.) unconditionally, and additionally refuses RFC1918 / loopback / link-local / ULA literals when public mode is on. The check is intentionally local-only (no DNS resolution — DNS rebinding would itself amplify the SSRF surface); operators that need a private target opt in via `ARANGO_CYPHER_CONNECT_ALLOWED_HOSTS=<csv>`. (4) `_require_session_in_public_mode` dependency added and wired into `/nl2cypher`, `/nl2aql`, `/nl-samples`, `/connections`, every mutating + listing variant of `/corrections`, and every variant of `/nl-corrections` (12 endpoints total). In default mode the dependency is a no-op — the existing UI keeps working; in `ARANGO_CYPHER_PUBLIC_MODE` it returns 401 without a valid `X-Arango-Session` / `Authorization: Bearer` token. The `session_token` field on the `/nl2cypher` body is *ignored* in public mode — the authenticated session's DB is used unconditionally for entity resolution so a caller can no longer point one user's NL request at another user's database by guessing the body field. (5) CORS startup guard: `CORS_ALLOWED_ORIGINS=*` combined with the new `ARANGO_CYPHER_CORS_CREDENTIALS=1` flag now refuses to start (the combination is never safe); with `*` and the credentials flag unset, `allow_credentials` is silently downgraded to `False` and the operator gets a startup warning. Explicit origin lists keep credentialed CORS on by default. (6) Both correction stores' `_mapping_hash` now normalise camelCase ↔ snake_case keys before hashing, fixing the silent collision where a UI POST (`conceptualSchema`/`physicalMapping`) and a Python-API call (`conceptual_schema`/`physical_mapping`) saved the same logical mapping under two different fingerprints — `lookup()` would miss the saved correction and the user would re-submit the same fix repeatedly. `.env.example` documents the four new tunables (`ARANGO_CYPHER_CORS_CREDENTIALS`, `ARANGO_CYPHER_EXPOSE_DEFAULTS_PASSWORD`, `ARANGO_CYPHER_CONNECT_ALLOWED_HOSTS`, expanded `ARANGO_CYPHER_PUBLIC_MODE` description). §4.4.5 security-model table updated: CORS / `.env` exposure rows rewritten to reflect the new behaviour, and three new rows added for SSRF, public-mode auth gating, and Pydantic-422 redaction. All 1047 prior tests still pass. |
| 2026-04-25 | **Housekeeping: `.env.example` completeness pass + `SchemaWarningBanner` key fix.** (1) `.env.example` extended to document every environment variable the service or CLI reads (audited by grep over `os.getenv` / `os.environ.get` in `arango_cypher/`): added `NL2CYPHER_TENANT_FIELD_REGEX` (tenant-scope property-name regex; cross-referenced to `multitenant_prd.md` §4.2), `ARANGO_CYPHER_PUBLIC_MODE` (suppresses `/connect/defaults` response body for publicly exposed deployments; cross-referenced to §9 security table where it was already flagged as a partial recommendation), `OPENAI_BASE_URL` / `OPENAI_MODEL` / `OPENROUTER_MODEL` / `ANTHROPIC_BASE_URL` (previously-undocumented provider overrides read by `nl2cypher/providers.py`), `CORRECTIONS_DB` / `NL_CORRECTIONS_DB` (dev-only SQLite persistence paths for the rule-learning tables), and a note that the `arango-cypher` CLI entry point reads `ARANGO_PASSWORD` while the FastAPI service reads `ARANGO_PASS` (the two names were previously undocumented aliases; set both if you use both entry points). No new runtime knobs introduced — this is pure documentation of the existing surface. The four knobs already called out in §9 security table (`CORS_ALLOWED_ORIGINS`, `SESSION_TTL_SECONDS`, `MAX_SESSIONS`, `NL_RATE_LIMIT_PER_MINUTE`) remain the production-relevant ones. (2) `ui/src/components/SchemaWarningBanner.tsx`: `others.map((w) => <div key={w.code}>)` collided when multiple warnings shared the same `code` (legitimate case on heuristic-mode DBs where one `HEURISTIC_FIELD_REJECTED` warning is emitted per collection that flunked tier-2 acceptance). Observed on `ic-knowledge-graph-temporal` during PRD §11 closeout dry-run. Key changed to `${code}-${index}`; warnings array is emitted in stable order per bundle reconcile so position is a safe tiebreaker. Cosmetic — the warnings were still rendered, just with a React console warning. |
| 2026-04-24 | **Service-side code-quality hardening (this-sprint items from the post-Wave-6a review).** Five changes committed as a single sprint, each with regression tests: (1) `/tenants?collection=<name>` now validates the caller-supplied name against `_COLLECTION_NAME_RE` (ArangoDB identifier grammar: `^[A-Za-z_][A-Za-z0-9_-]{0,255}$`) *before* embedding it in the backtick-interpolated `FOR t IN \`<name>\`` AQL, closing the one user-controlled identifier-injection surface in the service; 400 with a structural error message on rejection. (2) `_translate_errors` context manager extracted (`service.py`), replacing six copies of the `try / except Exception / HTTPException(500, detail=…_sanitize_error(str(e)))` boilerplate at every AQL-executing endpoint (`/execute`, `/execute-aql`, `/explain`, `/aql-profile`, `/tenants` ×2); nested `HTTPException`s are re-raised unchanged so upstream 400/422 statuses are not masked to 500, `KeyboardInterrupt` / `SystemExit` still escape (`except Exception`, not `BaseException`). (3) `_sanitize_error` credential-pattern fixed: `_CRED_RE` was a single `\S+`-terminated pattern, which only consumed the *scheme* of an `Authorization: Bearer <jwt>` header and let the token itself leak into the `HTTPException.detail`; split into `_CRED_RE` (key=value forms) + `_AUTH_HEADER_RE` (HTTP auth headers with `Bearer`/`Basic`/`Digest`/`Token` schemes, which match scheme + value as one unit). New `tests/test_service_sanitize.py` (25 cases) pins the URL, host:port, and credential redactions plus the seven `_translate_errors` invariants. (4) `tests/test_service_middleware.py` added (new file, 11 cases): pins CORS preflight reflection, `/health` response headers, `_TokenBucket` capacity + per-key isolation + refill-over-time (monkeypatched `time.time` to avoid wall-clock sleeps), and the session lifecycle — `_prune_expired` closes clients and evicts, `_evict_lru` respects `MAX_SESSIONS`, expired / unknown / missing tokens return 401. (5) `typer>=0.9.0` added to the `dev` extras in `pyproject.toml` so the 14 `typer_available`-gated CLI tests run on the default `pip install -e '.[dev]'` bootstrap instead of silently skipping. **Security-model table in §4.4.5 rewritten** to correct stale env-var names (actual names are `CORS_ALLOWED_ORIGINS`, `SESSION_TTL_SECONDS`, `MAX_SESSIONS`, `NL_RATE_LIMIT_PER_MINUTE`, not the `ARANGO_CYPHER_*` variants previously documented) and flip the Max-sessions, Rate-limiting, AQL-injection, Error-sanitization rows from "Not implemented" / "Partial" to "Implemented" now that each has a test pinning it. `.env.example` extended with a new "Service limits & CORS" block documenting the five runtime knobs (`NL_RATE_LIMIT_PER_MINUTE`, `SESSION_TTL_SECONDS`, `MAX_SESSIONS`, `CORS_ALLOWED_ORIGINS`, `ROOT_PATH`) plus the `ARANGO_CYPHER_ALLOW_HEURISTIC` safety opt-out introduced in WP-28. |
| 2026-04-23 | **Multi-tenant safety PRD linked from main PRD; §4.4 UI scope note clarified (stitch; see [`docs/multitenant_prd.md`](./multitenant_prd.md)).** Adds a stub "Multi-tenant safety" section before §16 that reproduces the six-layer defense-in-depth summary table (Layer 0 Storage → Layer 6 Execute) and points at the standalone `multitenant_prd.md` draft for the full architecture, algorithmic rewrite rules, EXPLAIN-plan validator spec, and MT-0..MT-8 work-package schedule. Scope note in §4.4 reworded to distinguish two meanings of "multi-tenant" that were silently conflated: the UI is not a multi-user authn/authz surface (each browser session is still a single operator, one ArangoDB credential per connection), but when the backend reports a tenant-scoped graph (per Wave 4r) the UI **does** surface the pinned `@tenantId` and route queries through the session-bound scope — so "multi-tenant isolation out of scope" no longer reads as a contradiction of the tenant selector we shipped in Wave 4r. Full fold-in (per `multitenant_prd.md` §Merge notes: §1–§2 → new §16, §3 → §5, §4–§9 → §16.*, §10 → §16.8, §11 → implementation-status table, §12 → §8) remains deferred to the start of Wave-MT when Layer 1/3/4/5 implementation begins and the main PRD needs concrete status entries to absorb rather than purely design commitments. No code changed in this entry. |
| 2026-04-22 | **Bug-fix PRD filed for schema-inference + NL feedback-loop cascade (see [`docs/schema_inference_bugfix_prd.md`](./schema_inference_bugfix_prd.md)).** A hybrid (GraphRAG + PG) pilot database produced an unrecoverable Translate-time parse error (`no viable alternative at input 'MATCH (d:Compliance.'`) on the NL question *"What are the different versions of the Compliance.rst document?"*. Root-cause investigation identified six distinct defects that combined to produce the failure, in pipeline order: (D1) the heuristic schema inference path in `_detect_type_field` + `_DOC_TYPE_FIELDS` treats any field in `{label, labels, type, _type, kind, entityType}` present in ≥80 % of sampled docs as a type discriminator regardless of cardinality or value shape, so `*_Documents` collections whose `label` field carries filename data (`"Compliance.rst"`, `"index.rst"`, `"requirements.txt"`) were exploded into 36 fake entities per collection, 43 of them database-wide carrying `.` in the name (illegal in `oC_SymbolicName` without backtick escape); (D2) `_build_fresh_bundle` silently falls back to the heuristic on `ImportError` at the deployed service, attaches no warning, and the broken bundle is then cached indefinitely because the shape fingerprint is stable (`source.kind: "heuristic"` confirmed on the pilot cache row, ~18 hours old and counting); running `acquire_mapping_bundle` (analyzer path) against the same live DB produced 183 entities with 0 dotted names, collapsing `IBEX_Documents` into one `IBEXDocument` entity with `style=COLLECTION` and `label` retained as a scalar property — the analyzer is correct, the heuristic is wrong; (D3) `_SYSTEM_PROMPT` + `_build_schema_summary` emit `Node :Compliance.rst (…)` to the LLM with no guidance to backtick-escape non-identifier labels, so the LLM faithfully copies the illegal label; (D4) `_call_llm_with_retry` returns `best_cypher` with `confidence=0.3` and a WARNING prefix buried in `.explanation` even when every attempt failed ANTLR parse, instead of failing closed like the Wave-4r tenant-guardrail path — the UI renders the invalid Cypher as a first-class result; (D5) `_pick_primary_entity_label` does not strip backticks before calling `resolver.resolve_entity`, so even a correctly-escaped LLM output (``MATCH (d:`Compliance.rst`)``) fails with `No entity mapping for: \`Compliance.rst\`. Available entities: Compliance.rst`; (D6) the Translate button is a pure Cypher→AQL call with no edge back into inference, so bad Cypher in the editor produces a fresh parse error on every click rather than re-routing to `/nl2cypher` with the parse error as retry context. Each layer assumes the others behave correctly; the bug-fix PRD schedules all six as a single wave (proposed WP-27 through WP-30 in `implementation_plan.md`) because fixing only D1 leaves the others as latent failures for the next schema the heuristic mis-infers. No code written in this entry — this is the PRD filing. |
| 2026-04-20 | **Wave 4r: tenant-scoping guardrail + UI tenant selector (multi-tenant isolation).** Closes the translation-quality regression surfaced by the post-Wave-4q visual test: the NL question "At Dagster Labs list all the GSuiteUsers in the Marketing department" was translating to `MATCH (u:GSuiteUser) WHERE u.DEPARTMENT='Marketing' RETURN u.NAME, u.EMAIL` — no `:Tenant` node, so the query would return cross-tenant rows on any database where `Tenant` is the isolation root. Root cause was the LLM having no ambient signal that the workspace was multi-tenant; the few-shot corpus had no tenant-scoped examples and the NL heuristic couldn't distinguish "at X" meaning "at tenant X" from ordinary English. Fix is a three-piece stack. **(1) Backend guardrail** (`arango_cypher/nl2cypher/tenant_guardrail.py`, ~180 LOC + 12 tests): new `TenantContext` (property/value/display) + `check_tenant_scope(cypher, tenant_context)` postcondition that fires when a context is active and the Cypher contains no `:Tenant` binding. Label regex anchored with `\b(?!\w)` so `:TenantUser` / `:TenantCVE` / `:TenantAppVersion` do not satisfy the `:Tenant` constraint (the prefix-collision trap is exactly the failure mode we're guarding against). Wired into `_call_llm_with_retry` after parse + EXPLAIN so we only burn retries on semantically-valid Cypher; exhaustion returns `method="tenant_guardrail_blocked"` with empty `cypher` — the translator **fails closed**. Rule-based fallback is also blocked when a context is active (it cannot enforce scope). `nl_to_aql` gets a looser `\bTenant\b` postcondition (no label context to distinguish from `TenantUser` in AQL). **(2) Catalog endpoint** (`GET /tenants`, ~45 LOC + 5 tests): session-authenticated, checks `db.has_collection("Tenant")` as the heuristic for multi-tenancy detection, returns `{detected, tenants: [{key, name, subdomain, hex_id}]}` sorted by name (LIMIT 10k). Returns `{detected: false, tenants: []}` cleanly for single-tenant graphs so the UI can be mechanical about show/hide. **(3) UI** (`ui/src/components/TenantSelector.tsx` + `App.tsx` wiring, ~220 LOC added): new typeahead selector in the header, only rendered when the conceptual schema declares a `Tenant` entity AND the backend confirms the collection exists (mirrors `has_tenant_entity` in both places). Selection persists in `localStorage` keyed by `(url, database)` and rehydrates on reconnect (with catalog-membership revalidation — stale selections are dropped). Shows `NAME` as the primary label with `SUBDOMAIN` as subtitle for disambiguation; injects `TENANT_HEX_ID` as the scope value when present (unique + indexed), falling back to `NAME` otherwise. Active scope surfaces as an amber badge next to the Ask input so it's always visible when a tenant is pinned. `nl2Cypher` / `nl2Aql` client methods accept `tenant_context` and the endpoints forward it through `TenantContextPayload` → `TenantContext` into the prompt builder. **Prompt composition.** `PromptBuilder` renders a "## Current tenant scope" block between the schema and the few-shot section when a context is active; zero-shot rendering is **byte-identical to pre-4r** when `tenant_context=None`, preserving WP-25.4 prompt caching for single-tenant users (pinned by `test_no_tenant_context_leaves_prompt_byte_identical`). Tests: 25 new (12 guardrail, 8 integration via `nl_to_cypher` with stub LLM, 5 endpoint smoke); full adjacent suite green (`test_nl2cypher*.py` + `test_service*.py`, 65 passed / 1 skipped). UI typechecks + builds clean. Follow-up tracked: schema-mapper issue #8 (emit `metadata.tenantEntity` hint so the backend heuristic can move from "collection named Tenant" to a mapping-driven signal). |
| 2026-04-20 | **Wave 4q (PR-3/3): remaining analyzer workarounds deleted + upstream reconciliation and statistics adopted (closes mapper issues #3, #4; optional adoption of #2).** Two post-processors in `arango_cypher/schema_acquire.py` retired: `_fixup_dedicated_edges` (~80 LOC, issue #3 — multi-type edge collections split into per-type `GENERIC_WITH_TYPE` entries) and `_backfill_missing_collections` (~160 LOC, issue #4 — non-graph collections backfilled via heuristic re-probing). Both closed capability gaps that shipped upstream in `arangodb-schema-analyzer` v0.2.0 and are now invariants of `AgenticSchemaAnalyzer.analyze_physical_schema` + `schema_analyzer.reconcile.reconcile_physical_mapping`. Pre-deletion golden-diff gate (`scripts/pr3_workaround_diff.py`, deleted after it served its purpose) ran `acquire_mapping_bundle` with and without both post-processors against six fixture DBs (`neo4j_movies_{pg,lpg}_test`, `cypher_{pg,lpg,hybrid}_fixture`, `northwind_test`) and confirmed byte-identical `physical_mapping` output in every case; deletion is a no-op on the output contract. Replaced the two call sites in `acquire_mapping_bundle` with an observability hook that reads `metadata.reconciliation.backfilled_collections` (when upstream reconciled against an LLM mapping) and emits a `logger.warning` listing the backfilled names, preserving the visibility we used to get by doing the backfill ourselves. Adopted issue #2 at `_safe_refresh_statistics`: when the analyzer has populated `metadata.statistics` with `statisticsStatus == "ok"`, we skip the duplicate local `compute_statistics` pass (saves one round of LENGTH + COLLECT AQL queries per `get_mapping()` call on the hot path). Local `compute_statistics` / `_classify_cardinality` / `enrich_bundle_with_statistics` retained as the fallback for the heuristic tier (which has no upstream stats), the stats-only-refresh cache path (cached bundle + drifted row counts), and defensive rebuilds on `partial` / `skipped_no_db`. Net in `schema_acquire.py`: **-190 LOC** (51 insertions / 241 deletions). Verification: full suite 753 passed / 4030 skipped / 1 deselected; integration suite 164 passed / 3845 skipped; `ruff check .` clean; diff gate green across all six fixtures pre-deletion. With PR-3 merged, all seven mapper/analyzer issues are resolved downstream and `arango-cypher-py` carries no local workarounds against `arangodb-schema-analyzer v0.3.0`. |
| 2026-04-20 | **Wave 4q (PR-2/3): fingerprint helpers rewired to upstream `schema_analyzer` v0.3.0 (closes mapper issue #7).** `_shape_fingerprint` and `_full_fingerprint` in `arango_cypher/schema_acquire.py` are now thin wrappers around `schema_analyzer.fingerprint_physical_shape` / `fingerprint_physical_counts`, both called with `exclude_collections={DEFAULT_CACHE_COLLECTION}` to preserve the WAVE_4M_ARCHITECTURE §5 invariant (the cache collection must not perturb its own fingerprint). `_index_digest` (~27 LOC) and `_iter_user_collections` (~24 LOC) deleted entirely — no external callers. Kept the two named fingerprint functions as wrappers so (a) tests in `tests/test_schema_acquire.py` and `tests/test_schema_change_detection.py` that `from arango_cypher.schema_acquire import _shape_fingerprint, _full_fingerprint` continue to resolve, and (b) every caller in this module hits the same exclusion policy without rediscovering it. The `schema_analyzer` import is lazy (inside each wrapper) because `analyzer` is an optional extra; when it is missing, both wrappers fall through to a coarse local `_fallback_fingerprint` (~22 LOC, covers collection set + optional counts — no per-index digest) so the heuristic-only tier keeps working. Net: ~51 LOC removed, ~22 LOC added back for the degraded-mode fallback, zero behavioural change for callers. `arangodb-schema-analyzer>=0.3.0,<0.4.0` added to the `[dev]` extras so CI test jobs can always resolve the upstream path (previously only the opt-in `[analyzer]` extra carried it — fine when it was only used by the mapping path, not by a top-level import). **One-time cache re-key event:** the upstream hash format differs from our local implementation — existing entries in `_arango_schema_cache` miss their fingerprint check exactly once after deployment and are rebuilt on the next `get_mapping()` call. Operationally invisible (rebuilds are routine and sub-second on our workloads); no migration script needed. Because the cache bust naturally invalidates pre-0.3.0 bundles, the two defensive `rmap.get("collectionName")` fallback branches at `schema_acquire.py:1053` (inside `_fixup_dedicated_edges`) and `:1316` (inside `compute_statistics`) — retained in Wave 4q/PR-1 as transitional shims — are also deleted here. Full suite: 753 passed / 4030 skipped / 1 deselected; fingerprint-specific tests (`tests/test_schema_acquire.py::TestSchemaFingerprints`, `tests/test_schema_change_detection.py`, `tests/test_service_schema_status.py`): 63 passed. PR-3 (retire `_fixup_dedicated_edges` + `_backfill_missing_collections` + adopt `metadata.statistics` / `metadata.reconciliation`) follows independently. |
| 2026-04-20 | **Wave 4q (PR-1/3): analyzer v0.3.0 pinned + key-normalization shim deleted (closes mapper issue #6, aka `arango-schema-mapper` #9).** `arangodb-schema-analyzer` dependency bumped from unpinned (resolving to 0.1.0) to `>=0.3.0,<0.4.0`. Upstream v0.3.0 rolls up v0.2.0 (issues #2–#6) plus issue #7 (cheap fingerprint probes). For this PR we only act on #6: `_normalize_analyzer_pm` (~30 LOC, line 1249) and `_normalize_props` (~15 LOC, line 1267) deleted from `arango_cypher/schema_acquire.py`, along with the one call site at `acquire_mapping_bundle` → `_normalize_analyzer_pm(pm)`. Upstream now emits canonical keys directly (`field` instead of `physicalFieldName`, `edgeCollectionName` instead of `collectionName` on relationships — entity `collectionName` is unchanged, so `get_mapping()` / `ArangoSchemaCache` / transpiler call sites still work verbatim). Defensive fallback branches that read `rmap.get("collectionName")` on relationships (schema_acquire.py:1054, 1348 in `compute_statistics`) are retained in this PR as cache-compatibility shims for any pre-0.3.0 persisted bundles; they will retire in PR-2 once the fingerprint re-key naturally busts the persistent cache. Full suite: 753 passed / 4030 skipped / 1 deselected (Anthropic live smoke, sandbox-network-blocked — unchanged from Wave 4p). PR-2 (fingerprints → upstream `fingerprint_physical_shape` / `fingerprint_physical_counts`) and PR-3 (retire `_fixup_dedicated_edges` + `_backfill_missing_collections` + adopt `metadata.statistics` / `metadata.reconciliation`) follow independently. Tracks the [downstream handoff note](https://github.com/ArthurKeen/arango-schema-mapper/pull/11). |
| 2026-04-20 | **Wave 4p: documentation hygiene + measurement gaps closed (no new code paths).** Three small, disjoint pieces of housekeeping triggered by a post-Wave-4o audit that found the plan documents were drifting from reality. **(1) Docs hygiene.** Flipped the PRD implementation-status row "NL-to-Cypher pipeline" from Partial → Done (WP-25 closed 2026-04-18, Wave 4l calibrated both OpenAI and Anthropic baselines, Wave 4o closed the feedback loop). Rewrote the §14 "Local learning" row and §14.1 body so "NL→Cypher few-shot: not started" is replaced with the shipped Wave 4o architecture (SQLite store + BM25-index invalidation listener). Retired the phantom v0.3 execution order block in `docs/implementation_plan.md`: WP-9 through WP-18 all landed 2026-04-11 .. 2026-04-13, but the detailed plan sections were still worded as future work, causing at least one follow-up agent session to spend cycles re-auditing already-shipped features. The v0.3 section now reads as a collapsed historical reference table pointing at the test fixtures and code modules that implement each WP. The live status matrix at the bottom of the implementation plan (and the PRD status overview) remain the single source of truth. **(2) TCK coverage re-measured.** The PRD previously cited "Projected 66.1 % pass rate (clause-focused)" without a measurement date or reproduction path. Ran `python tests/tck/analyze_coverage.py` (translation-only dry run, no DB) against all 3,861 currently-downloaded TCK scenarios: full TCK 32.2 % (1,245 / 3,861), core TCK 54.8 % (1,206 / 2,201, excluding the OOS `expressions/temporal`, `expressions/quantifier`, `clauses/call` categories), clauses-only subset 66.1 % (792 / 1,199 — exactly matching the prior projection; the two are now labeled as the same measurement rather than a guess vs. a number). New `tests/tck/COVERAGE_REPORT.md` captures the full per-category breakdown (`expressions/precedence` at 0 % / 104 is the single biggest expression-category gap) plus the top-15 translation-failure reasons (largest single lever: 1,560 scenarios are rejected at the leading-MATCH constraint — relaxing that would unlock most of the gap in one grammar change). The status-table row in §Implementation status links to the report and marks it as the reproduction of record. **(3) Translation P95 benchmarked.** PRD §7.7 used to read "Current P95: not benchmarked yet. Target is < 50 ms"; neither half was actionable. New `scripts/benchmark_translate.py` runs a 10-case representative corpus (single-hop / two-hop / variable-length / aggregation / WITH pipeline / OPTIONAL MATCH / CREATE, across `movies_pg` + `movies_lpg` + `movies_lpg_naked`) and reports mean / p50 / p95 / p99 / min / max for both cold-cache (unique Cypher per iteration — every call bypasses the WP-26 LRU) and warm-cache paths. Baseline measurement (2026-04-20): **cold P95 peaks at 2.74 ms** (two-hop) — roughly 20–30× below the PRD §2.1 50 ms target. Single-hop P95 is 1.54 ms. Warm-cache P95 is ≤ 0.05 ms across every case, confirming the WP-26 LRU is doing its job for long-running services. `tests/test_translate_perf.py` gates a deliberately-loose regression guard (25 ms cold / 1 ms warm / 50 ms single-hop) behind `RUN_PERF=1`, so the usual CI run is unaffected but a real order-of-magnitude regression (GC pause, N² translator blow-up) trips the gate. PRD §7.7 now carries the measured numbers and points at the benchmark script. No production code changed; 753 regular tests still green + 3 new opt-in perf tests. |
| 2026-04-20 | **Wave 4o: NL → Cypher feedback loop wired from persisted corrections back into the few-shot retriever.** Closes the last obvious gap in WP-25.1: approved `(question, cypher)` pairs now re-enter the BM25 corpus automatically instead of living in a dead-letter table. New `arango_cypher/nl_corrections.py` adds a SQLite-backed store (default: `nl_corrections.db`, configurable via `NL_CORRECTIONS_DB`) with CRUD semantics that mirror the Cypher → AQL `corrections.py`, but kept separate because the two operate at different pipeline layers and have different lookup keys / invalidation triggers. `nl_corrections` publishes an invalidation-listener channel (`register_invalidation_listener` / `unregister_invalidation_listener`); the nl2cypher core registers a `_invalidate_default_fewshot_index` listener on first `_get_default_fewshot_index()` call, so every save/delete forces a lazy rebuild on the very next translation request. Shipped corpora are loaded first, corrections appended afterwards, so a user's correction wins ties against a seed example with the same BM25 score. HTTP surface: `POST /nl-corrections` (save), `GET /nl-corrections` (list), `DELETE /nl-corrections/{id}` (delete one), `DELETE /nl-corrections` (delete all); `POST` returns 400 on empty question/cypher, `DELETE /{id}` returns 404 on missing row. 21 new tests in `tests/test_nl_corrections.py` across four groups: CRUD round-trip (7), listener semantics (5), FewShotIndex integration (3 — save-then-retrieve with real BM25, delete-then-disappear), and HTTP endpoints (5 — full round-trip + error codes + end-to-end "HTTP POST → listener → rebuild" with the real invalidation chain). README gains a "NL → Cypher feedback loop" section alongside the existing Cypher → AQL corrections docs. |
| 2026-04-20 | **Wave 4n: Wave 4m schema-change detection exposed on the HTTP surface.** `GET /schema/status` and `POST /schema/invalidate-cache` landed in `arango_cypher/service.py`, closing the last gap left by Wave 4m (the Python API was complete but not reachable from UI clients, platform orchestrators, or monitoring probes). `GET /schema/status` runs `describe_schema_change(session.db)` and returns the full `SchemaChangeReport` as JSON — four-valued `status` (`unchanged` / `stats_changed` / `shape_changed` / `no_cache`), `unchanged` / `needs_full_rebuild` ergonomic booleans, and the four fingerprints (current + cached, shape + full) so callers can diff them client-side if they want. `POST /schema/invalidate-cache` drops both cache tiers by default; `?persistent=false` drops only the process-local tier (useful for replica-local administrative actions that shouldn't affect shared DB state). Query params `cache_collection` / `cache_key` on both endpoints mirror the Python API for callers who use a non-default cache location. 10 new tests in `tests/test_service_schema_status.py` use FastAPI `dependency_overrides` with a `_MutableFakeDb` wrapper so schema drift can be simulated between HTTP calls without a live DB — covers all four status transitions, persistence toggle, and session auth rejection. README updated with `curl` examples alongside the existing Python examples. Companion to mapper issues #7 (cheap shape-fingerprint probe upstream-port candidate) and #8 (PRD §3.13 revision). |
| 2026-04-18 | **WP-25 complete (NL→Cypher pipeline hardening).** All five sub-packages landed on `main`: WP-25.1 dynamic few-shot retrieval (BM25-backed `FewShotIndex` over shipped `movies/northwind/social` corpora), WP-25.2 pre-flight entity resolution (`EntityResolver` / `ResolvedEntity` with DB-path for both `COLLECTION` and `LABEL` mapping styles, plus a null-resolver fallback), WP-25.3 execution-grounded validation (`explain_aql` + EXPLAIN feedback in the retry loop), WP-25.4 prompt caching (cache-friendly section ordering in `PromptBuilder`, `cached_tokens` propagation from OpenAI `usage.prompt_tokens_details.cached_tokens` into `NL2CypherResult` / `NL2AqlResult` / HTTP responses, Anthropic `cache_control` splitter + live `AnthropicProvider` against `POST /v1/messages`), WP-25.5 eval harness + regression gate (`tests/nl2cypher/eval/{corpus.yml,configs.yml,runner.py,baseline.json}` + `tests/test_nl2cypher_eval_gate.py` with a 5 pp / +20 % / +0.3-retry tolerance policy, gated behind `RUN_NL2CYPHER_EVAL=1`; corpus subsequently grown to 31 cases across `movies_pg` + `northwind_pg`, baseline refreshed against live OpenAI gpt-4o-mini at parse_ok=100% / pattern_match=87.1%). HTTP surface (`/nl2cypher`, `/nl2aql`) now accepts `use_fewshot`, `use_entity_resolution`, `session_token` and returns `cached_tokens` + `retries`. Full unit suite still green (651 passing, 16 skipped). Known follow-ups: refresh `baseline.json` against a live LLM, stand up a real `AnthropicProvider` behind the existing splitter, and grow the eval corpus. |
| 2026-04-17 | **Clarified product scope: service is the product, UI is debug/demo.** Added a "Product scope" key decision to the Executive summary, a primary-product goal in §2, a matching non-goal carving out multi-user workbench features, a scope banner at the head of §4.4 (Cypher Workbench UI), and a "What gets deployed" note in §15 stating the default platform deployment is headless. Triggered by the realization (post-WP-25 scoping) that undirected UI expansion would compete for engineering cycles against the conversion service + NL pipelines, which are the actual deliverable. The UI remains valuable for two things — debugging translations during development, and demoing the service to prospects — but is explicitly **not** a production multi-user workbench: no authn/authz, no multi-tenant isolation, no server-side persistence beyond what the service already stores, no collaboration. Any future "UI-included" deployment variant must be opt-in and separately versioned. |
| 2026-04-17 | **Added §1.2.1 "SOTA techniques, current gaps, and hardening plan"** and scoped **WP-25 (NL→Cypher pipeline hardening)** into five sub-packages. Triggered by research notes in `docs/research/nl2cypher.md` and `docs/research/nl2cypher2aql_analysis.md`. The current `nl2cypher.py` implements the zero-shot baseline (logical-only prompt, ANTLR-based self-healing) — correct but minimal. SOTA has moved to: (1) dynamic few-shot retrieval from a curated NL→Cypher corpus, (2) pre-flight entity resolution for labels and property values, (3) execution-grounded validation via AQL `EXPLAIN` in the retry loop, (4) prompt caching on the schema prefix, (5) evaluation harness + regression gate. WP-25.1/.2/.3/.4 are parallelizable (disjoint modules, single merge point in `nl2cypher.py`); WP-25.5 runs after .1 and .2. Task decomposition / multi-agent orchestration and SLM fine-tuning are explicitly deferred (the `LLMProvider` protocol already accommodates a fine-tuned endpoint when the time comes). The §1.2 invariant — LLM sees only the conceptual schema — is preserved; few-shot examples are conceptual-Cypher and entity resolution only rewrites string literals. Multi-subagent prompts for parallel execution added as "Wave 4" in `docs/agent_prompts.md`. |
| 2026-04-17 | **Naming resolved (§11).** Project name stabilized as `arango-cypher-py`, symmetric with the newly renamed Foxx sibling `arango-cypher-foxx`. The `-py` / `-foxx` suffixes are honest about what each package is (Python out-of-process distribution vs. Foxx in-database microservice) and leave the bare `arango-cypher` name free for a potential future umbrella/spec repo on the `arango-solutions` org. Distribution name in `pyproject.toml`, CLI command, `[project.urls]` (target: `arango-solutions/arango-cypher-py`), READMEs, PRD, and implementation plan all aligned. Python import package remains `arango_cypher` (unchanged — no import breakage). No PyPI migration needed (never published). GitHub org rename (`arango-solutions/arango-cypher` → `arango-solutions/arango-cypher-py`) is pending org-admin action; until that lands, the `pushurl` points at the current URL and GitHub auto-redirects will keep working. Local checkout directory `~/code/arango-cypher-py` unchanged. |
| 2026-04-17 | **Added §15 "Packaging and deployment to the Arango Platform".** Confirmed via pypi.org 404 that `arangodb-schema-analyzer` (declared in the `[analyzer]` extra) is not published to any package index — the only real obstacle to packaging this repo for ServiceMaker. Decision: **fix it upstream by publishing the analyzer**, not by building packaging tooling in this repo. Rejected three alternatives (vendored wheels, git URL deps, monorepo vendoring) as "absorbing a cost that belongs upstream." Rejected a full packaging/deployment CLI in this repo (Typer `package`/`deploy`/`redeploy`/`teardown` subcommands) on scope, release-cadence, token-blast-radius, and deployment-volume grounds; any deployment CLI will live in a separate project or be contributed to ServiceMaker itself. What this repo now owns: a README section with the manual deploy path (three curl commands), a prerequisite checklist, and a CI smoke test that `uv sync` succeeds on the packaged tarball. Corresponding implementation plan entry: WP-19 in `docs/implementation_plan.md` shrunk accordingly. |
| 2026-04-17 | **Neo4j cross-validation harness + translator correctness fixes.** Added a side-by-side harness that seeds the same fixture into Neo4j Community (via `docker-compose.neo4j.yml`) and ArangoDB, runs each Cypher query against both engines, and asserts row-for-row equivalence (column-count match, row-count match, positional compare for `ORDER BY` / multiset compare otherwise, with scalar normalization for int↔float). Shipped two suites: `tests/integration/test_movies_crossvalidate.py` (20/20 pass) and `tests/integration/test_northwind_crossvalidate.py` (14/14 pass), gated behind `RUN_INTEGRATION=1 RUN_CROSS=1`. Cross-module seed guard (`ensure_dataset` in `tests/integration/neo4j_reference.py`) lets both corpora share the single writable Neo4j Community instance. Closed three translator correctness bugs surfaced by the harness, each resolving a previously `divergence`-flagged Movies query: **(1) 3-valued logic on numeric ordered comparisons** — `_compile_expression` now emits `!= null` guards on both operands of `<`/`<=`/`>`/`>=` in `WHERE` so Cypher's NULL-as-false semantics survive translation (new `_is_obvious_non_null` helper skips the guard on literal operands). **(2) `ORDER BY` scope after implicit `COLLECT`** — `_append_return_aggregation` now maps each grouping expression to its `COLLECT` alias, so a Cypher `ORDER BY p.name` after `COLLECT name = p.name` is rewritten to `SORT name` instead of referencing the out-of-scope `p`. **(3) Cypher relationship-uniqueness rule** — `_translate_match_body` emits cross-group `FILTER r_i._id != r_j._id` for single-hop fixed-length non-embedded relationships in the same pattern, so `(p)-[:R]->(m)<-[:R]-(q)` correctly excludes `q == p`. 24 stale goldens resynced via a new `scripts/update_goldens.py` (surgical YAML rewrite that preserves block-literal formatting + bind-var style). Unit suite: **561 passing**. Cross-validation: **34 passing** (20 Movies + 14 Northwind) — zero divergences remaining. |
| 2026-04-15 | **WS-F/G sprint.** Filter pushdown into traversals (PRUNE for variable-length, conservative rules). Relationship uniqueness enforcement (`r1._id != r2._id` for multi-relationship patterns). WITH pipeline from multiple MATCHes verified working + golden tests. rdflib OWL ingestion (`arango_query_core/owl_rdflib.py`, `[owl]` extra). ICIJ Paradise Papers dataset: mapping fixture, download/seed script, 5 query golden tests. Native `shortestPath()` deferred (needs Java for ANTLR regeneration). 494 tests pass. |
| 2026-04-15 | **WS-A/B/C/D sprint.** Full OPTIONAL MATCH with comma-separated pattern parts. Multi-label COLLECTION-style matching (uses primary label + warning). Native `shortestPath()` deferred (needs grammar). Clause outline panel. Sample queries loader (8 built-in queries). Profile-aware warnings (full scan detection). Correspondence hints (Cypher↔AQL hover highlighting). Bidirectional graph editing (add/edit/delete entities/relationships from schema graph). Agentic tool contract expanded: `propose_mapping_overrides`, `explain_translation`, `validate_cypher`, `schema_summary` (8 tools total). TCK convergence: 203 new feature files downloaded (220 total), harness expanded, 3864 scenarios collected, projected 66.1% pass rate on clause-focused subset. 487 tests pass. |
| 2026-04-15 | **WS-7/8/9 sprint.** ANTLR grammar extended with `EXISTS {}` subquery, `FOREACH`, `COUNT {}` subquery — parser regenerated (antlr4 v4.13.2). Relationship MERGE with ON CREATE/ON MATCH SET (DEDICATED_COLLECTION + GENERIC_WITH_TYPE). List comprehensions + pattern comprehensions verified working. Cytoscape.js integration: results graph view + schema graph view with click-to-inspect panels, replacing SVG-based rendering. 22 new golden tests (5 EXISTS/COUNT/FOREACH, 10 relationship MERGE + comprehensions, 7 integration). 503 tests pass (0 failures). TypeScript build clean. |
| 2026-04-15 | **Phase 1-2 completion sprint.** WS-2: All built-in functions verified implemented (toString, toInteger, toFloat, toBoolean, head, tail, last, range, reverse, id, keys, properties, type, labels). 10 new golden tests. WS-1: Regex `=~` verified, `collect()` in RETURN added, named paths verified, EXISTS pattern predicates verified. 16 new golden tests. WS-3: DETACH DELETE bug fixed, MERGE clause implemented (node MERGE with ON CREATE/ON MATCH SET). 17 new golden tests. WS-4: AQL format/prettify button, variable-use highlighting, Cypher hover documentation (20+ keywords, 25+ functions), multi-statement support. WS-5: NL→Cypher prompt leak audit, LLM validation/retry loop with ANTLR parsing, pluggable LLM providers (OpenAI + OpenRouter), enhanced AQL validation. WS-6: Security hardening (_sanitize_error, public mode, rate limiting), index population in heuristic builder, OWL round-trip completion. |
| 2026-04-14 | **Cardinality statistics for query optimization (§14.2).** Compute collection document counts, edge counts, per-entity label counts, avg fan-out/fan-in per edge collection, cardinality pattern classification (1:1, 1:N, N:1, N:M), and selectivity ratio. Statistics stored in `MappingBundle.metadata["statistics"]`, surfaced in schema summary. `MappingResolver` gains `estimated_count()`, `relationship_stats()`, `preferred_traversal_direction()`. NL→AQL prompt enriched with cardinality annotations so LLM starts from selective side. Transpiler uses stats for undirected pattern direction and multi-part MATCH ordering. New `GET /schema/statistics` endpoint. |
| 2026-04-14 | **AQL editor enhancements, local learning, and direct NL→AQL.** AQL editor now fully editable with syntax-directed editing: autocompletion (keywords, functions, snippet templates), scoped variable prediction, document property prediction from mapping, bracket auto-close, code folding, undo/redo history, search/replace. Local learning via corrections store (§14.1): SQLite-backed `corrections.db`, `POST/GET/DELETE /corrections` endpoints, Learn button in UI, corrections management panel. AQL indentation post-processor (`_reindent_aql`). Domain/range inference for PG-style dedicated edges (`_infer_dedicated_edge_endpoints`). NL query history (localStorage). Token usage display for LLM-based NL→Cypher. **Added §1.3 NL→AQL direct path** — opt-in alternative to the two-stage pipeline that bypasses Cypher and generates AQL directly from the LLM using the full physical schema. `POST /nl2aql` endpoint, UI toggle on Ask bar. Updated §4.4.3 (AQL editor spec), §4.4.7 (tech stack), §14.1 (local learning), implementation status tables, roadmap phasing. |
| 2026-04-13 | **Added §1.2 NL→Cypher→AQL two-stage pipeline definition.** This is now a first-class architectural pattern, not a feature bullet: LLM converts NL to Cypher using the ontology as prompt context (same pattern as LangChain's GraphCypherQAChain); deterministic transpiler converts Cypher to AQL. The LLM never sees physical details. Updated executive summary, implementation status table, and v0.3 roadmap to reference §1.2. Added §1.1 Architectural principle: logical schema as query interface. Added §5.7 Index-aware physical mapping model (VCI, persistent, TTL indexes in mapping). Added §7.8 Index-informed transpilation strategy. Added WP-17 (NL2Cypher) to v0.3 roadmap. Added WP-18 (Index-aware transpilation) to v0.3 roadmap. Added VCI and naked-LPG advisory to §5.3. Updated §10 with new WPs. |
| 2026-04-11 | Added §8.2/8.3 openCypher TCK and Neo4j dataset testing requirements. Added implementation status tables to §5.3, §5.4. Expanded §10 Phase 6. Added unified implementation status table. Added §2.1 success criteria. Added §6.4 supported Cypher subset. Added §7.5 error taxonomy. Added §7.6 multi-hop/path semantics. Added §7.7 performance considerations. Expanded security model. Added extension capability matrix. Unified phasing schemes. Resolved open questions. |
| 2026-04-10 | Property-enriched mappings (§5.5), domain/range optimization (§5.5.1), context-aware autocompletion (§4.4.2C), visual mapping graph editor spec (§5.6). |
| 2026-02-17 | Initial PRD. |

## Executive summary
Build a **Python-native Cypher → AQL transpiler** that runs **outside** ArangoDB (CLI/library/service), uses **`arangodb-schema-analyzer`** to produce a **conceptual schema + conceptual→physical mapping** (and optionally OWL Turtle), and can translate Cypher against **pure PG**, **pure LPG**, or **hybrid** physical ArangoDB models.

Key decisions:
- **Product scope.** The deliverable is the Cypher→AQL conversion service (§4.3) and the NL→Cypher→AQL / NL→AQL pipelines (§1.2, §1.3) that run inside it. The Cypher Workbench UI (§4.4) exists to **debug** the service (visualize translations, replay activity, inspect schema mappings) and to **demo** it to prospects. It is not a full-featured multi-user workbench and is **not deployed by default** alongside the service (see §15).
- **New project**: the Foxx implementation (originally named `arango-cypher`, renamed 2026-04-17 to `arango-cypher-foxx`) remains stable; this is a separate Python project published as `arango-cypher-py`, the symmetric Python sibling (§11).
- **Name**: repo `arango-cypher-py` (target GitHub location `arango-solutions/arango-cypher-py`; rename of the existing `arango-solutions/arango-cypher` repo is pending org-admin action), Python import package `arango_cypher`, distribution name `arango-cypher-py`, CLI command `arango-cypher-py`.
- **Schema mapping**: depend on `arangodb-schema-analyzer` as a library and optionally consume/produce OWL Turtle via its tool contract.
- **NL → Cypher → AQL** (two-stage pipeline, §1.2): use an LLM to convert natural language to Cypher (passing the ontology/conceptual schema as prompt context, same pattern as LangChain's `GraphCypherQAChain`), then use the **deterministic** transpiler to convert Cypher to AQL. The LLM never sees collection names, type fields, or AQL. The transpiler never uses an LLM. This separation is a first-class architectural constraint.
- **NL → AQL** (direct path, §1.3): optionally bypass the intermediate Cypher representation and have the LLM generate AQL directly. The LLM is given the full physical schema (collection names, edge collections, field names, type discriminators) so it can produce valid AQL. This is useful when the Cypher transpiler does not yet support a required construct, or when the user wants to leverage AQL-specific features.
- **Parsing (as implemented)**: ANTLR4-generated Python parser from the openCypher grammar in-repo (`grammar/Cypher.g4`). Re-evaluating `libcypher-parser-python` remains an optional future migration if native wheels and AST mapping prove worthwhile (see S6).
- **Agentic workflow** (optional): provide a stable JSON-in/JSON-out "tool" interface for translate/explain that can be used in agent pipelines, but keep translation correctness deterministic.

### Implementation status overview

Single source of truth for what is built, partial, or planned. Updated 2026-04-17 (Neo4j cross-validation).

| Capability | Status | Details |
|------------|--------|---------|
| **ANTLR4 parser + normalized AST** | Done | `grammar/Cypher.g4`, `arango_cypher/parser.py`, `arango_cypher/_antlr/` |
| **Core translation (MATCH/WHERE/RETURN)** | Done | Single node, single hop, multi-hop, variable-length, inline property filters, boolean/comparison/string predicates, ORDER BY, LIMIT, SKIP |
| **WITH pipeline + aggregation** | Done | Single/multiple leading MATCHes + WITH stages; aggregation in both WITH and RETURN; COLLECT cannot mix with other aggregates |
| **OPTIONAL MATCH** | Done | Multi-hop chains, node-only, comma-separated multiple pattern parts |
| **UNWIND** | Done | Standalone and in-query |
| **CASE expressions** | Done | Simple and generic forms |
| **UNION / UNION ALL** | Done | Via AQL subqueries |
| **Multi-label matching** | Done | LABEL-style: full support. COLLECTION-style: uses primary label with warning. |
| **Parameters (`$param`)** | Done | Positional params rejected |
| **Write clauses (CREATE/SET/DELETE)** | Done | CREATE, SET, DELETE, DETACH DELETE, MERGE (nodes + relationships with ON CREATE/ON MATCH SET) |
| **Named paths / path functions** | Done | `p = (a)-[:R]->(b)`, `length(p)`, `nodes(p)`, `relationships(p)` |
| **List/pattern comprehensions** | Done | List comprehensions `[x IN list WHERE filter | expr]`; pattern comprehensions `[(a)-[:R]->(b) | expr]` |
| **EXISTS / regex `=~`** | Done | Regex `=~` done; pattern predicates supported; `EXISTS { }` subquery implemented (grammar extended + transpiler) |
| **FOREACH / COUNT subquery** | Done | FOREACH with updating clauses; COUNT { } subquery via grammar extension |
| **`arango.*` extension registry** | Done | search, vector, geo, document functions + procedures (shortest_path, k_shortest_paths, fulltext, near, within) |
| **MappingResolver** | Done | Entity/relationship resolution, property resolution, domain/range inference, IS_SAME_COLLECTION optimization |
| **Schema analyzer integration** | Done | `acquire_mapping_bundle(db)`, `get_mapping(db)`, `classify_schema(db)` implemented. Analyzer is the **primary tier** for all schema types (PG, LPG, hybrid) since v0.1.0 (28/28 acceptance tests). Heuristic fallback when analyzer not installed. See §5.2.1. |
| **Heuristic fallback correctness (hybrid schemas)** | **Done** | WP-27 (commit `0313395`, 2026-04-23) tier-splits `_DOC_TYPE_FIELDS` and applies cardinality + class-like-value rules to tier-2 candidates (`label`, `labels`, `kind`); rejection reasons captured in `metadata.heuristic_notes`. WP-28 (commit `ac6eaff`, 2026-04-23) emits an `ANALYZER_NOT_INSTALLED` warning on heuristic-fallback bundles, refuses service startup without analyzer (unless `ARANGO_CYPHER_ALLOW_HEURISTIC=1`), retries the analyzer on cache miss when newly importable, and adds `POST /schema/force-reacquire` plus the UI `SchemaWarningBanner`. Closes D1 + D2 of [`schema_inference_bugfix_prd.md`](./schema_inference_bugfix_prd.md). |
| **NL → Translate feedback loop** | **Done** | WP-29 (commit `d4a871f`, 2026-04-24) adds `_escape_label`, pre-escapes labels in `_build_schema_summary` + `_aql.py`, teaches `_SYSTEM_PROMPT` to backtick-escape non-identifier labels, and makes `_call_llm_with_retry` fail closed with `method="validation_failed"` on retry exhaustion (UI renders a red banner instead of writing invalid Cypher to the editor). WP-30 (commit `618aace`, 2026-04-24) adds the `editorCypherSource` reducer + regenerate-from-NL banner action so translate-time parse errors on NL-generated Cypher have a one-click recovery path with `retry_context` plumbed back to `PromptBuilder`. Transpiler's `_strip_label_backticks` (WP-27) closes D5. Closes D3 + D4 + D6 of [`schema_inference_bugfix_prd.md`](./schema_inference_bugfix_prd.md). The PRD §11.4 closeout E2E walkthrough on `ic-knowledge-graph-temporal` is the last manual step before the PRD itself flips OPEN → CLOSED. |
| **OWL Turtle round-trip** | Done | `MappingBundle.owl_turtle`; `/mapping/export-owl` and `/mapping/import-owl`; WS-6 completed import/`owlTurtle` round-trip path in the service and mapping loader. |
| **FastAPI service** | Done | 16+ endpoints shipped including `/nl2cypher`, `/schema/introspect`, `/mapping/export-owl`, `/mapping/import-owl`, `/suggest-indexes` |
| **Cypher Workbench UI** | Partial | Cypher editor (syntax highlighting, hover docs for keywords/functions, context-aware autocompletion, variable-use highlighting, parameter panel, multi-statement). AQL editor (**editable**, syntax-directed editing with autocompletion, snippet templates, format/prettify, scoped variable prediction, document property prediction, bracket auto-close, code folding, history, search/replace — see §4.4.3). Results panel (table/JSON/graph/explain/profile tabs). JSON mapping panel + schema graph view. Connection dialog with auto-introspect + database selector. NL2Cypher "Ask" bar with NL query history + token usage display. **Query history** with optional bounded result snapshots (restore Cypher/AQL/bind vars/rows without re-run), per-connection filter, `localStorage` quota defense (2026-05-26). Export (CSV/JSON). **Local learning** (Learn button, corrections management panel — see §14.1). |
| **UI: results graph view** | Done | Cytoscape.js force-directed graph from `_id`/`_from`/`_to` fields. Click-to-inspect node properties panel. Dark theme. |
| **UI: visual mapping editor** | Done | Cytoscape.js schema graph with bidirectional editing (add/edit/delete entities & relationships via context menu). Changes sync to JSON editor. |
| **UI: parameter binding panel** | Done | Auto-detects `$param` tokens, JSON value entry, localStorage persistence |
| **UI: query history** | Done | Multi-entry history with persistence |
| **CLI (`translate`/`run`/`mapping`/`doctor`)** | Done | All 4 subcommands implemented via Typer + Rich. Requires `[cli]` optional extra. |
| **Index-aware physical mapping** | Partial | `IndexInfo` dataclass, `resolve_indexes()`, `has_vci()` on `MappingResolver`. VCI warning in transpiler. Heuristic builder populates indexes from DB. |
| **NL-to-Cypher pipeline** | Done | §1.2: LLM path (OpenAI + Anthropic + OpenRouter) + rule-based fallback. WP-25 hardening complete (2026-04-18 / Wave 4l): dynamic few-shot retrieval (WP-25.1, BM25 over shipped corpora + user-approved corrections), pre-flight entity resolution (WP-25.2, exact + contains + `LEVENSHTEIN_DISTANCE` fuzzy), execution-grounded validation (WP-25.3, `EXPLAIN`-backed self-heal), prompt caching (WP-25.4, Anthropic end-to-end proven at 99.5 % cache-read), and evaluation harness + nightly regression gate (WP-25.5, 31 cases × 2 providers, pattern_match 93.5 % OpenAI gpt-4o-mini / 100 % Anthropic claude-haiku-4-5). NL → Cypher feedback loop via `POST /nl-corrections` → BM25 re-entry (Wave 4o, 2026-04-20). Prompt leak audit + ANTLR validation/retry loop; enhanced AQL validation. Domain/range inference for PG-style dedicated edges (`_infer_dedicated_edge_endpoints`). Token usage tracking (prompt/completion/cached/total). UI displays elapsed time + tokens. NL query history (localStorage). |
| **NL-to-AQL direct path** | Done | §1.3: Direct NL→AQL via LLM with full physical schema context. `POST /nl2aql` endpoint. UI toggle on Ask bar (Cypher vs AQL mode). `_build_physical_schema_summary()` for schema context. Requires LLM — no rule-based fallback. |
| **Agentic tool contract (`translate_tool`)** | Done | 8 tools: `cypher_translate`, `suggest_indexes`, `explain_mapping`, `cypher_profile`, `propose_mapping_overrides`, `explain_translation`, `validate_cypher`, `schema_summary`. `/tools/schemas` + `/tools/call` dispatch. |
| **Golden tests** | Done | YAML fixtures in `tests/fixtures/cases/` and `tests/fixtures/cases_v03/` |
| **Integration tests (datasets)** | Done | Movies full dataset (~170 nodes, 20-query corpus, PG + LPG variants), Northwind (14-query corpus), social dataset (PG/LPG/hybrid) |
| **Neo4j cross-validation harness** | Done | `docker-compose.neo4j.yml` + `tests/integration/neo4j_reference.py` (driver, `seed_neo4j_movies`, generic `seed_neo4j_pg`, `seed_neo4j_northwind`, cross-module `ensure_dataset` guard). Row-for-row equivalence asserted by `assert_result_equivalent` (column-count, row-count, ordered vs multiset compare, scalar normalization). Two suites passing end-to-end: Movies 20/20, Northwind 14/14. Gated behind `RUN_INTEGRATION=1 RUN_CROSS=1`. See §8.3.1. |
| **openCypher TCK harness** | Partial | 220 feature files, 3,861 scenarios collected. **Measured 2026-04-20** (translation-only dry run via `python tests/tck/analyze_coverage.py`): full TCK 32.2 % (1,245 / 3,861), core TCK 54.8 % (1,206 / 2,201, excluding OOS categories `expressions/temporal` + `expressions/quantifier` + `clauses/call`), clauses-only subset 66.1 % (792 / 1,199, confirms the prior projection). Requires live ArangoDB for the full runner-based measurement (harness + seeding + result normalization); the translation-only number is the primary upper bound tracked by CI. Full category breakdown and prioritized uplift backlog in [`tests/tck/COVERAGE_REPORT.md`](../tests/tck/COVERAGE_REPORT.md). Largest single lever (not on the current plan): relaxing the leading-MATCH constraint would unblock ≈ 1,560 scenarios currently rejected at the parser level. |

---

## 1) Problem statement
ArangoDB supports multiple physical graph modeling styles:
- **PG-ish**: "types-as-collections" (one vertex collection per label/type; one edge collection per relationship type, etc.)
- **LPG-ish**: "generic collections + type field" (single vertex collection with `type`, single edge collection with `type`, etc.)
- **Hybrid**: a mixture of both across entities/relationships, sometimes within the same query path.

Cypher is a conceptual (label/type-centric) query language. To execute on ArangoDB, we need:
- a **conceptual schema** (labels, relationship types, properties)
- a **mapping** to physical collections and type fields
- a **transpiler** that generates safe, performant AQL for the physical layout (including hybrid paths)

The Foxx version runs inside ArangoDB coordinators, which constrains dependencies and runtime. A Python implementation enables:
- richer parsing toolchains
- strong typing and better testing ergonomics
- easy integration into notebooks, CLIs, services, and "agentic workflows"

### 1.1 Architectural principle: logical schema as query interface

**All Cypher queries — whether written by hand, generated by an LLM, or produced by a NL-to-Cypher pipeline — are expressed against the *logical* (conceptual) schema, never against the physical ArangoDB layout.**

This is a first-class architectural constraint, not merely a design guideline. It motivates the entire system's layering:

```
┌────────────────────────────────────────────────────────┐
│  Query Authors  (human, LLM, NL2Cypher)                │
│  ↓ express queries using conceptual labels/types       │
├────────────────────────────────────────────────────────┤
│  Conceptual (Logical) Schema                           │
│  - Entity labels: Person, Movie, Company               │
│  - Relationship types: ACTED_IN, DIRECTED, KNOWS       │
│  - Properties: name, born, title, released             │
│  Source: arangodb-schema-analyzer (reverse-engineers    │
│          ontology from physical schema) or user-supplied│
├────────────────────────────────────────────────────────┤
│  Mapping Layer  (MappingBundle)                        │
│  - Conceptual → Physical resolution                    │
│  - Entity style (COLLECTION vs LABEL)                  │
│  - Relationship style (DEDICATED vs GENERIC_WITH_TYPE) │
│  - Index metadata (VCI, persistent, fulltext, geo)     │
├────────────────────────────────────────────────────────┤
│  Transpiler  (translate_v0.py)                         │
│  - Reads conceptual schema + physical mapping          │
│  - Generates safe, performant AQL for the actual layout│
│  - Leverages index metadata for optimization decisions │
├────────────────────────────────────────────────────────┤
│  Physical ArangoDB  (PG, LPG, or hybrid)               │
│  - Collections, edge collections, indexes              │
│  - The query author never needs to know this layer     │
└────────────────────────────────────────────────────────┘
```

**Consequences:**

1. **Query portability**: the same Cypher query works against PG, LPG, and hybrid physical layouts — the mapping layer absorbs the difference.
2. **Schema analyzer is canonical**: `arangodb-schema-analyzer` reverse-engineers the ontology (conceptual schema) from the physical schema. This is the **single source of truth** for what labels, types, and properties exist. The no-workaround policy (§5.2) ensures the analyzer improves at the source.
3. **NL-to-Cypher operates at the logical level**: any NL2Cypher pipeline receives the conceptual schema (entity labels, relationship types, property names) and generates Cypher against it. The transpiler handles the rest — the LLM never sees collection names, type fields, or AQL. See §1.2 for the full pipeline definition.
4. **Index information lives in the mapping, not in queries**: the physical mapping carries index metadata (§5.7). The transpiler uses this to make optimization decisions (edge filters vs vertex filters in traversals, index hints) without exposing physical details to the query author.

### 1.2 NL → Cypher → AQL: the two-stage pipeline

**This is the system's defining architectural pattern.** The pipeline has exactly two stages, with a clean separation of concerns:

```
┌──────────────────────────────────────────────────────────────┐
│  STAGE 1: NL → Cypher  (LLM-based, non-deterministic)       │
│                                                              │
│  Input:  Natural language question from the user             │
│  Context: Conceptual schema (ontology) —                     │
│           entity labels, relationship types, properties,     │
│           domain/range constraints                           │
│  Engine: LLM (OpenAI, Anthropic, local model, or any         │
│           provider — pluggable)                              │
│  Pattern: Same as LangChain's GraphCypherQAChain —           │
│           inject the ontology into the LLM prompt,           │
│           LLM generates Cypher against conceptual labels     │
│  Output: Cypher query expressed in conceptual terms           │
│          e.g. MATCH (p:Person)-[:ACTED_IN]->(m:Movie)        │
│               WHERE m.title CONTAINS "Matrix"                │
│               RETURN p.name                                  │
│                                                              │
│  The LLM NEVER sees:                                         │
│  - Physical collection names (nodes, edges, persons, movies) │
│  - Type discriminator fields (type, relation)                │
│  - AQL syntax                                                │
│  - Physical model style (PG, LPG, hybrid)                    │
├──────────────────────────────────────────────────────────────┤
│  STAGE 2: Cypher → AQL  (deterministic, algorithmic)         │
│                                                              │
│  Input:  Cypher query (from Stage 1, or hand-written)        │
│  Context: Conceptual schema + physical mapping               │
│           (MappingBundle from schema analyzer or heuristics)  │
│  Engine: translate_v0.py — ANTLR4 parser + AQL renderer      │
│  Output: AQL query + bind variables, ready to execute        │
│          against ArangoDB                                     │
│                                                              │
│  This stage is ALWAYS deterministic:                         │
│  - Same Cypher + same mapping = same AQL, every time         │
│  - No LLM involvement                                        │
│  - No network calls (other than optional AQL execution)      │
└──────────────────────────────────────────────────────────────┘
```

**Why Cypher as the intermediate representation — not AQL:**

- **LLM priors**: LLMs have extensive training data for Cypher (Neo4j's query language). They generate more accurate Cypher than AQL because Cypher is vastly more represented in training corpora.
- **Ontology alignment**: Cypher's label/type system (`Person`, `ACTED_IN`, `Movie`) maps directly to ontology classes and object properties. The LLM reasons in domain terms, not physical storage terms.
- **Portability**: Cypher generated against a conceptual schema works unchanged across PG, LPG, and hybrid physical models. If we generated AQL directly, the LLM would need to know the physical model — breaking the abstraction.
- **Ecosystem**: this is the same pattern used by Neo4j's Text2Cypher, LangChain's `GraphCypherQAChain`, and LlamaIndex's `KnowledgeGraphQueryEngine`. We are not inventing a new pattern; we are implementing the established one with ArangoDB as the backend instead of Neo4j.

**What the LLM prompt contains** (Stage 1 schema context):

```
Entity types:
  - Person (properties: name, born)
  - Movie (properties: title, released, tagline)

Relationship types:
  - ACTED_IN (Person → Movie, properties: roles)
  - DIRECTED (Person → Movie)
  - FOLLOWS (Person → Person)
  - PRODUCED (Person → Movie)
  - REVIEWED (Person → Movie, properties: summary, rating)
  - WROTE (Person → Movie)
```

This is the conceptual schema — the same ontology regardless of whether the data is stored in 8 collections (PG), 2 collections (LPG), or a mix (hybrid). The LLM generates Cypher against these labels. The transpiler resolves the physical details.

**Implementation requirements:**

1. **LLM provider is pluggable** — the system must not be hard-coded to any single provider. Support OpenAI, Anthropic, and local models via a provider interface.
2. **Schema context builder** — a function that takes a `MappingBundle` and produces the text prompt fragment describing entity types, relationship types, properties, and domain/range constraints.
3. **No rule-based fallback as primary** — a lightweight rule-based fallback may exist for demo/offline scenarios, but it is not the primary NL2Cypher path. The LLM-based path is the product.
4. **Validation loop** — after the LLM generates Cypher, the transpiler parses it. If parsing fails, the error is fed back to the LLM for a retry (similar to LlamaIndex's self-correction workflow).
5. **The transpiler is the primary path to AQL** — the deterministic transpiler from Cypher is the primary path. A direct NL→AQL mode exists as an alternative (§1.3) but is explicitly opt-in.

### 1.2.1 SOTA techniques, current gaps, and hardening plan

The §1.2 pipeline as implemented today (`arango_cypher/nl2cypher.py`) is a **correct but minimal** instantiation of the pattern: zero-shot system prompt containing the conceptual schema, single LLM call, one retry on ANTLR parse failure. The 2025-2026 state of the art for Text2Cypher has moved substantially past this baseline (see research notes in `docs/research/nl2cypher.md` and `docs/research/nl2cypher2aql_analysis.md`). This section records what SOTA looks like, what we already have, and exactly which gaps we intend to close.

**SOTA Text2Cypher reference architecture** (the pattern Neo4j, LangChain, and LlamaIndex converge on):

1. **Extract** entities from the user query (lightweight NER / rule-based / small LLM).
2. **Resolve** those entities against the database using vector search or an inverted index (so "Forest Gump" is rewritten to "Forrest Gump" before the main LLM call).
3. **Retrieve** dynamic few-shot (NL question, Cypher answer) examples whose intent matches the new question (BM25 or embedding-similarity over a curated corpus).
4. **Generate** the Cypher query — logical schema + resolved entities + retrieved examples in the prompt.
5. **Execute & validate** via an agent loop: syntactic parse check, then a low-cost semantic check (e.g. `EXPLAIN` against the live database) so collection/property hallucinations trigger a corrective retry rather than surfacing to the user.

**Gap analysis against the current implementation:**

| SOTA technique | Current state (`nl2cypher.py`) | Gap |
|----------------|--------------------------------|-----|
| Logical-only LLM prompt | `_build_schema_summary()` strips physical details; numeric-like / sentinel hints included | **Done** (core §1.2 invariant). |
| Self-healing retry | `_call_llm_with_retry()` retries on ANTLR parse failure, feeds error back to LLM | **Partial** — syntactic only. No semantic / execution-grounded retry. |
| Dynamic few-shot | `_SYSTEM_PROMPT` is zero-shot; no retrieval layer | **Missing.** This is the highest-ROI upgrade — we already have three curated corpora (`tests/fixtures/datasets/{movies,northwind,social}/query-corpus.yml`) whose `description`→`cypher` pairs are exactly what a few-shot retriever wants. |
| Entity resolution | LLM guesses string literals; `_fix_labels()` post-hoc rewrites hallucinated labels to mapping terms via fuzzy/role-synonym match | **Partial** — labels only. Property values (names, titles) are not resolved pre-LLM, so typos and variant spellings fail silently at execution time. |
| Execution-grounded validation | None | **Missing.** The retry loop stops at ANTLR. A Cypher query that parses but references a nonexistent label, property, or value will only surface the failure after AQL execution. |
| Prompt caching | None | **Missing.** The full schema is re-sent on every request. For schemas of any size this is wasteful; both OpenAI prompt caching (automatic above a token threshold) and Anthropic `cache_control` blocks are trivially available and cut token cost significantly. |
| Task decomposition / multi-agent | None | **Deferred.** Complex multi-subquery questions ("top 3 movies by each director, excluding documentaries") are a known weakness of single-shot generation. Decomposition is out of scope for the initial hardening pass; re-evaluate after the evaluation harness (below) reveals whether we are ceiling-bound on single-shot generation. |
| Small-model fine-tuning (SLM) | None | **Out of scope for this repo.** Fine-tuning Qwen-2.5 / Llama-3-8B on a curated ArangoDB Text2Cypher corpus is the 2026 SOTA for production latency/cost, but belongs in a separate research project with a GPU training pipeline and a versioned model registry. Reserve the hook: the `LLMProvider` protocol already lets a fine-tuned endpoint drop in unchanged. |

**The direct NL→AQL path (§1.3) is structurally disadvantaged.** LLMs produce noticeably weaker AQL than Cypher because AQL is under-represented in training data and is a procedural/physical language rather than a declarative/logical one (see `docs/research/nl2cypher2aql_analysis.md` for the full argument). The conclusion is **not** to remove §1.3 — it remains the escape hatch when the transpiler lacks a construct — but to **double down on §1.2 as the primary path** and to apply the SOTA upgrades there first. The same techniques (few-shot, entity resolution, execution-grounded validation) would help §1.3 too and should be layered in once §1.2 is hardened.

**Hardening plan (WP-25 in the implementation plan, scoped for multi-subagent execution):**

| Sub-package | Technique | Effort | Impact | Depends on | Status |
|-------------|-----------|--------|--------|-----------|--------|
| **WP-25.1** | Dynamic few-shot retrieval | Low (3–5 d) | High | — | **Done (2026-04-18)** |
| **WP-25.2** | Pre-flight entity resolution (labels + values) | Medium (5–7 d) | High | ArangoSearch view (optional; BM25/regex fallback when absent) | **Done (2026-04-18)** |
| **WP-25.3** | Execution-grounded validation loop (AQL `EXPLAIN`) | Medium (5 d) | Medium | WP-25.1 (for non-regression comparison) | **Done (2026-04-18)** |
| **WP-25.4** | Prompt caching (OpenAI + Anthropic) | Low (2 d) | Cost-only (no accuracy gain) | — | **Done (2026-04-18)** — OpenAI cached-token telemetry live; `AnthropicProvider` wired end-to-end against `POST /v1/messages` with `cache_control: {type: "ephemeral"}` splits, `cache_read_input_tokens` propagated to `cached_tokens`, and registered in `get_llm_provider()` (explicit `LLM_PROVIDER=anthropic` and auto-detect on `ANTHROPIC_API_KEY`). |
| **WP-25.5** | Evaluation harness & regression gate | Medium (3–4 d) | Meta (measures all of the above) | WP-25.1, WP-25.2 | **Done (2026-04-18)** — corpus (31 cases across `movies_pg` + `northwind_pg`, 5 categories) + configs + runner + CLI shipped. `baseline.json` refreshed against live OpenAI gpt-4o-mini under the `full` config: parse_ok=100%, pattern_match=87.1% (baseline / few_shot_bait / hallucination_bait / multi_hop = 100%; typo = 33%, the predicted gap when no DB is wired so WP-25.2 entity resolution falls back to no-op). Live gate (`RUN_NL2CYPHER_EVAL=1`) self-passes against the committed baseline. |

WP-25.1 / .2 / .3 / .4 executed in parallel during the Wave-4 rollout (disjoint modules with a single merge point in `arango_cypher/nl2cypher/_core.py`'s `PromptBuilder`). WP-25.5 followed once .1 and .2 had landed. Ready-to-launch sub-agent prompts remain archived in `docs/agent_prompts.md` under "Wave 4".

**Follow-ups carried out of WP-25:**

- ~~Refresh `tests/nl2cypher/eval/baseline.json` with a real-LLM report~~ Done 2026-04-18 (OpenAI gpt-4o-mini, `full` config, 31 cases, parse_ok=100%, pattern_match=87.1%). ~~Remaining: turn the gate on in nightly CI (currently opt-in via `RUN_NL2CYPHER_EVAL=1`).~~ Done 2026-04-18 (Wave 4k): new `.github/workflows/nl2cypher-eval.yml` — separate workflow from `ci.yml` so paid LLM calls run on a `cron: "0 6 * * *"` schedule (plus `workflow_dispatch` for manual refreshes), spins up the same `arangodb/arangodb:3.11` service as the `integration` job, seeds `nl2cypher_eval_movies_pg` + `northwind_cross_test` via the existing `tests/integration/datasets.py` seeders, then runs `tests/test_nl2cypher_eval_gate.py::test_gate_against_baseline` with `RUN_NL2CYPHER_EVAL=1 NL2CYPHER_EVAL_USE_DB=1`. Required GitHub repo secrets: **`OPENAI_API_KEY`** (or **`OPENROUTER_API_KEY`** — the test self-skips and exits 0 if neither is set, so unforked clones don't red-light). Estimated cost: ~$0.05 per nightly run at gpt-4o-mini × 31 cases. Failed runs upload `tests/nl2cypher/eval/reports/` as a 14-day artifact for triage. The workflow does **not** block PRs (intentionally — model variance vs. a 5 pp tolerance is acceptable for a regression signal but not for a merge gate). **Wave 4l (2026-04-20)** extended the workflow to a `strategy.matrix` of two providers — `openai` (calibrated against `baseline.json`) and `anthropic` (calibrated against `baseline.anthropic.json`, using `claude-haiku-4-5`). Each row is independent (`fail-fast: false`) so a single-provider regression doesn't mask the other. Baseline selection is wired via the new `NL2CYPHER_EVAL_PROVIDER` env var, which `test_gate_against_baseline` reads through the new `_baseline_path_for_provider()` helper (6 new unit tests pin the contract). Extra optional secret: **`ANTHROPIC_API_KEY`** (~$0.10 / nightly at Haiku 4.5 × 31 cases; row self-skips if unset).
- ~~Implement the Anthropic provider behind the existing `AnthropicProvider` stub and verify `cache_read_input_tokens` propagates through to `cached_tokens`.~~ Landed on `main` after WP-25 closure; the live cache hit is verified by the opt-in smoke test `tests/test_nl2cypher_caching.py::TestAnthropicLiveSmoke` (gated on `ANTHROPIC_API_KEY`).
- ~~Cross-provider baseline run with Anthropic + cache-hit measurement.~~ Done 2026-04-20 (Wave 4l): full live sweep against Anthropic `claude-haiku-4-5` (31 cases, both seeded fixtures, WP-25.2 + WP-25.3 engaged). Headline: **parse_ok 100% / pattern_match 100% / retries_mean 0** — every category (baseline / few_shot_bait / typo / hallucination_bait / multi_hop) at 100%, beating the OpenAI gpt-4o-mini baseline (93.5% pattern_match) by 6.5 pp. Notably the `typo` category lifts from 67% to 100% out-of-the-box because Claude Haiku handles edit-distance fuzziness in the generation step itself (independent of the WP-25.2 `LEVENSHTEIN_DISTANCE` resolver). Mean tokens 522 vs. 457 for OpenAI; mean latency 3.4s vs. 3.2s — both within noise. Committed as `tests/nl2cypher/eval/baseline.anthropic.json`. **Cache-hit measurement:** Haiku 4.5 requires a 4096-token minimum cacheable prefix (per Anthropic docs), and our prompts are ~500 tokens — so the Haiku eval reports `cached_tokens_mean=0`. End-to-end cache plumbing is separately **proven** with `claude-sonnet-4-5` (1024-token floor): cold call served 0 cached, warm call with an identical 2346-token cacheable prefix served **2346/2357 input tokens from cache (99.5%)**, confirming `split_system_for_anthropic_cache` + `cache_read_input_tokens` → `cached_tokens` propagation works.
- ~~Expand the eval corpus beyond the initial 13 cases across movies / northwind / social datasets.~~ Grown to **31 cases** (movies_pg: 21, northwind_pg: 10) on 2026-04-18: 9 baseline, 6 few_shot_bait, 6 typo, 7 hallucination_bait, 3 multi_hop. Further growth (social variant + cross-mapping) is welcome but no longer blocking the gate's regression signal.
- ~~Wire a live ArangoDB into `runner._main` so the `full` config actually exercises WP-25.2 and WP-25.3.~~ Done 2026-04-18 (Wave 4g): `tests/nl2cypher/eval/runner.py` gained `open_eval_db_handles()` (env-var-driven, per-fixture map keyed off `NL2CYPHER_EVAL_<FIXTURE>_DB`, defaults `nl2cypher_eval_movies_pg` / `northwind_cross_test`), `run_eval`/`run_case` accept `db_for_fixture=`, and the CLI gained `--with-db`. The live gate honors `NL2CYPHER_EVAL_USE_DB=1`. **Bug fix in the same commit:** `db` was previously gated on `use_execution_grounded` only, so the `few_shot_plus_entity` config silently skipped WP-25.2; the gate is now `use_execution_grounded OR use_entity_resolution`, restoring the intended config semantics.
- ~~Extend `EntityResolver` with fuzzy/edit-distance matching so typos like "Forest Gump" → "Forrest Gump" actually resolve against a live DB.~~ Done 2026-04-18 (Wave 4h): `_query_label_property` AQL now combines four scoring strategies — exact (1.00), contains (0.85), reverse-contains (0.70), and a new normalized `LEVENSHTEIN_DISTANCE` branch (≤ 0.90, gated by a configurable `fuzzy_threshold` defaulting to 0.7). Live verification on the seeded `nl2cypher_eval_movies_pg` DB resolves "Forest Gump" → "Forrest Gump" (0.82), "Toms Hanks" → "Tom Hanks" (0.81), and "Keenu Reeves" → "Keanu Reeves" (0.82) without false positives on truly absent entities ("Stephen Spielbreg" stays unresolved). 4 new unit tests pin the bind-var contract and the AQL-includes-LEVENSHTEIN invariant.
- ~~Refresh `baseline.json` once Wave 4g + 4h land so the typo category lifts in one step.~~ Done 2026-04-18 (Wave 4i): live OpenAI gpt-4o-mini run with `--with-db` against the seeded fixtures + fuzzy resolver. Headline: parse_ok=100% (unchanged), pattern_match=**90.3%** (↑ from 87.1%), typo=**66.7%** (↑ from 33.3%, +33.4 pp), retries_mean=0.03. One model-variance regression noted: hallucination_bait dipped to 85.7% (1 case of 7 — `eval_030` "List all actors" produced a less-precise `MATCH (p:Person) RETURN p.name` instead of the expected `Person.*ACTED_IN` join); within the gate's 5 pp tolerance and not reproducible across runs, but logged here so a future tightening can revisit.
- ~~Lift `eval_030`-style hallucination_bait robustness via few-shot enrichment.~~ Done 2026-04-18 (Wave 4j): added three canonical role-noun examples to `arango_cypher/nl2cypher/corpora/movies.yml` ("List all actors?", "Who are all the directors?", "List every writer in the database?") so the BM25 retriever surfaces the `Person + role-edge + DISTINCT` pattern when the user asks for "actors / directors / writers / producers". 5×5 replay of `eval_030` confirms the lift is deterministic, not noise. Refreshed baseline: pattern_match **93.5%** (↑ from 90.3%), hallucination_bait **100%** (recovered, ↑ from 85.7%), retries_mean **0** (↓ from 0.03). Live gate self-passes in 93 s.
- ~~Public schema-change detection API + persistent mapping cache so long-running services can skip unnecessary re-introspection.~~ Done 2026-04-20 (Wave 4m): replaced the single `_schema_fingerprint` (which hashed `name:type:count:idx_count` — and thus flipped on every row insert, *and* missed index shape changes that didn't alter the index count) with two orthogonal fingerprints: **shape** (collections + types + full index digests including VCI / `deduplicate` flags per Upstream Issue 01) and **full** (shape + per-collection row counts). New public API: `describe_schema_change(db) → SchemaChangeReport` for cheap read-only probes (≈ 20 ms for a 50-collection DB, no document sampling or AQL `COLLECT`), and `invalidate_cache(db)` for explicit cache busts. `get_mapping()` gained three routes: **unchanged** → serve from cache as before; **stats_changed** (shape stable, counts drift) → reuse cached conceptual + physical mapping and refresh only cardinality statistics (~50 ms vs. ~2–30 s for a full re-introspection); **shape_changed** / **no_cache** → full re-introspection. Added a persistent cache layer (`arango_cypher/schema_cache.py::ArangoSchemaCache`) backed by a user-land collection (default `arango_cypher_schema_cache`, excluded from its own fingerprints to avoid self-invalidation) and gated by `CACHE_SCHEMA_VERSION` so a future format bump silently ignores stale docs. Disable persistence per-call with `cache_collection=None` (for read-only DB users); force a rebuild with `force_refresh=True`. 23 new unit tests cover: fingerprint stability under row-count drift, fingerprint sensitivity to index-uniqueness flips (defence against the pre-existing index-count-only bug), `MappingBundle` round-trip through `bundle_to_doc` / `bundle_from_doc`, cache corruption / stale-version tolerance, stats-only refresh path, and cache-collection exclusion from fingerprints. The persistent tier lets containerized Arango Platform deployments share a warm cache across service instances and survive restarts without re-introspecting.

**Non-goals of WP-25:**

- No task decomposition / multi-agent orchestration in this pass (revisit after harness data).
- No SLM fine-tuning in this repo.
- No change to the §1.2 invariant that the LLM sees only the conceptual schema (few-shot examples and resolved entities extend the prompt but do not leak physical details — the examples themselves are conceptual-Cypher).

### 1.3 NL → AQL: the direct path (optional alternative) *(implemented)*

As a complement to the two-stage pipeline (§1.2), the system also supports **direct NL→AQL generation** where the LLM generates AQL without an intermediate Cypher representation.

```
┌──────────────────────────────────────────────────────────────┐
│  NL → AQL  (LLM-based, non-deterministic, opt-in)           │
│                                                              │
│  Input:  Natural language question from the user             │
│  Context: FULL physical schema —                             │
│           collection names, edge collection names,           │
│           field names, type discriminators,                   │
│           graph topology (what connects to what)             │
│  Engine: LLM (same providers as §1.2)                        │
│  Output: AQL query + bind variables, ready to execute        │
│                                                              │
│  The LLM SEES the physical model:                            │
│  - Collection names, edge collection names                   │
│  - Type discriminator fields and values                      │
│  - Property/field names                                      │
│  - Graph topology (traversal paths)                          │
└──────────────────────────────────────────────────────────────┘
```

**When to use the direct path instead of the two-stage pipeline:**

1. **Cypher transpiler gaps**: the deterministic transpiler does not yet support a required Cypher construct (e.g., complex aggregation patterns, AQL-specific features like `COLLECT AGGREGATE`, `PRUNE`, `SHORTEST_PATH`).
2. **AQL-specific features**: the user wants to leverage ArangoDB-specific capabilities that have no Cypher equivalent.
3. **Complex multi-hop queries**: some complex traversal patterns are more naturally expressed in AQL, and the LLM can generate them directly with the physical schema.
4. **Rapid prototyping**: skip the Cypher intermediate step when iterating on complex queries.

**Trade-offs vs. the two-stage pipeline:**

| Aspect | Two-stage (§1.2) | Direct (§1.3) |
|--------|-------------------|---------------|
| Determinism | Transpiler stage is deterministic | Fully non-deterministic |
| Physical model coupling | LLM never sees physical details | LLM sees full physical model |
| Portability | Same Cypher works across PG/LPG/hybrid | AQL is model-specific |
| LLM accuracy | High (LLMs trained on Cypher) | Lower (less AQL training data) |
| Feature coverage | Limited by transpiler | Limited by LLM capability |
| Correctability | Transpiler bugs can be fixed; corrections store works | LLM output varies between calls |

**Implementation:**

- Backend: `nl_to_aql()` function in `arango_cypher/nl2cypher.py`, `POST /nl2aql` endpoint in `service.py`
- Schema context: `_build_physical_schema_summary()` — provides collection names, edge collections, field names, type discriminators, and traversal topology
- LLM prompt: AQL-specific system prompt with ArangoDB query rules and conventions
- Validation: syntactic check for AQL keywords (`FOR`, `RETURN`, etc.) with retry loop
- UI: toggle on the Ask bar to switch between "Cypher" (two-stage, default) and "AQL" (direct) modes

---

## 2) Goals / non-goals

### Goals (v0.1–v0.3)
- **Primary product**: a deployable conversion service (library + CLI + HTTP) with a deterministic Cypher→AQL transpiler and an LLM-driven NL→Cypher pipeline. The UI (§4.4) is a debug/demo surface, not a separately supported product.
- **Translate** a defined subset of Cypher into **AQL + bind variables**.
- **Execute** translated AQL against ArangoDB (optional convenience wrapper).
- **Support PG, LPG, and hybrid** via `arangodb-schema-analyzer` mapping.
- Provide:
  - **Library API** (callable from other Python code)
  - **CLI** (run cypher, print AQL, execute, show results)
  - Optional **HTTP service** (translate/execute endpoints)
- Deterministic behavior by default; agentic enhancements are optional and non-authoritative.

### 2.1 Success criteria per phase

| Phase | Criteria | Target |
|-------|----------|--------|
| v0.1 | Golden tests passing (MATCH/WHERE/RETURN/WITH/ORDER BY/LIMIT) | 100% of golden test corpus |
| v0.1 | Translation P95 latency (single-hop queries) | < 50 ms |
| v0.1 | Integration tests passing (Movies + social datasets) | 100% |
| v0.2 | Write clause support (CREATE at minimum) for TCK setup | CREATE compiles and executes |
| v0.2 | TCK Match feature scenarios passing | ≥ 40% of Match*.feature |
| v0.2 | Schema analyzer integration (`acquire_mapping_bundle()`) | End-to-end mapping from live DB |
| v0.3 | UI: user can connect, translate, execute, and view results without touching JSON | Manual acceptance test |
| v0.3 | TCK overall pass rate | ≥ 25% of all scenarios (non-skipped) |
| v0.3 | Neo4j Movies dataset: full query corpus passing against both LPG and PG | 100% |
| v0.4+ | TCK overall pass rate | ≥ 60% of all scenarios |
| v0.4+ | CLI fully functional (`translate`, `run`, `mapping`, `doctor`) | All subcommands work |

### Non-goals (initially)
- **Full** openCypher TCK compliance in v0.1 -- but TCK is a **progressive goal**: each new Cypher feature should be accompanied by a check of which TCK scenarios it unblocks (see §8.2 for the phased strategy).
- Writing queries (CREATE/MERGE/DELETE/SET) in the first milestone, unless you explicitly want it. (Write support becomes a requirement in v0.2+ for TCK setup steps.)
- Full query optimizer equivalent to a database planner (we'll have a small internal logical plan, but not a cost-based optimizer).
- The Cypher Workbench UI (§4.4) is **not** a production multi-user workbench. Multi-user authn/authz, collaboration, server-side persistence, and multi-tenant isolation are explicitly out of scope — the UI targets single-operator debug/demo use (see §4.4 scope banner).

---

## 3) Users & primary use cases

### Personas
- **Data engineer**: wants to run Cypher for exploration and migrate Neo4j-ish workloads.
- **Application developer**: wants a Cypher compatibility layer for an app backed by ArangoDB.
- **Analyst / notebook user**: wants Cypher in Jupyter, quick iteration, shareable queries.
- **Agent workflow**: tools that need a stable "translate/execute/explain" contract.

### Core user stories
- **Translate-only**: "Given Cypher, show me AQL and bind vars."
- **Translate + execute**: "Run Cypher against database X and return JSON results."
- **Explain mapping**: "Show how labels/types were mapped to collections/fields (hybrid-aware)."
- **Validate**: "Warn if query references unknown labels/types based on inferred conceptual schema."

---

## 4) Product surfaces

### 4.1 Library API (primary)
Python package: `arango_cypher`

Suggested API:
- `translate(cypher: str, *, db=None, mapping=None, options=None) -> TranspiledQuery`
  - returns `{ aql: str, bind_vars: dict, warnings: list, metadata: dict }`
- `execute(cypher: str, *, db, mapping=None, options=None) -> list[dict] | Cursor`
- `get_mapping(db, *, strategy="auto", analyzer_options=...) -> MappingBundle`

### 4.2 CLI (v0.1)
Command: `arango-cypher-py` (or `arangodb-cypher`)

Commands:
- `translate`: prints AQL (+ bind vars JSON)
- `run`: executes and prints results (pretty table or JSON)
- `mapping`: prints mapping summary; optionally writes OWL Turtle
- `doctor`: checks connectivity, required collections/indexes, and config

### 4.3 HTTP service (shipped)
FastAPI service (`arango_cypher.service:app`) with endpoints:
- `POST /connect` --- authenticate to ArangoDB, returns session token
- `POST /disconnect` --- tear down session
- `GET /connections` --- list active sessions (admin/debug)
- `GET /connect/defaults` --- `.env` defaults for pre-filling the connection dialog (never returns password)
- `POST /translate` --- Cypher -> AQL + bind vars
- `POST /execute` --- translate and execute (requires session)
- `POST /validate` --- syntax-only or parse+translate validation
- `POST /explain` --- translate Cypher, run AQL EXPLAIN, return execution plan (requires session)
- `POST /aql-profile` --- translate Cypher, execute with profiling, return runtime stats (requires session)
- `GET /cypher-profile` --- JSON manifest for agent/NL gateways (S2A.0)

Run with: `uvicorn arango_cypher.service:app --host 0.0.0.0 --port 8000`

### 4.4 Cypher Workbench UI

> **Scope note.** The Workbench UI is a **debug and demo surface** for the conversion service (§4.3), not the product. It is optimized for a single operator (developer, SE, or demo presenter) inspecting and replaying translations against one dataset at a time. What is **out of scope** here is the UI becoming a *multi-user* surface: multi-user authentication/authorization, persistent server-side user state, cross-user collaboration, and per-user RBAC are explicitly not UI responsibilities — each browser session is a single operator holding one ArangoDB credential. What is **in scope**, and distinct from the above, is surfacing the backend's *tenant scope* when the database is multi-tenant: when the conceptual schema declares a `:Tenant` entity and the backend reports a tenant catalog (Wave 4r), the UI renders the tenant selector, persists the pinned selection, and forwards the session-bound `@tenantId` into `/nl2cypher` / `/nl2aql`. Tenant *isolation* is a backend concern governed by the six-layer architecture in `docs/multitenant_prd.md` (see "Multi-tenant safety" section); the UI consumes the result but does not itself enforce it. The UI is **not deployed by default** alongside the service (§15) — the default Arango Platform deployment is headless (library + CLI + HTTP endpoints). UI work should be scoped to features that directly improve the ability to debug the service or demonstrate its capabilities; any feature that would only be valuable in a multi-user workbench context belongs in a separate downstream product, not here.

#### 4.4.1 Architecture
SPA served by FastAPI. The browser does **not** connect to ArangoDB directly; all
database interaction flows through the service layer.

```
Browser                          FastAPI service             ArangoDB
+--------------------------+     +--------------------+     +----------+
| Cypher Editor            |     |                    |     |          |
| AQL Editor (editable)    | <-> | arango_cypher      | <-> | Database |
| Results / Explain / Prof |     | .service:app       |     |          |
+--------------------------+     +--------------------+     +----------+
```

#### 4.4.2 Cypher editor --- syntax-directed capabilities
The Cypher editor is a **full syntax-directed editing experience**.

**A) Syntax highlighting and structural awareness**
- Token-level highlighting: keywords (`MATCH`, `WHERE`, `RETURN`, `WITH`, `CALL`,
  `YIELD`, `UNION`, `OPTIONAL`, `UNWIND`, `CASE`, ...), labels (`:User`),
  relationship types (`[:KNOWS]`), properties, parameters (`$name`), strings,
  numbers, comments, `arango.*` namespace.
- Clause-level colorization: distinct background tint or gutter icon per clause block.
- Bracket/paren matching: highlight matching pairs, flash on close.
- Auto-close: `(`, `[`, `{`, `'` auto-close.
- Indentation: auto-indent continuation lines; smart indent after `WHERE`, `AND`, `OR`.
- Code folding: collapse multi-line clause bodies.

**B) Real-time error detection**
- Parse-error markers: red squiggly underline at exact error token; tooltip shows
  parse error. Triggered on keystroke debounce (300 ms) via `POST /translate`.
- Profile-aware warnings: amber squiggly for constructs not yet supported (from
  `/cypher-profile` `not_yet_supported` list).
- Bind-var warnings: warn if `$paramName` appears but is not defined in parameter panel.

**C) Autocompletion (context-aware)** *(core completions implemented -- see `ui/src/lang/cypher-completion.ts`)*
- After `:` inside `(…)` -> entity labels from mapping. **(implemented)**
- After `:` inside `[…]` -> relationship types from mapping. **(implemented)**
- After `.` on a bound variable -> property names from mapping for that label (requires `properties` in entity mapping). **(implemented)**
- After `arango.` -> registered extension functions/procedures from profile.
- After `$` -> parameter names from parameter panel.
- Start of line -> Cypher keywords appropriate to position.
- Inside `RETURN`/`WITH` -> aggregation functions, built-in functions.

The completion source uses a `MappingSchema` extracted from the current mapping JSON. Context detection walks backwards from the cursor to determine whether the position is inside a node pattern `(…)` or relationship pattern `[…]`. The schema ref is updated reactively when the mapping changes, so completions always reflect the current mapping without an editor re-mount.

**D) Navigation and reference**
- Variable-use highlighting: cursor on variable highlights all occurrences.
- Go-to-definition: Ctrl/Cmd+click on variable jumps to where it is first bound.
- Clause outline: minimap/sidebar showing clause structure (`MATCH` -> `WHERE` -> `RETURN`).
- Hover documentation: keyword descriptions, `arango.*` function signatures and AQL equivalents.

**E) Editing assistance**
- Snippet templates: `match`+Tab expands to template; customizable.
- Comment toggle: Ctrl/Cmd+`/`.
- Multi-cursor support.
- Query history: up/down in empty editor; history panel with search.
- Format/prettify: Ctrl/Cmd+Shift+F.

**F) Parameter binding**
- Auto-detection of `$paramName` tokens.
- JSON value entry per parameter.
- Persistence in localStorage per query hash.

Editor library: **CodeMirror 6** with custom Lezer grammar or community Cypher package.

#### 4.4.3 AQL editor --- syntax-directed editing with Explain / Profile
Side-by-side with Cypher editor. CodeMirror 6 instance, **editable** (transpiler output is the starting point; user can modify).

**A) Syntax highlighting** *(implemented)*
- AQL keywords (`FOR`, `IN`, `FILTER`, `RETURN`, `LET`, `SORT`, `LIMIT`,
  `COLLECT`, ...), bind parameters (`@@collection`, `@param`), functions,
  strings, numbers, comments.
- Bind-var references visually distinct (bold + colored).
- Line numbers always shown.

**B) Syntax-directed editing** *(implemented — `ui/src/lang/aql.ts`, `ui/src/components/AqlEditor.tsx`)*
- **Autocompletion** (Ctrl+Space or as-you-type): all AQL keywords, ~90 built-in functions (auto-insert `(`), snippet templates for common patterns.
- **Snippet templates**: `FOR ... IN` (loop), `FOR ... OUTBOUND/INBOUND` (traversal), `FILTER`, `COLLECT ... INTO`, `COLLECT AGGREGATE`, `LET ... =`, `LET ... = (subquery)`, `SORT ... ASC/DESC`, `LIMIT offset, count`, `RETURN { ... }`, `RETURN DISTINCT`, `INSERT`, `UPDATE`, `REMOVE`, `UPSERT`, `OPTIONS { indexHint }`.
- **Scoped variable prediction**: parses the AQL document to extract `FOR`, `LET`, and `COLLECT` bindings (including `COLLECT AGGREGATE` and `INTO` variables). Only variables in scope at the cursor line are suggested. Variables are boosted above keywords in the completion list.
- **Document property prediction**: after typing `var.`, resolves the variable's collection through the mapping (`physical_mapping.entities` / `physical_mapping.relationships`) and bind vars to offer property-level completions (e.g., `d.TENANT_ID`, `d.SERIAL_NUMBER`). System properties (`_key`, `_id`, `_rev`, `_from`, `_to`) are always included.
- **Bracket auto-closing**: `(`, `[`, `{`, `"`, `'` auto-pair.
- **Bracket matching**: highlights matching pairs.
- **Code folding**: fold gutter for collapsible blocks.
- **Undo/Redo**: full history (Ctrl+Z / Ctrl+Shift+Z).
- **Tab indentation**: Tab/Shift+Tab to indent/dedent.
- **Search/Replace**: Ctrl+F to find, Ctrl+H to replace.
- **Selection highlighting**: all occurrences of selected text are highlighted.

**C) Live synchronization** *(implemented)*
- Translate button updates AQL (debounced).
- Bind vars panel below AQL editor.
- Error state: if translation fails, show error inline instead of stale AQL.
- "modified" indicator when user has edited the AQL away from transpiler output.

**D) Local learning (corrections store)** *(implemented — `arango_cypher/corrections.py`, §14.1)*
- User edits the transpiled AQL, runs it successfully, clicks **Learn**.
- Correction stored in local SQLite (`corrections.db`) keyed on `(cypher, mapping_hash)`.
- On subsequent translates/executes of the same Cypher + mapping, the corrected AQL is used automatically (with a warning: "Using learned correction #N").
- **Corrections management panel**: view all stored corrections, delete individual entries.
- REST API: `POST /corrections`, `GET /corrections`, `DELETE /corrections/{id}`, `DELETE /corrections`.

**E) Explain and Profile** *(implemented)*
- **Explain** button -> `POST /explain` -> renders execution plan as interactive
  tree (type, estimatedCost, estimatedNrItems, index details). Raw JSON toggle.
- **Profile** button -> `POST /aql-profile` -> executes with profiling, shows
  runtime stats per plan node (actual time, rows, memory). Color-coded hotspots.
  Results go to Results panel.

**F) Correspondence hints (v0.4+)**
- Hovering over Cypher clause highlights corresponding AQL lines (via source-map
  metadata).

#### 4.4.4 Panels and layout
Split-pane layout with resizable dividers:

1. **Connection bar** (top).
2. **Cypher editor** (left) with Translate and Run buttons, parameter panel.
3. **AQL editor** (right, side-by-side) with Explain and Profile buttons, bind-vars
   panel, read-only/edit toggle.
4. **Results panel** (bottom, full width) with tabs: Table, Graph, JSON, Explain,
   Profile.
5. **Mapping panel** (drawer/tab): toggle between **JSON editor** and **visual graph
   editor** (5.6). Both views are bidirectionally synced. Visual graph shows entity
   nodes with properties, relationship edges, embedded relationships.
6. **Profile panel** (drawer/tab).

#### 4.4.5 Connection and credential model
- **Browser-supplied** (primary): user enters host, port, database, username,
  password in the connection dialog. Credentials travel to FastAPI only; the browser
  never contacts ArangoDB directly.
- **`.env` defaults** (convenience): `GET /connect/defaults` returns non-secret
  defaults (host, port, database, username) to pre-fill the dialog. Password is
  **never** returned.
- **Security constraints**: credentials are held in server-side session storage
  (in-process dict keyed by opaque token). No credentials are persisted to disk.
  Session tokens are short-lived (configurable TTL, default 30 min, sliding).

**Security model (expanded):**

| Concern | Policy | Status |
|---------|--------|--------|
| **CORS** | Allow configurable origins via `CORS_ALLOWED_ORIGINS` env var (comma-separated, or `*`). Default: `*` for dev convenience -- pin to an explicit allowlist in production. When `*` is combined with the new `ARANGO_CYPHER_CORS_CREDENTIALS=1` flag, the service refuses to start; with `*` and the credentials flag unset, `allow_credentials` is silently downgraded to `False` (and the operator is warned at startup) so a non-browser caller cannot honour the unsafe pairing. Pinned by `tests/test_service_hardening.py::TestCorsCredentialedWildcardRejected`. | Implemented |
| **Session tokens** | Generated via `secrets.token_urlsafe(32)`. Not JWT -- opaque, server-side lookup only. | Implemented |
| **Session TTL** | Default 30 min sliding window; configurable via `SESSION_TTL_SECONDS` env var. Expired sessions are evicted on next access *and* eagerly on every session lookup (`_prune_expired`). Pinned by `tests/test_service_middleware.py::TestSessionLifecycle`. | Implemented |
| **Max sessions** | Configurable via `MAX_SESSIONS` env var (default 100). Oldest-by-`last_used` is evicted when the cap is reached (`_evict_lru`). Pinned by `tests/test_service_middleware.py::TestSessionLifecycle`. | Implemented |
| **Rate limiting** | Per-session in-memory token bucket (`_TokenBucket`) for NL endpoints, keyed by Authorization header (falls back to client IP). Capacity = `NL_RATE_LIMIT_PER_MINUTE` (default 10). 429 response on exhaustion. Pinned by `tests/test_service_middleware.py::TestTokenBucket`. | Implemented (NL endpoints) |
| **Credential storage** | In-memory only. Never written to disk, logs, or response bodies (except session token). | Implemented |
| **`.env` exposure** | `GET /connect/defaults` returns host, port, database, username. The password field is the empty string by default; set `ARANGO_CYPHER_EXPOSE_DEFAULTS_PASSWORD` to `1` on a trusted single-user laptop if you want the connect dialog to auto-fill it. The endpoint itself is disabled (404) when `ARANGO_CYPHER_PUBLIC_MODE=1`. Pinned by `tests/test_service_hardening.py::TestConnectDefaultsRedaction`. | Implemented |
| **SSRF (`/connect`)** | Unconditionally rejects literal cloud-metadata IPs (AWS / Azure / OpenStack / GCP / Alibaba) and the corresponding shorthand hostnames so an anonymous caller cannot exfiltrate IAM credentials by pointing the connect dialog at `169.254.169.254`. In `ARANGO_CYPHER_PUBLIC_MODE` the policy is widened to refuse RFC1918 / loopback / link-local / ULA literals as well. Operators that legitimately need a private target opt in via `ARANGO_CYPHER_CONNECT_ALLOWED_HOSTS=<csv>`. No DNS resolution happens at the guard (DNS rebinding would itself be an SSRF amplifier); operators using DNS in front of private infra must allowlist explicitly. Pinned by `tests/test_service_hardening.py::TestConnectSsrfGuard`. | Implemented |
| **Public-mode auth gating** | When `ARANGO_CYPHER_PUBLIC_MODE=1`, every NL endpoint (`/nl2cypher`, `/nl2aql`, `/nl-samples`), every correction CRUD endpoint (`/corrections*`, `/nl-corrections*`), and `/connections` requires a valid session token resolved through `_get_session` (`X-Arango-Session` or `Authorization: Bearer`). The `session_token` field on `/nl2cypher` is ignored in public mode -- the authenticated session's DB is used unconditionally for entity resolution so a caller cannot swap their NL request onto another user's database by guessing the body field. Default mode preserves the unauthenticated single-user developer workflow. Pinned by `tests/test_service_hardening.py::TestPublicModeAuthRequired`. | Implemented |
| **Pydantic-422 redaction** | `_validation_error_handler` runs every `errors()` entry through `_sanitize_pydantic_errors`, which recursively walks the `input` dict and applies `_sanitize_error` to every string. The redacted shape is what gets logged *and* what gets returned to the client, so a payload that embeds `password=…` cannot leak to either side. In public mode the body fragment that previously trailed the log line is dropped entirely. Pinned by `tests/test_service_hardening.py::TestValidationErrorRedaction`. | Implemented |
| **AQL injection** | All collection names used for document lookup use `@@` bind parameters; all values use `@` bind parameters. The one backtick-interpolation call site (`/tenants?collection=<name>`) is guarded by `_COLLECTION_NAME_RE` (ArangoDB-identifier regex, 1--256 chars, `[A-Za-z_][A-Za-z0-9_-]*`) before the AQL is built; rejected names return 400. Pinned by `tests/test_service_tenants.py::TestTenantsCollectionNameValidation`. | Implemented |
| **Error sanitization** | `_sanitize_error` strips URLs, IPv4 host:port pairs, and credential-form patterns (`password=`, `api_key:`, `Authorization: Bearer <token>`, etc.) from any error message before it crosses an endpoint boundary. All AQL / DB-touching endpoints use the `_translate_errors` context manager so no raw upstream error can leak. Pinned by `tests/test_service_sanitize.py`. | Implemented |
| **HTTPS** | The service itself does not terminate TLS. Recommendation: use a reverse proxy (nginx, Caddy) for production. Document this requirement. | Not implemented (documentation gap) |
| **Multi-tenant** | Tenant isolation for NL-generated queries is the subject of the dedicated [`docs/multitenant_prd.md`](./multitenant_prd.md) draft (six-layer defense: storage segmentation via disjoint SmartGraphs, resolved schema classification, constrained Cypher generation, pre-translation rewriting, post-translation validation, and EXPLAIN-plan gating). The `/tenants` catalog endpoint and the UI tenant selector are the Layer 1/2 surfaces. Multi-user *workbench* features (authn/authz, collaboration, server-side persistence) remain out of scope for v0.1 -- see §4.4 scope banner. | **Phase 1 + Phase 2 done.** Wave 7 (2026-05-11): MT-0 (`scopingPathFromTenant`), MT-1 (Layer 1 session binding), MT-5 (Layer 5 EXPLAIN validator + Layer 6 `safe_execute`). Wave 8 (2026-05-27, PR #30): MT-2 (literal-tenant rejection at Layer 2), MT-3a (Cypher AST rewriter core), MT-4 (AQL AST rewriter + service wiring on `/translate`, `/execute(-aql)`, `/aql-profile`, `/nl2aql`). Remaining: MT-3 service wiring, MT-6 (plan-shape LRU), MT-7 (admin bypass + audit stream), MT-8 (red-team corpus). |

Service endpoints used by the connection model:

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/connect` | Authenticate, return session token |
| POST | `/disconnect` | Tear down session |
| GET | `/connections` | List active sessions (admin/debug) |
| GET | `/connect/defaults` | `.env` defaults for pre-fill |
| POST | `/translate` | Cypher -> AQL (no session required) |
| POST | `/execute` | Translate + execute (session required) |
| POST | `/validate` | Syntax / translation validation |
| POST | `/explain` | AQL EXPLAIN (session required) |
| POST | `/aql-profile` | Execute with profiling (session required) |
| GET | `/cypher-profile` | JSON manifest for agent/NL gateways |
| GET | `/schema/introspect` | Discover collections, edges, properties from connected DB (session required) |
| GET | `/schema/properties` | Infer properties for a specific collection (session required) |

#### 4.4.6 Results display
- **Table view** (default).
- **Graph view** (Cytoscape.js).
- **JSON view**.
- **Explain view**: interactive tree of AQL execution plan.
- **Profile view**: annotated plan with runtime metrics, color-coded hotspots.
- **Export**: CSV or JSON.

#### 4.4.7 Tech stack
- **Framework**: React (Vite).
- **Editor**: CodeMirror 6.
- **Cypher language mode**: Custom Lezer grammar or community package. Context-aware autocompletion (`ui/src/lang/cypher-completion.ts`).
- **AQL language mode**: Custom `StreamLanguage` definition with autocompletion, snippets, scoped variable prediction, and document property prediction (`ui/src/lang/aql.ts`).
- **Graph visualization**: Custom SVG with pan/zoom (results graph + read-only schema graph). Cytoscape.js planned for visual mapping editor (§5.6).
- **Mapping graph layout**: Two-column conceptual/physical layout with SVG bezier curves for mapping edges. Cytoscape-dagre planned for bidirectional graph editor.
- **Execution plan viz**: React tree component (custom or react-d3-tree).
- **HTTP client**: fetch.
- **Styling**: Tailwind CSS.
- **State management**: Zustand.
- **Local persistence**: `localStorage` (NL query history, connection state, parameters). SQLite (corrections store, backend).

#### 4.4.8 Keyboard shortcuts

| Shortcut | Action |
|----------|--------|
| Ctrl/Cmd+Enter | Translate |
| Shift+Enter | Execute |
| Ctrl/Cmd+Shift+E | Explain |
| Ctrl/Cmd+Shift+P | Profile |
| Ctrl+Space | Autocomplete |
| Ctrl/Cmd+Shift+F | Format |
| Ctrl/Cmd+/ | Toggle comment |
| Ctrl/Cmd+D | Select next occurrence |
| Ctrl/Cmd+Z / Ctrl/Cmd+Y | Undo / Redo |
| Up/Down (empty editor) | Query history |

#### 4.4.9 Phasing

| Phase | Scope | Status |
|-------|-------|--------|
| v0.3-alpha | FastAPI service with all endpoints. Connection dialog. Cypher editor with syntax highlighting (A), bracket matching, auto-close. AQL editor (read-only) with AQL syntax highlighting. Translate button -> AQL preview. No execute. | **Done** |
| v0.3-beta | Execute with table results. Cypher parse-error markers (B). AQL Explain button + tree view. Query history. Parameter binding (F). Bind-vars panel. `.env` defaults. Keyboard shortcuts. | **Done** -- all items implemented. |
| v0.3 | AQL Profile button + annotated plan view. Results graph view. Variable-use highlighting (D). Clause outline. Profile panel. AQL editor editable mode + syntax-directed editing. **Property-enriched mappings** (5.5). **Domain/range optimization** (5.5.1). **Context-aware autocompletion** (C). | **Partial** -- property mappings, domain/range optimization, autocompletion, profile: **done**. AQL editor editable with full syntax-directed editing (§4.4.3B): **done**. Results graph view: **partial** (custom SVG, no Cytoscape). Variable-use highlighting: **done**. Clause outline: **not started**. |
| v0.3.1 | **Visual mapping graph editor** (5.6): Cytoscape.js entity-relationship diagram, bidirectional sync with JSON editor. `GET /schema/introspect` endpoint. **Schema analyzer integration** (§5.1–5.3). | **Partial** -- `/schema/introspect` with 3-tier strategy (heuristic + analyzer): **done**. Visual mapping graph (read-only, SVG, dual-layer conceptual/physical): **done**. Cytoscape.js integration and bidirectional graph-to-JSON editing: **not started**. |
| v0.4 | Hover documentation (D). Profile-aware warnings (B). Format/prettify. Correspondence hints. Multi-statement. Export. Ontology (OWL Turtle) generation. `arango.*` / `$` / keyword autocompletion. NL-to-Cypher pipeline (§1.2). **Local learning** (§14.1). AQL snippet templates (§4.4.3B). AQL post-processing indentation. NL query history. Token usage display. | **Mostly done** -- NL-to-Cypher (LLM + rule-based fallback, pluggable providers, validation/retry): **done**. Export (CSV/JSON): **done**. `arango.*`/`$`/keyword autocompletion: **done**. OWL export/import endpoints: **done**. AQL snippet templates: **done**. Local learning (corrections store): **done**. AQL indentation (`_reindent_aql`): **done**. NL query history: **done**. Token usage display: **done**. Cypher hover docs, AQL format/prettify, multi-statement, variable-use highlighting: **done**. Profile-aware warnings, correspondence hints: **not started**. |

---

## 5) Schema detection & mapping (hard requirement)

### 5.1 Required dependency: `arangodb-schema-analyzer`
`~/code/arango-schema-analyzer` is a Python library named `arangodb-schema-analyzer` with:
- `AgenticSchemaAnalyzer` library API
- A stable **tool contract v1** (`schema_analyzer.tool.run_tool(request_dict)` or CLI `arangodb-schema-analyzer`)
- Export formats:
  - stable JSON export for transpilers (`operation="export"`)
  - OWL Turtle (`operation="owl"`)

### 5.2 Mapping contract we will consume
We will treat `export` output as the authoritative "transpiler mapping" contract.

Important: the analyzer already defines mapping styles and even provides injection-safe AQL fragments:
- Entity mapping styles: `COLLECTION` vs `LABEL` (generic + `typeField/typeValue`)
- Relationship mapping styles: `DEDICATED_COLLECTION` vs `GENERIC_WITH_TYPE`

This aligns exactly with your hybrid requirement: mapping is per entity type and per relationship type, so any mixture is representable.

#### No-workaround policy for schema analyzer gaps

The `arangodb-schema-analyzer` is the **canonical source** for reverse-engineering ontologies from ArangoDB schemas. When the transpiler encounters a situation where the analyzer's output is incomplete, incorrect, or missing a needed capability:

1. **Do not work around it** in the transpiler. Workarounds create hidden coupling, obscure the real gap, and lead to divergent behavior when the analyzer is later fixed.
2. **File a bug or feature report** against `arangodb-schema-analyzer` (repo: `~/code/arango-schema-analyzer`). Include:
   - The database schema that triggered the gap (collections, sample documents)
   - What the analyzer currently produces
   - What the transpiler needs it to produce
   - A concrete example of the Cypher query that would benefit
3. **Document the gap** in this PRD (§5.3 implementation status table) with a reference to the filed issue.
4. **Skip or error gracefully** in the transpiler until the analyzer is fixed. Use `CoreError` with code `"ANALYZER_GAP"` and a message referencing the issue.

This policy ensures that the analyzer and transpiler evolve together and that ontology extraction quality improves at the source rather than being papered over downstream.

#### 5.2.1 Analyzer promotion (resolved 2026-04-11)

As of `arangodb-schema-analyzer` v0.1.0, the analyzer passes all acceptance criteria for PG, LPG, and hybrid schemas (28/28 tests — see `docs/schema-mapper-lpg-bug-report.md` for the original gaps and resolution).

The `get_mapping(strategy="auto")` flow now routes **all** schema types through the analyzer first:
1. **Analyzer (primary)**: `acquire_mapping_bundle(db)` — handles PG, LPG, hybrid identically. Produces consistent ontology (same entity names, relationship types, domain/range) regardless of physical style. Per-type properties scoped by discriminator. Contract version 1 with JSON Schema validation.
2. **Heuristic (fallback)**: `classify_schema(db)` + `_build_heuristic_mapping(db, schema_type)` — used only when the analyzer is **not installed** (`ImportError`). Provides a reasonable best-effort mapping for PG and LPG.
3. **Explicit config (highest priority)**: user-supplied `MappingBundle` via UI, API, or code always takes precedence when provided.

The heuristic tier is retained as a zero-dependency fallback so the transpiler can function in environments where installing the analyzer is not feasible.

### 5.3 Detection strategy in `arango-cypher-py`
3-tier strategy (updated 2026-04-11 — analyzer promoted to primary for all schema types):
- **Explicit config** (highest priority): user-supplied mapping overrides; useful for unstable databases.
- **Analyzer** (primary): `arangodb-schema-analyzer` handles PG, LPG, and hybrid. Consistent ontology across all three physical styles.
- **Heuristic** (fallback): `classify_schema()` + `_build_heuristic_mapping()` when the analyzer is not installed.

`strategy="analyzer"` forces the analyzer (raises if not installed). `strategy="heuristic"` forces the heuristic.

> **Heuristic-tier correctness — shipped 2026-04-23/24.** WP-27 (commit `0313395`) hardens `_detect_type_field` against scalar data fields named `label`/`labels`/`kind` (tier-2 cardinality + class-like-value rules), and the transpiler now strips label backticks at the resolver boundary. WP-28 (commit `ac6eaff`) makes the analyzer-fallback path loud (`ANALYZER_NOT_INSTALLED` warning + UI `SchemaWarningBanner`), refuses service startup without `arangodb-schema-analyzer` unless `ARANGO_CYPHER_ALLOW_HEURISTIC=1` is set, retries the analyzer on cache miss when newly importable, and adds `POST /schema/force-reacquire`. WP-29 (commit `d4a871f`) closes the NL prompt + retry loop (label escaping + fail-closed `validation_failed`). WP-30 (commit `618aace`) closes the Translate-button feedback gap (`editorCypherSource` reducer + regenerate-from-NL action with `retry_context`). The bug-fix PRD remains marked OPEN only until the §11.4 closeout E2E walkthrough on `ic-knowledge-graph-temporal` is run by an operator with pilot-DB credentials.

#### Current implementation status

| Component | Status |
|-----------|--------|
| Explicit config (manual JSON mapping) | **Implemented** -- users supply `MappingBundle` via UI, API, or code |
| `MappingResolver` consuming export JSON | **Implemented** -- entities, relationships, properties, domain/range |
| Fast heuristic detection (`classify_schema`) | **Implemented** -- classifies as `pg`, `lpg`, or `hybrid` using `COLLECT DISTINCT` AQL queries; detects discriminator fields for both document and edge collections |
| Heuristic mapping builder (`_build_heuristic_mapping`) | **Implemented** -- fallback when analyzer not installed. Builds `MappingBundle` for PG and LPG; handles `typeField`/`typeValue`, per-type property sampling, domain/range inference |
| Schema analyzer integration (library call) | **Implemented** -- `acquire_mapping_bundle(db)` calls `arangodb-schema-analyzer` v0.1.0+ via `AgenticSchemaAnalyzer`. **Primary tier for all schema types** (PG, LPG, hybrid). See §5.2.1. |
| `get_mapping(db)` convenience API | **Implemented** -- `get_mapping(db)` runs: analyzer → heuristic fallback (on ImportError). Result cached by schema fingerprint. |
| Schema introspection endpoints | **Implemented** -- `GET /schema/introspect` calls `get_mapping(db)` → returns full conceptual + physical mapping via `MappingResolver.schema_summary()`. UI auto-introspects on connect. |

#### Requirements for schema analyzer integration

1. **Library import path** -- add `arangodb-schema-analyzer` as an optional dependency. Import `AgenticSchemaAnalyzer` or use the tool contract `run_tool({"operation": "export", ...})`.
2. **`acquire_mapping_bundle(db)` function** -- given a `python-arango` database handle, call the analyzer's `export` operation and return a fully populated `MappingBundle` (including `conceptual_schema` with structured `entities`/`relationships` and `fromEntity`/`toEntity`, `physical_mapping` with properties and domain/range, and optionally `owl_turtle`).
3. **UI "Introspect" button** -- the mapping panel's introspect flow should call the analyzer (via a new service endpoint) rather than just sampling document fields. The analyzer produces a complete conceptual-to-physical mapping with entity/relationship type detection, not just property names.
4. **OWL Turtle round-trip** -- `_mapping_from_dict` in `service.py` should read `owl_turtle` (or `owlTurtle`) from the request and populate `MappingBundle.owl_turtle`. The `/schema/summary` endpoint should include it in the response.
5. **Caching** -- cache the analyzer result by schema fingerprint (collections + indexes + sample hash). Avoid re-analyzing on every request.
6. **Fallback chain** -- when a user calls `translate()` without an explicit mapping but with a database connection, automatically run the 3-tier strategy: explicit > heuristic > analyzer.

### 5.4 OWL Turtle usage
You asked specifically for OWL TTL. We'll support two flows:
- **Primary runtime flow**: consume `export` mapping JSON (simpler, stable, already designed for transpilers).
- **Artifact/explain flow**: also store `owl` Turtle output alongside the export, for:
  - debugging
  - explaining hybrid partitions
  - offline review / documentation

Optional: implement a TTL ingestion path using `rdflib` so users can provide a TTL mapping file (air-gapped use, reproducible builds).

#### Current implementation status

| Component | Status |
|-----------|--------|
| `MappingBundle.owl_turtle` field | **Exists** -- field defined on the dataclass |
| OWL Turtle test fixtures | **Exist** -- `tests/fixtures/mappings/*.owl.ttl` files alongside export JSON |
| Loading OWL into MappingBundle | **Partial** -- `_mapping_from_dict()` reads `owlTurtle`; `mapping_bundle_for()` does not read `.owl.ttl` files |
| OWL export endpoint | **Implemented** -- `POST /mapping/export-owl` generates OWL Turtle from mapping |
| OWL import endpoint | **Implemented** -- `POST /mapping/import-owl` loads OWL Turtle into `MappingBundle` |
| OWL generation from enriched mapping | **Partial** -- export-owl endpoint works; no standalone library function |
| `rdflib` TTL ingestion | **Not implemented** |

Recommended libs:
- `rdflib` for parsing TTL

### 5.5 Property-enriched mappings

#### Current state (implemented)
Property-enriched mappings are fully supported. `MappingResolver.resolve_properties(label_or_type)` returns `dict[str, PropertyInfo]` with field name, type, indexed, required, and description metadata. The UI's default sample mapping includes properties on both entities and relationships. The editor's context-aware autocompletion (4.4.2C) consumes properties from the mapping to offer `.name`, `.age`, `.email` completions after bound variables. Domain/range on relationships is resolved via a 3-tier strategy (see 5.5.1) that also reads `fromEntity`/`toEntity` from the conceptual schema.

#### Why properties matter

1. **Autocompletion** -- the editor needs property names per label to offer `.name`, `.age`, `.email` after a bound variable (PRD 4.4.2C)
2. **Validation** -- warn if a Cypher query references a property that does not exist on the target label/collection
3. **Type awareness** -- knowing that `age` is a number and `name` is a string enables type-safe comparisons, aggregation hints, and index recommendations
4. **Visual mapping graph** -- a graph visualization of the schema needs to show entity nodes with their property lists and relationship edges with their property lists
5. **Ontology derivation** -- generating OWL/RDF from the mapping requires datatype properties, not just classes and object properties

#### Mapping shape (implemented)

Entity and relationship entries in `physical_mapping` gain an optional `properties` dict mapping conceptual property names to physical field metadata:

```json
{
  "physical_mapping": {
    "entities": {
      "Person": {
        "style": "COLLECTION",
        "collectionName": "persons",
        "properties": {
          "name":  { "field": "name",  "type": "string" },
          "age":   { "field": "age",   "type": "number" },
          "email": { "field": "email", "type": "string", "indexed": true }
        }
      }
    },
    "relationships": {
      "KNOWS": {
        "style": "DEDICATED_COLLECTION",
        "edgeCollectionName": "knows",
        "domain": "Person",
        "range": "Person",
        "properties": {
          "since": { "field": "since", "type": "number" }
        }
      }
    }
  }
}
```

Property metadata fields:

| Field | Type | Description |
|-------|------|-------------|
| `field` | string | Physical field name in the document (allows rename: conceptual `firstName` -> physical `first_name`) |
| `type` | string | Data type: `string`, `number`, `boolean`, `array`, `object`, `date`, `geo_point`, `null` |
| `indexed` | boolean | Whether the field has a persistent/hash index (for query optimization hints) |
| `required` | boolean | Whether the field is always present (vs sparse/optional) |
| `description` | string | Human-readable description (for hover docs and ontology generation) |

#### Property discovery strategies

1. **Explicit** -- user supplies properties in the mapping JSON (highest fidelity)
2. **Schema analyzer** -- `arangodb-schema-analyzer` export with `includeProperties=true` samples documents and infers property names, types, and cardinality
3. **Live introspection** -- new `GET /schema/properties?collection=persons&sample=100` endpoint that samples N documents from the connected database and returns inferred property names and types
4. **Hybrid** -- combine explicit overrides with analyzer/introspection results; explicit wins on conflict

#### Impact on existing code (implemented)

- `MappingResolver.resolve_properties(label_or_type: str) -> dict[str, PropertyInfo]` -- reads property metadata from entity or relationship mapping
- `translate_v0` optionally validates property references against the schema (warn, not error by default)
- Editor autocompletion (4.4.2C) consumes properties from the mapping via `extractSchema()` in `ui/src/lang/cypher-completion.ts` -- offers property names after `.` on bound variables
- Visual mapping graph (5.6) renders properties as node attribute lists

### 5.5.1 Domain/range relationship metadata and IS_SAME_COLLECTION optimization

#### Problem

When translating `MATCH (p1:Person)-[:KNOWS]->(p2:Person) RETURN p1, p2`, the transpiler emits an `IS_SAME_COLLECTION(@vCollection, p2)` filter after the graph traversal. This filter verifies that the target vertex belongs to the expected collection. However, when the mapping declares that the `KNOWS` edge collection exclusively connects `Person` (domain) to `Person` (range), the edge collection itself already constrains both endpoints -- the filter is pure overhead.

#### Solution

Relationship entries in `physical_mapping` support optional `domain` and `range` fields that name the conceptual entity labels at each end of the edge:

```json
"KNOWS": {
  "style": "DEDICATED_COLLECTION",
  "edgeCollectionName": "knows",
  "domain": "Person",
  "range": "Person"
}
```

| Field | Type | Description |
|-------|------|-------------|
| `domain` | string | Conceptual entity label for the source endpoint (`_from`) |
| `range` | string | Conceptual entity label for the target endpoint (`_to`) |

When the transpiler encounters a traversal pattern like `(a:X)-[:R]->(b:Y)`:
- **OUTBOUND**: if `R.range == Y`, skip `IS_SAME_COLLECTION` for `b`
- **INBOUND**: if `R.domain == Y`, skip `IS_SAME_COLLECTION` for `b`
- **ANY**: skip only if both `R.domain == Y` and `R.range == Y`

If `domain`/`range` is absent or does not match the target label, the filter is emitted as before (backward-compatible).

#### Domain/range inference (implemented)

When explicit `domain`/`range` fields are not present on the physical relationship mapping, `MappingResolver` infers them automatically via a 3-tier resolution in `_resolve_domain_range`:

1. **Explicit physical mapping** -- `domain`/`range` fields on the relationship entry (highest priority, original behavior)
2. **Conceptual schema relationships** -- `fromEntity`/`toEntity` from the `conceptual_schema.relationships` array (when present and not `"Any"`)
3. **Single-entity inference** -- when the mapping defines exactly one entity type, both endpoints must be that type

This means mappings that use the simple `entityTypes`/`relationshipTypes` format (without explicit `domain`/`range`) still benefit from the optimization when the conceptual schema provides enough information:

```json
{
  "conceptual_schema": {
    "relationships": [
      { "type": "KNOWS", "fromEntity": "Person", "toEntity": "Person" }
    ]
  },
  "physical_mapping": {
    "relationships": {
      "KNOWS": {
        "style": "DEDICATED_COLLECTION",
        "edgeCollectionName": "knows"
      }
    }
  }
}
```

The above mapping (without explicit `domain`/`range` on the physical side) will still skip `IS_SAME_COLLECTION` because `fromEntity`/`toEntity` in the conceptual schema resolve the constraint.

#### Implementation

- `MappingResolver.edge_constrains_target(rel_type, target_label, direction)` -- returns `True` when the filter can be safely omitted (only for `DEDICATED_COLLECTION` style with matching domain/range)
- `MappingResolver._resolve_domain_range(rel_type, rmap)` -- 3-tier resolution of domain/range from physical mapping, conceptual schema, or single-entity inference
- All traversal code paths in `translate_v0` consult `edge_constrains_target` before emitting `IS_SAME_COLLECTION`
- Golden tests in `tests/fixtures/cases/domain_range_optimization.yml` cover OUTBOUND, INBOUND, ANY, WHERE clause preservation, cross-type source, and the fallback when domain/range is absent

#### AQL comparison

**Without domain/range** (current default):
```aql
FOR p1 IN @@uCollection
  FOR p2, r IN 1..1 OUTBOUND p1 @@edgeCollection
    FILTER IS_SAME_COLLECTION(@vCollection, p2)
  RETURN {p1: p1, p2: p2}
```

**With domain/range declared**:
```aql
FOR p1 IN @@uCollection
  FOR p2, r IN 1..1 OUTBOUND p1 @@edgeCollection
  RETURN {p1: p1, p2: p2}
```

---

### 5.6 Visual mapping graph editor

The mapping panel (4.4.4 item 5) currently shows raw JSON. A **visual graph editor** provides a far more intuitive way to understand and edit the conceptual-to-physical mapping.

#### Architecture

The visual mapping graph renders the `conceptual_schema` + `physical_mapping` as an interactive **entity-relationship diagram**:

- **Entity nodes** -- rounded rectangles showing the conceptual type name (e.g. `Person`), the physical collection name, the mapping style badge (`COLLECTION` / `LABEL`), and a list of properties with types
- **Relationship edges** -- labeled arrows between entity nodes showing the relationship type (e.g. `KNOWS`), the physical edge collection, the mapping style, and edge properties
- **Embedded relationships** -- dashed arrows with an "EMBEDDED" badge, showing the `embeddedPath` and whether it's an array or object

#### Interaction model

| Interaction | Behavior |
|-------------|----------|
| **View** | Graph auto-layouts from the current mapping JSON. Nodes are draggable. Zoom/pan supported. |
| **Select node** | Side panel shows the full entity mapping detail: collection, style, all properties with types, indexes |
| **Select edge** | Side panel shows relationship detail: edge collection, style, direction, properties |
| **Add entity** | Click "+" button or double-click canvas. Dialog prompts for label name, collection, style. Node appears in graph and JSON updates. |
| **Add relationship** | Drag from one entity node to another. Dialog prompts for type name, edge collection, style. Edge appears and JSON updates. |
| **Add property** | Select a node/edge, click "Add Property" in side panel. Enter name, type, indexed flag. |
| **Edit** | Double-click a node/edge label to rename. Edit properties inline in the side panel. All changes sync to the JSON editor bidirectionally. |
| **Delete** | Select node/edge, press Delete or click remove button. Confirmation prompt. JSON updates. |
| **Import from DB** | "Introspect" button samples the connected database (via `GET /schema/introspect`) and populates the graph with discovered entities, relationships, and properties. User confirms before applying. |

#### Bidirectional sync with JSON editor

The visual graph and the JSON mapping editor are two views of the same data:
- Editing the JSON updates the graph in real time
- Editing the graph (drag, add, delete, rename) updates the JSON in real time
- The user can switch between views freely; the toggle is in the mapping panel header
- Invalid JSON in the text editor shows a parse error but does not break the graph (last valid state is retained)

#### Layout and rendering

- **Library**: Cytoscape.js (already in the tech stack for results graph view)
- **Layout**: `dagre` (hierarchical/layered) for initial auto-layout; user can drag nodes to customize
- **Node rendering**: Custom HTML node with label, collection badge, and property list (Cytoscape.js `html-node` extension or overlay div)
- **Edge rendering**: Labeled bezier curves with arrowheads; embedded relationships use dashed lines
- **Minimap**: Optional corner minimap for large schemas

#### Integration points

| Component | How it uses the visual graph |
|-----------|------------------------------|
| **Mapping panel** | Toggle between "JSON" and "Visual" views; both edit the same `MappingBundle` |
| **Autocompletion** (4.4.2C) | Property lists from the graph feed the editor's context-aware completions |
| **Validation** | Property references in Cypher are checked against the graph's property catalog |
| **Ontology export** | The enriched mapping (with properties) can generate OWL Turtle via the schema analyzer |
| **Introspection** | "Introspect" button populates the graph from a live database connection |

#### New API endpoints

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/schema/introspect` | Sample connected database: discover collections, edge collections, document properties (requires session) |
| GET | `/schema/properties` | Return inferred properties for a specific collection (sample N documents) |

### 5.7 Index-aware physical mapping model

The physical mapping carries index metadata so the transpiler can make informed optimization decisions without requiring the query author to know about physical indexes.

#### Why indexes belong in the mapping

1. **VCI (Vertex-Centric Indexes)**: in LPG graphs with a generic edge collection, a VCI on the edge `type` field allows the traversal engine to filter edges at the storage layer. When the transpiler knows a VCI exists, it can emit edge-direction filters inside the traversal `OPTIONS` instead of post-filtering vertices.
2. **Persistent indexes**: knowing that `name` is indexed on `persons` allows the transpiler to emit `OPTIONS { indexHint: "idx_persons_name" }` for filtered scans.
3. **Fulltext / Geo / TTL indexes**: these affect which `arango.*` extension functions are available and performant for a given collection.

#### Mapping shape extension

Each entity and relationship in `physicalMapping` gains an optional `indexes` array:

```json
{
  "physicalMapping": {
    "entities": {
      "Person": {
        "collectionName": "nodes",
        "style": "LABEL",
        "typeField": "type",
        "typeValue": "Person",
        "indexes": [
          {
            "type": "persistent",
            "fields": ["name"],
            "unique": false,
            "name": "idx_nodes_name"
          }
        ]
      }
    },
    "relationships": {
      "ACTED_IN": {
        "edgeCollectionName": "edges",
        "style": "GENERIC_WITH_TYPE",
        "typeField": "relation",
        "typeValue": "ACTED_IN",
        "indexes": [
          {
            "type": "persistent",
            "fields": ["relation"],
            "unique": false,
            "name": "idx_edges_relation",
            "vci": true
          }
        ]
      }
    }
  }
}
```

#### Index metadata fields

| Field | Type | Description |
|-------|------|-------------|
| `type` | string | Index type: `persistent`, `hash`, `skiplist`, `fulltext`, `geo`, `ttl`, `inverted` |
| `fields` | string[] | Fields covered by the index |
| `unique` | boolean | Whether the index enforces uniqueness |
| `sparse` | boolean | Whether null values are excluded |
| `name` | string | ArangoDB index name (for `indexHint`) |
| `vci` | boolean | Whether this is a vertex-centric index (only meaningful on edge collections) |
| `deduplicate` | boolean | Whether the index deduplicates array values |

#### VCI detection and advisory

When the transpiler encounters an LPG graph that uses a generic edge collection (`GENERIC_WITH_TYPE` style) but has **no VCI** on the edge type field:

1. **Log a warning**: `"Edge collection 'edges' uses GENERIC_WITH_TYPE but has no VCI on field 'relation'. Traversal performance will be degraded."`
2. **Advise the data owner**: the CLI `doctor` command and the UI introspection panel should surface this as a recommendation: "Consider creating a vertex-centric index on `edges.relation` for improved traversal performance."
3. **Offer to create**: for interactive sessions (CLI `run`, UI), offer to create the VCI. For ETL pipelines, document the recommended index definition so the data owner can add it to their ingest process.
4. **Degrade gracefully**: the transpiler still works without VCI — it just cannot emit edge-level filters in traversal `OPTIONS`.

#### Naked-LPG handling

A "naked LPG" graph is one that uses generic `nodes`/`edges` collections with a type field but lacks VCI indexes and may lack explicit property indexes. The system must handle this gracefully:

- Test fixtures include both "naked" (no indexes) and "indexed" (with VCI) LPG variants
- The transpiler detects the missing indexes via the mapping and warns
- The `doctor` command reports which indexes would improve performance
- The schema analyzer should emit index information in its export; if it does not, file an `ANALYZER_GAP` report per the no-workaround policy (§5.2)

---

## 6) Cypher parsing strategy (critical choice)

### 6.1 Requirements for a parser
We need:
- correct tokenization and precedence for expressions
- a parse tree rich enough to build an internal AST
- maintainability (avoid "hand-rolled" parser for full Cypher)

### 6.2 Candidate approaches (ranked)

#### Option A (preferred): `libcypher-parser-python`
Use `libcypher-parser-python` (Python bindings over `libcypher-parser`) to parse Cypher into a C-backed AST/parse tree, then convert into our normalized AST.

Pros:
- purpose-built Cypher parser
- avoids maintaining grammar ourselves

Cons:
- native dependency (platform wheels availability must be verified)
- AST shape may be low-level; still need a normalizer

#### Option B (fallback): ANTLR4-generated parser from openCypher grammar
Use openCypher grammar sources and generate a Python parser.

Pros:
- controllable; pure-python runtime (ANTLR runtime dependency only)
- grammar is public and versionable in the repo

Cons:
- you own grammar drift / compatibility
- need to write visitors and handle ambiguities

#### Option C (not recommended as primary): builder-only libraries
Packages like `opencypher` are helpful for *constructing* Cypher, not parsing arbitrary Cypher inputs. Not enough for a transpiler.

### 6.3 Parser evaluation plan (1-week spike)
Deliverables:
- parse a representative query corpus (MATCH/WHERE/RETURN/WITH/aggregations/patterns)
- emit a normalized internal AST
- confirm error reporting quality and performance

Decision criteria:
- feature coverage for our target subset
- reproducibility and install story (esp. Apple Silicon)
- license compatibility

### 6.4 Supported Cypher subset

Concrete reference of which Cypher constructs are supported, partially supported, or planned. Updated 2026-04-15.

#### Clauses

| Construct | Status | Limitations | Target |
|-----------|--------|-------------|--------|
| `MATCH` (single node) | Done | | v0.1 |
| `MATCH` (single hop) | Done | | v0.1 |
| `MATCH` (multi-hop / `*1..N`) | Done | Max depth cap `_MAX_VLP_DEPTH = 10` | v0.1 |
| `OPTIONAL MATCH` | Partial | Single-segment relationship pattern only; node-only or multi-segment not supported | v0.3 |
| `WHERE` | Done | See predicate table below | v0.1 |
| `RETURN` | Done | | v0.1 |
| `RETURN DISTINCT` | Partial | Single projection item only | v0.2 |
| `ORDER BY` | Done | | v0.1 |
| `LIMIT` | Partial | Integer literals only (no expressions/parameters) | v0.2 |
| `SKIP` | Partial | Integer literals only; without LIMIT uses large bound | v0.2 |
| `WITH` (pipeline) | Done | Single or multiple leading MATCHes + WITH stages | v0.2 |
| `WITH` + aggregation | Partial | Aggregation in WITH and RETURN (including `COLLECT()`); COLLECT cannot mix with other aggregates | v0.2 |
| `UNWIND` | Done | Standalone and in-query | v0.1 |
| `CASE` (simple + generic) | Done | | v0.1 |
| `UNION` / `UNION ALL` | Done | Via AQL subqueries | v0.1 |
| `CREATE` | Done | | v0.2 |
| `MERGE` | Done | Node MERGE + relationship MERGE with ON CREATE/ON MATCH SET; DEDICATED_COLLECTION and GENERIC_WITH_TYPE styles | v0.4+ |
| `SET` | Done | | v0.2 |
| `DELETE` / `DETACH DELETE` | Done | | v0.4+ |
| `FOREACH` | Not started | | v0.4+ |
| `CALL` procedure | Partial | Only `arango.*` registered procedures | v0.3 |

#### Predicates and expressions

| Construct | Status | Notes |
|-----------|--------|-------|
| Boolean `AND` / `OR` / `NOT` / `XOR` | Done | |
| Comparisons `=`, `<>`, `<`, `>`, `<=`, `>=` | Done | Chained comparisons rejected |
| `IN` list | Done | |
| `IS NULL` / `IS NOT NULL` | Done | |
| `STARTS WITH` | Done | |
| `ENDS WITH` | Done | Emulated with `RIGHT`/`LENGTH` |
| `CONTAINS` | Done | |
| `EXISTS` / pattern predicates | Done | Pattern predicates supported; `EXISTS { }` subquery implemented via ANTLR grammar extension | v0.3 |
| Regex `=~` | Done | v0.3 |
| `CASE` expressions | Done | |
| Parameters (`$param`) | Done | Positional params rejected |

#### Aggregation functions

| Function | Status | Notes |
|----------|--------|-------|
| `COUNT(*)` / `COUNT(expr)` / `COUNT(DISTINCT expr)` | Done | In WITH and RETURN |
| `SUM` / `AVG` / `MIN` / `MAX` | Done | In WITH and RETURN |
| `COLLECT` | Done | Cannot mix with other aggregates |
| Aggregation in RETURN | Done | Including collect(); v0.2 |

#### Patterns and paths

| Construct | Status | Notes |
|-----------|--------|-------|
| Inline property filters `{name: "Alice"}` | Partial | Parameterized map values `{key: $param}` not supported |
| Multi-label matching `:Person:Actor` | Partial | Requires LABEL-style mapping |
| Named paths `p = (a)-[:R]->(b)` | Done | v0.3 |
| Path functions `length()`, `nodes()`, `relationships()` | Done | v0.3 |
| List comprehensions | Not started | v0.4+ |
| Pattern comprehensions | Not started | v0.4+ |
| COUNT subquery | Not started | v0.4+ |

#### Built-in functions

| Function | Status | AQL equivalent |
|----------|--------|----------------|
| `size(expr)` | Done | `LENGTH(expr)` |
| `toLower(expr)` | Done | `LOWER(expr)` |
| `toUpper(expr)` | Done | `UPPER(expr)` |
| `coalesce(expr, ...)` | Done | `NOT_NULL(expr, ...)` |
| `type(r)` | Done | v0.2 |
| `id(n)` | Done | v0.2 |
| `labels(n)` | Done | v0.2 |
| `keys(n)` | Done | v0.2 |
| `properties(n)` | Done | v0.2 |
| `toString(expr)` | Done | v0.2 |
| `toInteger(expr)` / `toFloat(expr)` | Done | v0.2 |
| `head(list)` / `tail(list)` / `last(list)` | Done | v0.3 |
| `range(start, end[, step])` | Done | v0.3 |
| `reverse(list)` | Done | v0.3 |

---

## 7) Translation architecture (deterministic core)

### 7.1 High-level pipeline
1) **Parse** Cypher → parse tree  
2) **Normalize** parse tree → internal AST (stable shape)  
3) **Resolve** labels/types/properties using conceptual schema + physical mapping  
4) **Lower** AST → logical plan (a small set of operations: node scan, expand, filter, project, aggregate, sort, paginate)  
5) **Render** logical plan → AQL string + bind vars  
6) (Optional) **Execute** via `python-arango`

### 7.2 Internal models
- `NormalizedAst` (pydantic models or dataclasses)
- `ConceptualSchema` + `PhysicalMapping` imported from `schema_analyzer` (or mirrored types if you want to decouple)
- `LogicalPlan` nodes (dataclasses)
- `AqlQuery` object: `{ text, bind_vars, debug }`

### 7.3 Hybrid physical model handling (core requirement)
At translation time, every conceptual node label and relationship type must be resolved to a **physical implementation**:
- entity:
  - `COLLECTION` → `FOR v IN @@collection`
  - `LABEL` → `FOR v IN @@collection FILTER v[@typeField] == @typeValue`
- relationship:
  - `DEDICATED_COLLECTION` → scan/traverse the edge collection
  - `GENERIC_WITH_TYPE` → scan generic edge collection + filter by type

For a path pattern like `(a:User)-[:FOLLOWS]->(b:User)`:
- `User` might be `COLLECTION(users)` in PG but `LABEL(vertices, type="User")` in LPG
- `FOLLOWS` might be dedicated edge collection `follows` or generic `edges` with `type="FOLLOWS"`

The renderer must produce correct AQL in all combinations:
- PG vertex + PG edge
- PG vertex + LPG edge
- LPG vertex + PG edge
- LPG vertex + LPG edge

### 7.4 AQL safety
Never string-interpolate collection names or user expressions directly:
- use Arango bind parameters:
  - `@@collection` for collection names
  - `@param` for values

We should use (or replicate) the analyzer's `PhysicalMapping.aql_entity_match()` and `aql_relationship_traversal()` patterns where possible, then extend them for:
- multi-hop expansions
- OPTIONAL MATCH patterns
- multiple relationship types
- predicate pushdown

### 7.5 Error taxonomy and degradation strategy

The transpiler produces four categories of errors. Each has a defined behavior, HTTP status, and UI presentation.

| Category | Trigger | Behavior | HTTP status | UI presentation |
|----------|---------|----------|-------------|-----------------|
| **Parse error** | Invalid Cypher syntax | Fail immediately; report token position and expected tokens | 400 | Red squiggly at error token; tooltip with parse error message |
| **Unsupported construct** | Valid Cypher but not in the supported subset (§6.4) | Raise `CoreError` with the construct name and a "not supported in v0" message | 400 | Amber squiggly underline; tooltip explains which construct and which version will add it |
| **Mapping resolution failure** | Query references a label, relationship type, or property not present in the mapping | Configurable: `warn` (default) emits a warning but translates with best-effort collection name; `error` fails translation | 400 (error mode) or 200 with warnings | Warning icon in AQL panel; warnings array in JSON response |
| **Execution error** | AQL is valid but ArangoDB rejects it or returns an error (permission denied, collection not found, timeout) | Return the ArangoDB error code and message | 502 | Error banner in results panel with ArangoDB error details |
| **Connection error** | Cannot reach ArangoDB or session expired | Return connection failure details | 503 (unreachable) or 401 (session expired) | Connection dialog re-opens; toast notification with error |

**Degradation strategy:**
- Translation is **all-or-nothing** per query: the transpiler does not emit partial AQL with placeholder gaps. If any clause fails, the entire translation fails with a clear error.
- Warnings are **additive**: a successful translation may include warnings (unknown label, missing index, deprecated construct) without blocking the AQL output.
- The service never exposes raw Python tracebacks to the client; all errors are wrapped in a structured `{ "error": true, "code": "...", "message": "...", "position": {...} }` response.

### 7.6 Multi-hop patterns and path semantics

#### Variable-length traversals

Cypher patterns like `(a)-[:KNOWS*1..3]->(b)` translate to AQL graph traversals with explicit depth bounds:

```aql
FOR b, r IN 1..3 OUTBOUND a @@edgeCollection
  RETURN b
```

The transpiler caps unbounded patterns (`*` or `*..`) to `_MAX_VLP_DEPTH = 10` and emits a warning.

#### Multi-segment paths

Patterns with multiple hops like `(a)-[:KNOWS]->(b)-[:LIVES_IN]->(c)` are compiled as **nested FOR loops**, each traversal binding the next start variable:

```aql
FOR b, r1 IN 1..1 OUTBOUND a @@edgeCollection1
  FOR c, r2 IN 1..1 OUTBOUND b @@edgeCollection2
    RETURN {a, b, c}
```

#### Path uniqueness

Cypher mandates **relationship uniqueness** within a `MATCH` pattern: no relationship can appear twice in the same result row. The current implementation does **not** enforce this for multi-segment patterns beyond what ArangoDB's traversal engine provides natively. This is a known conformance gap -- the TCK will catch scenarios where this matters.

**Planned approach (v0.3):** emit `FILTER r1 != r2` predicates for multi-segment patterns, or use AQL's `PRUNE` for variable-length traversals.

#### Named paths and path functions

| Feature | Status | Planned AQL lowering |
|---------|--------|---------------------|
| `p = (a)-[:R]->(b)` | Not started | `LET p = { vertices: [a, b], edges: [r] }` |
| `length(p)` | Not started | `LENGTH(p.edges)` |
| `nodes(p)` | Not started | `p.vertices` |
| `relationships(p)` | Not started | `p.edges` |
| `shortestPath` | Partial | `CALL arango.shortest_path(...)` procedure exists; native Cypher `shortestPath()` syntax not yet parsed |
| `allShortestPaths` | Not started | `K_SHORTEST_PATHS` in AQL |

#### OPTIONAL MATCH limitations

Current OPTIONAL MATCH requires:
- A prior bound `MATCH` clause (cannot lead with OPTIONAL MATCH alone unless wrapped)
- Exactly one relationship pattern segment (no node-only or multi-segment optional)
- No variable-length patterns in the optional segment

These compile to AQL subqueries with fallback null rows. Multi-segment OPTIONAL MATCH is targeted for v0.3.

### 7.7 Performance considerations

#### Translation performance

- **Current P95 (measured 2026-04-20)**: cold-cache worst case across a 10-query representative corpus is **2.74 ms P95** (two-hop `MATCH` on `movies_pg`); single-hop is **1.54 ms P95**. PRD §2.1 target of < 50 ms is met with ~20–30× headroom. Warm-cache (WP-26 LRU hit) P95 is **≤ 0.05 ms** across every query in the corpus — sub-millisecond end-to-end for repeated identical queries, which is the steady state in a long-running service. Full report emitted by `python scripts/benchmark_translate.py` (text or `--json`). Regression guard lives in `tests/test_translate_perf.py` (gated on `RUN_PERF=1`); the gate is deliberately loose (25 ms cold / 1 ms warm) so it only fires on order-of-magnitude regressions, not CI-runner noise.
- **ANTLR parse cost**: ANTLR4 Python runtime is not cached per query, but the translation-level LRU in `arango_cypher/api.py` (WP-26, 256 entries) covers the typical repeat-query hot path. Below-LRU AST caching would offer a further ~1–2 ms win per cold-path call; not pursued because cold P95 is already well under target.
- **Mapping resolution**: `MappingResolver` construction is lightweight (dict lookups); no concern at current scale.

#### AQL quality

The transpiler prioritizes **correctness over optimization** in v0.1. Known AQL quality gaps:

| Gap | Impact | Planned fix |
|-----|--------|-------------|
| No filter pushdown into traversals | Filters on traversal target properties are applied after the traversal `FOR` loop, not as `PRUNE` or early filter | v0.2: push `FILTER` into the traversal body where safe |
| No index hint emission | The transpiler does not emit `OPTIONS { indexHint: ... }` even when the mapping declares indexed properties | v0.3: optional index hints from `PropertyInfo.indexed` |
| `IS_SAME_COLLECTION` overhead | Emitted when domain/range is unknown; optimized away when declared (§5.5.1) | Done for explicit domain/range |
| `COLLECT` / aggregation in `RETURN` | Direct `RETURN` aggregation (including `COLLECT()`) is implemented; `COLLECT` still cannot mix with other aggregates in one projection | Ongoing edge-case hardening |

#### Service resource management

- **Concurrent sessions**: in-process dict keyed by opaque token. No upper bound currently enforced. **Recommendation**: add a configurable `MAX_SESSIONS` limit (default 100) with LRU eviction.
- **Result set limits**: `POST /execute` returns all rows. **Recommendation**: add a configurable `max_rows` parameter (default 10,000) with a `truncated: true` flag.
- **Translation caching**: not implemented. For repeated identical queries with the same mapping, a `functools.lru_cache` on the `translate()` function (keyed by cypher + mapping hash) would avoid redundant parsing.

### 7.8 Index-informed transpilation strategy

The transpiler uses index metadata from the physical mapping (§5.7) to make optimization decisions. This is a key consequence of the architectural principle (§1.1): physical details like indexes live in the mapping, not in queries.

#### VCI-aware traversal optimization

When a relationship uses `GENERIC_WITH_TYPE` style and the mapping declares a VCI on the edge type field:

```
// Without VCI — transpiler must post-filter vertices after traversal
FOR v, e IN 1..1 OUTBOUND startNode edges
  FILTER e.relation == "ACTED_IN"

// With VCI — transpiler can use edge filter in traversal OPTIONS
FOR v, e IN 1..1 OUTBOUND startNode edges
  OPTIONS { edgeCollections: ["edges"] }
  FILTER e.relation == "ACTED_IN"
  // The VCI allows the storage engine to skip non-matching edges
```

The transpiler decision tree:

| Physical layout | VCI present? | Strategy |
|-----------------|-------------|----------|
| `DEDICATED_COLLECTION` | N/A | Traverse named edge collection directly — no type filter needed |
| `GENERIC_WITH_TYPE` + VCI | Yes | Emit `FILTER e.typeField == typeValue` — VCI makes this efficient at storage level |
| `GENERIC_WITH_TYPE` no VCI | No | Same filter, but log a performance warning; the filter is applied post-read |

#### Persistent index hints

When the mapping declares a persistent index on a property used in a `WHERE` filter:

```aql
FOR v IN @@collection
  OPTIONS { indexHint: "idx_persons_name", forceIndexHint: false }
  FILTER v.name == @p0
```

The transpiler emits `indexHint` only when:
1. The filter is a direct equality or range comparison on an indexed field
2. The index covers the filter fields
3. `forceIndexHint` is `false` (advisory, not mandatory — lets the query optimizer override if it has better information)

#### Implementation phasing

| Capability | Version | Notes |
|------------|---------|-------|
| Index metadata in mapping model | v0.3 (WP-18) | `IndexInfo` dataclass, `MappingResolver.resolve_indexes()` |
| VCI detection + warning | v0.3 (WP-18) | CLI `doctor`, UI introspection panel |
| VCI-aware traversal filter | v0.3 (WP-18) | Emit edge filter strategy based on VCI presence |
| Persistent index hints | v0.4+ | `OPTIONS { indexHint: ... }` emission |
| Index suggestion (agentic) | v0.4+ | `suggest-indexes` tool contract |

---

## 7A) Arango extensions and nested-document policy

### 7A.1 Design goals for extensions
- **Keep core Cypher portable**: standard Cypher should translate without needing Arango-specific constructs.
- **Namespaced and explicit**: Arango-only features must be clearly marked and easy to lint/deny in "portable mode".
- **Deterministic translation**: extensions compile to AQL predictably; no hidden runtime prompts.
- **Security**: no raw AQL injection; enforce bind variables and allowlist capabilities.

### 7A.2 Extension registry (compile-time plugin surface)
Implement an internal registry that maps Cypher-level extension calls to AQL fragments.

Conceptual interfaces:
- `FunctionRegistry`:
  - key: `arango.<name>` (e.g. `arango.bm25`, `arango.vector_near`, `arango.geo_distance`)
  - signature: argument kinds + return kind
  - compiler: `compile(call_ast, ctx) -> (aql_expr: str, bind_vars: dict, warnings: list)`
- `ProcedureRegistry`:
  - key: `arango.<name>` invoked via `CALL arango.<name>(...) YIELD ...`
  - yield schema: list of columns produced (names + conceptual types)
  - compiler: `compile(call_ast, ctx) -> (aql_subquery: str, bind_vars: dict, yielded_columns: list, warnings: list)`

Policy knobs:
- `extensions.enabled: bool` (default `false` in "portable mode"; `true` in Arango mode)
- `extensions.allowlist: set[str]` (e.g. allow `arango.search` but disallow `arango.aql`)
- `extensions.denylist: set[str]`

### 7A.3 Cypher surface syntax for extensions

#### A) Namespaced functions (`arango.*`) in expressions (preferred for v0.1)
These are used inside `WHERE`, `RETURN`, `ORDER BY`, etc., and compile to AQL expressions.

Examples (illustrative; exact names are part of the registry spec):
- Full text scoring/ranking: `arango.bm25(n)` → `BM25(n)` (when `n` comes from a view doc)
- Geo distance: `arango.geo_distance(n.location, point({longitude: 32, latitude: 45}))`
- Vector similarity: `arango.cosine_similarity(n.embedding, $queryEmbedding)`

Constraints:
- Must compile to **pure AQL expressions** (no new FROM sources).
- If an extension requires a different FROM (e.g. ArangoSearch view), it must be a procedure (or a clause extension in later versions).

#### B) Procedures (`CALL arango.*`) for source-changing operations
Some Arango features are fundamentally "source changing" (e.g. querying an ArangoSearch view, vector topK retrieval). Those are better expressed as procedures that yield rows.

Shape:
- `CALL arango.search("viewName", {query: "...", ...}) YIELD doc, score`
- `CALL arango.vector_near("collectionOrView", $vector, {k: 20}) YIELD doc, distance`

Compilation model:
- compiles into an AQL subquery that returns an array of rows, then unwinds into the Cypher pipeline.

Initial v0.1 stance:
- We can **design** the procedure interface in v0.1, but implement only a minimal subset once we have `WITH` pipeline semantics working (v0.2+).

#### C) Escape hatch (optional, disabled by default): `CALL arango.aql(text, bindVars)`
This is powerful but risky. If included at all:
- **off by default**
- requires explicit allowlist enablement
- requires bind vars provided separately
- must run in a "least privilege" execution mode

### 7A.4 Mapping Arango capabilities (full text, vector, geo)
High-level mapping targets for the registry:
- **Full-text search**:
  - procedure form that changes source to an ArangoSearch view
  - AQL: `FOR d IN view SEARCH ... SORT BM25(d) DESC RETURN ...`
- **Vector search**:
  - procedure form that returns topK candidates
  - AQL pattern depends on index type/version, but always: (a) query vector (b) topK results (c) return docs + distance/score
- **Geospatial search**:
  - function form (distance computations)
  - procedure form if we want geo "near" as a source

Implementation note:
- The registry is how we keep these additions from contaminating the core transpiler logic.

#### Registered extensions (implemented)

All extensions below are implemented in `arango_cypher/extensions/` and registered via `register_all_extensions()`.

**Functions** (used in expressions: `WHERE`, `RETURN`, `ORDER BY`, etc.)

| Module | Cypher syntax | AQL output | Category |
|--------|--------------|------------|----------|
| `search.py` | `arango.bm25(doc[, k, b])` | `BM25(...)` | Full-text |
| `search.py` | `arango.tfidf(doc[, normalize])` | `TFIDF(...)` | Full-text |
| `search.py` | `arango.analyzer(expr, analyzerName)` | `ANALYZER(...)` | Full-text |
| `vector.py` | `arango.cosine_similarity(v1, v2)` | `COSINE_SIMILARITY(...)` | Vector |
| `vector.py` | `arango.l2_distance(v1, v2)` | `L2_DISTANCE(...)` | Vector |
| `vector.py` | `arango.approx_near_cosine(v1, v2[, opts])` | `APPROX_NEAR_COSINE(...)` | Vector |
| `vector.py` | `arango.approx_near_l2(v1, v2[, opts])` | `APPROX_NEAR_L2(...)` | Vector |
| `geo.py` | `arango.distance(lat1, lon1, lat2, lon2)` | `DISTANCE(...)` | Geo |
| `geo.py` | `arango.geo_distance(a, b[, ellipsoid])` | `GEO_DISTANCE(...)` | Geo |
| `geo.py` | `arango.geo_contains(a, b)` | `GEO_CONTAINS(...)` | Geo |
| `geo.py` | `arango.geo_intersects(a, b)` | `GEO_INTERSECTS(...)` | Geo |
| `geo.py` | `arango.geo_in_range(a, b, low, high[, incLow, incHigh])` | `GEO_IN_RANGE(...)` | Geo |
| `geo.py` | `arango.geo_point(lon, lat)` | `GEO_POINT(...)` | Geo |
| `document.py` | `arango.attributes(doc[, ...])` | `ATTRIBUTES(...)` | Document |
| `document.py` | `arango.has(doc, attr)` | `HAS(...)` | Document |
| `document.py` | `arango.merge(doc1, doc2, ...)` | `MERGE(...)` | Document |
| `document.py` | `arango.unset(doc, attr1, ...)` | `UNSET(...)` | Document |
| `document.py` | `arango.keep(doc, attr1, ...)` | `KEEP(...)` | Document |
| `document.py` | `arango.zip(keys, values)` | `ZIP(...)` | Document |
| `document.py` | `arango.value(doc, path)` | `VALUE(...)` | Document |
| `document.py` | `arango.values(doc[, ...])` | `VALUES(...)` | Document |
| `document.py` | `arango.flatten(array[, depth])` | `FLATTEN(...)` | Document |
| `document.py` | `arango.parse_identifier(id)` | `PARSE_IDENTIFIER(...)` | Document |
| `document.py` | `arango.document(id)` or `(coll, key)` | `DOCUMENT(...)` | Document |

**Procedures** (used via `CALL arango.*(...) YIELD ...`)

| Module | Cypher syntax | AQL output | Category |
|--------|--------------|------------|----------|
| `procedures.py` | `CALL arango.fulltext(coll, attr, query)` | `FULLTEXT(coll, attr, query)` | Full-text |
| `procedures.py` | `CALL arango.near(coll, lat, lon[, limit])` | `NEAR(...)` | Geo |
| `procedures.py` | `CALL arango.within(coll, lat, lon, radius)` | `WITHIN(...)` | Geo |
| `procedures.py` | `CALL arango.shortest_path(start, target, edgeColl, dir)` | `FOR v IN dir SHORTEST_PATH start TO target edgeColl RETURN v` | Graph |
| `procedures.py` | `CALL arango.k_shortest_paths(start, target, edgeColl, dir)` | `FOR p IN dir K_SHORTEST_PATHS start TO target edgeColl RETURN p` | Graph |

### 7A.5 Nested-document policy (hierarchical JSON)

#### Default: nested documents are properties (dot-path access)
For a document like:
`{"location":{"long":32,"lat":45},"address":{"zip":1234567,"city":"Springfield"}}`

Default semantics:
- `n.address.zip` is a property access on `n`
- in AQL, this lowers to `n.address.zip` (with safe handling for missing fields where needed)

This is the recommended default because it matches ArangoDB's document model and keeps translation predictable.

#### Optional (mapping-driven): embedded conceptual entities + embedded relationships
Some nested objects should behave like conceptual nodes/relationships (without necessarily being physically separate documents).

We will support this only when the **mapping** (from `arangodb-schema-analyzer` or explicit override) declares an embedded relationship, e.g.:
- conceptual relationship: `(:User)-[:HAS_ADDRESS]->(:Address)`
- physical: `User.address` is an embedded object

Lowering strategy:
- treat the "virtual node" value as a computed value (`LET addr = u.address`)
- allow property predicates/projections over that value (`addr.zip`, `addr.city`)

Critical constraint:
- Unless we define a stable identity rule, "virtual nodes" do not have real `_id` and cannot participate in general graph traversal semantics.

### 7A.6 "Virtual edges" support: v0.1 vs later

#### Definition
A "virtual edge" is a conceptual relationship where the physical representation is:
- embedded object/array inside a document, or
- a foreign-key-like reference field (e.g. `user.companyId`) without an edge collection.

#### v0.1 (supported)
- **Dot-path property access** (nested documents) in expressions and projections.
- **Embedded relationship as computed value** (mapping-driven):
  - one-hop only
  - no path expansion
  - no uniqueness semantics beyond what the document already provides
  - no `MERGE`/writes

Example of what v0.1 can do:
- `(u:User)` with mapping declaring `HAS_ADDRESS` embedded:
  - allow `MATCH (u:User) WHERE u.address.zip = 1234567 RETURN u.address.city`
  - allow `MATCH (u:User)-[:HAS_ADDRESS]->(a:Address) WHERE a.zip = 1234567 RETURN a`
    - implemented as `LET a = u.address` (no separate scan)

#### v0.1 (not supported)
- Variable-length traversal over virtual edges: `-[:HAS_ADDRESS*1..3]->`
- Joining virtual nodes across rows by identity (no `_id`)
- Using virtual edges with graph algorithms or path uniqueness semantics
- OPTIONAL MATCH semantics involving virtual edges unless explicitly designed (defer)

#### Later (v0.3+) possible extensions
If you want richer semantics, we can add one (or more) explicit identity strategies:
- **Synthetic identity** for embedded objects (e.g. hash of parent `_id` + JSON pointer)
- **Materialized view** strategy: treat embedded objects as a derived "virtual collection" via AQL views/subqueries
- **FK expansion** strategy: treat `*_id` fields as joinable references with `DOCUMENT()`

These should be **explicitly configured** because they affect correctness/performance expectations.

---

## 8) Testing strategy & migration plan

### 8.1 Test layers
- **Unit tests** (pure Python):
  - parser normalization tests
  - mapping resolution tests
  - AQL rendering tests
- **Golden tests** (snapshot):
  - Cypher input → expected AQL + bind vars
  - stable formatting enforced
- **Integration tests** (requires ArangoDB):
  - run AQL and validate results on a seeded dataset
  - validate hybrid mappings end-to-end

Recommended libs:
- `pytest`, `pytest-cov`
- `syrupy` (or `pytest-regressions`) for snapshots
- `hypothesis` for property-based tests (optional but great for expression rendering and bind var stability)
- `docker` / `docker compose` for integration environment

### 8.2 openCypher TCK (Technology Compatibility Kit)

The openCypher project publishes a **Technology Compatibility Kit** as Gherkin `.feature` files at [github.com/opencypher/openCypher/tree/master/tck/features](https://github.com/opencypher/openCypher/tree/master/tck/features). Each `.feature` contains scenarios that specify graph setup, a Cypher query, and expected results -- providing exhaustive coverage of Cypher language semantics.

#### Current implementation

| Component | Path | Status |
|-----------|------|--------|
| Download script | `scripts/download_tck.py` | Implemented -- fetches `.feature` files from GitHub API; supports `--only-match` filter |
| Gherkin parser | `tests/tck/gherkin.py` | Implemented -- extracts Feature/Scenario/Step with doc strings and data tables |
| TCK runner | `tests/tck/runner.py` | Implemented -- translates Cypher, executes AQL, compares results; produces passed/skipped/failed |
| Sample feature | `tests/tck/features/sample.feature` | One trivial scenario (empty graph returns empty) |
| Real TCK features | not downloaded | `.feature` files are fetched on demand, not checked in |

#### Running TCK tests

```bash
python scripts/download_tck.py                       # download all features
python scripts/download_tck.py --only-match Match1    # download subset
RUN_INTEGRATION=1 RUN_TCK=1 pytest -m tck             # run harness
```

#### Current coverage gap

Most scenarios are **skipped** because:
- `Given having executed:` setup still fails for some write patterns (relationship `MERGE`, exotic `CREATE`/`SET` shapes, etc.) even though `CREATE`, `SET`, `DELETE`/`DETACH DELETE`, and node `MERGE` are now supported
- Some Cypher constructs (list comprehensions, `FOREACH`, multiple relationship types in one hop) are not yet supported
- `Scenario Outline` / `Examples` parameterized scenarios are not expanded by the parser

#### What must be implemented to make the TCK useful

1. **Write clause support** (at minimum `CREATE` for graph setup) -- single biggest blocker
2. **Result normalization** -- TCK uses Neo4j conventions for node/relationship literals; the runner needs a normalizer that compares structurally
3. **Error expectation scenarios** -- some scenarios assert a query *should* produce an error
4. **Scenario Outline / Examples expansion** -- the Gherkin parser should expand parameterized scenarios

#### Phased TCK strategy

| Phase | Scope | TCK impact |
|-------|-------|------------|
| v0.1 | Core `MATCH`/`WHERE`/`RETURN`/`WITH`/aggregation | Scenarios starting from empty graph with no setup (rare) |
| v0.2 | Add `CREATE`/`SET` (at least for setup) | Unlocks the vast majority of "Given having executed" steps |
| v0.3 | `OPTIONAL MATCH`, `UNWIND`, `CASE`, path patterns | Unlocks `OptionalMatch`, `Unwind`, `Expressions` features |
| v0.4+ | Remaining gaps (list comprehensions, `FOREACH`, etc.) | Progressively approaches full TCK pass rate |

### 8.3 Neo4j sample datasets with bundled Cypher queries

Neo4j publishes well-known sample datasets, each with seed data, a data model, and example Cypher queries. These test **practical correctness** -- do real-world queries against realistic data return right answers?

#### Available datasets

| Dataset | Source | Graph pattern | Typical queries |
|---------|--------|---------------|-----------------|
| **Movies** | [neo4j-graph-examples/movies](https://github.com/neo4j-graph-examples/movies) | `Person`-`ACTED_IN`/`DIRECTED`->`Movie` | Actor lookup, co-actors, shortest paths, recommendations |
| **Northwind** | [neo4j-graph-examples/northwind](https://github.com/neo4j-graph-examples/northwind) | `Customer`-`PURCHASED`->`Order`-`CONTAINS`->`Product` | Supply chain queries, aggregations |
| **ICIJ (Paradise Papers)** | [neo4j-graph-examples/icij-paradise-papers](https://github.com/neo4j-graph-examples/icij-paradise-papers) | `Entity`/`Officer`/`Intermediary`/`Address` | Investigation traversals, shortest paths |

#### Current implementation

| Component | Path | Status |
|-----------|------|--------|
| Movies LPG fixture | `tests/fixtures/datasets/movies/lpg-data.json` | Implemented -- full dataset (~170 nodes, ~250 edges) |
| Movies PG fixture | `tests/fixtures/datasets/movies/pg-data.json` | Implemented -- separate `persons`/`movies` + per-rel edge collections |
| Movies seeders | `tests/integration/datasets.py` | `seed_movies_lpg_dataset(db)`, `seed_movies_pg_dataset(db)` |
| Movies smoke tests | `tests/integration/test_neo4j_movies_dataset_smoke.py` | Multi-label, edge property filter, unlabeled start node |
| Movies query corpus | `tests/fixtures/datasets/movies/query-corpus.yml` | 20 queries, runs against both LPG and PG |
| Northwind PG fixture | `tests/fixtures/datasets/northwind/pg-data.json` | Implemented -- 6 vertex + 6 edge collections |
| Northwind seeder | `tests/integration/datasets.py` | `seed_northwind_dataset(db)` |
| Northwind query corpus | `tests/fixtures/datasets/northwind/query-corpus.yml` | 14 queries (projections, filters, multi-hop, OPTIONAL MATCH, COLLECT) |
| Neo4j reference driver | `tests/integration/neo4j_reference.py` | Connection (`bolt://127.0.0.1:27687`), `seed_neo4j_movies`, generic `seed_neo4j_pg`, `seed_neo4j_northwind`, `ensure_dataset` cross-module guard, `run_cypher` with scalar coercion |
| Neo4j container | `docker-compose.neo4j.yml` | Neo4j Community, `openSesame` credentials, shared across suites |
| Cross-validation -- Movies | `tests/integration/test_movies_crossvalidate.py` | 20/20 pass (all divergence flags removed) |
| Cross-validation -- Northwind | `tests/integration/test_northwind_crossvalidate.py` | 14/14 pass |
| Social dataset | `tests/integration/seed.py` | `seed_social_dataset(db, mode)` for PG/LPG/hybrid |

#### §8.3.1 Cross-validation harness

Cross-validation is the strongest correctness gate available short of a full openCypher TCK pass: for each query in a corpus, we run the **raw Cypher** against the canonical Neo4j engine and the **translated AQL** against ArangoDB, then assert that both engines produced semantically identical results. A disagreement is, by construction, a translator bug (or a corpus bug where the query under-specifies ordering).

Key design points:

- **Row equivalence** (`assert_result_equivalent` in `test_movies_crossvalidate.py`, reused by Northwind):
  - Column count must match; column names are allowed to diverge because Neo4j preserves raw dotted expressions (`p.name`) while AQL must rename them.
  - Row count must match exactly.
  - When the query contains `ORDER BY`, rows are compared **position-wise**.
  - Otherwise, both result sets are sorted by a deterministic key derived from the normalized row values and compared as multisets.
  - Scalars are normalized via `_normalize_scalar`: `float`/`int` round-trip (AQL `SUM`/`AVG` returns floats), missing keys and `None` compare equal, nested lists/dicts recurse.
- **Dataset isolation on shared Neo4j Community** (`ensure_dataset`): Neo4j Community has a single writable database, so each seeder wipes it first. The shared module-level `_active_dataset` tracks which corpus is currently loaded; each test module's driver fixture calls `ensure_dataset(driver, name, seed_fn)` and reseeds only on change, so mixing suites in a single pytest session (either order) works.
- **Divergence escape hatch**: corpus entries may carry a `divergence:` free-form string. The test still translates (so a translator crash is never masked) and still asserts `expected_min_count` on the Neo4j side, but skips the row-equivalence check and records the divergence as the skip reason. This gate is the workflow for landing a harness before the translator is bug-free. As of 2026-04-17 **no divergence flags remain on either Movies or Northwind**.
- **Tie-break discipline on the corpus**: any query without a total `ORDER BY` is compared as a multiset; queries with a non-total `ORDER BY` must still be stabilized in the corpus (e.g., the 2026-04-17 update on `nw_order_count_by_customer` added `, c.companyName` to a `ORDER BY orderCount DESC`). Both Cypher and AQL are entitled to break ties freely.

Activation:

```bash
docker compose -f docker-compose.neo4j.yml -p arango_cypher_neo4j up -d
RUN_INTEGRATION=1 RUN_CROSS=1 pytest tests/integration/test_movies_crossvalidate.py \
                                     tests/integration/test_northwind_crossvalidate.py
```

Adding another dataset now takes three steps:

1. Seed fixture + query corpus under `tests/fixtures/datasets/<name>/`.
2. Add a `seed_neo4j_<name>(driver)` in `neo4j_reference.py` (usually a one-line wrapper around `seed_neo4j_pg` with label/reltype overrides derived from the mapping fixture).
3. Copy `test_northwind_crossvalidate.py` as a template; swap in the new seeder and mapping name.

#### Next steps (requirements)

1. **Add ICIJ Paradise Papers cross-validation** -- mapping fixture and corpus already exist (5 golden queries); wire up a `seed_neo4j_icij` and a cross-validation suite following the Northwind template.
2. **Add PG-layout Movies cross-validation** -- the LPG corpus already passes; add a separate suite that points the same corpus at the PG fixture + `movies_pg` mapping to verify layout-independent correctness.
3. **Automate dataset download** -- a script similar to `scripts/download_tck.py`:
   ```bash
   python scripts/download_neo4j_dataset.py --dataset movies --format lpg
   python scripts/download_neo4j_dataset.py --dataset northwind --format pg
   ```
4. **CI wiring** -- add the `cross` marker tier to nightly CI (see §8.3's CI integration table below); the harness needs a Neo4j container in the runner.

#### How TCK and dataset tests complement each other

| Dimension | openCypher TCK | Neo4j sample datasets |
|-----------|---------------|----------------------|
| **Purpose** | Language conformance | Practical correctness |
| **Scope** | Exhaustive Cypher syntax/semantics | Domain-specific patterns |
| **Data setup** | Each scenario creates its own tiny graph | Shared realistic dataset seeded once |
| **What it catches** | Edge cases (NULL handling, type coercion, uniqueness) | Integration bugs (wrong collection, missing filter, wrong join direction) |
| **Schema coverage** | Mostly single-label, simple graphs | Multi-label, multi-relationship-type, realistic data |
| **Mapping coverage** | Tested against one mapping (LPG) | Should be tested against LPG, PG, and hybrid |

#### CI integration

| Tier | Command | When to run |
|------|---------|-------------|
| Fast (unit + golden) | `pytest -m "not integration and not tck"` | Every commit / PR |
| Integration (datasets) | `RUN_INTEGRATION=1 pytest -m integration` | Every PR, nightly |
| Cross-validation (Neo4j equivalence) | `RUN_INTEGRATION=1 RUN_CROSS=1 pytest -m cross` | Nightly or on-demand; requires `docker compose -f docker-compose.neo4j.yml up -d` |
| TCK | `RUN_INTEGRATION=1 RUN_TCK=1 pytest -m tck` | Nightly or on-demand |

### 8.4 Converting existing Foxx (`arango-cypher-foxx`) tests
We'll treat the legacy JS/Foxx test suite as **spec** and migrate in stages:

#### Step 1: Extract a corpus
Create a `tests/fixtures/cypher_cases/` directory with files like:
- `case_001.yml`:
  - `cypher`
  - `expected_aql`
  - `expected_bind_vars`
  - optional `mapping_override`
  - optional `notes`

#### Step 2: Recreate "golden AQL" expectations
For translation parity, snapshot the AQL output of the Foxx version (where applicable) and store as expected outputs for the Python version.

#### Step 3: Add integration semantics incrementally
Once translation matches, add integration assertions:
- seed database with a minimal dataset
- execute AQL produced by Python
- compare result sets (order-insensitive unless ORDER BY is part of query)

#### Step 4: Optional: compatibility harness
If you want automation, we can build a small script that:
- runs the JS translator to capture AQL outputs
- emits Python fixture YAML files
This reduces manual porting effort dramatically.

---

## 9) Agentic workflow support (optional, but easy to add)
Keep deterministic translation as the source of truth.

Add an optional "tool contract" layer similar to the schema analyzer:
- `translate_tool(request_dict) -> response_dict`
  - request fields: cypher, connection (optional), mapping (optional), options
  - response: ok/error, aql, bind_vars, mapping_summary, warnings

Where agentic adds value:
- "Explain why this label mapped to that collection"
- "Suggest missing indexes for performance"
- "Propose a mapping override for ambiguous hybrid areas"

Recommended libs (optional):
- `pydantic` for tool IO models
- whichever LLM provider you already use; do not make it a hard dependency

---

## 10) Phased delivery plan (unified roadmap)

This section consolidates all phasing schemes (original Phase 0-6, UI §4.4.9, TCK §8.2) into a single version-aligned roadmap. Each version lists Cypher features, infrastructure, UI, and testing milestones together.

> **Detailed implementation plan**: For work package breakdowns, dependency graphs, file-level deliverables, and execution order, see **[`implementation_plan.md`](./implementation_plan.md)**.

### v0.1 — Core read-only transpiler ✅ COMPLETE

| Area | Deliverables | Status |
|------|-------------|--------|
| **Infrastructure** | Project layout, `pyproject.toml`, CI skeleton, ANTLR4 parser from openCypher grammar, DB connection config | Done |
| **Cypher** | MATCH (node, single-hop, multi-hop, variable-length), WHERE (boolean, comparisons, IN, IS NULL, STARTS WITH, ENDS WITH, CONTAINS), RETURN (columns, aliases), ORDER BY, LIMIT, SKIP, UNWIND, CASE, UNION | Done |
| **Mapping** | `MappingBundle` + `MappingResolver` consuming export JSON, property resolution, domain/range inference, IS_SAME_COLLECTION optimization | Done |
| **Extensions** | `arango.*` registry: search, vector, geo, document functions + procedures | Done |
| **Service** | FastAPI with 16+ endpoints including `/nl2cypher`, `/corrections`, `/schema/introspect` | Done |
| **UI** | Cypher editor (syntax highlighting, autocompletion). AQL editor (editable, syntax-directed editing — §4.4.3). Results table/JSON/graph. JSON mapping panel + schema graph. Connection dialog with auto-introspect + DB selector. NL2Cypher bar + history + token display. Local learning (corrections store). Export (CSV/JSON). | Done |
| **Testing** | Golden tests (YAML fixtures), integration tests (Movies + social datasets), TCK infrastructure (download, parse, run) | Done |
| **Success criteria** | 100% golden tests passing, 100% integration tests passing | Met |

### v0.2 — Write clauses + aggregation completeness + schema analyzer

| Area | Deliverables | Status |
|------|-------------|--------|
| **Cypher** | CREATE (at minimum, for TCK setup), SET, RETURN DISTINCT (multi-column), LIMIT/SKIP with expressions, aggregation in RETURN (not just WITH), `type(r)`, `id(n)`, `labels(n)`, `keys(n)`, `properties(n)`, `toString()`, `toInteger()`, `toFloat()` | **Partial** — CREATE/SET/DELETE/DETACH DELETE/node MERGE, aggregation in RETURN (incl. `collect()`), listed builtins, regex `=~`, named paths + path functions: **done**. RETURN DISTINCT (multi-column), LIMIT/SKIP with expressions: **not started** |
| **Mapping** | `acquire_mapping_bundle(db)` -- wire up `arangodb-schema-analyzer` as optional dependency. Fast heuristic classifier. Caching by schema fingerprint. `get_mapping(db)` convenience API. | Done |
| **CLI** | `translate`, `run`, `mapping`, `doctor` subcommands. `console_scripts` entry point in `pyproject.toml`. | Done |
| **UI** | Parameter binding panel. Bind-vars panel. Query history (multi-entry, searchable). Keyboard shortcuts (§4.4.8). | Done |
| **Testing** | TCK: ≥ 40% of Match*.feature passing. Scenario Outline/Examples expansion in Gherkin parser. Result normalization. | Not started |
| **Success criteria** | CREATE compiles and executes. End-to-end mapping from live DB. CLI fully functional. TCK Match ≥ 40%. | |

### v0.3 — Language breadth + UI completeness + datasets

| Area | Deliverables | Status |
|------|-------------|--------|
| **Cypher** | Full OPTIONAL MATCH (multi-segment, node-only). EXISTS / pattern predicates. Regex `=~`. Named paths + path functions. Multi-label matching for COLLECTION-style. Native `shortestPath()` syntax. `head`, `tail`, `last`, `range`, `reverse`. | **Done** — all items complete except native `shortestPath()` (needs ANTLR grammar extension; available via `CALL arango.shortest_path()` extension). Full OPTIONAL MATCH with comma-separated parts: **done**. Multi-label COLLECTION-style: **done** (uses primary label + warning). |
| **Mapping** | OWL Turtle round-trip (load + generate). `rdflib` ingestion path. Index-aware mapping model (`IndexInfo`, VCI detection, `resolve_indexes()`). | **Done** — OWL Turtle round-trip: **done**. `rdflib` ingestion (`parse_owl_with_rdflib`, `[owl]` extra): **done**. `IndexInfo`, VCI detection, `resolve_indexes()`: **done**. |
| **Optimization** | VCI-aware traversal filter emission. VCI/index advisory in `doctor` and UI. Index metadata in schema analyzer export (or `ANALYZER_GAP`). | **Partial** — VCI warning in transpiler; heuristic builder populates indexes from DB; `doctor`/UI advisory polish: **not started** |
| **NL2Cypher** | Two-stage NL→Cypher→AQL pipeline (§1.2). **Stage 1 (LLM):** pluggable LLM provider converts natural language to Cypher. The LLM prompt contains only the conceptual schema (entity labels, relationship types, properties, domain/range) — never collection names, type fields, or AQL. Same pattern as LangChain's `GraphCypherQAChain`. Schema context builder function. Validation loop (parse failure → retry). **Stage 2 (transpiler):** existing deterministic Cypher→AQL transpiler — no changes needed. UI: NL input mode in query editor. | **Partial** — LLM path + rule-based fallback: **done**. Pluggable providers (OpenAI + OpenRouter): **done**. Prompt leak audit + ANTLR validation/retry + enhanced AQL validation: **done**. Domain/range inference for PG edges: **done**. Token usage: **done**. |
| **UI** | Visual mapping graph editor (Cytoscape.js + dagre, bidirectional sync). Results graph view. AQL Profile button + annotated plan. Variable-use highlighting. Clause outline. AQL editor editable with syntax-directed editing (§4.4.3). Local learning (§14.1). Sample queries loader (corpus files). NL input + history. Token usage display. | **Done** — All items complete. Clause outline: **done**. Sample queries loader: **done**. Bidirectional graph editing: **done**. Profile-aware warnings: **done**. Correspondence hints (Cypher↔AQL hover highlighting): **done**. |
| **Testing** | Expand Movies dataset to full (~170 nodes). Add Movies query corpus YAML. PG layout variant. Add Northwind dataset. TCK overall ≥ 25%. Naked-LPG variant (no VCI). | **Done** — Movies full dataset + query corpus (PG + LPG), Northwind corpus, social dataset integration tests: **done**. TCK 66.1% projected (clause-focused), exceeding 25% target. |
| **Success criteria** | User can connect, translate, execute, and view results without touching JSON. Full Movies query corpus passing against LPG + PG. TCK ≥ 25%. VCI advisory working. NL2Cypher generates valid Cypher for basic queries. | |

### v0.4+ — Advanced features + TCK convergence

| Area | Deliverables | Status |
|------|-------------|--------|
| **Cypher** | **Done:** node MERGE + relationship MERGE with ON CREATE/ON MATCH SET; DELETE/DETACH DELETE; FOREACH; list/pattern comprehensions; EXISTS { } subquery; COUNT { } subquery; WITH pipeline from multiple MATCHes. | **Done** |
| **Optimization** | Filter pushdown into traversals. Index hint emission from `PropertyInfo.indexed`. Translation caching. Relationship uniqueness enforcement. | **Done** — Filter pushdown (PRUNE for variable-length): **done**. Translation caching (LRU OrderedDict, 256 entries): **done**. Relationship uniqueness (`r1._id != r2._id`): **done**. Index hint emission: **not started**. |
| **UI** | Hover documentation. Profile-aware warnings. Format/prettify. Correspondence hints (source maps — §4.4.3F). Multi-statement. | **Done** — All items complete. Profile-aware warnings: **done**. Correspondence hints (Cypher↔AQL hover highlighting): **done**. |
| **Agentic** | `translate_tool(request_dict)` JSON-in/JSON-out wrapper. Explain-why-mapped. Suggest-indexes. Propose-mapping-overrides. | **Done** — 8 tools: translate, suggest_indexes, explain_mapping, cypher_profile, propose_mapping_overrides, explain_translation, validate_cypher, schema_summary. |
| **Testing** | TCK overall ≥ 60%. Automate dataset download script. ICIJ Paradise Papers dataset. | **Done** — TCK projected 66.1% (clause-focused). ICIJ Paradise Papers: mapping fixture, seed script, 5 query golden tests: **done**. |
| **Success criteria** | TCK ≥ 60%. All CLI subcommands work. Agentic tool contract functional. | |

---

## 11) Naming & repo strategy (your explicit questions)

### Should this be a new project?
**Yes.** The runtime and dependency model is fundamentally different from Foxx. Keep both until Python is mature.

### How should we name it?
Recommended:
- **Repo**: `arango-cypher-py`
- **Python package**: `arango_cypher` (import-friendly)
- **CLI**: `arango-cypher-py`

### Should we rename the existing `arango-cypher` (Foxx)?
**Resolved (2026-04-17):** yes. The Foxx repo was renamed to `arango-cypher-foxx`, and this Python project stabilized as `arango-cypher-py`. The `-foxx` / `-py` suffixes mirror each other and truthfully describe what each package is (in-database Foxx microservice vs. out-of-process Python distribution). The bare `arango-cypher` name is intentionally kept free on the `arango-solutions` org for a potential future umbrella/spec repo that describes the Cypher→AQL concept and links to implementations. The GitHub rename of `arango-solutions/arango-cypher` → `arango-solutions/arango-cypher-py` is pending org-admin action; `[project.urls]` and git `pushurl` will be updated once the rename lands, and GitHub's automatic redirect keeps the current URL working in the meantime.

---

## 12) Proposed tech stack (summary)
- **DB**: `python-arango`
- **Schema mapping**: `arangodb-schema-analyzer` (library + tool contract)
- **Cypher parsing**: ANTLR4 openCypher grammar (in-repo `grammar/Cypher.g4`)
- **CLI**: `typer` + `rich`
- **Service**: `fastapi` + `uvicorn`
- **TTL parsing** (optional ingestion): `rdflib`
- **Testing**: `pytest`, `pytest-cov`, `httpx` (service tests), snapshot (`syrupy`)
- **Quality**: `ruff` + `mypy` (optional early; recommended by v0.2)
- **Frontend (Cypher Workbench UI)**:
  - **Framework**: React + TypeScript (Vite)
  - **Editor**: CodeMirror 6 (Cypher + AQL language modes via Lezer grammars)
  - **Graph viz**: Cytoscape.js
  - **Execution plan viz**: react-d3-tree or custom tree component
  - **Styling**: Tailwind CSS

---

## 13) Resolved questions (formerly open)

These questions were listed as "to resolve during Phase 1-2". Phase 1-2 work is complete; resolutions recorded here.

| Question | Resolution |
|----------|------------|
| Exact Cypher subset required for v0.1: do you need `WITH` immediately? | **Yes.** `WITH` pipeline is implemented (partial: single leading MATCH + WITH stages). Full subset documented in §6.4. |
| Translation parity with Foxx outputs, or "best AQL" even if it differs? | **Best AQL.** The Python transpiler generates its own AQL style (bind-parameter-heavy, IS_SAME_COLLECTION optimization). Foxx parity is not a goal; correctness is validated via golden tests and integration tests, not output comparison. |
| Canonical field names for LPG type fields (`type`, `_type`, `label`)? | **Configurable via mapping.** The mapping's `typeField` / `typeValue` fields specify the physical field name per entity/relationship. Common conventions: `type`, `labels` (array), `relation`. The transpiler does not assume any default -- the mapping is authoritative. |
| Constraints on running native deps (`libcypher-parser-python`)? | **ANTLR4 chosen.** Pure-Python ANTLR4 runtime avoids native dependency issues (Apple Silicon, musl, Windows). `libcypher-parser-python` remains an optional future migration if performance warrants it (see §6.2). |

## 14) Remaining open design questions

| Question | Owner | Target resolution |
|----------|-------|-------------------|
| Should `CREATE` in v0.2 be full write-clause support or only enough for TCK setup seeding? | Product | v0.2 planning |
| How should path uniqueness (relationship isomorphism) be enforced for multi-segment patterns? `FILTER r1 != r2` vs `PRUNE` vs post-filter? | Engineering | v0.3 |
| Should the visual mapping editor support drag-to-create relationships, or only form-based creation? | Design | v0.3.1 |
| What is the caching invalidation strategy for `acquire_mapping_bundle()` when the database schema changes? | Engineering | v0.2 |
| Should the CLI support piping (`echo "MATCH..." \| arango-cypher-py translate`)? | Product | v0.2 |
| Which LLM provider(s) should NL2Cypher support? §1.2 requires pluggable providers. Which are prioritized for v0.3? (OpenAI, Anthropic, local models via Ollama) | Product | v0.3 planning |
| Should VCI creation be automatic (ELT) or require explicit user approval? | Engineering | v0.3 |
| Should the schema analyzer export include index metadata natively, or should the transpiler query indexes separately via `python-arango`? | Engineering | v0.3 |
| ~~How should the UI present NL input — as a separate editor mode, a toggle, or a dedicated panel?~~ | Design | **Resolved** — implemented as "Ask" bar above Cypher editor with NL query history dropdown (localStorage). |
| **Local learning from user corrections** — see §14.1 below | Engineering / Product | **Both paths done.** Cypher→AQL corrections via `arango_cypher/corrections.py` (SQLite, exact-match override). NL→Cypher corrections via `arango_cypher/nl_corrections.py` (SQLite, fed into BM25 few-shot retriever through `_invalidate_default_fewshot_index` listener, shipped 2026-04-20 as Wave 4o). HTTP surface: `POST /corrections`, `POST /nl-corrections`, plus `GET` / `DELETE` on both collections. |

### 14.1) Local learning from user corrections

Users frequently need to edit the LLM-generated Cypher or the transpiled AQL before it produces correct results. These corrections represent high-value training signal that could improve future queries against the same database.

**Two feedback points in the pipeline:**

1. **NL → Cypher** *(implemented 2026-04-20 as Wave 4o)*: User edits the generated Cypher, runs it successfully, clicks "Learn." The `(question, corrected_cypher, mapping_hash)` tuple is stored in `nl_corrections.db` (SQLite). Storage module `arango_cypher/nl_corrections.py` mirrors the `corrections.py` API at the NL layer, and exposes a `register_invalidation_listener(callable)` channel. `arango_cypher/nl2cypher/_core.py` registers `_invalidate_default_fewshot_index()` on first use, so every save/delete triggers a lazy rebuild of the process-wide BM25 `FewShotIndex` on the next translation — the corrected example re-enters the prompt as a few-shot hint against similar future questions. Corrections are appended after shipped corpora so they win BM25 ties against equal-scoring seed pairs. HTTP surface: `POST /nl-corrections`, `GET /nl-corrections`, `DELETE /nl-corrections/{id}`, `DELETE /nl-corrections` (clear-all).

2. **Cypher → AQL** *(implemented)*: User edits the transpiled AQL, runs it successfully, clicks "Learn." The `(cypher, corrected_aql, mapping_hash)` pair is stored and used as an override for identical Cypher inputs. These corrections also serve as a transpiler bug discovery queue.

**Storage (implemented):** Local SQLite file (`corrections.db`) with an `aql_corrections` table. Schema: `id`, `cypher`, `mapping_hash` (SHA-256 of conceptual + physical mapping), `database`, `original_aql`, `corrected_aql`, `bind_vars`, `created_at`, `note`. All data stays local — nothing is sent externally. Thread-safe via `threading.Lock`.

**Implementation details:**
- Backend module: `arango_cypher/corrections.py`
- REST API: `POST /corrections` (save), `GET /corrections` (list), `DELETE /corrections/{id}` (delete one), `DELETE /corrections` (clear all)
- Integration: `translate_endpoint` and `execute_endpoint` check for matching corrections before returning transpiled AQL. If a match is found, the corrected AQL is used with a warning message ("Using learned correction #N").
- `mapping_hash`: deterministic SHA-256 of the JSON-serialized conceptual schema + physical mapping, ensuring corrections don't apply across schema versions.

**Retrieval for NL → Cypher (implemented):** BM25 over shipped `corpora/{movies,northwind,social}.yml` + `nl_corrections.all_examples()` via `arango_cypher/nl2cypher/fewshot.py::FewShotIndex`, rebuilt lazily on any `nl_corrections` mutation through the invalidation listener. Top-K (default 3) examples are injected into the LLM prompt ahead of the user question. Retrieval protocol is pluggable (`Retriever` protocol) so an embedding-based retriever (sentence-transformers, OpenAI embeddings) can replace BM25 later without API churn.

**Retrieval for Cypher → AQL (implemented):** Exact Cypher match + mapping hash — deterministic override.

**UI (implemented):**
- AQL editor is fully editable with a "modified" indicator when content diverges from transpiler output.
- **Learn** button: appears when AQL is modified; saves the `(cypher, mapping_hash, original_aql, corrected_aql)` tuple.
- **Learned (N)** button: toggles a corrections management panel listing all stored corrections with delete controls.

**Key concerns:** Schema drift (corrections become stale — `mapping_hash` ensures corrections only apply when schema matches), overfitting (per-example delete / clear-all available), privacy (all local, stated explicitly in UI).

### 14.2) Cardinality statistics for query optimization *(implemented)*

Graph query performance depends critically on traversal direction, join ordering, and filter placement. Without knowledge of collection sizes and edge fan-out/fan-in patterns, both the deterministic transpiler and the LLM-based NL→AQL generator make structurally valid but potentially expensive choices. Cardinality statistics address this by providing the query pipeline with quantitative knowledge about the physical data.

#### 14.2.1 Statistics computed

| Statistic | Scope | How computed | Purpose |
|-----------|-------|-------------|---------|
| **Document count** | Per vertex collection | `RETURN LENGTH(collection)` | Know which collections are large vs. small |
| **Edge count** | Per edge collection | `RETURN LENGTH(edgeCol)` | Know edge density |
| **Label count** | Per entity type (LPG/hybrid with type discriminator) | `FOR d IN col FILTER d.typeField == typeValue COLLECT WITH COUNT INTO c RETURN c` | When multiple entity types share a collection, know the per-type cardinality |
| **Avg out-degree** | Per edge collection, relative to source collection | `edgeCount / sourceVertexCount` | Measures fan-out: how many edges leave each source vertex on average |
| **Avg in-degree** | Per edge collection, relative to target collection | `edgeCount / targetVertexCount` | Measures fan-in: how many edges arrive at each target vertex on average |
| **Cardinality pattern** | Per relationship type | Derived from avg degree ratios | `1:1`, `1:N`, `N:1`, `N:M` classification |
| **Selectivity ratio** | Per relationship from→to | `edgeCount / (sourceCount × targetCount)` | How "selective" the relationship is — low ratio means sparse connectivity |

#### 14.2.2 How statistics aid NL→AQL generation (§1.3)

The physical schema summary passed to the LLM is enriched with cardinality context:

```
Collection 'Device' (entity: Device) — ~50,000 documents
Collection 'Tenant' (entity: Tenant) — ~120 documents

Edge collection 'tenantDevice' (TENANTDEVICE)
  Connects: Tenant('Tenant') -> Device('Device')
  ~50,000 edges, avg fan-out: 417/tenant, avg fan-in: 1/device
  Pattern: 1:N (each tenant has many devices; each device belongs to 1 tenant)
```

**Impact on LLM query generation:**

1. **Start from the selective side**: When the user asks "devices for tenant WPP", the LLM sees that Tenant has 120 docs and Device has 50,000, and that fan-out from Tenant is 417. It will generate `FOR t IN Tenant FILTER t.NAME == "WPP" FOR d IN OUTBOUND t tenantDevice RETURN d` rather than scanning all 50,000 devices.

2. **Avoid full scans on large collections**: If the LLM sees `Device: ~50,000`, it knows not to `FOR d IN Device` without a filter.

3. **Traversal direction choice**: Fan-out/fan-in ratios tell the LLM which direction produces fewer intermediate results. A relationship with avg fan-out of 1 and avg fan-in of 500 should be traversed OUTBOUND from the specific side.

4. **Aggregation strategy**: Knowing collection sizes helps the LLM choose between `COLLECT WITH COUNT` vs. `LENGTH()` and decide whether to add `LIMIT` for safety.

#### 14.2.3 How statistics aid Cypher→AQL transpilation (§1.2)

The deterministic transpiler uses statistics through `MappingResolver`:

1. **Traversal direction for undirected patterns**: Cypher `(a:Person)-[:KNOWS]-(b:Person)` (no arrow) currently maps to `ANY`. With statistics, if `KNOWS` has asymmetric fan-out (e.g., avg out-degree 5, avg in-degree 200), the transpiler can emit `OUTBOUND` from the filtered side instead of `ANY`, reducing the search space.

2. **Multi-part MATCH ordering**: When a query has multiple MATCH clauses or comma-separated patterns, the transpiler can order the outer loops to start from the most selective (smallest cardinality) collection.

3. **Filter placement**: Property filters on large collections should be pushed as early as possible. Statistics quantify "large" and help prioritize which filters to emit first.

4. **Index hint decisions**: Combined with index metadata (§5.7), cardinality informs whether an index scan or a full collection scan is cheaper.

#### 14.2.4 Data model

Statistics are stored in `MappingBundle.metadata["statistics"]`:

```json
{
  "statistics": {
    "computed_at": "2026-04-14T12:00:00Z",
    "collections": {
      "Device": { "count": 50000 },
      "Tenant": { "count": 120 },
      "tenantDevice": { "count": 50000, "is_edge": true }
    },
    "entities": {
      "Device": { "estimated_count": 50000 },
      "Tenant": { "estimated_count": 120 }
    },
    "relationships": {
      "TENANTDEVICE": {
        "edge_count": 50000,
        "avg_out_degree": 416.7,
        "avg_in_degree": 1.0,
        "cardinality_pattern": "1:N"
      }
    }
  }
}
```

#### 14.2.5 Implementation

- **Computation**: `compute_statistics(db, bundle)` in `arango_cypher/schema_acquire.py`. Uses fast AQL `LENGTH()` for counts; derived metrics computed in Python.
- **Storage**: Populated into `MappingBundle.metadata["statistics"]` during `get_mapping()` or via `GET /schema/statistics` endpoint.
- **MappingResolver**: New methods `estimated_count(label)`, `relationship_stats(rel_type)`, `preferred_traversal_direction(rel_type)`.
- **NL→AQL integration**: `_build_physical_schema_summary()` reads statistics from the bundle and appends count/degree annotations to each collection/edge line.
- **Transpiler integration**: the translator reads `estimated_count()` to pick the most selective of a node's labels and `relationship_stats()` for the LPG variable-length index choice (`_translate_v0/fanout.py`). It does not use `preferred_traversal_direction()`: an undirected relationship matches both orientations, and narrowing it to the direction the statistics favour (as the translator did until 2026-11) dropped the other half of the matches.
- **API**: `GET /schema/statistics` endpoint returns computed statistics for the connected database.

## 15) Packaging and deployment to the Arango Platform

This section covers how `arango-cypher-py` is packaged for, and deployed to, the Arango Platform's Container Manager (see `docs/arango_packaging_service/` for the upstream platform API and ServiceMaker tool).

**What gets deployed.** The default platform deployment is **headless**: the library, CLI, and FastAPI HTTP endpoints in `arango_cypher.service` (§4.3). The Cypher Workbench UI (§4.4) is a debug/demo surface — it is **not** included in the default ServiceMaker tarball and is **not** exposed by the platform's Container Manager in standard deployments. Operators who want to run the UI against a deployed service do so locally (pointing their browser at a local dev server that proxies to the platform endpoint) or via a separate, explicitly-enabled packaging variant. Any future "UI-included" tarball must be opt-in, separately versioned, and carry the §4.4 scope disclaimer.

### 15.1 Design decision: fix the root cause upstream, don't build a toolchain here

**Status (resolved 2026-04-23).** Our sibling library `arangodb-schema-analyzer` (source at `~/code/arango-schema-analyzer`) is **published to PyPI**. The `[analyzer]`, `[service]`, and `[dev]` extras of `pyproject.toml` pin it to a version band. *(2026-09-15: the `>=0.6.1,<0.7` band quoted here at resolution time is historical — the current band is declared in `pyproject.toml` only and follows the alignment invariant in `docs/PRD.md` §7.1; do not treat any literal in this section as current.)* Inside the ServiceMaker build container, `uv sync --extra service` resolves it directly from the public index; no private registry, no git auth, no vendored wheels.

With the upstream fix in place, *this repo needs no packaging tooling at all*. Deployment is `tar -czf` of the repo plus the three documented curl commands in [`docs/arango_packaging_service/deployment_runbook.md`](./arango_packaging_service/deployment_runbook.md).

**Historical context.** Prior to 2026-04-23 the analyzer was declared in `[analyzer]` as a bare name with no version, path, or URL — on a developer machine it worked because `pip install -e ~/code/arango-schema-analyzer` pre-installed it, but inside the ServiceMaker build container (no private-index network, no git auth) resolution failed with "no matching distribution found." The chosen remediation was the simplest: publish once in the sibling repo rather than paper over the resolution failure in every consumer. The first published release was `0.6.0` (2026-04-23); `0.6.1` (adopted via PR #8 on 2026-04-24) adds the DoS hardening and schema-analyzer cache-path tunables that §4.4.5 now documents.

### 15.2 Why this over the alternatives

We considered and rejected three more-invasive approaches:

| Option | Why rejected |
|--------|--------------|
| **Vendor pre-built wheels into `vendor/` here** | Shifts the fix into every consumer repo. `pyproject.toml` rewriting logic, a `vendor/` refresh workflow, and a manifest-driven build script — all to work around a sibling repo that isn't published. Fixes the problem once per consumer rather than once total. |
| **Git URL deps (`arangodb-schema-analyzer @ git+ssh://...`)** | Build container needs SSH auth to the git host. Unreliable in opaque runners. Couples our build to the analyzer's git location and revision scheme. |
| **Monorepo vendoring (copy sibling source into `third_party/`)** | Source-level drift with upstream. Obscures the fact that these are distinct libraries with distinct release cycles. |

All three absorb a cost that belongs upstream. Publishing once is strictly simpler than vendoring forever.

We also considered and rejected **adding a full packaging-and-deployment CLI** (`package` / `deploy` / `redeploy` / `teardown` as Typer subcommands, backed by a `packaging.toml` manifest and an `httpx` client for the platform API). Reasons:

- **Packaging is repo-specific; deployment is generic.** If we ever want a deployment CLI, it belongs in its own tool that can deploy *any* ServiceMaker tarball — not buried inside a Cypher-to-AQL library.
- **Release cadence mismatch.** The Cypher translator shouldn't re-release because the platform API changed. And vice versa.
- **Token blast radius.** Platform bearer tokens stay with the deployment tool (or in `.env` for manual curl), never in this repo's dev loop.
- **Over-engineering for volume.** Until we redeploy more than weekly, a README section with three curl commands is strictly better than a CLI nobody runs. A CLI nobody runs rots faster than a runbook.

### 15.3 Deliverables (this repo)

All this repo owns:

1. **A README section** in `docs/arango_packaging_service/` documenting the manual deploy path:
   - Build the tarball: `uv build --sdist` (or `tar -czf`), producing `arango-cypher-py-<ver>.tar.gz`.
   - Upload: `curl POST /_platform/filemanager/global/byoc/` with `ARANGO_PLATFORM_TOKEN` from `.env`.
   - Deploy: `curl POST /_platform/acp/v1/uds` with the app instance spec.
   - Redeploy: bump `pyproject.toml` version, rebuild, upload, deploy (same three commands with a new version).
   - Teardown: `curl DELETE` against FileManager and ACP.

2. **Prerequisite checklist** in that same doc: ensure `arangodb-schema-analyzer` is pinned in `pyproject.toml` to a published version (no bare names, no paths, no git URLs) before packaging. *Satisfied as of 2026-04-24 (then `>=0.6.1,<0.7`); the current band lives in `pyproject.toml` and follows `docs/PRD.md` §7.1 — checked 2026-09-15.*

3. **A smoke test in CI** (gated behind `RUN_PACKAGING=1`, off by default): run `uv sync` against the packaged tarball inside a clean container and confirm it succeeds. Catches dependency-graph regressions that would break a deploy — without the overhead of an actual platform round-trip.

### 15.4 Deliverables (outside this repo)

Not in scope for `arango-cypher-py`, tracked for visibility:

1. **Publish `arangodb-schema-analyzer` to PyPI.** Tracked in `~/code/arango-schema-mapper`. **Done 2026-04-23** with the `0.6.0` release; floor bumped to `0.6.1` on 2026-04-24 (PR #8) to pick up the upstream DoS hardening.
2. **(Optional, future) `arango-platform-deploy` CLI.** A separate project — or a contribution to ServiceMaker itself — that wraps the Container Manager API as `deploy` / `redeploy` / `teardown` subcommands over any tarball. Generic; reusable for every ArangoDB Python service. Built only when the volume of deployments justifies it, which is not today.

### 15.5 Open questions and future work

| Question | Owner | Target resolution |
|----------|-------|-------------------|
| ~~Timeline and process for publishing `arangodb-schema-analyzer` to PyPI.~~ | ~~`arango-schema-mapper` owner~~ | **Resolved 2026-04-23** — published as `0.6.0`; current floor `0.6.1`. |
| ~~Does ArangoDB have an internal package index we should target instead of (or in addition to) public PyPI?~~ | ~~Engineering / DevOps~~ | **Resolved 2026-04-23** — public PyPI is the primary distribution; air-gapped / BYOC environments point `uv` at an internal mirror via `UV_INDEX_URL` at install time (same mechanism as every other PyPI-hosted dep). No repo-side change needed. |
| Should `arango-query-core` (the workspace sibling package in this repo) be split into a separately published library? It is co-packaged by hatchling today, which works fine for `arango-cypher-py` consumers but not for downstream libraries that want the core without the Cypher layer. | Engineering | Before v1.0 |
| Can ServiceMaker's base images be extended to include `rdflib` + `neo4j` driver so the `owl` and `neo4j` extras don't inflate every deployed tarball? | Engineering / ArangoDB ServiceMaker team | v0.4 |
| When deployment volume justifies automation, does the deployment tool live in a new dedicated repo or as a contribution to upstream ServiceMaker? | Product | When redeploy frequency > weekly |

## Multi-tenant safety

> This section is a **stub + pointer**. The full architecture, algorithms, and implementation plan live in [`docs/multitenant_prd.md`](./multitenant_prd.md) as a standalone draft. The fold-in into this document (promoting this section to a numbered §16, with §1–§2 of the standalone PRD absorbed here, §3 absorbed into §5, §4–§9 becoming subsections, and §11 absorbed into the implementation-status table) is scheduled to coincide with the start of Wave-MT (first Layer 1/3/4/5 implementation). Until then, this stub exists so that a reader of the main PRD does not miss that a committed six-layer architecture exists.

### Scope

When `arango-cypher-py` is deployed against an ArangoDB database holding multiple tenants' data, ad-hoc NL queries from a tenant user must not read data belonging to another tenant. Two failure modes produce cross-tenant reads if unguarded:

1. **Underconstraint** — the LLM omits a tenant filter (`MATCH (e:Employee) RETURN e`).
2. **Injection** — the user names another tenant in the NL prompt and the LLM obliges.

These are **data-leak-class defects**, not translation-quality nits. The existing Wave 4r guardrail (`tenant_guardrail.py`, tenant selector UI, `/tenants` endpoint) closes the single most common underconstraint case via a prompt + regex-postcheck + retry loop — necessary but insufficient, because it relies on the LLM eventually doing the right thing.

### Six-layer defense-in-depth architecture

| # | Layer | Mechanism | Status |
|---|---|---|---|
| 0 | Storage | Disjoint SmartGraphs (per-tenant shard key) + satellite collections (tenant-independent reference data) | **Partially adopted** (2026-04-23, PR #6) — mapper exposes `physicalLayout` (`smartgraph` / `satellite` / `regular` / `system`), `physicalMapping.shardFamilies`, and `metadata.multitenancy.{style, tenantKey[], physicalEnforcement}`. Downstream consumers: `tenant_guardrail.multitenancy_physical_enforcement()` labels violations as data-leak vs. translation-quality; `nl2cypher/tenant_scope.py` reads `tenantScope.role` / `tenantScope.tenantField` for denorm-filter scope satisfaction. Still missing from the storage side: disjoint-SmartGraph fixtures in integration tests (MT-0) and the per-tenant EXPLAIN-plan validator (MT-5). |
| 1 | Session | Server-bound `@tenantId`, injected from authenticated session; never trusted from the request body | **Done** (Wave 7, 2026-05-11) — `_Session` carries `tenant_id`/`tenant_key`/`is_admin`; `/connect` validates the bound tenant against the `Tenant` collection; `/nl2cypher` + `/nl2aql` honor session tenant in tenant-user mode (workbench mode preserves body tenant for backward compat). |
| 2 | LLM | Manifest-aware prompt + few-shot + regex postcheck + retry | **Done** (Wave 4r, 2026-04-20) |
| 3 | Cypher AST | Algorithmic tenant-predicate injection on the parsed Cypher, before transpilation | Not implemented |
| 4 | AQL AST | Tenant-predicate injection on the transpiled AQL; covers the NL→AQL direct path and `/execute-aql` | Not implemented |
| 5 | Pre-execute | EXPLAIN-plan validator that refuses any plan scanning a tenant-scoped collection without a bind-var tenant predicate | **Done** (Wave 7, 2026-05-11) — `arango_cypher/tenant_plan_validator.py` walks EXPLAIN nodes (EnumerateCollection / Index / Traversal / Subquery), rejects unconstrained scans + literal tenant predicates, recurses into subqueries with fresh walker state, and logs every pass (`TENANT_SCOPE_OK`) and refusal (`TENANT_SCOPE_VIOLATION`) with AQL+plan digests for audit. |
| 6 | Execute | `db.aql.execute(query, bind_vars={**client, "tenantId": session.tenant_id})` (session value wins) | **Done** (Wave 7, 2026-05-11) — `safe_execute` in `arango_query_core/exec.py` overrides client `@tenantId`/`@tenantKey` with session values, calls Layer 5 validator, then executes; service adapter `arango_cypher/service/safe_exec.py` is wired into `/execute`, `/execute-aql`, `/aql-profile` and surfaces `TenantScopeViolation` as HTTP 403 with violation code + AQL/plan digest. |

**Key commitments** (restated from `multitenant_prd.md` so they are visible to a main-PRD reader):

- **Layer 5 is the security boundary.** Layers 3 and 4 exist for transparency (the user sees the rewritten Cypher/AQL), developer ergonomics, and defense-in-depth; the only check that matters for audit is the EXPLAIN-plan validator. If Layer 5 passes, the query is safe; if it fails, the query does not run.
- **Fail-closed everywhere.** Unknown entity labels, missing manifest entries, unparseable plans, and any ambiguity cause refusal, not a permissive fallback.
- **The LLM is never trusted.** Layer 2 reduces retry burden on Layers 3–5 but is not counted as a defense for audit purposes.
- **Storage layout is a schema-mapper concern.** The mapper must surface each collection's `physicalLayout.kind` (`smartgraph` / `satellite` / `regular` / `system`), `smartGraphAttribute`, `isDisjoint`, and `graphName`, plus each entity's `tenantScope.scopingPathFromTenant`. Everything downstream reads from this manifest. **Status (2026-04-23):** `arangodb-schema-analyzer >= 0.6.x` emits `physicalLayout`, `physicalMapping.shardFamilies`, and `metadata.multitenancy.{style, tenantKey[], physicalEnforcement}`; `tenantScope.role` / `tenantScope.tenantField` are consumed by `nl2cypher/tenant_scope.py`. The fields still on the roadmap are `smartGraphAttribute` and `isDisjoint` on the physical-layout entries, tracked as follow-ups on the upstream analyzer.
- **Bind-variables only.** The tenant identifier is always passed as a bind variable (`@tenantId`), never inlined as a literal. Layer 5 refuses plans that encode a tenant constraint as a string literal.

See [`docs/multitenant_prd.md`](./multitenant_prd.md) for the threat model (T1–T8), per-layer algorithms (§4–§9), admin bypass (§10), work packages MT-0..MT-8 (§11), testing strategy including red-team corpus (§12), and open questions (§13).

### Relationship to this PRD's existing mentions of tenant scoping

- §4.4 (Cypher Workbench UI) scope note clarifies that the UI consumes but does not enforce tenant isolation.
- The 2026-04-20 changelog row for Wave 4r documents the shipped Layer 2 guardrail, catalog endpoint, and UI tenant selector; those are the foundation on which Layers 1, 3, 4, 5, and 6 build.
- §5 (Schema detection & mapping) will grow a `physicalLayout` block at fold-in time to satisfy the Layer 0/manifest requirement; this is tracked as WP-MT-0 in `multitenant_prd.md` §11.

---

## 16) Development guide reference

For contributor onboarding and development workflow, see:

- **Environment setup**: install with `pip install -e ".[dev,service]"`. Requires Python 3.11+ (`requires-python` in `pyproject.toml`).
- **Running tests**:
  - Unit + golden: `pytest -m "not integration and not tck"`
  - Integration (requires ArangoDB): `docker compose up -d && RUN_INTEGRATION=1 pytest -m integration`
  - TCK (requires ArangoDB): `python scripts/download_tck.py && RUN_INTEGRATION=1 RUN_TCK=1 pytest -m tck`
- **Regenerating ANTLR parser**: `antlr4 -Dlanguage=Python3 grammar/Cypher.g4 -o arango_cypher/_antlr` (the `-visitor` flag was dropped 2026-04-27 — see PR #3; we never consumed the generated visitor, and removing it deletes ~550 LOC of dead code per grammar regen)
- **Starting dev servers**: `uvicorn arango_cypher.service:app --reload --port 8001` (backend) + `cd ui && npm run dev` (frontend on port 5173, proxies API to 8001)
- **Adding a new Cypher construct**: modify `arango_cypher/translate_v0.py` (translation logic), add golden test cases to `tests/fixtures/cases/`, run `pytest` to verify, update §6.4 of this PRD.
- **Code quality**: `ruff check .` for linting. `mypy` is recommended but not yet enforced in CI.

---

## 17) Named-graph scoping (optional graph awareness)

### 17.1 Motivation

An ArangoDB database is a namespace, not a single graph. In practice a database
frequently accumulates collections from unrelated sources:

- the **domain graph** the operator actually wants to query;
- a **GraphRAG / vector import** (text chunks, embeddings) that shares the DB;
- **graph-analytics result collections** (GAE / Pregel write-back: PageRank,
  community labels, centrality);
- **application-control collections** (users, jobs, audit, feature flags);
- this transpiler's own **`arango_cypher_schema_cache`** and any
  **benchmark/query-corpus** collections.

Running schema introspection — and therefore the NL prompt context and
whole-graph meta-queries (`MATCH (n) …`) — across *all* of these produces noise
at best and wrong answers at worst (e.g. an unlabeled `MATCH (n)` that has to
guess which collection is "the" domain collection).

ArangoDB **named graphs** already encode the answer. A named graph declares:

- **edge definitions** — each is `(edgeCollection, from[], to[])`;
- **orphan collections** — vertex collections with no edges in the graph.

The union of those collections *is* the subgraph the operator cares about.

### 17.2 Behaviour

`arango-cypher-py` is **optionally** graph-aware:

- **No graph selected (default).** Behaviour is byte-identical to prior
  releases: every collection in the database is considered.
- **A named graph selected.** Schema introspection, the conceptual schema fed to
  the LLM, transpilation, and execution all consider **only** the collections
  that belong to that graph. Non-member collections (ops, RAG side-stores, the
  schema cache) are invisible to the session.

Graph selection is **per session** and happens **after** connecting — you cannot
know which graphs exist, or whether scoping is even useful, before you can read
the database. This mirrors the post-analysis tenant-selection model (§16 / Wave
7); a named graph is, however, cheaper to enumerate than tenancy (a single
`db.graphs()` call), so the UI can offer it immediately after connect.

### 17.3 API surface

- **`GET /graphs`** — lists the connected database's named graphs. Each entry:
  `{ name, edgeDefinitions: [{edgeCollection, from[], to[]}], vertexCollections[],
  orphanCollections[], collectionCount }`. Returns `{ graphs: [] }` cleanly for
  databases with no named graphs so the UI can be mechanical about show/hide.
- **`POST /session/graph`** — body `{ graphName: string | null }`. Binds the
  session to the named graph (validated to exist — HTTP 404 on miss) or, when
  `null`, clears the binding ("all collections"). Returns
  `{ graph_name, bound }`.
- The mapping-consuming endpoints (`/schema/introspect`, `/schema/statistics`,
  `/schema/status`, `/schema/invalidate-cache`, `/schema/force-reacquire`) read
  `session.graph_name` and scope accordingly.

### 17.4 Implementation notes

- **Scoping point.** Because the UI carries the introspected mapping into every
  subsequent NL / translate / execute call, the single place scoping must be
  applied is mapping acquisition. `get_mapping(db, *, graph_name=None, …)` threads
  the name through `_build_fresh_bundle` → `acquire_mapping_bundle`.
- **Filtering, not re-analysis.** `AgenticSchemaAnalyzer.analyze_physical_schema`
  has no collection/graph scoping hook, so the analyzer still introspects the
  whole DB once and the resulting `MappingBundle` is filtered to the graph's
  collection set. `graph_collections(db, name) -> (vertex_colls, edge_colls)`
  resolves the membership; `_filter_bundle_to_graph(bundle, vertex_colls,
  edge_colls)` filters `physical_mapping.entities` (by `collectionName`),
  `physical_mapping.relationships` (by `edgeCollectionName`), the matching
  `conceptual_schema` entries, and `metadata.statistics`. (A future optimisation
  could scope the analyzer's snapshot to skip analysing non-member collections.)
- **Cache isolation.** The mapping cache key is made graph-aware in both tiers —
  in-process (`<db>::graph::<name>`) and persistent (`mapping::graph::<name>`) —
  so each graph and the unscoped "all collections" view get independent cached
  bundles and never alias one another.
- **Fingerprints.** Shape/full fingerprints remain whole-database for now; a
  change in a non-member collection harmlessly re-introspects+re-filters a
  graph-scoped bundle (correct, slightly conservative). Per-graph fingerprints
  are a possible later optimisation.

### 17.5 Relationship to other features

- **Subsumes the "schema hygiene" follow-up.** Operational/side collections do
  not need a heuristic exclusion list when the operator scopes to a named graph
  — they simply aren't members.
- **Composes with tenancy.** Graph scoping (which *collections*) and tenant
  scoping (which *rows*) are orthogonal and may both be active on a session.

## 18) Cypher→AQL AI fallback (recoverable transpile failures)

### 18.1 Motivation

The deterministic transpiler supports a defined Cypher subset (v0). A query that
uses a feature outside that subset raises `CoreError` with code `UNSUPPORTED` /
`NOT_IMPLEMENTED`, which the service returns as HTTP 422. Historically this was a
dead end in the workbench: the user saw a red banner with a raw message and no
way forward (the only recovery, "Regenerate from NL", appeared *solely* when the
Cypher originated from the NL pipeline). Hand-written Cypher hitting an
unsupported feature had no recovery at all.

### 18.2 Behaviour

When a Cypher query fails to transpile with a **recoverable** code
(`UNSUPPORTED` / `NOT_IMPLEMENTED` — *not* syntax errors and *not* tenant/Layer-4
refusals), the workbench offers a one-click **"Generate AQL with AI"** action in
the error banner. It:

- sends the **failing Cypher** to `POST /nl2aql` in a new `cypher` field, so the
  LLM translates Cypher→AQL using the same physical-schema system prompt (the
  user message reframes the task; the original NL question is also passed as
  context when the Cypher came from the NL pipeline);
- on success, loads the generated AQL into the AQL pane and runs it via
  `POST /execute-aql` (so **Layer 4 rewrite and Layer 5 validation still apply** —
  the fallback stays inside the tenant-safety boundary);
- is **offered, never automatic** — no silent LLM spend and no surprise
  execution. The user stays in control.

This also fixed two related UI papercuts: the client previously **dropped** the
human-readable `message` from structured refusals (tenant scope / Layer-4),
showing the machine `error` kind instead; `formatDetail` now prefers `message`,
and `ApiError` exposes `code` so the UI can branch on it.

### 18.3 API surface

- `POST /nl2aql` gains an optional `cypher: str | None`. When present,
  `nl_to_aql(..., cypher=...)` asks the LLM to translate that Cypher to AQL
  rather than answering `question`. Tenant Layer-4 rewrite on the result is
  unchanged.

### 18.4 Relationship to other features

- **Complements the untyped-relationship / meta-query transpiler work
  (§ earlier).** Those expand what the *deterministic* path covers; this is the
  graceful escape hatch for everything still outside it.
- **Does not weaken tenant safety.** Generated AQL runs through `/execute-aql`,
  which enforces Layer 4 and Layer 5 exactly as for NL→AQL.

## 19) Schema catalog sidecar (analysis off the request path)

### 19.1 Motivation

Acquiring a mapping is expensive: the schema analyzer runs an LLM-grade
conceptual extraction, statistics computes live cardinalities, and even a cache
"hit" historically paid a shape + full fingerprint walk (`db.collections()` +
per-collection `count()` + `indexes()`) to decide whether the cached bundle was
still valid. On a remote cluster that is tens of seconds to minutes — and it
landed **inline on a user click** (connect, "Refresh schema", graph-scope
change). The interactive workbench felt frozen.

The fix is architectural, not a micro-optimisation: **move analysis out of the
request path entirely.** A small set of databases ("a handful") is kept analyzed
out of band by a sidecar; the service only ever *reads* the result.

### 19.2 Model

- **The catalog is the existing persistent cache.** Each database's analyzed
  `MappingBundle` already lives in that database's shared
  `arango_cypher_schema_cache` collection (graph-scoped slots included). The
  sidecar is simply the single *writer*; every service instance is a *reader*.
  No new central store is needed for a handful of databases in a shared,
  multi-user deployment.
- **Freshness is bounded by the sidecar schedule** (`interval_seconds`, default
  30 min). This is the agreed tolerance: "fresh until the next scheduled sync."
  The explicit **"Refresh schema"** button remains the power-user escape hatch
  for an immediate synchronous rebuild.

### 19.3 Sidecar (`arango_cypher/catalog/`)

- **Registry** (`registry.py`): `configs/catalog.yml` lists the databases and
  optional named-graph scopes to keep warm. Credentials are referenced by
  environment variable (`password_env`, `username_env`) — a referenced-but-unset
  variable is a hard error so a misconfiguration fails loudly; secrets are never
  stored in the file (`configs/catalog.yml` is gitignored; `catalog.example.yml`
  is the committed template). When no file exists, a single entry is built from
  the standard `ARANGO_URL` / `ARANGO_DB` / `ARANGO_USER` / `ARANGO_PASSWORD`
  variables so the sidecar works out of the box.
- **Sync** (`sync.py`): `sync_once` runs one pass; `sync_forever` loops with an
  event-interruptible sleep; `sync_entry` syncs a database's whole-DB mapping
  plus each configured graph scope. Each target calls the existing
  `get_mapping(force_refresh=True, graph_name=…)`, which persists to the cache.
  Per-target failures (connect or analyze) are captured as `SyncResult` records
  and never abort the rest of the pass.
- **CLI** (`scripts/catalog_sync.py`): `--once` (cron-friendly) or the default
  long-running loop; `--config`, `--interval`, `--log-level`. Credentials come
  from the environment, never the command line.

### 19.4 Read-only request path

- `schema_acquire.read_cached_mapping(db, *, graph_name=…)` serves the
  in-memory tier, then the persistent catalog, and returns `None` on a true
  miss. It **never** fingerprints, calls the analyzer, or writes — worst case is
  a single persistent-cache document read.
- `GET /schema/introspect` and `GET /schema/statistics` read via that path and
  carry a `status`:
  - `"ready"` — a mapping was served.
  - `"pending"` — not analyzed yet; an empty schema is returned with a
    `SCHEMA_PENDING` warning. The endpoint does **not** analyze inline and does
    **not** spawn background threads (an earlier draft did; it was removed
    because unbounded request-path threads are a footgun and corrupted test
    isolation). The sidecar, or "Refresh schema" (`force=true`, the one
    remaining synchronous-rebuild path), populates it.
- **UI**: `introspectSchemaUntilReady` polls briefly while `pending` and shows a
  "schema is being analyzed" notice instead of an error; connect and
  graph-scope rehydrate both use it. "Refresh schema" still forces a rebuild.

### 19.5 Relationship to other features

- **Builds on named-graph scoping (§17).** The sidecar warms each registered
  graph scope's cache slot, so scoped sessions are served from the catalog too.
- **Does not change tenant safety (§Multi-tenant safety).** Execution still
  flows through Layers 4–6; only *schema acquisition* moved off the request
  path.
- **Subsumes the inline TTL fast-path** as the primary latency mechanism: with
  read-only catalog reads the request path no longer fingerprints at all; the
  TTL fast-path remains a within-process optimisation for the sidecar and the
  `force=true` rebuild.
