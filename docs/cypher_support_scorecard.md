# Cypher Support Scorecard

> **Superseded figures (2026-10-31).** The execution figures below predate the TCK push. Current conformance on a live ArangoDB is **3,808 / 3,861 (98.6%)** in harness mode and **3,713 (96.2%)** with `translate()`'s default options, from the ratcheted `tests/tck/execution_ledger.tsv`; see `docs/cypher_tck_conformance_plan.md`.

**Authoritative measurement:** `tests/tck/COVERAGE_REPORT.md` (generated)
**Compatibility contract:** `docs/cypher_capability_matrix.md`
**Machine-readable declaration:** `arango_cypher/profile.py`

| Dimension | Measured | Date |
| --- | --- | --- |
| Syntax (parse + translate) | regenerated from the TCK corpus | 2026-09-09 |
| Round trip (execute + assert) | carried from the last execution run | 2026-08-06 |

> Two dates on purpose. The syntax half needs no database and was regenerated for this
> scorecard; the execution half needs a live ArangoDB and has not been re-run since
> 2026-08-06, when 2,409 scenarios translated rather than today's 2,411. Refresh before
> quoting round-trip figures externally — see [Reproduce](#reproduce).

## Headline

| | Full corpus (3,861) | Core subset (2,805) |
| --- | ---: | ---: |
| **Syntax passability** (translates or correctly rejects) | **2,678 · 69.4%** | **2,529 · 90.2%** |
| **Semantically verified** (executes + matches TCK assertion) | **1,296 · 33.6%** | **1,288 · 45.9%** |

Core excludes exactly two categories: `expressions/temporal` and `clauses/call`.
Removing them moves passability from 69.4% to 90.2% — the clearest statement of where
the deficit is concentrated.

## The funnel

Measured against all 3,861 scenarios:

| Stage | Count | Rate | Lost at this step |
| --- | ---: | ---: | --- |
| Parses (ANTLR grammar) | 3,709 | 96.1% | — |
| Translates (emits AQL) | 2,411 | 62.4% | −1,300, overwhelmingly temporal |
| Executes (ArangoDB 3.11) | 2,408 | 62.4% | −1 |
| TCK assertion passes | 1,296 | 33.6% | −1,112 ran but returned the wrong result |

Three readings that a single "coverage %" would hide:

1. **Parsing is effectively solved** — 96.1%. Grammar is not the constraint.
2. **Translation is the constraint** — 62.4%, and 829 of the top blockers are temporal
   functions alone.
3. **Valid AQL is nearly guaranteed once translation succeeds** — 2,408 of 2,409
   translated scenarios executed (99.96%). But only **53.8%** of what executed returned
   the asserted result. *Runs* and *right* are different metrics, and the gap between
   them is the remaining work.

## Round-trip evidence, by strength

| Evidence | Scope | Result | What it proves |
| --- | --- | --- | --- |
| TCK execution | 2,408 scenarios on ArangoDB 3.11 | 1,296 assertions passed (53.8%) | AQL is valid and runs; spec-test proxy only |
| Layout matrix | 33 queries × 3 physical models (pg / lpg / hybrid) | 99 executed combinations | One Cypher query, unchanged, across layouts |
| Neo4j cross-validation | 20 movies + 14 northwind = 34 queries | **34 / 34 row-by-row agreement** | Direct equivalence against Neo4j 5-community |

The cross-validation suite is the only direct equivalence evidence in the project. It is
small, but it is the strongest tier — the same query runs on both engines and results are
normalized and compared row by row.

Supporting suites (collected test counts): fast unit + golden **1,815**, TCK harness
**3,809**, integration **4,008**, cross-validation **34**, golden fixture cases **35**.

## Top blockers

| Count | Cause |
| ---: | --- |
| 829 | Temporal functions — `datetime` (372), `time` (112), `localtime` (110), `duration` (64), `.truncate` family (171) |
| 50 | Harness exclusions (procedure step) |
| 33 | `Updating clauses are not supported in v0` |
| 26 | Aggregates in unsupported positions — `count` (15), `collect` (11) |
| 22 | `MATCH is required before WITH in v0 subset` |
| 20 | Grammar rejections (`no viable alternative at input`) |

Temporal support is the single highest-leverage gap: 1,004 scenarios, 14.6% passable.

## Category detail

See the generated table in `tests/tck/COVERAGE_REPORT.md`. Categories at 100% passability:
`return-skip-limit`, `boolean`, `conditional`, `existentialSubqueries`, `map`,
`mathematical`, `null`, `precedence`, `string`, `typeConversion`,
`countingSubgraphMatches`. Lowest: `clauses/call` (3.8%), `expressions/temporal` (14.6%),
`clauses/merge` (58.7%), `clauses/with-where` (63.2%), `clauses/set` (64.2%).

Only one category changed between 2026-08-06 and 2026-09-09: `clauses/delete`,
65.9% → 70.7% (+2 scenarios).

## How to read this

- **Passability is not correctness.** A scenario is passable if it translates *or* is
  correctly rejected. A clean rejection of a query the TCK expects to fail is a good
  outcome, but it is not support.
- **The TCK assertion is a proxy, not equivalence.** Matching the TCK's declared result is
  weaker than agreeing with a reference engine. Only the 34 cross-validation queries
  provide the latter.
- **Figures copied into prose drift.** The generated report is authoritative; this
  document included. Regenerate rather than trust the numbers above.

## Reproduce

```bash
# Syntax — translation only, no database required
./.venv/bin/python tests/tck/render_coverage_report.py --write

# Round trip — execution + TCK assertions (needs ArangoDB on :28529)
docker compose up -d
RUN_INTEGRATION=1 ./.venv/bin/python tests/tck/analyze_execution.py > tck-execution.json
./.venv/bin/python tests/tck/render_coverage_report.py --execution-json tck-execution.json --write

# Reference-engine equivalence (needs ArangoDB + Neo4j)
docker compose -f docker-compose.neo4j.yml -p arango_cypher_neo4j up -d
RUN_INTEGRATION=1 RUN_CROSS=1 pytest -m cross
```
