# arango-cypher-py — Product Requirements Document (Consolidated)

**Status:** Active development · **v0.2.0**
**Last updated:** 2026-10-23
**Owner:** arango-cypher-py maintainers

> **This is the single, all-encompassing PRD for the project.** It consolidates the
> product vision, architecture, requirements, current state, and roadmap that were
> previously spread across several documents. Those documents remain as the
> *detailed records of their respective tracks* and are the place to go for
> work-package breakdowns, dependency graphs, and historical changelogs:
>
> | Track | Detailed source of record |
> | --- | --- |
> | Full product PRD + per-feature changelog | [`python_prd.md`](./python_prd.md) |
> | Cypher→AQL coverage completion | [`cypher_coverage_plan.md`](./cypher_coverage_plan.md) |
> | Generated TCK measurements | [`../tests/tck/COVERAGE_REPORT.md`](../tests/tck/COVERAGE_REPORT.md) |
> | Feature-level compatibility contract | [`cypher_capability_matrix.md`](./cypher_capability_matrix.md) |
> | Multi-tenant defense-in-depth | [`multitenant_prd.md`](./multitenant_prd.md) |
> | Work-package execution plan | [`implementation_plan.md`](./implementation_plan.md) |
> | LDBC SNB benchmark (validation + performance) | [`ldbc_benchmark_plan.md`](./ldbc_benchmark_plan.md) |
> | Schema-inference bug fixes | [`schema_inference_bugfix_prd.md`](./schema_inference_bugfix_prd.md) |
> | Project assessment (outside-in critique) | [`2026-05-26-project-assessment.md`](./2026-05-26-project-assessment.md) |
> | Platform packaging / deployment | [`arango_packaging_service/`](./arango_packaging_service/) |
>
> Where this PRD and a source document disagree, **this PRD wins** for current
> intent; the source document wins for historical detail.

---

## 1. Executive summary

`arango-cypher-py` is a **Python-native NL → Cypher → AQL stack for ArangoDB**. It
runs **outside** the database (as a library, CLI, or HTTP service) and lets users
query an ArangoDB graph using [openCypher](https://opencypher.org/) — or plain
natural language — regardless of how the data is physically modeled.

Two paths share one engine:

1. **Cypher → AQL transpiler** — a *deterministic* translator from openCypher to
   ArangoDB Query Language across **property-graph (PG)**, **labeled-property-graph
   (LPG)**, and **hybrid** physical models.
2. **NL → Cypher pipeline** — an LLM generates *conceptual* Cypher from a
   natural-language question; the transpiler converts it to AQL. **The LLM never
   sees physical mapping details; the transpiler never uses an LLM.** This
   separation is a first-class architectural constraint, not a guideline.

The product also ships: a `arangodb-schema-analyzer`-driven mapping layer that
reverse-engineers a conceptual schema from a live database; a six-layer
multi-tenant safety architecture; a registry of `arango.*` extension functions
(search / vector / geo / document); schema-change detection with a two-tier cache;
and a browser-based **Cypher Workbench** UI for debugging and demos, presented on
the platform as **Arango Cypher**.

### 1.1 Defining decisions

- **Product scope.** The deliverable is the conversion *service* (library + CLI +
  HTTP) and the NL pipelines that run inside it. The Workbench UI exists to
  **debug** and **demo** the service; it is not a production multi-user workbench
  and is not deployed by default (see §11).
- **Conceptual schema is the query interface.** All Cypher — hand-written,
  LLM-generated, or NL-derived — is expressed against the *logical* schema, never
  the physical ArangoDB layout. The mapping layer absorbs the difference, which is
  what makes a single query portable across PG / LPG / hybrid.
- **Cypher is the intermediate representation, not AQL.** LLMs produce far better
  Cypher than AQL (training-data abundance + declarative/logical fit). A direct
  NL → AQL escape hatch exists (§5.3) but is explicitly secondary.
- **Schema analyzer is canonical.** `arangodb-schema-analyzer` is the single
  source of truth for what labels, types, and properties exist. A no-workaround
  policy fixes inference problems at the source rather than patching downstream.
- **Determinism by default.** Same Cypher + same mapping = same AQL, every time.
  Agentic and LLM enhancements are optional and never authoritative.
- **Safe AQL output.** Bind parameters (`@@collection`, `@param`) throughout;
  user input is never string-interpolated into queries.

---

## 2. Problem statement

ArangoDB supports multiple physical graph-modeling styles:

- **PG** — types-as-collections: one vertex collection per label, one edge
  collection per relationship type.
- **LPG** — generic collections + type discriminator: a single vertex collection
  with a `type` field, a single edge collection with a `type` field.
- **Hybrid** — a mixture of both, sometimes within a single query path.
- **GraphRAG-style** — a generic `Node` collection plus a generic, type-
  discriminated `relations` edge collection carrying thousands of relationship
  types (the open-vocabulary case).

Cypher is a conceptual, label/type-centric language. To execute it on ArangoDB we
need a conceptual schema (labels, relationship types, properties), a mapping to
physical collections and type fields, and a transpiler that emits safe, performant
AQL for the actual layout — including hybrid paths.

A Python implementation (vs. the in-database [Foxx sibling](https://github.com/ArthurKeen/arango-cypher-foxx))
enables richer parsing toolchains, strong typing and testing ergonomics, and easy
integration into notebooks, CLIs, services, and agentic workflows.

---

## 3. Goals / non-goals

### Goals (v0.1–v0.4)

- A deployable conversion service: **library + CLI + HTTP**, with a deterministic
  Cypher→AQL transpiler and an LLM-driven NL→Cypher pipeline.
- Translate a broad, documented subset of Cypher into **AQL + bind variables**.
- Optionally execute translated AQL against ArangoDB.
- Support **PG, LPG, and hybrid** physical models via `arangodb-schema-analyzer`.
- Make cross-tenant data leakage **structurally impossible** when deployed against
  a multi-tenant graph (§7).
- Deterministic behavior by default; agentic/LLM enhancements are optional.

### Non-goals

- Full openCypher TCK compliance in early versions (TCK is a *progressive* goal).
- A cost-based query optimizer equivalent to a database planner (a small internal
  logical plan is in scope; a full optimizer is not).
- The Workbench UI as a production multi-user product (no multi-user authn/authz,
  collaboration, server-side persistence, or UI-level tenant isolation).
- SLM fine-tuning inside this repo (the `LLMProvider` protocol reserves the hook).

### Success criteria (per version)

| Version | Representative criteria |
| --- | --- |
| v0.1 | 100% golden + integration tests; single-hop translation P95 < 50 ms |
| v0.2 | Write clauses compile and execute; live-DB mapping; CLI complete |
| v0.3 | UI connect→translate→execute→view without touching JSON; Movies corpus passes PG+LPG; TCK ≥ 25% |
| v0.4+ | Translation-only Full TCK ≥ 60% and execution-grounded compatibility reported separately; agentic tool contract functional |
| LDBC M2 (after WP-35) | Every LDBC SNB Interactive v2 read and update except IC14 (GDS) matches the Neo4j oracle (`matches_oracle`) on both PG and LPG (`tests/ldbc/execution_ledger.tsv`). INS1 counts when its verbatim text fails closed on the upstream schema bug it carries (`WORKS_AT` for `WORK_AT`, [ldbc_snb_interactive_v2_impls#10](https://github.com/ldbc/ldbc_snb_interactive_v2_impls/issues/10)) and its labelled schema-fix variant matches (LDBC-1). **Achieved 2026-10-29.** |
| LDBC M3 | Every LDBC BI query except BI10, BI15, BI19 and BI20 (GDS/APOC) matches the oracle on both layouts. **Achieved 2026-10-29.** |

---

## 4. Users & use cases

**Personas:** data engineer (Neo4j-ish migration / exploration), application
developer (Cypher compatibility layer over ArangoDB), analyst / notebook user
(Cypher in Jupyter), agent workflow (stable translate/explain tool contract),
and **tenant user** of a deployed multi-tenant service (NL questions, must never
see another tenant's data).

**Core user stories:**

- *Translate-only* — "Given Cypher, show me AQL and bind vars."
- *Translate + execute* — "Run Cypher against database X, return JSON."
- *Ask in natural language* — "Who acted in 'Forest Gump'?" (typo tolerated) →
  conceptual Cypher → AQL → results.
- *Explain mapping* — "Show how labels/types mapped to collections/fields."
- *Validate* — "Warn if a query references unknown labels/types."
- *Stay in my tenant* — every NL/AQL query is provably scoped to the caller's
  tenant, or it is refused before execution.

---

## 5. Architecture

### 5.1 System overview

Two consumption modes share one engine. The FastAPI service hosts both the JSON
HTTP API and the built UI bundle; the same Python package can be imported directly
from application code. The transpiler is pure (no I/O); the NL pipeline reaches out
to LLM providers; the schema cache writes its persistent tier into the connected
ArangoDB itself so it survives restarts and is shared across replicas.

```
Clients (Workbench UI · application code)
        │
        ▼
FastAPI service (arango_cypher.service)
  ├── Cypher → AQL transpiler        (pure, deterministic)
  ├── NL → Cypher pipeline + tenant guardrail
  └── Schema cache + change detection
        │                    │
        ▼                    ▼
   ArangoDB             LLM providers
 (user data +        (OpenAI · Anthropic
  schema cache)        · OpenRouter)
```

### 5.2 Layered query interface (the core invariant)

```
Query authors (human · LLM · NL2Cypher)
  ↓ express queries in conceptual labels/types
Conceptual (logical) schema     ← from arangodb-schema-analyzer (canonical)
  ↓
Mapping layer (MappingBundle)   ← conceptual→physical resolution, styles, indexes
  ↓
Transpiler (translate_v0)       ← emits safe AQL + bind vars for the real layout
  ↓
Physical ArangoDB (PG / LPG / hybrid)
```

Consequences: **query portability** (one Cypher query runs against any layout),
**analyzer is canonical**, **NL operates at the logical level**, and **index
metadata lives in the mapping**, never in queries.

### 5.3 The two pipelines

**Stage 1 — NL → Cypher (LLM, non-deterministic).** Input: an NL question.
Context: the *conceptual* schema only (entity labels, relationship types,
properties, domain/range). Output: Cypher in conceptual terms. The LLM never sees
collection names, type discriminator fields, AQL, or the physical model style.
(One scoped exception, outside this path: the offline `mine-examples` tool must
show the model the saved AQL it is translating, with a physical → conceptual name
table, and still demands conceptual Cypher back — see "Mined examples".)

**Stage 2 — Cypher → AQL (deterministic, algorithmic).** Input: Cypher (from
Stage 1 or hand-written). Context: conceptual schema + physical mapping. Output:
AQL + bind variables. No LLM, no network calls (other than optional execution).

**Why Cypher as the IR, not AQL:** LLM priors (Cypher is far better represented in
training data), ontology alignment (labels map to ontology classes), portability
(conceptual Cypher is layout-independent), and ecosystem fit (the same pattern as
Neo4j Text2Cypher, LangChain `GraphCypherQAChain`, LlamaIndex).

**Direct NL → AQL (§5.3 escape hatch).** An opt-in path where the LLM sees the
*full physical schema* and emits AQL directly — used only when the transpiler
lacks a construct or an AQL-specific feature is required. It is structurally weaker
(non-deterministic, layout-coupled, lower LLM accuracy) and deliberately separate
from the primary path. Crucially, it is **no less safe**: the multi-tenant
security boundary (§7, Layer 5) applies to *every* execute path.

---

## 6. Product surfaces

### 6.1 Library API (primary)

```python
from arango_cypher import translate
from arango_query_core import MappingBundle

mapping = MappingBundle(
    conceptual_schema={"entityTypes": ["Person"], "relationshipTypes": ["KNOWS"]},
    physical_mapping={
        "entities": {"Person": {"style": "COLLECTION", "collectionName": "persons"}},
        "relationships": {"KNOWS": {"style": "DEDICATED_COLLECTION", "edgeCollectionName": "knows"}},
    },
)
result = translate("MATCH (n:Person) RETURN n.name", mapping=mapping)
result.aql, result.bind_vars
```

Also exposed: `nl_to_cypher` / `nl_to_aql`, `get_mapping` / `describe_schema_change`
/ `invalidate_cache`, `get_cypher_profile` / `validate_cypher_profile`, and the
`register_all_extensions` registry helpers.

### 6.2 CLI

`translate`, `run`, `mapping`, `doctor`, `synthbank` and `mine-examples` subcommands
(Typer + Rich, via the `[cli]` extra). Commands that connect take explicit
`--host`/`--port` first, else `ARANGO_URL` (so HTTPS clusters are reachable), else
`ARANGO_HOST`/`ARANGO_PORT`; they authenticate with JWT over HTTPS (platform
clusters refuse HTTP Basic) and Basic over HTTP, overridable with
`ARANGO_AUTH_METHOD`.

### 6.3 HTTP service (FastAPI)

Endpoint families (all under `arango_cypher.service`):

- **Connection & session** — `POST /connect`, `POST /disconnect`,
  `GET /connections`, `GET /connect/defaults`, `GET`/`POST /connect/platform`,
  `GET /connect/platform/diagnostics` (§13, platform sessions).
- **Cypher → AQL** — `POST /translate`, `POST /execute`, `POST /validate`,
  `POST /explain`, `POST /aql-profile`, `GET /cypher-profile`.
  `POST /execute` and `POST /execute-aql` take an optional `max_rows`
  (1–1,000,000): the response then holds at most that many rows, with
  `truncated: true`, `row_limit`, and a warning when the result had more, and
  the service closes the server-side cursor it stopped reading. Without
  `max_rows` every row is returned. A result travels as one JSON document, so
  the Workbench asks for at most 1,000 rows (`RESULT_ROW_LIMIT`): an unbounded
  `RETURN a, b` over IAM came to 156,861 rows and 257 MB, which the browser
  never finished receiving.
  AQL that ArangoDB rejects as invalid (HTTP 400, at EXPLAIN or at
  execution) is still refused, but is not reported as a tenant-scope
  refusal: when the service generated the AQL (`/execute`, `/aql-profile`) it
  is a 500 `GENERATED_AQL_REJECTED` whose message says it is a bug in Arango
  Cypher, not in the user's query; AQL the caller supplied (`/execute-aql`)
  is a 400 `AQL_REJECTED`. A query naming a collection the database does not
  have (ERR 1203) is not a tenant refusal either. Built from the schema
  mapping, it means the database changed after its schema was analyzed: a 409
  `SCHEMA_OUT_OF_DATE` naming the collection and saying to click Refresh
  schema. In AQL the caller supplied, it is a 400 `AQL_UNKNOWN_COLLECTION`.
  Neither is reported to the error inbox. A connection that drops before the answer arrives
  (even after a 200, mid-body) reads in the Workbench as a lost connection
  with the advice to retry or add LIMIT, not as "Failed to fetch".
- **NL → Cypher / AQL** — `POST /nl2cypher`, `POST /nl2aql` (responses carry
  `cached_tokens` for cost telemetry).
- **Schema** — `GET /schema/introspect`, `GET /schema/status`,
  `POST /schema/invalidate-cache`, `POST /schema/force-reacquire`,
  `GET /schema/statistics`, `POST /schema/index/create`. Schema warnings are
  returned as `{code, message, severity}` (`install_hint` when there is one),
  whatever form the analyzer reported them in, with a code specific to each
  message (clients dismiss by code); `severity: "info"` marks notes
  about normal operation (e.g. `ANALYZER_BASELINE_NO_LLM`: the schema was read
  without an LLM), which the Workbench keeps out of its warning banner.
- **Mapping** — `/mapping/export-owl`, `/mapping/import-owl`, `/suggest-indexes`.
- **Corrections (local learning)** — `POST|GET|DELETE /corrections` (Cypher→AQL),
  `POST|GET|DELETE /nl-corrections` (NL→Cypher).
- **Multi-tenant** — `GET /tenants[?collection=…]`; `tenant_context` field on the
  NL routes; `safe_execute`/EXPLAIN validation on every execute path.
- **Agentic tools** — `GET /tools/schemas`, `POST /tools/call`.
- **Mined examples** — `GET /examples` (session-scoped; §10, "Mined examples").
- **Error inbox** — the service files its own bugs as issues in a private
  GitHub repository (`ARANGO_CYPHER_ISSUE_REPO`, with a fine-grained token
  limited to issues on that repository, `ARANGO_CYPHER_ISSUE_TOKEN`; both or
  neither, and without them nothing is reported). Reported: generated AQL that
  ArangoDB rejects (`translator`), other 500s caused inside the service
  (`server_error`), and unhandled exceptions (`crash`). An unhandled
  exception returns JSON with `INTERNAL_ERROR` and a message whether or not
  the inbox is configured. Not reported: 4xx (the user's query, permissions,
  tenant refusals) and 5xx caused by ArangoDB or the network. One issue per kind of error,
  identified by a fingerprint of kind, code and summary with quoted text and
  numbers masked, so one bug hit on different collections is one issue.
  Repeats update the issue's counts: occurrences, hashed users, versions,
  endpoints, first and last seen. A closed issue whose error recurs is
  reopened with a comment. Edits people make to an issue survive the updates.
  An issue's example carries the Cypher, the generated AQL and the collection
  bind values, never other bind values, and nothing passes through the issue
  text unsanitized. Requests only enqueue. A background thread files and
  updates the issues, at most 20 new issues an hour, and keeps the counts
  across GitHub failures and restarts. A reported error's response carries
  `report: {reference, seen, issue, status}` next to `detail`. The Workbench
  adds "This was reported automatically to the Arango Cypher team (reference …,
  seen N times so far)". `GET /connect/platform/diagnostics` shows the
  inbox's state, never the token. `package-byoc.sh` bakes the two settings,
  and only them, into the bundle. (`arango_cypher/service/error_inbox.py`,
  `error_handlers.py`, `github_issues.py`.)

> **Operational note.** Agent/IDE shells inject `HTTP_PROXY`/`HTTPS_PROXY`/
> `ALL_PROXY` pointing at a loopback proxy that blocks DB traffic. Start the
> backend from a plain terminal or strip those vars first
> (`scripts/run_service.sh` does this).

### 6.4 Cypher Workbench UI (debug/demo)

React + TypeScript + Vite + CodeMirror 6. Chat-first, progressive-disclosure
workbench: an NL "Ask" composer, side-by-side Cypher/AQL editors, a query
inspector, results panel (table / JSON / graph via Cytoscape.js / explain /
profile), a schema-graph mapping view, a connection dialog with auto-introspect,
query history with bounded result snapshots, and local-learning ("Learn")
controls. UI architecture rules (object-centric canvas, left-click selects /
right-click acts, overlays over routes) are enforced project-wide.

The Workbench MUST use the Arango application colour scheme (the design rules in
`AGENTS.md`): Arango Green `#006532` for primary actions, links and active
states; Arango neutrals for surfaces, borders and text; `#da1a20` for errors
only. It MUST offer a day/night toggle in the header, default to **day**, and
remember the viewer's choice (browser storage; a blocked store just means the
choice is not remembered). Every surface follows the toggle live — Tailwind
utilities, the CodeMirror editors, and the Cytoscape/SVG graphs, whose canvas is
light gray by day with Arango Green selection.

The Workbench MUST offer "Report a problem" (settings menu, and beside the
query error banner and the NL → Cypher error banner). It builds a GitHub issue
for `arango-solutions/arango-cypher` — a public repository — and opens GitHub's
pre-filled new-issue page in the user's own browser: no token in the service,
nothing filed until the user submits it there, and Copy for users without a
GitHub account. The user sees and can edit the full text first; their own
"What happened" text is kept apart from the generated details, so changing an
option never discards it. By default a report carries only the app and analyzer
versions (`GET /health` reports `version` and `analyzer_version`), the error and
the browser, under a fixed title. Because errors can quote the query, the error
is reduced by default: quoted text (straight, backtick and typographic quotes,
not apostrophes in prose) and long digit runs are masked, and the attempted
Cypher an NL → Cypher failure appends is cut. The Cypher, the AQL and the
unreduced error are included only when the user ticks "include my query", since
they may contain names or values from the user's data; the dialog warns that
other values can still appear and must be checked. The report files the error of
the banner it was opened from. A report too long for GitHub's link limit is
shortened — the user's own text first, then the details from the end, so the
versions always survive — the dialog says so, and Copy keeps the full text (when
the clipboard is unavailable, the full report is shown selected for copying).

> `ui/dist/` is gitignored; rerun `cd ui && npm run build` after pulling UI
> changes. The service logs a `UI bundle is stale` warning on drift.

---

## 7. Schema detection, mapping & change detection

### 7.1 Mapping model

`arangodb-schema-analyzer` (from PyPI) is the **primary tier** for all schema
types. It produces a `MappingBundle` (conceptual schema + physical mapping +
metadata). When the analyzer is unavailable, a heuristic fallback runs and emits
an `ANALYZER_NOT_INSTALLED` warning; the service refuses to start on a heuristic
bundle unless `ARANGO_CYPHER_ALLOW_HEURISTIC=1`.

**The supported analyzer band is declared in `pyproject.toml`, not here.** It
admits the analyzer's current minor (and the previous one while a coordinated
raise is in flight) and MUST stay aligned with `arango-sparql-py`'s band: the two are co-installed in sibling dev venvs (CDF installs `arango-sparql-py` and the analyzer, not this package — verified 2026-09-15) and mutually exclusive bands are unsatisfiable — a lower
ceiling silently downgrades a co-installed newer analyzer and breaks its
consumers. Raising the band is therefore a coordinated change across both
repositories, never a unilateral one.

Physical styles the mapping layer resolves:

| Style | Entities | Relationships |
| --- | --- | --- |
| **PG** | one collection per type | `DEDICATED_COLLECTION` per type |
| **LPG** | generic collection + `typeField` (`LABEL` style) | `GENERIC_WITH_TYPE` |
| **Hybrid** | mix per type | mix per relationship type |
| **Embedded** | — | `EMBEDDED` (object/array on the parent doc) |

> **Note (2026-09-15).** `arangodb-schema-analyzer` 0.14.0 is the portfolio owner of
> LPG type detection (tier-1 discriminator names qualify on coverage alone; candidate
> fields `@type` / `entity_type` / `category` / `predicate`) and answers the `LABEL` /
> `GENERIC_WITH_TYPE` questions in the CSI it emits. This package's presence-based
> detector in `arango_cypher/schema_acquire.py` is **planned for retirement** in favour
> of consuming those answers (CDF unified-architecture paper, step 4); until then it
> remains the heuristic fallback described above.

Property roles are classified (`identifier`, `name`, `categorical`, `temporal`,
`numeric`, `free_text`) and surfaced to both the NL prompt and the entity resolver
so name-vs-token matching strategies are chosen correctly. Open-vocabulary edge
collections are capped and normalized (per-type domain/range derived from
`_fromType`/`_toType`) so GraphRAG schemas render and resolve sanely. *(2026-09-15:
the analyzer 0.14.0 now performs `_fromType`/`_toType` endpoint resolution itself; the
local derivation is retained only for the heuristic fallback path and is slated to go
with the detector retirement noted above.)*

### 7.2 Cardinality statistics

`compute_statistics(db, bundle)` stores per-collection counts, per-type label
counts, avg fan-out/fan-in, cardinality pattern (`1:1`/`1:N`/`N:1`/`N:M`), and
selectivity in `MappingBundle.metadata["statistics"]`. These feed traversal
direction, multi-part MATCH ordering, and filter placement in the transpiler, and
enrich the physical schema summary for the NL→AQL path.

### 7.3 Change detection & two-tier cache

Two fingerprints drive a cheap read-only probe: a **shape fingerprint** (collection
set + types + full index digests, stable under ordinary writes) and a **full
fingerprint** (shape + row counts). `describe_schema_change(db)` returns one of
`unchanged` / `stats_changed` / `shape_changed` / `no_cache`, letting long-running
services skip unnecessary re-introspection:

- `unchanged` → serve cached bundle (~1 ms)
- `stats_changed` → reuse mapping, refresh cardinality only (~50 ms)
- `shape_changed` / `no_cache` → full re-introspection

Caching is two-tier: a process-local dict in front of an ArangoDB-collection cache
(`arango_cypher_schema_cache` by default, excluded from its own fingerprints,
gated by `CACHE_SCHEMA_VERSION`). The persistent tier survives restarts and is
shared across replicas. Exposed via `GET /schema/status` and
`POST /schema/invalidate-cache`.

**A served schema rechecks itself.** `GET /schema/introspect` answers from the
cache and never fingerprints or analyzes on the request path, because on a
large database the fingerprints alone cost seconds. Nothing else re-analyzes
it where the catalog process doesn't run, as on BYOC. So introspect starts a
background **recheck**: on the first serve after a restart, then at most every
`ARANGO_CYPHER_SCHEMA_RECHECK_S` seconds per database and graph (default
300; 0 turns it off). A recheck is `get_mapping(revalidate=True)`: it compares
the live fingerprints with the cached schema's even inside the cache TTL. It
re-analyzes only when the shape changed, refreshes statistics when only counts
changed, and does nothing otherwise. A forced refresh counts as a fresh check.
The response carries `rechecking` and `schema_hash`. The Workbench keeps
polling while `rechecking` and swaps in the new schema when the hash changes.

**Service collections are not user data.** arango-cypher-py and
arango-sparql-py each keep a schema cache and mined examples in the databases
they serve: `arango_cypher_schema_cache`, `arango_cypher_examples`,
`arango_sparql_schema_cache` and `arango_sparql_examples`. This service's
two use their configured names. None of them may appear in an analyzed
schema: not as a class, shard family, statistic, sharding-profile entry or OWL
class. All four are excluded from both fingerprints, so one app's cache writes
never make the other re-analyze. Every analysis tier (the analyzer, the
heuristic detector and mapping builder, the fallback fingerprint) sees the
database through `hide_service_collections`
(`arango_cypher/service_collections.py`), whose `collections()` leaves them
out, because the analyzer itself skips only `_` system collections (issue #19;
mirrors arango-sparql-py's `schema/acquire.py`).

---

## 8. Cypher → AQL transpiler

### 8.1 Pipeline

1. **Parse** — Cypher → ANTLR4 parse tree (in-repo `grammar/Cypher.g4`,
   `arango_cypher/_antlr/`). No visitor; the renderer walks the tree directly.
2. **Resolve** — labels, relationship types, properties against the mapping
   (`MappingResolver`), with domain/range inference and `IS_SAME_COLLECTION`
   optimization.
3. **Emit** — parse tree + resolved mapping → AQL string + bind vars, with a
   post-pass that re-indents by `FOR` depth and prepends a `WITH` collection
   declaration for traversals.

**Semantic validation.** Between parse and emit, `check_semantics`
(`_translate_v0/semantic_check.py`, expression rules in `semantic_expr.py`,
types in `semantic_types.py`) runs Neo4j's compile-time semantic analysis over
the parse tree, so a query no Cypher engine accepts is refused instead of
translated and run. It tracks the variables in scope per clause and the type
each one is shown to have, and rejects: a variable bound twice
(`VariableAlreadyBound` — a path variable naming a bound variable, a bound node
re-declared in `CREATE`/`MERGE` alone or with labels/properties, a bound
relationship in `CREATE`/`MERGE`, `UNWIND` onto a bound name); one name used as
two kinds of value (`VariableTypeConflict`); a pattern predicate introducing a
variable (`UndefinedVariable`); a pattern expression used as a value
(`UnexpectedSyntax`, as in Neo4j 5): `size((a)-->())`, `RETURN (a)-->()`,
`WITH (a)-->() AS x`. A pattern expression is valid only as a predicate: a
`WHERE` condition, an operand of `AND`/`OR`/`XOR`/`NOT`, a `CASE WHEN`
condition, or `exists()`'s argument. The message names the replacements:
`COUNT { (a)-->() }`, `EXISTS { (a)-->() }`, or a pattern comprehension. The
old `size((a)-->())` used to translate to `LENGTH()` of a boolean, a silently
wrong count. A value of the wrong type where the query shows
the type (`InvalidArgumentType` — `NOT`/`AND`/`OR`/`XOR`/`WHERE` over a
non-boolean, arithmetic on strings/booleans/lists/maps/graph elements, `IN` a
non-list, subscripting a scalar or a list with a non-integer, a property read
off a scalar, list or path, `type()`/`length()`/`size()`/`labels()`/
`properties()`/... of the wrong kind of value, a non-integer `SKIP`/`LIMIT`, `DELETE` of a number, string or boolean);
a negative `SKIP`/`LIMIT` literal; integer literals outside 64 bits and float
literals outside double range; duplicate column names, an unaliased `WITH`
expression, `UNION` branches with different columns or mixed `UNION`/`UNION
ALL`; and, in a projection that aggregates, an item or `ORDER BY` item that
mixes an aggregate with anything but literals, parameters, the projection's
columns (in `ORDER BY`) and grouping keys that are a variable or a property
access (or a property of a grouped variable) — `RETURN me.age + count(*)`
without `me.age` as a key, or a compound key such as `me.age + you.age`
reused beside the aggregate, is `AmbiguousAggregationExpression`
(`semantic_aggregation.py`). Errors Neo4j raises at run time are raised here
when a literal shows the offending value: `range()` of a non-integer or with a zero step, and a
conversion (`toBoolean()`, `toInteger()`, `toFloat()`, `toString()`) or
`type()`/`labels()` of a list-literal element of the wrong kind in a list
comprehension. The refusal is a `CypherSemanticError` (a `CoreError`) with code
`CYPHER_SEMANTIC_ERROR` (compile-time `SyntaxError` class) or
`CYPHER_TYPE_ERROR` (run-time `TypeError`/`ArgumentError` class), its message
led by the openCypher error detail; the service answers it with HTTP 422, and
it is never `UNSUPPORTED`, so the AI fallback (§8.5) does not try to "recover"
an invalid query. The analysis only rejects what the query shows: a type it
cannot see (a property value, a parameter, most function results) passes, and
an open scope (`YIELD *`) turns the undefined-variable check off.

**Name safety.** Cypher values reach AQL as bind parameters. Cypher *names*
(variables, aliases, property keys, labels) reach the AQL text, and
backtick-quoted ones are copied with their backticks. Inside AQL backticks a
`\` starts an escape, so a name such as ``n.`a\` `` would end its quoting
early and splice the rest of the query into the AQL as code. Translation
therefore refuses (`UNSUPPORTED`) any backtick-quoted name containing a
backslash or a backtick (`_reject_unsafe_escaped_names`, run on every parse
tree). Any other content means the same thing in both languages. A variable
or property key spelled like an AQL reserved word (`like`, `filter`, `sort`,
`in`, `limit`, … in any case) is a plain name in Cypher but a syntax error in
AQL, so it is emitted backtick-quoted (`_quote_aql_reserved_names`); so is a
property key AQL cannot spell bare (`n._001`, `n.ä`) and a variable that
starts well but holds other characters (`aä`). A variable AQL refuses even
quoted — one that does not start, after any underscores, with an ASCII letter
(`_001`, TCK Create4; `ä`) — is renamed in the tree to a fresh, deterministic
name (`v_001`, numbered while it clashes; `_rename_invalid_aql_variables`); a
column named by it as written keeps that name. Names the
translator derives from a quoted name (`_x_distinct`, `x_1`, `{var}TypeField`)
stay inside its backticks (`_affix_name`), and bind names keep only the
characters AQL accepts in one (`_pick_bind_key`). An unnamed node or
relationship gets an AQL variable that no other part of the query uses or
names (`_anonymous_var`). Parameter
names must be valid AQL bind names (`[A-Za-z_][A-Za-z0-9_]*`). ``$`p` `` binds
as `@p`. The attribute name in `REMOVE n.prop` is emitted as an escaped AQL
string. Collection names never reach the AQL text: they come from the mapping,
which callers send with `/translate` and `/execute`, and many are not bare AQL
identifiers (`IAM-AWS-DOCS-DEMO_Chunks`). Every collection reference is a
`@@` bind parameter, including the traversal `WITH` prelude, which reuses a
collection's existing bind parameter and otherwise adds `@withCollectionN`
names that clash with no other bind variable (`_with_collection_refs` in
`formatting.py`).

String literals are copied to AQL with their quotes, since both languages end
a string in the same place, but the escapes AQL reads differently are
rewritten: `\N`, `\T`, `\B`, `\F`, `\R` (plain letters in AQL) become their
lowercase forms, and the eight-digit `\UXXXXXXXX` (absent from AQL) becomes a
`\u` surrogate pair (`_aql_string_from_cypher`).

The translator core lives in `arango_cypher/_translate_v0/` (`core.py`,
`writes.py` with the write translators `write_create.py`, `write_merge.py`,
`write_mutate.py` and `write_foreach.py`, `formatting.py`, shared `state.py`
contextvars). Translation results
are LRU-cached (256 entries).

### 8.2 Supported subset (read)

`MATCH` (single/multi pattern parts, multiple clauses, multi-hop, bounded
variable-length `*1..n`); `WHERE` (boolean `AND`/`OR`/`NOT`/`XOR`, comparisons,
`IN`, `IS [NOT] NULL`, `STARTS WITH`/`ENDS WITH`/`CONTAINS`, regex `=~`);
`RETURN` (projections, aliases, `DISTINCT`, `ORDER BY`, `SKIP`, `LIMIT`); `WITH`
pipelines + aggregation (`count`, `avg`, `sum`, `min`, `max`, `collect`);
`WITH … MATCH`; `OPTIONAL MATCH` (companion and sole-reading-clause forms);
`UNWIND`; `CASE` (simple + generic, emitted as nested AQL ternaries because
AQL has no `CASE`; a null condition falls through, a missing `ELSE` is null);
`UNION` / `UNION ALL`; list & pattern
comprehensions; named paths + `length`/`nodes`/`relationships`; `EXISTS { }` and
`COUNT { }` subqueries; `CALL { … }` subqueries (below); label predicates on untyped vars
(`WHERE risk:RISK_FACTOR`); scalar/builtin functions and `arango.*` extensions
(§9); inline pattern properties; named parameters `$param`; dot-path property
access (`n.address.zip`). A parameter that shares its name with a bind
variable the translation generates (`typeValue`, `@collection`, …) and would
have its value replaced is refused (`UNSUPPORTED`) rather than silently rebound.
A variable-length relationship on a type-discriminated (LPG) edge collection
follows only edges of its type; `*0..` includes the zero-hop row. An open-ended
range (`*`, `*2..`) is limited to 10 hops (`_MAX_VLP_DEPTH`), and the
translation says so in a warning. `coalesce()` is AQL `NOT_NULL()`.
A `MATCH` after `WITH` may have several comma-separated pattern parts. A
variable it names that is already bound is correlated, not rebound. Its inline
properties refer to the variables the `WITH` bound, under their renamed
names (`{id: knownTagId}` after a `WITH` that grouped `knownTagId` again). An
`ORDER BY` after `WITH` that names a variable no longer in scope is refused
(`UNSUPPORTED`), as Cypher raises `UndefinedVariable`.
`OPTIONAL MATCH` yields one row per match, and one null-extended row when
nothing matches, wherever it appears: after `MATCH`, after `WITH`, or leading a
`WITH` pipeline. A pattern with several parts matches as a whole, and a part
may have any number of hops; one relationship is never matched twice within
a pattern. A hop may name several relationship types (`[:A|B]`, also
variable-length): on PG the traversal follows each type's edge collection, on
LPG it tests the type field against the list, and `type(r)` is the type of
the edge matched. `startNode(r)` / `endNode(r)` are the documents the edge's
`_from` / `_to` name, as a MATCH binds a node, and null for null. Types mapped in different styles, or to different
type-discriminated collections, are refused; so is a write to such a
relationship and an edge scan `MATCH ()-[r:A|B]->()`. `p = shortestPath((a)-[:R*]-(b))` between
nodes bound earlier (another pattern part or a WITH) is a breadth-first
traversal that keeps the first path reaching `b`, so it honours the type
guard on LPG; a pair with no path yields no row, as in a MATCH. The path is
`{nodes, edges}`, so `length(p)` and `nodes(p)` work on it. One relationship,
any type and direction, minimum length 0 or 1; relationship properties are
refused. The grammar is regenerated with `scripts/regen_antlr.sh`. Aggregates follow Cypher's null semantics:
`count(x)`, `count(DISTINCT x)` and `collect(x)` skip nulls, and `sum()` over
no rows is 0. `percentileDisc(x, p)` and `percentileCont(x, p)` gather `x`
like `collect()` and compute Neo4j's definition over the sorted non-null
values after grouping (`percentiles.py`); a `p` outside [0, 1] fails each
group with `NumberOutOfRange`. `min()`/`max()` of a value the query shows to
be a string, or an element of a list of mixed types, are gathered the same
way and pick the least/greatest value in Cypher's order (strings by code
point, types by rank); `min`/`max` of anything else, a property above all,
stay AQL aggregates. An `ORDER BY` item may use an aggregate the projection
computes (`RETURN u.city, count(*) AS c ORDER BY count(*)`), which reads that
item's value; any other aggregate there is refused (`UNSUPPORTED`). The clauses of a `WITH` pipeline compile in query order, so a
`MATCH` or `UNWIND` between two `WITH`s reads the rows of the `WITH` before it.
An `UNWIND` before the first `MATCH` of such a pipeline is refused
(`NOT_IMPLEMENTED`). An unaliased variable is returned under its Cypher name
even when it is bound under another AQL name. Two unaliased columns inferred
from the same property (`RETURN a.name, b.name`) are named `name` and `b_name`,
with or without aggregation, and `ORDER BY` resolves each expression to its own
column. `ORDER BY` sorts null after every value ascending and before every
value descending, as Cypher does (AQL sorts null below every value), through
a leading "is null" sort key on each key that can be null. `RETURN DISTINCT`
honours the sort direction, and a projection alias named like a variable
(`RETURN a.name AS a ORDER BY a`) sorts by the alias. `DISTINCT` keeps the
order of the rows it reads, as Neo4j does: after `ORDER BY …` a `WITH DISTINCT`
or `RETURN DISTINCT` keeps each value's first row in that order, so a
following `LIMIT` keeps the top rows (AQL's `COLLECT` would re-sort them by
value). `SKIP` and `LIMIT` count distinct rows (AQL applies a `LIMIT` before
`RETURN DISTINCT`, so the distinct rows are read from a subquery). A string the
query shows to be one (a string literal, a string function) sorts and
range-compares (`<`, `<=`, `>`, `>=`) by code point, as in Cypher, through the
hex of its UTF-8 bytes (`TO_HEX`): AQL's ICU collation ignores case at the
first level and puts `é` beside `e` (LDBC BI2's tie-break by tag name). A
property the mapping declares `string` does too, but only when its value is a
string at run time (`IS_STRING(x) ? TO_HEX(x) : x`), since the declaration is
not checked against the data. A value of unknown type keeps AQL's order
in both, so sorting and comparing agree (TCK WithOrderBy1 [45]) and a range
filter on it can still use an index. `ORDER BY` a value the query shows to
be a list (`UNWIND [[1], ['a']] AS l … ORDER BY l`, `collect(…)`), or an
element of a list of mixed types (`UNWIND [1, 'a', null] AS x`), sorts in
Cypher's cross-type order — maps, nodes, relationships, lists, paths,
strings, booleans, numbers, null last ascending — lists element by element:
the key is `[type rank, value]`, a list's elements keyed the same way one
level deep (`sort_keys.py`; the semantic pass records each sort item's type).
Every other sort, a property above all, keeps the plain key an index can
serve. After a `COLLECT`, an `ORDER BY` on a grouping key's own expression
(`WITH a.name AS name, count(*) AS c ORDER BY a.name`) reads the key.
`RETURN type(r)` on its own is a
scalar column. An undirected single hop from a node back to itself matches each
self-loop once, in every pattern position, and an undirected pattern with both
ends anonymous (`MATCH ()--()`, an edge-collection scan) yields each edge once
per orientation and a self-loop once. An undirected hop between two *different*
variables still yields a self-loop twice (an ANY traversal visits it in both
directions), where Cypher matches it once (TCK Match3 [11]/[15]/[16],
CountingSubgraphMatches1 [10]/[11]). This is a **known, accepted divergence**:
matching a self-loop once would mean replacing every undirected traversal by
an edge scan or a union of an OUTBOUND and an INBOUND traversal, which costs
the traversal's performance on every undirected pattern (LDBC) to fix a
pattern that only differs on self-loops.
`CALL { … }` subqueries (`arango_cypher/_translate_v0/subqueries.py`): for
each incoming row the body runs once, and the row continues once per row it
returns, with its columns added; a row whose body returns nothing is dropped.
A leading `WITH a, b` imports outer variables (only plain variables, as in
Cypher); a body without one is uncorrelated. Every `RETURN` item of the body
is a variable or aliased, and none may rebind a variable in scope. Bodies
joined by `UNION` / `UNION ALL` must return the same columns. The body is an
AQL subquery over the outer variables; names it shares with the outer query
are renamed (AQL refuses to redeclare a name in scope, ERR 1511), and its bind
variables merge as a `UNION` branch's do. Writes in the body, a `CALL { … }`
in a query that writes or inside `EXISTS { }` / `COUNT { }`, an importing
`WITH` with `WHERE`/`DISTINCT`/`ORDER BY`/`SKIP`/`LIMIT`/aliases, `RETURN *`
in the body and `CALL { … } IN TRANSACTIONS` are refused. An aggregate
column keeps its Cypher name when its variable is bound under another AQL name
(`RETURN a, count(b)` returned the column as `a_1`).
An unlabeled node reached by a traversal matches any node its edges lead to:
on a mapping with several node collections it gets no collection check (it
was refused, "A single label is required"); with one node collection, or one
domain collection beside side stores, it is checked against that one. An
unlabeled node a pattern starts from, not yet bound, is scanned in the
collection its first relationship's endpoint guarantees (`(x)<-[:HAS_CREATOR]-…`
starts at a Person), and refused when that is not one collection.
In a `UNION`, internal names that repeat across branches with different values
are renamed per branch (`@@collection_u1`); quoted AQL text is left untouched.

Predicates follow Cypher's three-valued logic (true / false / null), which
AQL's two-valued operators do not: AQL's `null == null` and `null != 'x'` are
true, and `NOT null` is true. A comparison (`=`, `<>`, `<`, …), `IN` or
string operator (`STARTS WITH`, `ENDS WITH`, `CONTAINS`, `=~`) with a null
operand is null — a string operator with any operand that is not a string
is null too (`1 CONTAINS 1`; AQL's string functions would convert it), each
operand the query does not show to be a string being guarded by `IS_STRING`;
every string contains `''` (AQL's `CONTAINS(x, '')` is false), and `NOT x ENDS
WITH y` negates the whole test (`string_predicates.py`) — so `WHERE n.x = null` and `WHERE n.x = $p` with a null `$p`
match nothing, and `WHERE NOT n.name = 'b'` and `WHERE n.name <> 'b'` drop
rows whose `name` is null. `NOT` of null is null. A predicate in a filter
(`WHERE`, a comprehension's or quantifier's `WHERE`, a `CASE WHEN` condition)
compiles to the two-valued test the filter observes, with De Morgan's laws
under `NOT`; an equality keeps its plain `n.p == @v` form beside a null guard
on the parameter, so ArangoDB still serves it from an index. A predicate
whose value is returned or stored keeps null (`RETURN n.age > 30` is null for
a null `age`). The quantifiers `any`/`all`/`none`/`single` return Cypher's
three-valued answer: the predicate is evaluated per element as a value and the
counts of true, false and null results decide (`all(x IN [1, null] WHERE x > 0)`
is null, `any(…)` of it is true; a null list gives null;
`_translate_v0/local_scopes.py`).

**Integer literals** may be decimal, hex (`0x1F`) or octal (`0o17`, and the legacy `017`); all reach AQL in decimal, since AQL has no octal literal and its hex literals stop short of 64 bits (`0x7FFFFFFFFFFFFFFF` is ERR 1501). The grammar accepts `0o` (`grammar/Cypher.g4`, `OctalInteger`); one parser, `literals.parse_integer_literal`, serves the translator, the constant evaluator and the semantic pass.

**Constant expressions are evaluated at translate time**
(`_translate_v0/const_eval.py`, value rules in `cypher_values.py`, functions in
`cypher_functions.py`). An expression whose inputs are all known — literals,
variables a `WITH` bound to constants, deterministic functions of those — is
evaluated with Cypher's semantics and emitted as a literal (strings and
composites as bind variables, never interpolated). Cypher and AQL disagree on
exactly these values: nulls inside lists and maps (`[1, null] = [1, null]` is
null), comparison across types (`1 < '1'` is null, `NaN` orders false), integer
division and remainder (Java's), number formatting (`toString(1e20)` is
`'1.0E20'`), 64-bit overflow. Anything the evaluator does not model (data,
parameters, `rand()`, aggregates) is compiled to AQL as before. A runtime
Cypher error becomes `FAIL()` at its position, so it fires only when evaluated
(`CASE WHEN false THEN 1/0 END` is fine); a compile-time error (wrong arity)
still rejects the query. A NaN or infinite result is not folded (JSON cannot
carry it).

A variable local to an expression — a list comprehension's, quantifier's or
`reduce()`'s — and an `UNWIND` variable get a fresh AQL name when the same name
is bound elsewhere in the query: AQL keeps a `FOR` variable in scope after Cypher
drops it at a `WITH`, and redeclaring it is an error (ERR 1511).

An inline pattern property (`(n {name: 'Alice'})`, `[r {since: $s}]`) is the
same `=`: exact, served by an index on the property, and never true for a
null value. `translate(..., case_insensitive_inline_strings=True)` (also a
`TranslateOptions` field) opts string literals into a case-insensitive match
(`LOWER(n.name) == LOWER('alice')`, which no index serves) for callers, such
as an NL front end, that want it; it is part of the translation cache key.

An unaliased RETURN column is keyed by a short name inferred from its
expression (`n.name` → `name`, `count(*)` → `count`, `a.name, b.name` →
`name`, `b_name`): the result shape API, UI and NL clients read, and the
default. `translate(..., cypher_column_names=True)` (also a
`TranslateOptions` field, and part of the translation cache key) names the
column as openCypher does, by the expression as written in the query —
`{"n.name": …, "count(*)": …, "n.age IS NULL": …}` — with aliases unchanged
(`columns.py`). Only the column name changes: the AQL variables a COLLECT,
DISTINCT or ORDER BY binds keep their inferred names, a single unaliased
column is still returned bare, an OPTIONAL MATCH's null row uses the same
keys, and a CALL subquery's RETURN keeps the keys its enclosing query reads
it by. The service and CLI do not expose the option.

Arithmetic follows Cypher, where AQL's operators are numeric only and read
null as 0 (`null + 1` is 1, `'a' + 'b'` is 0). An arithmetic expression with
a null operand is null; nested arithmetic shares one null guard over its
operands, and a function call keeps its own (`coalesce(n.a - 1, 0)`). `+`
adds numbers, concatenates a string with a string or number (`'a' + 1` is
`'a1'`, evaluated left to right so `1 + 2 + 'a'` is `'3a'`), concatenates two
lists, and otherwise adds an element to a list; a null operand makes the result
null, lists included (`[1] + null` is null and `[1] + [null]` is `[1, null]`,
as on Neo4j 5.26). The operation is chosen at translation time when the operand
types are known: literals, functions with a fixed result type (`count()`,
`toString()`, …) and arithmetic results. Otherwise `+` dispatches on the
operand types at run time. A type the mapping declares for a property does
not choose the operation: it is not checked against the data, and the schema
analyzer declares numeric properties `string`, so `n.age + 1` concatenated
and `ORDER BY n.id` sorted as text when it did. Dividing two operands known
to be integers truncates toward zero (`7 / 2` is 3); two properties declared
`integer` truncate when both values are whole at run time, and otherwise
divide as numbers.

The conversion functions return null where AQL's conversions are total.
`toInteger()` truncates toward zero (`toInteger(3.7)` is 3; AQL's
`TO_NUMBER` does not truncate) and reads a numeric string; `toInteger()`
and `toFloat()` of a non-numeric string are null (`TO_NUMBER('abc')` is
0). `toBoolean()` reads `'true'`/`'false'` in any case and is null for any
other string (AQL's `TO_BOOL('false')` is true). `toString(null)` is null
(`TO_STRING(null)` is `""`). An aggregate's argument compiles like any
expression (`sum(toInteger(x))`, `count(n.x = 1)`).

A pattern comprehension (`[(a)-[:R]->(b:L)-[:S]->(c) WHERE … | expr]`)
matches its whole pattern, as a pattern predicate does: every hop with its
type, direction, range and properties, and each end node's labels
(collection membership on PG, supertypes included) and properties. It must
start at a bound variable (from the query or an enclosing comprehension,
quantifier or `reduce()`). A named node or relationship already bound is
matched, not rebound; one that is not is introduced by the comprehension,
and a named variable-length relationship is its list of edges. A named path
(`[p = (a)-->(b) | p]`) is `{nodes, edges}`, as in MATCH. `exists()` of a
pattern is the pattern predicate itself. In MATCH, a named path is bound for a
single node (`MATCH p = (a)` is `{nodes: [a], edges: []}`) and before a `WITH`
(`MATCH p = (a)-[*]->(b) WITH p …`), where each hop's traversal path
contributes its vertices and edges in pattern order; and in an `OPTIONAL MATCH`
or a `MATCH` after a `WITH` (`OPTIONAL MATCH p = (a)-[:X]->(b)`, null when
nothing matches), in the order the pattern is written even when it is compiled
from its bound end. There a named variable-length relationship is its list of
edges too, and so it is in a first MATCH: a variable-length relationship the
query reads (`[r:T*1..2]`, and `[r*1..1]`, which is variable-length syntax)
gets the traversal's path (`LET r = path.edges`), a named path splices each
variable-length hop's vertices and edges, and the properties of a
variable-length relationship hold for every edge, not only the last.
Relationship uniqueness across a pattern's traversals covers merged hops and
variable-length edge lists. An empty interval (`*2..1`, `*..0`) matches
nothing. `MATCH … UNWIND … RETURN` runs the UNWIND before the projection's
`COLLECT`/`SORT`, and an UNWIND variable is in scope for `RETURN *`. A query
whose only MATCHes are `OPTIONAL` projects its RETURN over one row of null
variables when nothing matches (`n.missing IS NULL` is true).

`x[i]` is Cypher's: unless the query shows a list read with an integer or a
map read with a string, the subscript checks the run-time types and fails
with the Cypher TypeError (`InvalidArgumentType`,
`MapElementAccessByNonString`) where AQL returns null (`subscripts.py`).
Positional parameters (`$0`, `$1`) are AQL's `@0`, `@1`.

`IN` has the precedence openCypher and Neo4j give it, not the openCypher 9
grammar's (which listed it among the postfix list operators): it binds looser
than `+`/`-` and than subscripts and slices, and tighter than comparison —
`3 IN list[0]` is `3 IN (list[0])`, `[1] + 2 IN [3] + 4` is
`([1] + 2) IN ([3] + 4)`, `false = true IN [true]` is `false = (true IN [true])`
(`oC_InExpression` in `grammar/Cypher.g4`). Its value is Cypher's: null for a
null list, false for an empty one, null for a null element, true when found,
and otherwise null if the list holds a null (AQL's `null IN [null]` is true,
its `5 IN [1, null]` false) (`three_valued.compile_in`).

An aggregate may be nested in a projection expression (`head(collect(x))`,
`100 * count(*) + count(x)`, `list + collect(x)`, `[y IN collect(x) | …]`):
each aggregate call is grouped as a variable of its own and the expression
is evaluated after grouping, with grouping keys referring to their group
variables. A projection with `collect()` and another aggregate groups
`INTO` rows (`PUSH` is not a valid `COLLECT AGGREGATE` expression on
ArangoDB 3.11). An aggregate where a list comprehension, quantifier,
`reduce()` or pattern comprehension binds its own variable
(`[x IN xs | count(*)]`) is refused, as Cypher raises InvalidAggregation; the
list such a construct iterates over may aggregate.

### 8.3 Supported subset (write)

`CREATE` (nodes, relationships, whole-map params `CREATE (n $props)`, and
`CREATE → SET/REMOVE`, folded into the inserted document); `SET` (`=`, `+=`,
property and whole-document forms); `DELETE` / `DETACH DELETE`; `REMOVE`
(property removal); `MERGE` (node and single-hop relationship, with
`ON CREATE` / `ON MATCH SET` of properties, and of labels where the mapping
stores several per node; an unlabeled node MERGEs within the one collection
unlabeled nodes live in; a RETURN after MERGE may aggregate, sort and page.
MERGE refuses to rebind a variable bound before it, a null property value (of a node or a relationship), a variable-length relationship, an
action on an undefined variable, and a whole-map `SET n = …`/`+= …` action on
a merged node, which it used to drop. On a single-hop relationship MERGE a
whole-map action applies in order with the property actions (`ON CREATE SET
r = a` copies node a's properties, `ON MATCH SET r += {w: null}` merges a map;
the UPDATE becomes a REPLACE of `OLD`, and `_from`/`_to`/the type field
survive a `=`; `merge_docs.py`); a property set to the literal `null` is left
out of the INSERT and removed on match (`keepNull: false`). A named path
(`MERGE p = (a {num: 1})`, `MERGE p = (a)-[r:T]->(b)`) is bound to the merged
node, or to the endpoints and relationship in pattern order, for a single
MERGE of a node or one hop; beside another MERGE or over a longer pattern it
is refused. An ON CREATE / ON MATCH item on a bound endpoint of a
relationship MERGE (`ON CREATE SET b.created = 1`) writes that node after the
UPSERT, when the UPSERT's `OLD` says it created (or matched) the
relationship: one UPDATE per endpoint collection (two variables in LPG's one
`vertices` collection share it), and a RETURN of that endpoint, which would
show it without the SET, is refused (`merge_bindings.py`). An item on a
variable the MERGE neither writes nor binds as an endpoint stays refused. A
relationship MERGE matches on the relationship's properties as well as its
endpoints and type (`MERGE (a)-[:T {name: 'r2'}]->(b)` does not match an
`r1` edge; it used to). A relationship MERGE whose endpoints no earlier
clause binds merges the whole pattern, as openCypher does, when it is the
query's first clause: the pattern is read first (`_merge_found`), and if it
is missing both nodes and the relationship are created — one INSERT per node
collection, then the edge (`merge_patterns.py`). After another clause (each
row would have to see the nodes earlier rows created), over several hops,
or with ON CREATE / ON MATCH, it is refused (`NOT_IMPLEMENTED`); it used to
compile to AQL reading undefined variables. A
`SET n.p = v` after a node MERGE joins both the INSERT and the UPDATE
document, after the ON CREATE / ON MATCH items; any other SET after a MERGE
is refused); `FOREACH` (with `SET`, and — newly — `CREATE` / `DELETE`); and `WITH … SET`/`DELETE`/`REMOVE` on MATCH-bound document variables
(including identity aliases such as `WITH p AS x`, and relationship variables
from traversals).

A query whose only updating clauses are `DELETE` / `DETACH DELETE` reads
before it writes. Its reading clauses (any `MATCH`, `OPTIONAL MATCH` and
`UNWIND`) compile through the read translator into a list of matched rows;
the ids of the deleted nodes and relationships are collected from it
(nulls of an unmatched `OPTIONAL MATCH` dropped, a node matched on several
rows deleted once), and for `DETACH DELETE` so are the ids of every edge
touching a deleted node; then each collection is written by one `REMOVE`
over the ids that live in it: edges first, then nodes, including every
member collection of a supertype. `DETACH DELETE p` of a named path removes
its nodes and relationships (and, detaching, every edge touching its nodes);
`DELETE p` without DETACH is refused, since the check that its nodes keep no
other relationship is not made. A `RETURN` reads the matched rows; a row
that reads a property or the labels (`labels()`, `properties()`, `keys()`)
of a node or relationship the query deleted fails with Neo4j's
`DeletedEntityAccess`, through a per-row `FILTER v == null OR FAIL(…)`, so a
match with no rows still succeeds and returning the entity itself or its
`type()` stays allowed (`_deleted_entities_read` in `deletes.py`). AQL
forbids reading a collection after writing it (ERR 1579), which deleting
inside the matching loop did as soon as a pattern matched more than one row.

**No write is dropped.** The single-part router hands a query part to one
translator by the kind of write it sees (FOREACH, MERGE, CREATE, DELETE,
SET/REMOVE), and each compiles only its own clauses. A write of another kind
beside it (`CREATE (:X) MERGE (:X)`, `MERGE (n) REMOVE n.x`, `MATCH (a) SET
a.x = 1 FOREACH (…)`, a FOREACH beside SET/DELETE after a WITH) used to be left
out of the AQL without an error; it is refused (`UNSUPPORTED`,
`refuse_dropped_writes`). So are the other parts a write compiler silently
skipped: an UNWIND, CALL or OPTIONAL MATCH before a FOREACH, a second FOREACH,
a RETURN after a FOREACH, a whole-map or label SET inside one, an OPTIONAL
MATCH before CREATE/MERGE (compiled as a MATCH, so its unmatched rows lost
their write), and `REMOVE n:Label` where the mapping keeps no labels array.

`SET`, `REMOVE` and `DELETE` may also stand before a WITH (`MATCH (n) SET
n.num = n.num + 1 WITH n WHERE n.num % 2 = 0 RETURN n.num`, `MATCH (n) WITH n,
n.num AS num DELETE n WITH sum(num) AS s RETURN s`; TCK Set6, Remove3,
Delete6). The writes nest inside the matching loop, and the clauses after them
read each written variable as the document after the write (`LET n_new =
NEW`). After such a write the query may filter, project, aggregate and
`UNWIND`, but a later `MATCH`, `OPTIONAL MATCH`, `CALL` or second write is
refused (`UNSUPPORTED`): AQL cannot read a collection after writing it in the
same query (ERR 1579), nor write one twice (`pipeline_writes.py`). `CREATE`
and `MERGE` before a WITH in a query with MATCH stay refused.

A `DELETE` after a WITH of an expression rather than a MATCH-bound variable
(`WITH collect(n) AS ns DETACH DELETE ns[$i]`, `WITH {k: n} AS m DELETE m.k`;
TCK Delete5) reads first: the pipeline compiles into a list of rows carrying
the values to delete, the null ones are dropped and a non-entity fails the
query, and each collection is then written by one REMOVE (edges, the edges
touching deleted nodes under DETACH, then nodes); a RETURN reads the
materialised rows (`pipeline_deletes.py`).

A CREATE may stand before a WITH (`MATCH … CREATE … WITH … UNWIND … MATCH …
CREATE`, LDBC INS1/4/6/7), with only CREATE as the updating clause. The query
compiles as a chain of stages, one per CREATE group, each a list of rows; a new
node's key is generated in its stage (`UUID()`) so later parts' edges can name
it, and a stage that matches nothing does not undo an earlier stage's
creations. Then every new node and edge is inserted with one INSERT per
collection, and a RETURN reads the last stage's rows. A MATCH that reads what
the query created (a created variable, label or relationship type) is refused
(`NOT_IMPLEMENTED`): it would need several statements.

**How SET and REMOVE are written** (`arango_cypher/_translate_v0/property_writes.py`).
A statement's `SET` and `REMOVE` items are gathered per variable, in query
order, and each variable gets one write, because a second write to a collection
is refused (ERR 1579):
- property changes, `+=` maps and removals become one `UPDATE`; a removal is a
  `null` under `OPTIONS {keepNull: false}` (`UPDATE` merges, so a document
  without the attribute would leave it in place);
- `SET n = {…}` becomes one `REPLACE` that keeps `_from`/`_to` on edges and the
  type field under label-style mappings (a plain replacement drops them: the
  edge is rejected, ERR 1233, or the node silently loses its label);
- after `CREATE`, the items are folded into the inserted document; a value that
  reads the created variable sees the document as the earlier items left it.

Each variable is written in its own collection: a relationship variable in its
edge collection and an end node in its label's collection (an unlabeled one as
the read path infers it, failing closed when that is ambiguous). Translation
refuses (`UNSUPPORTED`) the forms that used to write the wrong thing: two
variables writing one collection (`SET a.x = 1, b.x = 2` with `a`, `b` both
`:User`), a nested property (`SET n.a.b` wrote `n.b`), a variable-length
relationship, a variable not bound by the first `MATCH` pattern (such as one
from `OPTIONAL MATCH`), `SET n:Label` under a mapping that cannot store a second
label, and a write that follows a `MATCH` with several pattern parts. Only the
first would be applied, writing rows the others exclude. A write after several
reading clauses (`MATCH (n) OPTIONAL MATCH (n)-[r]->() REMOVE r.num RETURN n`,
TCK Remove1 [6]; `MATCH (a) MATCH (b) SET a.x = b.x`) compiles them as a
pipeline and writes after them (`translate_multi_reading_mutation`); a
variable an OPTIONAL MATCH leaves null is written over `(x == null ? [] :
[x])`, since SET, REMOVE and DELETE of null do nothing — after a WITH too
(`nullable_vars` in `pipeline_writes.py`). `REMOVE n:Label` is skipped under such a mapping, as it always was.

A Cypher property is never null: `SET n.p = null`, a null entry of a `+=` map
or of a parameter map removes the property (`UPDATE … OPTIONS {keepNull:
false}`), and `SET n = {…}` stores the map without its null entries. A
`RETURN` after `SET`/`REMOVE` reads the written document (`LET n_new = NEW`),
not the one matched. After a sole `OPTIONAL MATCH` that matches nothing there
is one row whose variables are null, and `SET`/`REMOVE`/`DELETE` of null does
nothing (each write is guarded: `FOR _w IN (n == null ? [] : [n]) UPDATE _w …`).
`SKIP`/`LIMIT` refuse a parameter whose value is not a non-negative integer.

An `EXISTS { … }` pattern correlates with a node only when the query binds
that name before the subquery (`scopes.py`); otherwise the name is the
subquery's own variable (a traversal target, or a scan of the unlabeled-node
collection for a single node), and a named relationship is the subquery's
edge variable. A multi-hop pattern inside `EXISTS { }` compiles as a MATCH
over the outer query's bound variables, every hop tested; with a `WITH`
aggregation it fails closed rather than being shortened to its first hop.

**Multi-label nodes in a type-discriminated collection.** A `LABEL` entity
mapping may name a `labelsField` (`"labels"`) besides its `typeField`: a node
with several labels then stores all of them in that array, its type field
holding one (`label_tests.py`). `CREATE (:A:B)` writes both fields; a label
test reads both (`n.type == 'B' OR 'B' IN n.labels`); `labels(n)` returns the
array, the type field, or `[]` for an unlabeled node; `SET n:L` / `REMOVE n:L`
update the array and keep the type field on a label the node still has.
Without `labelsField` nothing changes: the type field alone is the label.

`keys(x)` and `properties(x)` of a node or relationship (an object with `_id`)
leave out the system attributes (`_id`, `_key`, `_rev`, `_from`, `_to`) and the
mapping's type/labels fields; a map keeps every key.

The written-to rows are those the whole first pattern matches. End-node labels
(including a collection-style label on a polymorphic relationship), end-node
properties and relationship properties are filters, as on the read path. Before
2026-10-24 the write path dropped them: `MATCH (:Person {id: $p})-[l:LIKES]->
(:Post {id: $q}) DELETE l` removed every LIKES edge of the person
(`ldbc_benchmark_plan.md` §3.5).

**Recently closed write-clause gaps (2026-06):** unlabeled `SET`/`DELETE`/`REMOVE`
on `MATCH (n)`; **multiple `MERGE` clauses** in one statement; **multi-hop
relationship `MERGE`**; **`CREATE`/`DELETE` inside `FOREACH`**.

**Single-part `CREATE` as a pipeline (2026-10).** A single-part `CREATE`
that would write one collection twice (`CREATE (), ()`; two relationships of an
LPG edge collection), or whose `RETURN` aggregates, sorts, pages or
de-duplicates, compiles as the CREATE pipeline of §8.3 with one stage: every
new document is built first and each collection gets one `INSERT` over all of
them; the `RETURN` reads the stage's rows, so `CREATE … RETURN … LIMIT 0` still
creates. `CREATE` refuses an undirected or variable-length relationship and new
labels or properties on a bound variable (openCypher `RequiresDirected
Relationship`, `CreatingVarLength`, `VariableAlreadyBound`).

**Known gaps (write forms that translate but fail on a server, or misbehave):**
- List concatenation with `+` (`n.list + [4, 5]`) is translated as numeric
  addition.

**Recently closed write-clause gaps (2026-08):** `MATCH … WITH … SET`/`DELETE`/
`REMOVE` tails (`_append_multipart_mutate_tail`); computed WITH projections
still fail closed.

**The governing AQL constraint (ERR 1579).** AQL cannot read a collection after
modifying it, nor write the same collection twice, within one query; nor can a
REMOVE and an INSERT/UPSERT share a collection. One statement therefore holds a
multi-write form only when each write targets a distinct physical collection.
`translate()` refuses the others with an actionable error that names
`translate_plan()` when a multi-statement plan can run the query (§8.3.1). No
query ever silently writes the wrong collection.

#### 8.3.1 Multi-statement plans (WP-6)

`translate_plan(cypher, mapping=…, params=…, <translate()'s options>)` returns a
`TranslationPlan`: an ordered tuple of `PlanStep`s (each a `TranspiledQuery`
plus `input_param`, the bind parameter it receives the previous step's rows
as, and `output_param`, the one the next step receives its rows as) and
`result_step`, the step whose rows are the query's result (`plans.py`).

- A query that fits one statement is a one-step plan whose query is exactly
  `translate()`'s; `translate()` itself is unchanged and still returns one
  `AqlQuery` or raises.
- Otherwise the query is cut between clauses. openCypher applies each clause to
  all rows before the next one, so running one step over all rows before the
  next step starts keeps that order. Clauses are grouped greedily: a step takes
  as many clauses as the translator compiles into one statement.
- Every step but the last returns one object per row, keyed by the variables in
  scope at the cut (`_plan_row` when none is, so the row count carries). The
  next step's Cypher is `WITH <vars> …`; it compiles like a `CALL { WITH … }`
  body over `FOR _row IN @_stepN_rows`, binding each node and relationship
  again by key from the collection its pattern names (`DOCUMENT(@@coll,
  _row.a._key)`), so it sees what earlier steps wrote (null if one deleted it)
  and can write it; values pass as they are (`plan_steps.py`, `plan_scope.py`).
- A step binds only the query parameters its AQL reads.
- Only a query with a write and two or more clauses is cut; a read-only query,
  a UNION, a query with CALL, and a semantically invalid query are never plans,
  and keep `translate()`'s error.

`execute_plan(db, plan)` runs a plan: one step as a plain query, several in one
stream transaction that declares every collection a step binds for writing.
Each step's rows feed the next step's `input_param`; a failing step (or commit)
aborts the transaction and raises `PlanExecutionError` (its `step`, the cause
chained). The executor (`plan_exec.run_statements`) is shaped for
arango-query-core, which owns query execution; it lives here behind an
`# upstream:` marker until the package ships it.

`arango_cypher.execute()`, the CLI's `run`, the TCK harness and the service's
`POST /execute` run plans; the service returns each step in `plan_steps`.

**Policy: plans are refused for tenant-bound sessions.** `POST /execute` answers
a multi-statement query from a tenant-bound session with 422
`MULTI_STATEMENT_TENANT_BOUND`, deliberately and fail-closed: tenant Layer 4
(AST injection) and Layer 5 (EXPLAIN-plan validation) certify one statement,
not a step that reads an earlier step's rows (`@_stepN_rows`). Workbench and
single-tenant sessions run plans. Follow-up: Layers 4/5 certify plan steps.

**Known gaps.** A node MERGE yields one existing match per row (an UPSERT),
where openCypher yields every match (TCK Merge5 [3], Match8 [2]); a
relationship MERGE whose endpoints no earlier clause binds, after another clause
or with ON CREATE / ON MATCH, stays refused. Both need a per-row step — one
statement per input row, each seeing the rows before it — which plans do not
have.

### 8.4 Not yet supported

Neo4j's built-in procedures (`db.labels()` …: only registered procedures run,
see "Registered procedures" in §9); durations held in variables, adding durations, `duration.between()`,
`time()` and time-of-day values (`a.time + duration(…)` is null), time zones,
and selecting from another temporal value (`date({date: d, day: 3})`);
`allShortestPaths()` and a
`shortestPath()` whose endpoints are not bound earlier in the query; and
multi-part write pipelines other than CREATE (mid-pipeline `DELETE`/`SET`
before a following `WITH`) as one statement; a write that reads what the same
query wrote runs as a multi-statement plan (§8.3.1).

**Label hierarchies (supertypes)** — a label whose members live under other
labels, such as LDBC's `Message` ⊇ `Post`, `Comment`. WP-35 adds them in stages
(decision D8). A conceptual entity lists its members in `subtypes`, and
arango-query-core ≥ 0.2.5 resolves the hierarchy
(`MappingResolver.is_supertype`, `supertype_members`).
- **LPG (stage 1).** The supertype maps as a `MULTI_LABEL` entity over the
  node's labels array (`labelsField`, `labelValue`), and `(m:Message)` filters
  `@labelValue IN m[@labelsField]`.
  - CREATE of a member writes its supertypes into the array, and a
    whole-document SET keeps it.
  - CREATE by a supertype, and MERGE on a supertype or on one of its members,
    are refused (`UNSUPPORTED`).
  - A node naming both a member and its supertype (`(m:Comment:Message)`)
    filters on the member's type field.
  - An entity style the translator does not know is refused
    (`INVALID_MAPPING`) wherever a node is matched.
- **PG (stage 2).** The supertype maps as a `UNION` entity over its members
  (`members`, or the conceptual `subtypes`), each keeping its own collection
  (`arango_cypher/_translate_v0/supertypes.py`).
  - A scan of `(m:Message)` iterates a `UNION` of one subquery per member. The
    node's inline properties filter inside each branch, so each member's
    index serves `{id: $id}`. `WHERE` filters after the union.
  - A traversal target, or the label predicate `x:Message`, tests that the
    document is in a member collection. The test is left out when the edge's
    domain or range already guarantees it (`HAS_CREATOR` reaches only posts
    and comments).
  - A member mapped `LABEL` (a type-discriminated shared collection) adds its
    type filter. A member of any other style is refused (`INVALID_MAPPING`).
  - SET and REMOVE on a variable matched by a `UNION` supertype are refused
    (`UNSUPPORTED`), because an UPDATE names a single collection. DELETE
    removes from every member collection. Other variables in the same match
    can still be written.
- **Extra labels.** A supertype label that the primary label implies adds no
  filter (`(p:Post:Message)`). One it does not imply is tested, under both
  styles. That includes a `COLLECTION` primary, so `(x:Person:Message)` matches
  nothing instead of every Person.
- **A mapping that maps the supertype as if it were a concrete label** makes
  translation succeed, but the AQL describes the mapping, not the data. Under
  PG it scans a collection named after the supertype; under LPG it compares the
  type field with the supertype's name, which no document holds. The result is
  empty rows.

**Temporal values** (`arango_cypher/_translate_v0/temporal.py`, WP-41). A
stored temporal property is an epoch-ms number or an ISO-8601 string; a value
the query computes is an ISO-8601 string in UTC. AQL's `DATE_*` functions read
all of them:
- `datetime(x)` / `localdatetime(x)` → `DATE_ISO8601(x)`, also from
  `{epochMillis}`, `{epochSeconds}` or calendar fields (year with month/day,
  ordinalDay, quarter/dayOfQuarter or ISO week/dayOfWeek, plus hour … nanosecond,
  kept to the millisecond); `date(x)` is the `YYYY-MM-DD` of `x`, so
  `date(m.creationDate) = date($d)` compares days (LDBC BI16).
- `.year`, `.month`, `.day`, `.hour` … `.epochMillis` lower to `DATE_YEAR(…)` and
  friends on a property the mapping declares `date`/`datetime`/…
  (`n.creationDate.year`), on a temporal function, and on a variable a `WITH`
  bound to one (LDBC IC10's `birthday.month`). On a parameter, which may be a
  map, the accessor is chosen at run time (`IS_OBJECT(@p) ? @p.year :
  DATE_YEAR(@p)`, LDBC BI13). Anywhere else `.year` stays attribute access
  (`m.year` of a movie). Components are UTC; a null value gives null; an
  accessor it cannot lower (`.timezone`) is refused (`UNSUPPORTED`).
- `duration({years, quarters, months, weeks, days, hours, minutes, seconds,
  milliseconds, microseconds, nanoseconds})` is an ISO-8601 duration
  (`P14M`, `PT129600S`). `t ± duration({…})` and `duration({…}) + t` add the
  months first, the day clamped to the target month as Neo4j does
  (31 January + 1 month is 29 February), then the rest as milliseconds, exact
  in UTC (LDBC BI2, BI17); a null value or field gives null. A duration held in
  a variable, `duration('P1D')`, adding durations and arithmetic on a temporal
  value without a duration (`datetime() - datetime()`) are refused.
- A comparison between a temporal value the query computes and anything else
  is made in epoch milliseconds (`DATE_TIMESTAMP` on both sides), since a
  stored ISO string with an offset or an epoch number does not compare with it
  as text. A property whose every declaration says `"encoding": "epoch_ms"`
  (`PropertyInfo.encoding`, arango-query-core ≥ 0.2.6) keeps its plain form,
  which an index on it serves. A comparison of a property with a parameter is
  unchanged: the parameter is passed in the property's encoding.
- An accessor on a property the mapping types as a string, number or boolean
  is refused: such a value has no `.year`, and an epoch-ms timestamp typed by
  its storage would read a missing attribute (null on every row).

Copied to AQL as attribute access, `n.creationDate.year` read a stored
timestamp's attribute and returned `null`, which is why accessors were refused
until this lowering.

**Temporal value library** (`arango_cypher/temporal/`, TCK full-court press).
Cypher's six temporal types (`Date`, `LocalTime`, `Time`, `LocalDateTime`,
`DateTime`, `Duration`) with Neo4j's java.time semantics, in pure Python at
nanosecond precision for years ±999,999,999, with no dependency on the
translator. It is the evaluator that constant temporal expressions are folded
with at compile time. It is ported from arango-gql-gateway's
`internal/temporal`, which passes every TCK Temporal1–10 scenario.
- API: `construct(kind, arg)` (string in every ISO form Neo4j accepts, map of
  fields and selections, or conversion), `truncate`, `from_epoch` /
  `from_epoch_millis`, `duration_between(a, b, unit)`, `add` / `subtract` /
  `multiply` / `divide`, `compare` / `equals`, `get_property` (every Neo4j
  accessor) and `to_string`. `str(value)` is java.time's form, which the TCK
  expects.
- Errors are `TemporalError` subclasses carrying the Neo4j error name
  (`InvalidArgumentValue`, `InvalidArgumentType`, `UnsupportedTemporalUnit`,
  `NumberOutOfRange`). Clock forms (`date()`, `datetime.statement()`,
  `localtime({timezone: z})`) raise `NotConstant`.
- Named zones resolve from an embedded IANA tz database built without
  backzone (`zoneinfo.zip`, `scripts/build_tzdata.sh`), so results match Java
  and do not depend on the host.
- `tests/test_temporal_tck_tables.py` evaluates every Temporal1–10 scenario
  through the library: 999 of 1,004 rows reproduce. The other 5 read the clock.
- The compile-time evaluator folds constant temporal expressions with it
  (`_translate_v0/const_temporal.py`): temporal functions, operators,
  accessors and `toString` on values known at translate time. The result is
  bound as its Neo4j string form, and an invalid construction becomes
  `FAIL()`. A temporal expression over data or parameters still lowers through
  `temporal.py`.
- **Temporal strings at run time** (`TranslateOptions.temporal_strings`,
  `translate(..., temporal_strings=True)`; `_translate_v0/temporal_strings.py`).
  A temporal value is stored and returned as its Neo4j string. With the option
  on, a string in one of those forms is read in AQL as the value it spells
  wherever its type is only known at run time:
  - accessors (`d.week`, `d.offsetMinutes`, `d.nanosecond`,
    `dur.minutesOfHour`), at nanosecond precision;
  - `+` / `-` of a temporal and a duration or of two durations, and `*` / `/`
    of a duration and a number, with java.time's rules;
  - `ORDER BY` and `<` / `<=` / `>` / `>=`: a Time by its instant on the day,
    a DateTime by its instant.

  The AQL is a small set of reusable snippets (parse, format, shift, scale,
  sort key) over `LET`-bound subqueries. Calendar fields use `DATE_*` on the
  date part, so years 0000 to 9999. A named zone keeps its stored offset in
  arithmetic, since AQL has no zone rules.

  The option is off by default: such a string is then text, and the AQL is
  unchanged, which keeps LDBC and the goldens byte-identical. The TCK harness
  turns it on. On ArangoDB, 989 of the 1,004 `expressions/temporal` scenarios
  pass. What remains is CREATE … WITH … MATCH reading the collection it just
  wrote (Temporal8 [1]-[5], AQL ERR 1579).
- The same clock read twice folds: `duration.between(date(), date())` (or
  `inMonths` / `inDays` / `inSeconds`) is `PT0S`, since Neo4j reads the
  statement (or transaction) clock once (Temporal10 [12]). Only exactly the
  same reading counts: the same kind, no arguments, the same source (`x()` is
  `x.statement()`); `realtime()` and a time zone argument are not folded.

**A pattern whose last node is bound is read from that node.** In a MATCH
after WITH or an OPTIONAL MATCH, `(m:Message)-[:HAS_TAG]->(tag)` with `tag`
already bound is compiled reversed (`… INBOUND tag …`), each relationship's
direction flipped, instead of scanning every message and keeping those that
reach `tag` (LDBC BI2 ran past 30 s at the smallest scale).

**Supertype scans keep their place.** A scan of a `UNION` supertype after
earlier clauses reads a variable bound before it (`FILTER x == x` in each
member branch), so ArangoDB cannot hoist it above the loop that binds it:
hoisted, the member documents ride along every row of that loop, and LDBC BI1
on PG passed the 1 GB query memory limit on ArangoDB 3.11 (3.12.12 does not
carry them; the dependency can go when CI's correctness tier moves to 3.12).

**ArangoDB behaviour the translation avoids.** After a `COLLECT` that has no
group keys and counts (`COLLECT WITH COUNT INTO`, `AGGREGATE x = COUNT(…)`), a
later uncorrelated subquery evaluates to null on 3.11.14 and 3.12.12
([arangodb/arangodb#23553](https://github.com/arangodb/arangodb/issues/23553)).
`count(*)` therefore compiles to `SUM(1)` (it was `COUNT(1)`, and
`WITH count(*) AS c RETURN c, [x IN range(1, 3) | x]` lost its list while a
`MATCH` after `WITH count(*)` lost its rows) and `count(x)` to
`SUM(x == null ? 0 : 1)`; an expected-failure live test reports when a server
release fixes the issue.

The LDBC baseline probe found both problems
([`ldbc_benchmark_plan.md`](./ldbc_benchmark_plan.md) §3.2).

Typeless relationships and leading `WITH`/`UNWIND` computational pipelines
have constrained support rather than being blanket exclusions. The precise
mapping, semantics, and performance limits are maintained in
[`cypher_capability_matrix.md`](./cypher_capability_matrix.md).

### 8.5 AI fallback for recoverable transpile failures

When a hand-written or NL-generated Cypher query fails to transpile on an
*unsupported but recognizable* construct, an optional LLM-backed fallback can
propose an equivalent. This never overrides the deterministic path; it is a
recovery affordance, surfaced with provenance.

---

## 9. `arango.*` extensions

A registry-gated namespace (`ExtensionRegistry` + `ExtensionPolicy`) exposes
ArangoDB-native capabilities to Cypher, arity-checked and allow/deny-list aware:

- **Search/text** — `bm25`, `tfidf`, `analyzer`, `like`, `levenshtein_distance`,
  `levenshtein_match`, `ngram_match`, `ngram_similarity`, `phrase`, `boost`,
  `min_match`, `tokens`, `soundex`, `regex_test`/`matches`/`replace`.
- **Vector** — `cosine_similarity`, `l2_distance`, `approx_near_cosine`,
  `approx_near_l2`.
- **Geo** — `distance`, `geo_distance`, `geo_contains`, `geo_intersects`,
  `geo_in_range`, `geo_point`.
- **Document** — `attributes`, `has`, `merge`, `unset`, `keep`, `zip`, `value`,
  `values`, `flatten`, `parse_identifier`, `document`.
- **Procedures** (`CALL arango.*`) — `fulltext`, `near`, `within`,
  `shortest_path`, `k_shortest_paths`.
- **Registered procedures** (`arango_cypher/procedures.py`). Any other
  procedure is registered on the `ExtensionRegistry` passed to `translate`, and
  follows the invocation protocol. Its compiler receives a
  `ProcedureInvocation`: the compiled arguments (or none for an implicit
  `CALL p`), their static types, a resolver for implicit parameters, and
  whether the call is standalone. It returns a `CompiledProcedure`: an AQL
  expression of row objects and the column names.

  Knowing the columns, the translator applies Cypher's rules:
  - `YIELD a, b AS c [WHERE …]`;
  - `YIELD *` and no `YIELD` in a standalone call (all columns);
  - a void procedure in a query leaves the rows unchanged;
  - an implicit call takes each input from the same-named parameter
    (`MissingParameter` if it is absent);
  - a `CALL` stage between `WITH`s.

  These are rejected as Neo4j does: an unknown procedure
  (`ProcedureNotFound`); the wrong number of arguments; an argument whose type
  the query shows does not match; a missing output; `YIELD *` inside a query;
  a yielded name already bound or yielded twice; an aggregate argument.

  `TableProcedure` / `register_table_procedure(registry, "ns.p(in ::
  INTEGER?) :: (out :: STRING?)", rows)` is a ready-made procedure whose result
  is a fixed table keyed by its inputs, passed as a bind parameter. It suits
  test doubles and fixed lookups. The TCK harness registers each
  `there exists a procedure` fixture this way (`tests/tck/procedures.py`):
  all 52 `clauses/call` scenarios pass.

The entity resolver emits a structured `IndexAdvisory` whenever a fuzzy name probe
runs against a field with no inverted/ArangoSearch coverage; the advisory carries a
ready `add_index` spec and is actionable from the UI via `POST /schema/index/create`.

---

## 10. NL → Cypher pipeline (Text2Cypher, hardened)

The pipeline implements the SOTA Text2Cypher reference architecture
(extract → resolve → retrieve → generate → execute/validate), packaged as WP-25:

- **Multi-provider LLM support** — `OpenAIProvider`, `AnthropicProvider`,
  `OpenRouterProvider`; auto-detected from API-key presence or pinned via
  `LLM_PROVIDER`. Without a key, a rule-based fallback runs (demo/offline only).
- **Dynamic few-shot retrieval (WP-25.1)** — `FewShotIndex` with BM25 over shipped
  `movies`/`northwind`/`social` corpora plus user-approved corrections.
- **Synthetic few-shot bank (query-first, build-time)** — `arango-cypher-py synthbank`
  generates a per-database bank from the conceptual schema using the nine shared
  `arango_query_core.nl.synthbank` shapes, with slots filled from values sampled
  out of live data *through the transpiler* (the mapping stays authoritative).
  Every kept gold Cypher MUST translate and execute non-empty; a ranking shape
  MUST have a strictly unique extremum; an entity anchor MUST be near-unique
  (≥ 0.95 distinct in the sample); a property is orderable only when its values
  are real numbers or ISO dates. Paraphrase is opt-in — an LLM provider is passed
  explicitly, never inferred from a configured key — and every paraphrase MUST
  pass the offline slot-preserving guard. Generated banks load through
  `FewShotIndex` only when listed in `NL2CYPHER_FEWSHOT_BANKS`: off by default,
  because the prompt change must be justified by an eval run first.
- **Mined examples (saved-query mining, build-time)** — `arango-cypher-py
  mine-examples` turns a database's own saved AQL — the graph visualizer's saved
  queries (`_queries`) and canvas actions (`_canvasActions`), and the query
  editor's saves (`_editor_saved_queries`), with their names and descriptions —
  into verified examples:
  - a saved query whose EXPLAIN plan writes MUST NOT be executed; every source and
    candidate run is read-only, bounded server-side (30 s) and read to at most
    2 000 rows — more is *too large to compare*, never compared on a prefix;
  - canvas actions run on selected nodes, so start vertices are sampled from
    vertices that have edges in the graph, from collections the action's own
    text names first;
  - the LLM drafts a question and conceptual Cypher; a draft is kept only if its
    transpiled AQL returns **the same documents** as the saved query (same vertex
    and edge ids, edge endpoints counting as vertices; or the same scalar rows) —
    not merely non-empty. A failed draft is fed back with its reason, up to three
    times; the provider is passed explicitly, never inferred from a configured key;
  - verified examples are stored in the mined database (`arango_cypher_examples`,
    configurable via `ARANGO_CYPHER_EXAMPLES_COLLECTION`) with provenance — source
    query and fingerprint, verdict, the mapping hash and model — one per saved
    query: re-mining replaces it, a query that no longer verifies loses it;
  - the service serves them without an LLM (`GET /examples`), and the Workbench
    shows them first among the Ask suggestions and in Sample Queries. They are
    **not** few-shot examples until an eval run justifies the prompt change, as
    for generated banks.
- **Pre-flight entity resolution (WP-25.2)** — `EntityResolver` rewrites user
  string literals against the live DB before generation, combining exact /
  contains / reverse-contains / `LEVENSHTEIN_DISTANCE` scoring with a configurable
  `fuzzy_threshold`. When none of those matches, a word-level pass compares the
  mention with each run of as many words in the value (`word_fuzzy_threshold`,
  default 0.8), so a typo in one word of a longer value still resolves ("Matrx"
  → "The Matrix"; ties go to the shortest value). When labels tie on a value
  (`Customer.country` and `Supplier.country` both hold "Germany"), the label the
  question names wins ("Show suppliers from Germny" → `Supplier`), because the
  resolution tells the model which label to use; a strictly better match on
  another label still wins. Role-aware (identifier/name first), exact-first two-pass
  collection scans for performance on large collections, and `alt_labels` guidance
  for ambiguous types.
- **Execution-grounded validation (WP-25.3)** — translated AQL is run through
  `_api/explain` in a self-healing retry loop; semantic errors (missing
  collections, unbound vars) feed back into the next prompt.
- **Prompt caching (WP-25.4)** — cache-friendly section ordering; OpenAI automatic
  prefix caching; Anthropic explicit `cache_control` splits; `cached_tokens`
  propagated uniformly and surfaced on results and HTTP responses.
- **Graph-intent + value-shape awareness** — "show me … as a graph" yields a
  path-returning Cypher; value-shape hints steer the LLM toward fuzzy/`CONTAINS`
  matching of token-shaped fields instead of inventing legal names.
- **Evaluation harness + regression gate (WP-25.5)** — 31-case corpus
  (`movies_pg` + `northwind_pg`, 5 categories), reproducible runner, tolerance
  policy (5 pp / +20% / +0.3 retry), nightly CI matrix over OpenAI + Anthropic.

> **Shared NL engine (2026-07; the only loop since 2026-10).** The
> language-agnostic primitives — LLM providers, BM25 few-shot retrieval, and the
> generate→validate→repair loop — were extracted into the published
> `arango-query-core` distribution behind a `QueryLanguageAdapter` seam.
> `arango_cypher.nl2cypher` supplies the Cypher adapter (`CypherAdapter`). Until
> issue #24, `nl_to_cypher` still ran its own copy of the loop.
>
> `nl_to_cypher` now generates only through `NLQueryEngine`
> (`arango_cypher/nl2cypher/engine_path.py`, arango-query-core ≥ 0.2.4); its own
> loop and the `ARANGO_CYPHER_NL_ENGINE` switch are gone. The behaviour and
> result contract are those of the old loop:
> - the tenant guardrail runs as the first postcondition, so a violation is
>   retried with its hint;
> - resolved entities render through the grounding seam, after the examples;
> - the tenant and graph-intent sections extend the grammar section;
> - the caller's hint rides on the first user message, in the old wording;
> - the query is found in a fenced block anywhere in the response, and
>   `CypherAdapter.normalize` strips prose before an unfenced query and
>   repairs labels the mapping lacks, before validation;
> - a provider call that raises spends one attempt of the retry budget; when
>   every call raises, the rule-based translator answers;
> - a few-shot index that raises costs the examples, not the LLM: the question
>   is asked zero-shot;
> - method values, explanation wording and token accounting are unchanged.
>
> Before the old loop was removed, both ran the same NL test modules. On the
> eval (gpt-4o-mini, 31 cases, 3 runs each) the engine matched the old loop case
> for case without a database (pattern match 83.9% both), and was not worse
> with the fixtures seeded (94.6% vs 93.5%).

**Quality baseline (live, 31 cases, both fixtures seeded):**

| Metric | OpenAI `gpt-4o-mini` | Anthropic `claude-haiku-4-5` |
| --- | --- | --- |
| parse_ok | 100.0% | 100.0% |
| pattern_match | 83.9% | **100.0%** |
| typo category | 16.7% | **100%** |
| retries_mean | 0.000 | 0.000 |

> The OpenAI figures reflect a 2026-08-01 baseline refresh
> (`tests/nl2cypher/eval/baseline.json`); the April figures no longer reproduced
> (provider drift, not a code change). The Anthropic column is unchanged.

### 10.1 Local learning (feedback loops)

Two independent correction stores. **Cypher→AQL** (`corrections.py`, SQLite, keyed
by `(cypher, mapping_hash)`) is a deterministic override and a transpiler-bug
discovery queue. **NL→Cypher** (`nl_corrections.py`) captures approved
`(question, cypher)` pairs and re-enters them into the BM25 few-shot retriever on
the very next request (appended after shipped corpora, so a user correction wins
BM25 ties). All data stays local.

### 10.2 Agentic tool contract

Eight JSON-in/JSON-out tools: `cypher_translate`, `suggest_indexes`,
`explain_mapping`, `cypher_profile`, `propose_mapping_overrides`,
`explain_translation`, `validate_cypher`, `schema_summary` — dispatched via
`/tools/schemas` + `/tools/call`. The `get_cypher_profile()` manifest
(`profile_schema_version`-versioned) lets NL/agent gateways discover the supported
subset programmatically.

### 10.3 Caller-supplied postconditions

Callers MUST be able to attach domain invariants to the generate-validate-repair
loop, alongside the built-in parse, `EXPLAIN` and tenant-scope checks. A
postcondition is a caller-supplied object exposing `code`,
`check(query, *, context)` and `prompt_section()`.

Semantics the loop MUST honour:

* Postconditions run only after parse and `EXPLAIN` succeed, so a retry is spent
  only on syntactically and physically valid Cypher.
* The tenant guardrail runs **first**. A statement violating both reports the
  tenant violation, because a cross-tenant leak is a security failure and a
  domain-correctness problem is not.
* Violations share the caller's `max_retries` budget; they get no separate
  allowance.
* The **first** violation wins; checks are not accumulated.
* On budget exhaustion the loop fails closed — a statement that never satisfies a
  postcondition is never returned.
* `prompt_section()` is rendered **once** into the cacheable system prefix and
  MUST NOT vary between attempts of the same call, preserving provider-side
  prompt caching.
* A check that raises is treated as a violation (`{code}_error`), never allowed
  to abort translation.

The mechanism is owned by the shared NL engine
(`arango_query_core.nl.postconditions`, `>=0.2.0`) so `nl2cypher` and `nl2sparql`
cannot re-diverge; `arango_cypher.nl2cypher.postconditions` re-exports it and
hosts Cypher-specific checks.

---

## 11. Multi-tenant safety (defense-in-depth)

When deployed against a multi-tenant graph, two failure modes leak cross-tenant
data: **underconstraint** (LLM omits the tenant filter) and **injection** (user
names another tenant). These are data-leak-class defects. The architecture makes
leakage **structurally impossible, independent of LLM behavior**, via six layers:

| # | Layer | Mechanism | Status |
| --- | --- | --- | --- |
| 0 | Storage | Disjoint SmartGraphs (per-tenant shard key) + satellite collections | Partial (analyzer metadata adopted) |
| 1 | Session | Server-bound `@tenantId` from the authenticated session; body tenant ignored in tenant-user mode | **Done** |
| 2 | LLM | Manifest-aware prompt + few-shot + regex postcheck + retry | **Done** |
| 3 | Cypher AST | Algorithmic tenant-predicate injection on parsed Cypher | Phase 3a done (core); route wiring pending |
| 4 | AQL AST | Tenant-predicate injection on transpiled AQL; covers NL→AQL + `/execute-aql` | **Done** |
| 5 | Pre-execute | EXPLAIN-plan validator refusing any plan that scans a tenant-scoped collection without a bind-var tenant predicate | **Done** |
| 6 | Execute | `safe_execute` overrides client bind vars with the session tenant (session value wins) | **Done** |

**Key principles.** Layer 5 is the *security boundary* — if it passes, the query
is safe by definition; if it fails, the query does not run. **Fail-closed
everywhere** (unknown labels, missing manifest entries, unparseable plans →
refuse). **The LLM is never trusted** (Layer 2 reduces retry burden but is not
counted as a defense for audit). **Bind-variables only** — the tenant identifier
is never inlined as a literal; Layer 5 refuses literal tenant predicates and
bind-value mismatches against the session.

Detailed threat model (T1–T8), the formal definition of "safe", per-layer
specs, the red-team corpus, and remaining work (MT-6 plan-shape LRU, MT-7 admin
bypass + audit log, MT-8 security review) live in
[`multitenant_prd.md`](./multitenant_prd.md).

---

## 12. Testing & quality

- **Golden tests** — YAML fixtures in `tests/fixtures/cases/` and `cases_v03/`,
  exact AQL + bind-var matches.
- **Integration tests** — corpus execution (Movies ~170 nodes, 20-query corpus,
  PG + LPG; Northwind 14-query corpus; social PG/LPG/hybrid; ICIJ Paradise
  Papers) **and live schema acquisition**. Gated by `RUN_INTEGRATION=1` (Arango
  on host port 28529 / 28530).
- **Live-database requirement coverage** — every requirement in §7 (schema
  detection, mapping, cardinality statistics, change detection) MUST have
  integration coverage exercising it against a real ArangoDB, not only unit tests
  over recorded fixtures. §7 describes behaviour that is only observable against a
  live instance; fixture-based tests cannot detect a change in what the analyzer
  returns, nor a defect in a code path they stub out. See
  [`tests/integration/test_schema_acquisition.py`](../tests/integration/test_schema_acquisition.py),
  whose first run exposed a fingerprint defect that had been latent behind
  stubbed unit tests.
- **Neo4j cross-validation** — every corpus query runs against Neo4j (the
  reference Cypher engine) *and* the translated AQL; result sets are diffed
  row-by-row (`assert_result_equivalent`). Two suites pass end-to-end: Movies
  20/20, Northwind 14/14. Gated by `RUN_INTEGRATION=1 RUN_CROSS=1`
  (`docker-compose.neo4j.yml`, Bolt 27687).
- **openCypher TCK harness** — 220 feature files / 3,861 scenarios, measured in
  four separate outcomes: parses, translates, executes, and satisfies the TCK's
  declared result/error assertion. Core excludes only temporal and generic
  `CALL` categories. **Current figures are deliberately not reproduced here** —
  they are regenerated into
  [`tests/tck/COVERAGE_REPORT.md`](../tests/tck/COVERAGE_REPORT.md), which is the
  authoritative measurement. Figures quoted in prose have drifted twice; cite the
  report instead. The assertion outcome is not a direct Neo4j-equivalence claim;
  use cross-validation for that evidence.
- **Conformance ratchet** — every scenario's outcome is recorded per scenario in
  [`tests/tck/conformance_ledger.tsv`](../tests/tck/conformance_ledger.tsv).
  `tests/tck/test_conformance_ratchet.py` runs in the default suite (no database,
  ~9s) and MUST fail if any scenario leaves a passing outcome, and if
  improvements go unrecorded. Aggregate coverage cannot detect a change that
  fixes ten scenarios and breaks nine; the per-scenario ledger diff makes every
  state change reviewable. Regenerate with
  `./.venv/bin/python tests/tck/ledger.py --write`. Passing outcomes are
  `translatable` and `correct_rejection` — never "translates" alone, since 690
  scenarios are negative tests where translating is the defect.
- **Execution ratchet** — translatability is an upper bound, not conformance.
  [`tests/tck/execution_ledger.tsv`](../tests/tck/execution_ledger.tsv) records,
  per scenario, whether it returned the TCK's expected rows (or its expected
  error) on a live ArangoDB; this is the headline conformance figure and the one
  comparable with other Cypher-on-Arango engines.
  `tests/integration/test_tck_execution_ratchet.py` runs the corpus (~1 min) in
  the integration job on every PR and MUST fail if a scenario stops passing or an
  improvement goes unrecorded. The harness runs a scenario's steps in order — a
  control query's table is checked against the control query — and compares
  cells with Cypher equality (a boolean never equals a number). The harness
  translates with `cypher_column_names=True`, so result columns carry the
  names the TCK's tables use (`n.name`, `count(*)`). Regenerate with
  `RUN_INTEGRATION=1 ./.venv/bin/python tests/tck/execution_ledger.py --write`.
- **NL eval gate** — opt-in (`RUN_NL2CYPHER_EVAL=1`), nightly CI matrix over
  OpenAI + Anthropic against committed baselines.
- **CI** — `ci.yml` (ruff + unit incl. the conformance ratchet + integration on
  Py 3.11/3.12 against Arango 3.11) on every push/PR; `nl2cypher-eval.yml` nightly (non-blocking); and
  `tck-coverage.yml` weekly/manual, publishing translation and live-execution
  metrics while running the Neo4j cross-validation suites.

Engineering rules enforced repo-wide (see `.cursor/rules/`): read-before-write,
test-what-you-touch, verify-before-done, incremental-over-atomic,
comprehensiveness-over-simplification, wiring-over-deletion, modularity (source
files ≤ 1500 lines), and checkpoint-regularly.

### 12.1 LDBC SNB benchmark (validation + performance)

The LDBC Social Network Benchmark's reference Cypher queries, run on its
generated data, form a third evidence tier between the TCK (language coverage on
tiny graphs) and the Movies and Northwind cross-validation (small hand-built
corpora). The plan, research and baseline are in
[`ldbc_benchmark_plan.md`](./ldbc_benchmark_plan.md).

- **LDBC-1 Corpus.** All SNB Interactive v2 operations (IC1–14, IS1–7,
  INS1–8, DEL1–8) and BI queries (BI1–20) are vendored **verbatim** from the
  LDBC reference repositories, pinned to an upstream commit, with Apache-2.0
  attribution.
  - Verbatim files are never edited.
  - Any adaptation (for example, replacing a GDS procedure, or correcting an
    upstream bug) is a separately labelled variant.
  - Verbatim and variant outcomes are reported separately.
- **LDBC-2 Layouts.** Every operation is measured on a **PG** and an **LPG**
  physical layout.
  - Both are loaded from the same LDBC data generator output by one
    deterministic, idempotent loader.
  - Each load verifies that per-entity counts are identical across the CSV, PG
    and LPG.
  - Both layouts have committed mapping fixtures (`ldbc_pg`, `ldbc_lpg`).
  - The bundle acquired live from each loaded layout is diffed against its
    fixture. This is §7 live-database coverage
    (`tests/integration/test_ldbc_mapping_acquisition.py`,
    `tests/ldbc/mapping_diff.py`). Every difference must be a known analyzer
    gap filed upstream (arangodb-schema-analyzer #21 property types, #43
    supertypes, #44 domain/range of multi-label relationships, #46 sparse
    properties); anything else fails. Labels, collections, styles, type values
    and relationship types already agree. CI's integration job installs the
    analyzer extra, so the diff runs on every push.
- **LDBC-3 Outcome ledger.** Each operation × layout records `parses`,
  `translates`, `executes` and `matches_oracle`, or a terminal state with a
  reason:
  - `fails_closed`
  - `translates_wrong`
  - `out_of_subset` (GDS or APOC)

  A per-cell ratchet in the default suite (translation tier, no database) fails
  CI when any cell leaves a passing state, or an improvement goes unrecorded.
- **LDBC-4 Oracle.** Expected results come from a pinned Neo4j 5.x running the
  verbatim reference queries on the same data and parameters.
  - Results are recorded as committed snapshots, so the integration tier needs
    no Neo4j.
  - `RUN_CROSS` re-verifies the snapshots against live Neo4j.
  - Write operations are compared by post-state.
  - Ordered comparison applies only when the query's ordering is total.
- **LDBC-5 Fail closed.** `translates_wrong` is a defect, and it takes priority
  over unsupported features. A construct the transpiler cannot lower correctly
  MUST raise, not emit plausible AQL.
- **LDBC-6 Performance.**
  - **What is timed:** translation time is reported separately from execution
    time. For each operation × layout × scale factor, the harness reports
    p50/p95/p99 over several parameter bindings after warm-up.
  - **What is recorded with each run:** server-side execution time, an
    `EXPLAIN` plan digest, and the environment (versions, hardware, scale
    factor, data checksum, git SHA).
  - **Where results go:** they are emitted as JSON. Published prose is
    generated from them and conformance-checked, never hand-copied.
  - **Implementation:** `tests/ldbc/bench.py` (the runner; a cell whose row
    counts differ from the oracle's is `rows_differ`, not timed),
    `tests/ldbc/report.py` (rendering, conformance check, run comparison).
    Timed runs happen on the benchmark VM only (plan §10, decision 11): the
    repository is public, so no numbers go into CI artifacts. CI runs the
    runner as a smoke test (`tests/integration/test_ldbc_bench_live.py`).
- **LDBC-7 Fair use.** Every published number carries "These are not LDBC
  Benchmark Results" and attribution. The harness is never described as an
  implementation of an LDBC Benchmark. Until product signs off, benchmark
  numbers stay internal (on the benchmark VM; never in this public
  repository's CI artifacts) and only the method is published.
- **LDBC-8 Scale.**
  - CI uses SF0.003, downloaded on demand and cached. Each data set is
    verified against a pinned **content digest**: a SHA-256 over each entity's
    sorted, decompressed rows. An archive hash is not used, because LDBC
    rebuilds the sample archives with fresh Spark part-file names while the
    rows stay the same. A digest mismatch stops the run
    (`tests/ldbc/datasets.py`).
  - Performance runs use SF0.1 and SF1 or larger, on a dedicated cloud VM
    running ArangoDB 3.12. CI's correctness tiers stay on 3.11.
  - LDBC publishes no SF0.1 data set for the BI layout this harness loads (BI
    starts at SF1), so SF0.1 is generated with LDBC's data generator pinned to
    the commit of the samples, and verified by content digest like the
    downloads (`tests/ldbc/datagen.py`). Datagen at that commit reproduces
    the published SF0.003 rows exactly in both encodings
    (`tests/integration/test_ldbc_datagen_live.py`, opt-in with
    `RUN_LDBC_DATAGEN=1`), which is what makes the digest pin sound.
  - Each workload is loaded in its reference timestamp encoding: epoch-ms for
    Interactive, ISO-8601 for BI.
- **LDBC-9 Database lifecycle.** A run owns the databases it starts and
  removes them when it finishes, on success or failure, including a failed
  start. The suites and tools use `tests/ldbc/stack.py` and
  `docker-compose.ldbc.yml`, which has its own ports and capped memory.
  - A server named by `ARANGO_URL` or `NEO4J_URI` (CI's services, a developer's
    instance) is used as-is and never started or stopped.
  - `LDBC_KEEP_STACK=1` keeps the stack for inspection.

---

## 13. Packaging & deployment

The Arango Platform deployment (via ServiceMaker → Container Manager) ships the
FastAPI service together with the Cypher Workbench, matching every other BYOC
service in the estate: advertising a UI (`has_ui`) is what makes the platform
present a service as an app rather than a bare endpoint. A headless variant —
library, CLI and HTTP endpoints only — remains available via
`PACKAGE_INCLUDE_UI=0` when packaging and `--no-ui` when deploying. The Workbench
keeps its debug/demo scope (§6.4); shipping it by default is a packaging
decision, not a promotion to a supported production surface.

The service MUST be packaged with its mount prefix baked in
(`SERVICE_ROOT_PATH`): the platform's deploy metadata does not forward arbitrary
environment to the container, and without `ROOT_PATH` the `/docs` page renders
but points Swagger at the cluster-root spec — documenting ArangoDB's Core API
instead of this service.

The service MUST answer at its bare mount root: the platform's Apps view opens a
BYOC service at its mount prefix, not at `/frontend`, so a service that serves
only its named UI mounts appears as "App Not Responding" in the launcher while
every other endpoint reports healthy. Only the exact root is bound — never a
catch-all — so unknown paths still return a genuine 404 rather than the SPA shell
with status 200. Deployment verification MUST probe the mount root, not only the
endpoints the service defines.

On the platform the Workbench MUST NOT ask a signed-in user for cluster
credentials. The platform gateway forwards the caller's platform JWT as
`Authorization: Bearer`, and the operator injects the coordinator as
`ARANGO_DEPLOYMENT_ENDPOINT`; `GET /connect/platform` reports whether a request
carries both, and `POST /connect/platform` opens a session authenticated with
that JWT — listing the databases that user may open and, when the caller names
none, opening the instance's mount database if the user can open it (else
`_system`, else their first database: a mount database need not exist) — so the
user only picks a database and a graph (or all collections). Platform sessions
MUST:

- store and bake no credential; the coordinator validates the JWT, so the session
  sees exactly what the user's platform permissions allow;
- take the endpoint from server configuration only, never from the request;
- follow the JWT each request carries (the platform rotates it), and refuse — not
  fall back to the stored token — a request that carries none or one that does
  not decode;
- answer an unusable token with 401 and an unknown database with 404, never 500;
- reach the operator endpoint over its TLS: it presents a certificate from the
  cluster's own CA, which the container does not trust by default. The operator
  injects that CA as `ARANGO_DEPLOYMENT_CA` (a file path, or the PEM text), and
  the endpoint is verified against it; without it the endpoint is not verified
  (as the platform's first-party services connect to it). An explicit
  `ARANGO_URL` is verified against the system trust store;
  `ARANGO_CYPHER_PLATFORM_CA_BUNDLE` names another CA, and
  `ARANGO_CYPHER_PLATFORM_VERIFY_TLS=on|off` overrides;
- run work that outlives the request (the background schema warm) as the same
  user, never as a more privileged one: the forwarded JWT expires, so when the
  injected integration sidecar (`INTEGRATION_HTTP_ADDRESS[_FULL]`) names the
  caller (`/_integration/authn/v1/identity`), the work gets a token minted for
  that user (`/_integration/authn/v1/createToken`,
  `ARANGO_CYPHER_SIDECAR_TOKEN_LIFETIME_S`, default 3600); a token is never
  minted without a named user (the sidecar would default to root), and without
  one the work keeps the request's token. `POST /connect/platform` returns the
  user it identified;
- name the cause when the endpoint cannot be reached (TLS, refused, timeout) and
  the endpoint's `scheme://host:port` — never the token.

`GET /connect/platform/diagnostics` reports, for checking a deployment, what the
platform provides and whether each piece works: the injected endpoint and CA, the
TLS policy in use and a direct request under it (and under the injected CA), the
forwarded login, and the sidecar (whether it names the caller, and a 60-second
token minted for them, tried against the endpoint). It reports facts about tokens
(claim names, lifetime, whether python-arango's own pre-check accepts them),
never a token or a claim value.

`ARANGO_CYPHER_PLATFORM_AUTH=off` disables the path; off the platform the
credentials dialog is unchanged. Every UI request MUST resolve against the
service mount (the directory the SPA was served from), never the origin root,
which on the platform is ArangoDB itself.

The persistent schema cache (a user-land collection in the connected DB) lets
containerized replicas share a warm cache and survive restarts. See
[`byoc-deployment.md`](./byoc-deployment.md) for the Container Manager runbook and
[`arango_packaging_service/`](./arango_packaging_service/) for the platform API.

**Naming / repos.** Repo `arango-cypher-py`, import package `arango_cypher`,
distribution `arango-cypher-py`, CLI `arango-cypher-py`. The in-database sibling is
[`arango-cypher-foxx`](https://github.com/ArthurKeen/arango-cypher-foxx). The bare
`arango-cypher` name is reserved for a potential umbrella/spec repo.

**Tech stack.** `python-arango`; `arangodb-schema-analyzer`; ANTLR4 openCypher
grammar; `typer`+`rich` (CLI); `fastapi`+`uvicorn` (service); `rdflib` (optional
OWL ingestion); `pytest`/`httpx`/`syrupy` (tests); `ruff`/`mypy` (quality);
React + TypeScript + Vite + CodeMirror 6 + Cytoscape.js + Tailwind (UI).

---

## 14. Current status

| Capability | Status |
| --- | --- |
| ANTLR4 parser + translation core (MATCH/WHERE/RETURN/WITH/OPTIONAL/UNWIND/CASE/UNION) | **Done** |
| Read subset incl. comprehensions, named paths, EXISTS/COUNT subqueries, label predicates | **Done** |
| Write clauses (CREATE/SET/DELETE/REMOVE/MERGE/FOREACH) incl. 2026-06 gap closures | **Done** (within ERR-1579 limits) |
| `arango.*` extension registry (search/vector/geo/document/procedures) | **Done** |
| MappingResolver + schema-analyzer integration (PG/LPG/hybrid) + heuristic fallback | **Done** |
| Property-role classification + open-vocab edge normalization | **Done** |
| Schema change detection + two-tier persistent cache | **Done** |
| Cardinality statistics | **Done** |
| NL → Cypher pipeline (WP-25.1–.5) + feedback loops + agentic tools | **Done** |
| NL → AQL direct path | **Done** |
| FastAPI service (connection/translate/NL/schema/mapping/corrections/tools) | **Done** |
| Cypher Workbench UI (chat-first redesign, schema graph, results, history, learning) | **Done** (debug/demo scope) |
| Neo4j cross-validation (Movies 20/20, Northwind 14/14) | **Done** (34/34 re-verified 2026-08-06) |
| LDBC SNB benchmark (§12.1) | **Correctness done, performance in progress.** Phase 0 (WP-31 to WP-34) and Phase 1 (WP-35 to WP-42) are complete, and milestones M2 and M3 were achieved on 2026-10-29: every operation in subset matches the Neo4j oracle on PG and LPG, with no `translates_wrong` cell. The live mapping diff (LDBC-2) and the benchmark runner with its nightly workflow (LDBC-6, WP-43) are done. Open: SF0.1 and SF1 data and runs (LDBC-8; LDBC publishes no SF0.1 in the loader's format), the optimisation loop (WP-44), then the hybrid layout. Current figures are in `tests/ldbc/execution_ledger.tsv` and `translation_ledger.tsv`; they are intentionally not inlined here |
| Multi-tenant Layers 1, 2, 4, 5, 6 | **Done** |
| Multi-tenant Layer 3 (Cypher AST injection) route wiring | **Partial** (core done) |
| Multi-tenant Layer 0 (storage) | **Partial** |
| openCypher TCK | Dry-run and live-execution passability tracked per scenario in [`tests/tck/conformance_ledger.tsv`](../tests/tck/conformance_ledger.tsv); current rates in [`tests/tck/COVERAGE_REPORT.md`](../tests/tck/COVERAGE_REPORT.md) (figures intentionally not inlined — they drift) |

---

## 15. Roadmap

| Version | Focus |
| --- | --- |
| **v0.1** ✅ | Core read-only transpiler; mapping; extensions; service; UI; golden + integration tests |
| **v0.2** | Write-clause + aggregation completeness; live-DB mapping; CLI; TCK Match ≥ 40% |
| **v0.3** | Language breadth (full OPTIONAL MATCH, EXISTS, named paths); OWL round-trip; index-aware mapping; NL pipeline; UI completeness; TCK ≥ 25% |
| **v0.4+** | Translation-only TCK ≥ 60% Full (**target met** — see `COVERAGE_REPORT.md` for the current rate); improve execution-grounded compatibility, temporal/procedure support, and optimization (filter pushdown, translation caching, relationship uniqueness) |
| **Multi-tenant** | MT-3b route wiring, MT-6 plan-shape LRU, MT-7 admin bypass + audit, MT-8 red-team |
| **Compiler architecture** | Normalized AST / logical plan; retain and broaden the implemented leading-`WITH`/`UNWIND` computational pipeline support |

---

## 16. Open questions

**Transpiler / coverage.** A write that reads what the same query wrote needs a
multi-statement/transaction execution model (the single-`AqlQuery` output can't
represent it) — adopt one, or keep failing closed? *(2026-10-28: CREATE
pipelines and DELETEs write each collection once after reading everything, so
the LDBC writes need no such model; the question stays open for writes that
read their own writes.)* Should the leading-`MATCH`
constraint be relaxed (large TCK lever, non-trivial grammar/renderer work)?
Index-hint emission from `PropertyInfo.indexed` — when? *(Decided 2026-10-26:
label hierarchies are modeled in arango-query-core. Stage 1 is an LPG
labels-array field; stage 2 is a PG member-collection union. See
`ldbc_benchmark_plan.md` D8 and WP-35.)*

**NL pipeline.** Default fuzzy matching to portable `CONTAINS`/`=~` vs. invest
further in the ArangoSearch `arango.*` path as primary? Task decomposition /
multi-agent for complex multi-subquery questions — revisit after harness data.
SLM fine-tuning — separate research project; the `LLMProvider` hook is reserved.

**Multi-tenant.** Tenant identifier format (`_key` canonical) and hierarchical
tenants (`== @tenantId` vs `IN @allowedTenants`)? Write-operation policy against
satellite collections? Should Layer 4 rewrite anonymous traversals into named-graph
form? Does the Platform inject a tenant identifier via ingress header (so Layer 1
reads it instead of requiring it at `/connect`)?

**Product / UX.** Should the "create index" advisory become a dedicated Indexing
panel vs. the current inline strip? `App.tsx` exceeds the modularity cap —
extract a `useQueryActions` hook (tracked tech debt).

---

*End of consolidated PRD. For historical detail and per-feature changelogs, follow
the source-document links in the header.*
