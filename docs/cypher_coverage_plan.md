# Cypher Coverage Completion Plan

**Status:** Transpiler coverage (M1+M2) **complete: 22/22**. Semantics (M3): WP-S1
done; WP-S2 done (incl. S2c); WP-S3 done (incl. S3c).
**Owner:** transpiler / NL→Cypher
**Date:** 2026-06-23

This plan defines the work to close openCypher coverage in the v0 transpiler and
to fix the NL→Cypher semantic gaps surfaced by the "as a graph" / approximate-match
issue. It is grounded in a measured baseline against a real-world financial
knowledge-graph benchmark (22 NL/Cypher/AQL queries), now living in the repo as
`tests/test_translate_finreflectkg_corpus.py` (fixture `finreflectkg.export.json`).

---

## 1. Measured baseline

Transpiler result on the 22-query benchmark (LPG: `Node` + `relations`, both
`type`-discriminated):

| Result | Original baseline | After WP-C1…C3 |
| --- | --- | --- |
| ✅ Transpiles | 15 / 22 (68%) | **22 / 22 (100%)** |
| ❌ Coverage gap | 7 / 22 | 0 / 22 |

The 7 original failures collapsed into **three transpiler gaps** plus a
**cross-cutting NL→Cypher semantic issue**; the transpiler gaps are now closed:

| Gap | Error | Queries | Status |
| --- | --- | --- | --- |
| **A. `collect()` aggregation** | `Unsupported function in v0: COLLECT` | q05, q09, q10 | ✅ WP-C3 (DISTINCT, slice, mixed-with-aggregate) |
| **B. List subscript / slice `x[i]`, `x[i..j]`** | `Only IN operator is supported in v0` | q01, q05, q09, q10 | ✅ WP-C2 |
| **C. Scalar fn aliases `upper`/`lower`** | `Unsupported function in v0: upper` | q03, q04, q06 | ✅ WP-C1 |
| **S. NL semantics: "as a graph" + fuzzy match** | (transpiles, wrong intent) | q03, q06 | ⏳ WP-S1/S2 |

### Remaining *semantic* gaps (transpile ✅, correctness ⏳)

100% transpile-success is **not** 100% semantic correctness. Tracked for the
WP-S phase:

- ~~**Label predicate on an untyped variable.**~~ ✅ **WP-S2c (done).**
  `MATCH (risk) … WHERE risk:RISK_FACTOR OR risk:EVENT` (q10/q11/q20) now compiles
  the label suffix to the mapping's discriminator filter
  (`risk[@typeField] == @typeValue` for LABEL/GENERIC_WITH_TYPE,
  `IS_SAME_COLLECTION(...)` for COLLECTION) instead of dropping it to a no-op.
- **"as a graph" intent + approximate entity match** (the CINF query). → **WP-S1/S2**.

What already works and must stay working (regression guard): anonymous edge
scans (`MATCH ()-[r]->()`), `type(r)`/`labels(n)` discriminator mapping,
multi-hop fixed paths, variable-length `*2..4`, multi-`MATCH` pipelines,
`WITH … count()` aggregation, `=~` regex, `CONTAINS`, `IN`, `IS [NOT] NULL`,
path variables `MATCH p = … RETURN p`, list comprehension `[n IN nodes(path) | n.name]`.

---

## 2. The CINF query — root-cause of the "flawed" result (answers to #2)

NL: *"Show me, **as a graph**, the publicly-traded companies that Cincinnati
Financial (CINF) has a stake in."*

Generated: `MATCH (c:COMP {name: "cincinnati financial corporation"})-[:has_stake_in]->(p:COMP) RETURN p.name`

Three independent defects:

1. **Returns a scalar, not a graph.** The question explicitly asks for a graph,
   so the Cypher should bind and return a path (`MATCH p = (c)-[:Has_Stake_In]->(x) RETURN p`)
   — i.e. nodes + relationships the UI can render — not `p.name`.
2. **Brittle exact match.** `{name: "cincinnati financial corporation"}` (and the
   AQL `LOWER(name) == LOWER(...)`) is an equality test. The entity resolver guessed
   a *full legal name*; in this dataset the node's `name` is the ticker-style token
   `"cinf"`. An exact match — even case-folded — misses.
3. **Wrong label.** It used `:COMP` for the anchor; CINF is an `:ORG` in the source.

### 2.a — Does the analyzer tell us the shape of `name` values?

Partly. The consumer (`schema_acquire._profile_property_values`) already records,
per property: `type`, `sampleValues` (≤4 distinct, ≤48 chars), `sentinelValues`,
`numericLike`, `required`. So for `name` it *does* capture representative values
(e.g. `["cinf","aapl","msft"]`) that reveal casing and token-vs-phrase shape —
**but we don't currently feed a "value shape" signal to the LLM or the resolver
in a way that says "match approximately; names are short lowercased tokens".**

Planned additions (cheap, sampled): a `valueShape` descriptor per string property —
`{casing: "lower|upper|mixed", avgLen, maxLen, looksLikeToken: bool, distinctRatio}` —
derived from the same sample, surfaced in the prompt's schema card and consumed by
the resolver to choose exact vs. fuzzy strategy.

### 2.b — Does Cypher lack fuzzy text functions?

Core openCypher has only `CONTAINS`, `STARTS WITH`, `ENDS WITH`, and `=~` (regex).
There is **no built-in similarity/fuzzy/full-text** in the language itself — Neo4j
exposes those via procedures (`db.index.fulltext.queryNodes`, `apoc.text.*`). So:
- For NL→Cypher we should generate `CONTAINS`/`=~` for approximate name matches
  (portable), and
- Optionally expose ArangoSearch-backed fuzzy matching through the existing
  `arango.*` extension namespace (e.g. `arango.search(...)`, `arango.ngram_match(...)`)
  for callers who opt in.

### 2.c — Should ArangoDB use an inverted (ArangoSearch) index on `name`?

Yes, for production. `LOWER(name) == …` and `name LIKE "%…%"` are full collection
scans on `Node`. An **ArangoSearch View** (or inverted index) on `name` with a
`text`/`ngram` analyzer enables `SEARCH ANALYZER(...)`, `PHRASE`, `NGRAM_MATCH`,
and `LEVENSHTEIN_MATCH` — fast fuzzy lookups. This pairs with the workbench's
"offer to create the index" pattern (same UX we use for VCI): detect the missing
analyzer/view when the resolver falls back to a scan, and offer one-click creation.

---

## 3. Work packages

Each WP is independently shippable, test-first, and flips one or more corpus
entries from GAP → SUPPORTED (the `test_known_gap_*` cases fail when a gap closes,
which is the prompt to promote the entry).

### WP-C1 — Scalar function aliases (Gap C) · *S, ~0.5 day*
- Map `upper`→`UPPER`, `lower`→`LOWER` (alias of canonical `toUpper`/`toLower`)
  in the function compiler.
- Audit other common non-canonical aliases the LLM emits (`len`→`size`, etc.).
- **Closes:** q03, q04, q06 (transpile-level). *Caveat:* q03/q06 also need WP-S
  for correct intent.
- **Tests:** unit cases in `test_translate_functions` + corpus promotion.

### WP-C2 — List subscript & slice operators (Gap B) · *M, ~2 days*
- Compile `expr[i]` → AQL `NTH(expr, i)` / `expr[i]`; `expr[i..j]` → `SLICE(...)`;
  open-ended `[i..]`, `[..j]`. Negative indices per Cypher semantics.
- Wire into `oC_ListOperatorExpression` (currently only `IN`).
- **Closes:** q01 (with WP-C4), and the `[0..5]`/`[0..3]` slices in q05/q09/q10.
- **Tests:** golden tests for index/slice + corpus promotion.

### WP-C3 — `collect()` aggregation (Gap A) · *L, ~3–4 days*
- Recognize `collect(x)` / `collect(DISTINCT x)` in `WITH`/`RETURN` aggregation
  position → AQL `COLLECT … AGGREGATE g = UNIQUE(x) | PUSH(x)` (DISTINCT→`UNIQUE`).
- Compose with grouping keys, post-aggregation `WHERE` (→ `FILTER`), `ORDER BY`,
  `LIMIT`, and slice-of-collect (`collect(...)[0..5]`, needs WP-C2).
- **Closes:** q05, q09, q10.
- **Tests:** dedicated aggregation goldens (grouping, DISTINCT, slice, filter).

### WP-C4 — Group-by computed key (q01) · *SUBSUMED by WP-C2*
- The aggregation pipeline already supported a computed grouping key
  (`COLLECT k = <expr> AGGREGATE …`); q01's only blocker was the list subscript
  in `labels(n)[0]`. Once WP-C2 landed, q01 transpiles. No separate work needed.

### WP-S1 — NL "return a graph" intent · ✅ DONE
- `_detect_graph_intent` (conservative regex: "as a graph/network/subgraph",
  "visualize/visualise", "show/draw/render/display/plot … graph/network/
  relationships/connections", "graph of …") in `nl2cypher/_core.py`.
- On a hit, `PromptBuilder.graph_intent=True` appends an "Output shape: return a
  graph" section instructing a **path-returning** Cypher (`MATCH p = … RETURN p`)
  with an inline example — appended (not baked into the cacheable prefix) so it
  costs tokens only when relevant. Threaded through `_call_llm_with_retry`.
- Tests: `TestGraphIntentDetection` in `tests/test_nl2cypher.py` (detector
  positives/negatives, builder rendering, and recording-provider integration
  asserting the section reaches/omits from the live system prompt).
- **Improves:** q03, q06 correctness; general "show me … graph" questions.

### WP-S2 — Approximate entity matching · ✅ DONE (incl. S2c)
- **S2a — valueShape surfaced (done).** `_property_quality_hint` now renders
  `shape: <…>` and `e.g. "…", "…"` from analyzer property metadata
  (`valueShape`/`value_shape`, `examples`/`exampleValues`/`sampleValues`), and
  `_build_schema_summary` appends a "Value-shape hints" block (only when present)
  telling the LLM to match the real value form and prefer fuzzy/`CONTAINS` over
  brittle exact equality — so it stops inventing legal names for token fields.
- **S2b — symbol/ticker matching (done).** `extract_candidates` captures a
  parenthesized symbol ("Cincinnati Financial **(CINF)**") *first*, and the
  resolver's property set now includes `ticker`/`symbol`/`code`/`id`, so an exact
  symbol match resolves even when the long name fuzzes. (The resolver already
  emitted exact/contains/reverse/Levenshtein scoring; S2b feeds it better
  candidates + fields.)
- Tests: `TestValueShapeHints` (`tests/test_nl2cypher.py`),
  `TestExtractCandidates`/`TestIdentifierPropertyCandidates`
  (`tests/test_nl2cypher_entity_resolution.py`).
- **S2c — label predicate on untyped var (done).** `MATCH (risk) …
  WHERE risk:RISK_FACTOR` no longer compiles to a no-op. `_compile_expression`'s
  `OC_PropertyOrLabelsExpression` branch (`core.py`) now calls a new
  `_compile_label_predicate(var, labels_ctx, bind_vars)` helper for the
  pure-variable + labels case. It reads the active mapping via the existing
  `_active_resolver` ContextVar (no parameter-threading refactor needed) and
  emits, per label: `var[@typeField] == @typeValue` for LABEL/GENERIC_WITH_TYPE,
  or `IS_SAME_COLLECTION(@coll, var)` for COLLECTION. Multiple labels (`n:A:B`)
  AND together (Cypher "has all labels"); `OR` of label predicates composes
  through the normal boolean compiler. Unknown labels raise `MAPPING_NOT_FOUND`
  rather than silently passing. Tests: `tests/test_translate_label_predicate.py`
  + corpus guard `test_label_predicate_on_untyped_var_emits_filter` (q10/q11/q20).
- **Improves:** q10/q11/q20 semantic correctness (plus q03/q04/q06/q07).

### WP-S3 — ArangoSearch index advisory + fuzzy compilers · ✅ DONE (incl. S3c)
- **S3a — `arango.*` fuzzy compilers (done).** Extended the *existing*
  registry-gated extension framework (`arango_cypher/extensions/search.py`,
  alongside `arango.bm25`/`tfidf`/`analyzer`) with a fuzzy/text family:
  `arango.like`, `starts_with`, `in_range`, `levenshtein_distance`,
  `levenshtein_match`, `ngram_match`, `ngram_similarity`, `phrase`, `boost`,
  `min_match`, `tokens`, `soundex`, `regex_test`/`matches`/`replace` → the
  identically-signatured AQL functions, arity-checked. Honors the existing
  `ExtensionPolicy` gating (no registry → `EXTENSIONS_DISABLED`; allowlist/
  denylist respected). Tests: `TestFuzzyTextFunctions` in `tests/test_extensions.py`.
- **S3b — ArangoSearch advisory (done).** `EntityResolver` now records a
  structured `IndexAdvisory(collection, field, …)` (deduped) whenever a fuzzy
  name probe runs against a field with no inverted/ArangoSearch coverage — i.e.
  a full Levenshtein scan. `IndexAdvisory.suggested_inverted_index()` returns a
  ready `add_index` spec (`inverted`, `text_en` analyzer) and `.as_dict()` is
  UI-ready. Exposed via `EntityResolver.advisories`. Mirrors the missing-VCI
  advisory. Tests: `TestArangoSearchAdvisory` in
  `tests/test_nl2cypher_entity_resolution.py`.
- **S3c — create endpoint + UI affordance (done).** End-to-end wiring of the
  advisory: (1) `NL2CypherResult` gained an `advisories: list[dict]` field;
  `nl_to_cypher` reads `resolver.advisories` after resolution and attaches them
  (even when `resolve()` raised — a partial probe may already have flagged a
  missing index). (2) `POST /nl2cypher` forwards `advisories`. (3) New
  `POST /schema/index/create` (session-authenticated) reconstructs the inverted-
  index spec **server-side** from validated `collection`/`field`/`analyzer`
  (the client never sends a free-form spec), is idempotent (`created:false` if
  an inverted index already covers the field), and 404s on a missing collection
  / 400s on a bad name. (4) UI: `client.ts` gains `IndexAdvisory` +
  `createIndex`; `App.tsx` renders an amber "Slow fuzzy match detected" strip
  below the NL input with a per-advisory "Create index" button (creating/
  created/error states), mirroring the `SchemaWarningBanner` action pattern.
  Tests: `tests/test_service_index_create.py`, advisory pass-through in
  `tests/test_service_nl.py`, threading in `tests/test_nl2cypher.py`.

### WP-V1 — Broaden the corpus & guard semantics · *ongoing*
- Add execution-grounded checks (not just transpile-success) for the corpus where
  a live DB is available, asserting result *shape* (path vs scalar) for graph
  questions.
- Expand beyond these 22 with a feature-matrix corpus (every clause/function once).

#### WP-V1a — Text2Cypher corpus harness (large-scale, real LLM Cypher)

A far bigger, execution-grounded benchmark than the 22-query FinReflectKG set:
the neo4j-derived **movies Text2Cypher** corpus (~1,942 real LLM-emitted Cypher
queries against the movies demo graph, which our `movies_pg` fixture matches
exactly). Harness: **`scripts/eval_text2cypher_corpus.py`** — opt-in, reads an
external `--dataset` path (the dataset's redistribution license is pending, so it
is **not** committed), runs the transpiler over the `cypher` column, reports
transpile coverage + ranked failure buckets, and (`--with-db`) executes the
generated AQL against a seeded movies PG DB. Treat the dataset's `cypher` +
Neo4j `results` as the reference; its own machine-translated `aql_query` is not
used.

**Measured baseline (2026-07-01):**

| Metric | Start | After WP-V1a fixes |
| --- | --- | --- |
| Transpile coverage | 85.8% (1666/1942) | **90.4% (1756/1942)** |
| Ran without error (of transpiled) | 95.1% | **97.8%** |

Fixes landed from this corpus (each with regression tests):
- Unlabeled traversal endpoints inferred from relationship domain/range
  (`(m:Movie)<-[:REVIEWED]-()` → Person), COLLECTION-style only.
- Two variable-collision (ERR 1511) classes: back-reference self-cycle and
  unnamed-edge collision across MATCH clauses.
- COLLECT group-var shadowing (`COLLECT director = director.name`).
- ORDER BY on a RETURN alias emitted an unbound `SORT` (collection-not-found).
- Cypher scalar functions inside aggregate arguments (`max(size(x))` → LENGTH).

**Ranked remaining backlog (execution errors, ~39; diminishing returns):**
- **Transpile gaps (higher leverage):** `count{}` / `exists{}` subquery
  expression syntax — *closed 2026-07-05* for the common shapes: single-node
  bodies (`exists { (m:Label) WHERE … }`, `count { (m:Label) }`, correlated
  bare-outer-node `exists { (n) WHERE … }`), relationship / multi-hop /
  trailing-`RETURN` forms, and now `WITH`+aggregation counting existentials
  (`exists { MATCH (n)-->(m) WITH n, count(*) AS c WHERE c > 3 RETURN true }`,
  WP-V1d — required an ANTLR grammar change + parser regen). TCK
  `expressions/existentialSubqueries` is 10/10. Still open here: non-count
  aggregates / grouping by subquery-local vars / multi-clause `WITH` chains
  inside a subquery (refused with a clear error rather than mis-compiled), and
  untyped `-->` inside a subquery (the general typeless-relationship bucket).
  Also multi-type hops `[:A|B]`, `collect()` nested in `size()`.
- **Execution-correctness tail:** ~14 remaining ERR-1511 (auto edge/node
  namespace pollution + multi-`WITH` scope), `FOR IN <alias>` (iterating a
  `collect()` result), var-lost-after-COLLECT (double-aggregation scope),
  UNWIND-var-lost, a few `unexpected INTO` syntax cases, `date()`.

#### WP-V1c — Single-node EXISTS/COUNT subquery bodies · *done 2026-07-05*

`_compile_single_node_subquery_body` handles the no-relationship-chain forms
the subquery compiler previously refused: a labelled node scans its
collection (`FOR m IN <coll> FILTER <discriminator> <props> <where> RETURN 1`)
with collision-safe bind keys; a bare named node that re-uses an outer
binding probes a single-element list (`FOR _sq_probe IN [n] FILTER <where>`)
so the predicate references the outer variable directly. Anonymous unlabelled
uncorrelated nodes (`exists { () }`) are refused cleanly. TCK
`expressions/existentialSubqueries` 6/10 → 9/10; the bigger win is the
LLM-corpus, where single-node existentials are common. Tests:
`tests/test_translate_pattern_subquery_shorthand.py::TestSingleNodeSubquery`.
*Superseded 2026-11-23:* every EXISTS/COUNT body, single-node ones included,
compiles through the MATCH-after-WITH compiler (PRD, EXISTS/COUNT
paragraph); this function and the single-hop copy beside it were removed.

#### WP-V1d — WITH+aggregation counting existentials · *done 2026-07-05*

The `oC_SubqueryBody` grammar rule only accepted `( ReadingClause )+ Return?`,
so `exists { MATCH … WITH … count(*) … RETURN true }` failed at **parse**
time ("no viable alternative"). Extended the rule to
`( ( oC_ReadingClause | oC_With ) SP? )+ ( oC_Return )?` and regenerated the
ANTLR parser (`grammar/Cypher.g4` → `CypherParser.py` / `Cypher.interp`, via
ANTLR 4.13.2 in a throwaway `eclipse-temurin` container — lexer byte-identical;
parser diff confined to the subquery rule + ATN). `UpdatingClause` is
deliberately *not* accepted, so the openCypher "update-in-existential is an
error" scenario stays a correct parse-rejection.
`_compile_subquery_with_aggregation` lowers the counting shape to a fresh
traversal + `COLLECT WITH COUNT INTO <alias>` + optional post-COLLECT HAVING
`FILTER`; the target is a fresh loop variable (it's enumerated, not
correlated). Unsupported `WITH` shapes (DISTINCT, ORDER BY/SKIP/LIMIT,
non-`count(*)` aggregates, grouping by subquery-local vars, multiple `WITH`s)
are refused with `UNSUPPORTED` rather than mis-compiled. TCK
`expressions/existentialSubqueries` 9/10 → **10/10** (Full 2067→2068, Core
1906→1907). Tests:
`tests/test_translate_pattern_subquery_shorthand.py::TestAggregationSubquery`.

#### WP-V1b — Leading-clause constraint relaxed (TCK mega-lever) · *done 2026-07-01*

The largest single TCK lever: no-MATCH computational pipelines. Cypher starting
with `WITH` (constant projection) or `UNWIND` over literals — no MATCH, no writes
— previously failed "MATCH is required before WITH". A dedicated order-walking
handler (`_translate_computational_multi_part`) now compiles them to a plain
`LET`/`FOR` AQL pipeline (reusing `_apply_with`/`_append_return`); routing only
sends the no-MATCH/no-write case there, leaving MATCH-led and write paths
unchanged. Also fixed the collect-without-grouping-key bug this exposed
(`COLLECT INTO x` → `COLLECT AGGREGATE x = PUSH(…)`).

**TCK dry-run coverage (translation-only):**

| Subset | 2026-04-20 | 2026-07-01 |
| --- | --- | --- |
| CORE (excl. temporal/quantifier/call) | 54.8% | **86.4%** (1902/2201) |
| FULL | 32.2% | **53.4%** (2063/3861) |

"MATCH is required before WITH" fell 475 → 22. The remaining 22 were **write-tail
combos** (`WITH … MERGE`, `UNWIND … CREATE`) needing read+write pipeline
integration — largely closed in WP-V1e (below). Full breakdown:
`tests/tck/COVERAGE_REPORT.md`.

#### WP-V1e — Write-tail combos (UNWIND→write, WITH→CREATE) · *done 2026-07-05*

Two read+write pipeline gaps closed (TCK +6 overall: Full 2068→2074, Core
1907→1913):

- **`UNWIND … CREATE` / `UNWIND … MERGE`.** A shared UNWIND-aware
  reading-clause compiler (`_compile_write_reading_clauses`) turns MATCH/UNWIND
  reading clauses into the `FOR …` prefix the write nests in
  (`UNWIND [..] AS x CREATE (n {p:x})` → `FOR x IN [..] INSERT {p:x}`).
  `UNWIND … CREATE` was hard-rejected; `UNWIND … MERGE` translated but
  *silently dropped* the `FOR` (unbound loop var in the UPSERT) — now fixed.
  Supports MATCH-then-UNWIND and multiple UNWINDs; UNWIND-before-MATCH refused.
- **`MATCH … WITH … CREATE`** (incl. after aggregation, e.g. `WITH count(p) AS c
  CREATE (m {released:c})`). The multi-part handler now emits a tail CREATE
  (`_append_multipart_create_tail`, reusing the append-mode `_compile_create`)
  nested in the already-built WITH pipeline, referencing bound vars.
- **`MATCH … WITH … MERGE`** (WP-V1f) — single-node and relationship MERGE
  (incl. between WITH-bound nodes, `MATCH (a) MATCH (b) WITH a, b MERGE
  (a)-[:R]->(b)`), after aggregation, with `RETURN`, and `ON CREATE/MATCH SET`.
  `_append_multipart_merge_tail` builds the WITH pipeline then splices the
  standalone MERGE UPSERT after it (the UPSERT already renders against bound
  vars by name), so the existing MERGE translator is reused unchanged.

Still deferred (refused with a specific error, never mis-compiled):
`UNWIND … WITH … CREATE`/`MERGE` (leading-UNWIND multi-part routing), and a
tail MATCH before a MERGE (would double-compile). **`WITH … SET`/`DELETE`/
`REMOVE`** landed as a follow-up (`_append_multipart_mutate_tail`) — document
variables from MATCH (including `WITH p AS x` aliases) mutate in-place;
computed projections still reject. Tests: `TestWithMutateTail` in
`tests/test_translate_write_clause_gaps.py`.

#### WP-V1g — `RETURN *` / `WITH *` star projection · *done 2026-07-05*

`RETURN *` / `WITH *` was rejected ("RETURN items required" — the `*` token
yields no projection items). `*` now expands to every *user-named* variable in
scope, in declaration order. A new `_active_user_vars` contextvar tracks the
in-scope user variables (populated from the matched pattern AST; auto-generated
traversal / anonymous-node bindings are never added), and each `WITH` replaces
the scope with its projected variables (`WITH *` passes it through, applying an
optional `WHERE`). `RETURN *` always emits the named-object form (even for one
variable and under `DISTINCT`). This was the single biggest lever in this arc —
**TCK +16** (Full 2076→2092, Core 1915→1931), since `*` appears across many
categories. `WITH *, <items>` and `RETURN *` mixed with aggregation are refused
rather than mis-compiled. Tests: `tests/test_translate_star_projection.py`.

#### WP-V1h — `any`/`all`/`none`/`single` list quantifiers · *done 2026-07-07*

The list-quantifier predicates parsed but weren't compiled ("Unsupported atom
in v0"). `_compile_quantifier` lowers `<kind>(x IN list WHERE pred)` to a
count-subquery test binding the quantifier variable as the loop variable:
`any` → `LENGTH(FOR x IN list FILTER pred RETURN 1) > 0`, `none`/`single` →
`== 0`/`== 1`, `all` → `LENGTH(FOR x IN list FILTER NOT (pred) RETURN 1) == 0`
(missing WHERE defaults the predicate to `true`). No grammar change — the atom
was already in the grammar. This was the single biggest lever of the whole
arc: **Full TCK +574** (2092→2666) with `expressions/quantifier` 12→544 (90%),
plus **+42 Core** (quantifiers appear in boolean / match-where scenarios too).
Caveat: AQL treats a null predicate as false, so Cypher's three-valued
semantics for `null` list elements aren't reproduced exactly (the remaining
~60 quantifier scenarios) — correct for the common non-null case. Tests:
`tests/test_translate_quantifiers.py`.

#### WP-V1i — Chained comparisons · *done 2026-07-07*

`a < b < c` (openCypher = `a < b AND b < c`) was rejected ("Chained
comparisons not supported"). The comparison compiler now walks the partial
comparisons, compiling each adjacent pair via the extracted
`_compile_one_comparison` (keeping the per-operand null-guard on ordered
operators) and AND-ing them. TCK +10 (Full 2666→2676, Core 1973→1983);
`expressions/comparison` 69→83%. Tests:
`tests/test_translate_chained_comparison.py`.

#### WP-V1j — `reduce()` sum-fold · *done 2026-07-07*

`reduce(acc = init, x IN list | body)` was a grammar gap ("no viable
alternative"). Added an `oC_ReduceExpression` rule + regenerated the parser
(ANTLR 4.13.2 in a container), and `_compile_reduce` lowers the numeric
sum-fold shape (`acc + f(x)`, accumulator once, all top-level `+`, no string
literals) to `(init + SUM((FOR x IN list RETURN f(x))))`. AQL has no general
fold, so other accumulations (`*`, string concat, subtraction, multi-reference)
raise a clear `NOT_IMPLEMENTED` capability error rather than mis-translating.
No TCK delta (reduce isn't TCK-tested) — this closes item #4 of the
FinReflectKG vocabulary bug report. Tests: `tests/test_translate_reduce.py`.

---

## 4. Sequencing & milestones

| Milestone | WPs | Corpus result |
| --- | --- | --- |
| **M1 — quick wins** | WP-C1, WP-C2 | 15 → ~18/22 (q01 partial, q03/q04/q06 transpile) |
| **M2 — aggregation** | WP-C3, WP-C4 | ~18 → 22/22 transpile |
| **M3 — semantics** | WP-S1, WP-S2 | "as a graph" + fuzzy correct; q03/q06 intent fixed |
| **M4 — performance/UX** | WP-S3, WP-V1 | ArangoSearch advisory + execution-grounded guards |

Recommended order: **C1 → C2 → C3 → C4** (transpile to 22/22 first, fully
test-guarded), then **S1 → S2 → S3** (semantic correctness + performance).
Each WP is one PR, checkpointed, with its corpus entries promoted in the same PR.

---

## 5. Risks & non-goals

- **Risk:** `collect()` + slice + post-filter interaction is the hardest piece;
  keep WP-C3 narrow (single-level grouping) before nested aggregation.
- **Risk:** ArangoSearch requires analyzers/views that may not exist; WP-S3 must
  degrade gracefully to `CONTAINS`/`=~` when absent.
- **Non-goal (this plan):** write clauses (`CREATE`/`MERGE`/`SET`), `OPTIONAL MATCH`
  semantics beyond current support, and APOC-equivalent procedure coverage —
  track separately.

---

## 6. Open questions for review

1. Priority order — do you want **transpile-completeness (M1+M2) first**, or the
   **"as a graph"/fuzzy semantic fixes (M3) first** since that's the visible demo defect?
2. For fuzzy matching, default to **portable `CONTAINS`/`=~`**, or invest in the
   **ArangoSearch extension path** (WP-S3) as the primary?
3. Should WP-S3's "create index" follow the exact VCI advisory UX, or be a
   separate "Indexing" panel?
