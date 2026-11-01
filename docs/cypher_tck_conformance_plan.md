# TCK Conformance — status and plan

**Status:** Core plan delivered · **Updated:** 2026-10-31 · **Owner:** transpiler
**Authoritative measurement:** `tests/tck/execution_ledger.tsv`, ratcheted on every PR by
`tests/integration/test_tck_execution_ratchet.py` (live ArangoDB 3.11)
**Scorecard:** `docs/cypher_support_scorecard.md` · **Contract:** `docs/cypher_capability_matrix.md`

Not to be confused with `docs/cypher_coverage_plan.md` (2026-06-23), which targets the
22-query FinReflectKG benchmark. This document covers the openCypher TCK corpus
(220 feature files, 3,861 scenarios).

## Current figures — 2026-10-31 (main 0150b39)

A scenario counts when its query returns the TCK's expected rows on a live ArangoDB, or
— for a negative scenario — when translation or execution raises an error. That is the
definition arango-gql-gateway uses; neither harness verifies side effects.

| Measurement | Correct | % |
| --- | ---: | ---: |
| **Harness mode** (`cypher_column_names=True`, `temporal_strings=True`) | **3,808 / 3,861** | **98.6%** |
| Default `translate()` options | 3,713 / 3,861 | 96.2% |
| arango-gql-gateway, same definition (its report, TCK 2024.3) | 3,816 / 3,897 | 97.9% |

Harness mode = 3,148 rows matched + 660 correctly rejected. Of the 3,148, **33 are
negative scenarios that pass only because ArangoDB raised some error** while executing
the AQL; the translator did not itself recognise the query as invalid. They meet the
definition but are weak evidence. `analyze_execution.py --tsv` marks them
`error at execute`.

The two opt-in options are the maintainer's decision (default off so existing API, UI
and LDBC output is unchanged):

- `cypher_column_names` — an unaliased column is keyed by its Cypher text (`"n.name"`,
  `"count(*)"`), as Neo4j drivers and the TCK expect, instead of an inferred short key.
- `temporal_strings` — a string in one of Neo4j's temporal forms is read as that temporal
  value at run time (accessors, arithmetic, ordering).

`TCK_DEFAULT_OPTIONS=1` runs the harness without them.

### Progress

| Date | Correct | % | Note |
| --- | ---: | ---: | --- |
| 2026-09-10 | 1,566 | 40.6% | first live run (old harness) |
| 2026-10-30 | 1,918 | 49.7% | re-baseline after harness fixes (#82) |
| 2026-10-31 | 3,808 | 98.6% | #83–#95 |

| PR | Change | Δ |
| --- | --- | ---: |
| #82 | Execution ledger + ratchet; harness fidelity (steps in order, Cypher cell equality, outline expansion, parameter tables) | re-baseline |
| #83 | Semantic analysis pass: invalid Cypher rejected with Neo4j's error class | +296 |
| #84 | Graph clauses: CREATE/SET/MERGE, multi-label LPG, named paths, `keys()` | +191 |
| #85 | Opt-in `cypher_column_names` | +37 |
| #86 | Compile-time constant evaluation with Cypher semantics; three-valued quantifiers | +140 |
| #87 | Temporal types (java.time semantics) folded at translate time | +950 |
| #88 | Writes before WITH, Cypher sort order, percentiles; refuse silently dropped writes | +58 |
| #89 | Octal/hex literals; TCK string cells unescaped | +17 |
| #90 | Opt-in `temporal_strings` (run-time temporal values) | +39 |
| #91 | Pattern expressions as values rejected (Neo4j 5) | +8 |
| #92 | Write sequencing within one statement | +16 |
| #93 | Registered table-backed procedures; all 52 CALL scenarios | +55 |
| #94 | IN precedence in the grammar; path shapes; run-time TypeErrors | +60 |
| #95 | WP-6 multi-statement plans (`translate_plan` / `execute_plan`) | +23 |

## Maintainer decisions

| Decision | Choice |
| --- | --- |
| Column naming | Opt-in `cypher_column_names`; default unchanged |
| Run-time temporal strings | Opt-in `temporal_strings`; default unchanged |
| WP-6 multi-statement plans | Built (`translate_plan` / `execute_plan`, one stream transaction); `translate()` unchanged |
| Pattern expressions used as values (`size((a)-->())`) | Rejected as in Neo4j 5; `COUNT { }` / `EXISTS { }` instead (the old form returned a wrong count) |
| Plans in tenant-bound sessions | Refused (422 `MULTI_STATEMENT_TENANT_BOUND`), fail closed until the guardrail certifies plan steps |
| Undirected self-loops | Matched once per direction, as AQL's `ANY` does — accepted divergence (fixing it rewrites every undirected hop, costing LDBC performance) |
| `CALL` | Supported through the procedure registry; the TCK's procedures are registered as table-backed procedures (a public capability, `arango_cypher/procedures.py`) |

## Silent wrong answers fixed along the way

Some of these passed the TCK before they were fixed, because the harness does not check
side effects. Correctness took priority over the count: five former "passes" are now
honest refusals or failures.

- A write beside another kind of write (CREATE beside MERGE, DELETE beside MERGE, a
  second FOREACH, a write after OPTIONAL MATCH) was **dropped** from the AQL (#88).
- `MERGE … SET n.p` dropped the SET; `ON CREATE SET b.x` on a relationship MERGE wrote
  onto the relationship; a relationship MERGE ignored its properties when matching (#88, #92).
- `size((d)--())` returned the length of a boolean (#91).
- `[1] + null` was `[1, null]`; Neo4j 5.26 says null (#86).
- A folded datetime in a named zone made `DATE_TIMESTAMP` null, so a filter on it matched
  nothing (#87).
- `keys(r)` leaked `_id/_rev/_from/_to` (#84); a later `MATCH (a:X)` ignored `:X` on a
  bound `a` (#94).

## What is left (53 in harness mode)

| Area | Count | Why |
| --- | ---: | --- |
| `expressions/temporal` | 15 | Temporal8 [1]–[5]: `WITH temporal … MATCH` shape the read translator refuses |
| `clauses/match`, `useCases` | 15 | undirected self-loops (accepted divergence); relationship-list matching across pattern parts; a 22-hop path past the depth cap of 10; edge uniqueness in the bound-MATCH compiler |
| `expressions/comparison` | 7 | ordering across types and with NaN at run time (AQL has no NaN) |
| `clauses/merge` | 3 | a MERGE must yield every existing match per row (UPSERT yields one); needs per-row plan steps |
| other | 13 | `WITH null` before MATCH; a label test on a null variable; non-constant SKIP; NaN in ORDER BY; small list / path shapes |

Next work packages, if pursued: per-row plan steps for MERGE; relationship-list matching;
Temporal8 routing; strengthening the 33 execution-error passes into translator
rejections; and side-effect verification in the harness (neither harness does it today,
and it would make both figures stricter).

## How to measure

```bash
docker compose up -d                                   # ArangoDB on :28529
RUN_INTEGRATION=1 ARANGO_URL=http://localhost:28529 \
  ./.venv/bin/python tests/tck/execution_ledger.py     # diff against the committed ledger
./.venv/bin/python tests/tck/analyze_execution.py --tsv out.tsv [--only clauses/match]
TCK_DEFAULT_OPTIONS=1 ./.venv/bin/python tests/tck/analyze_execution.py --tsv default.tsv
```

Two runs on one server need distinct databases (`TCK_DB_NAME`, `--db-name`): every
scenario resets the graph collections. After a branch is rebased, regenerate **both**
ledgers (`tests/tck/ledger.py --write`, `tests/tck/execution_ledger.py --write`) — a
textual merge of the TSVs is not a measurement.

## History: the 2026-09-10 plan

The original plan measured 1,566 correct (40.6%) and grouped the 2,244 failures into
missing syntax (1,133, of which temporal 857 and `CALL` 50), false accepts of negative
tests (413), semantic mismatches (480), AQL that failed to run (206) and parse/harness
issues (130). It recommended targeting core conformance with temporal and `CALL` as
explicit exclusions. Both were delivered instead: temporal through compile-time folding
plus opt-in run-time strings, and `CALL` through registered procedures. The 2026-10-30
re-baseline also found harness defects (control queries never ran, `True == 1`
comparisons, outline placeholders swallowing `<`, the first parameter of every scenario
dropped) that had both hidden failures and invented passes.
