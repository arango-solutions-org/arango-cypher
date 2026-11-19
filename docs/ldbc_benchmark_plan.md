# LDBC SNB Benchmark — Research & Implementation Plan

**Status:** Phases 0 and 1 complete (M2 and M3 achieved 2026-10-29); Phase 2 (performance) in progress
**Date:** 2026-10-23
**Implements:** [`PRD.md`](./PRD.md) §12.1 (LDBC SNB benchmark)
**Work packages:** WP-31 … WP-44 (continues the numbering in [`implementation_plan.md`](./implementation_plan.md))

> These are not LDBC Benchmark Results. This plan uses the LDBC Social Network
> Benchmark (SNB) schema, data generator output, and reference queries as a
> validation and performance corpus. It is not an implementation of an LDBC
> Benchmark, and no number it produces is an audited result. (See §2.6.)

---

## 1. Why

Today the transpiler has three kinds of evidence. Golden fixtures pin exact AQL.
The openCypher TCK measures language coverage over tiny, synthetic graphs. Neo4j
cross-validation compares results on Movies (~170 nodes) and Northwind. None of the
three exercises **realistic multi-hop analytical Cypher written by someone else**
against a **non-trivial graph**, in **both physical layouts**, at **sizes where
performance is measurable**.

LDBC SNB fills that gap. It has:

- An industry-standard schema with label hierarchies (`Message` ⊇ `Post`,
  `Comment`), polymorphic edges, and multi-valued properties.
- 57 reference Cypher queries and update operations written for Neo4j by the LDBC
  task force, Apache-2.0 licensed.
- A deterministic data generator with scale factors from 0.003 (2.4 MB) to 30,000.
- Reference implementations in Cypher *and* SQL, which give us an independent
  oracle for correctness.

The benchmark therefore serves three goals the user stated: **validate** the
transpiler, **detect and fix** defects (especially silent ones), and **measure
performance** across PG and LPG.

---

## 2. Research summary

### 2.1 The LDBC benchmark family

| Benchmark | What it is | Query language of the reference | Relevance here |
| --- | --- | --- | --- |
| **SNB Interactive v2** (2023/24) | Transactional workload: 14 complex reads (IC1–IC14), 7 short reads (IS1–IS7), 8 inserts (INS1–INS8), 8 *deep* deletes (DEL1–DEL8). SF up to 1,000. | Cypher (Neo4j 5.19), SQL (PostgreSQL, Umbra, MSSQL) | **Primary.** Multi-hop navigational reads plus writes. |
| **SNB Business Intelligence (BI)** | Analytical workload: BI1–BI20, plus daily insert/delete micro-batches. | Cypher (Neo4j 5.20), SQL (Umbra), GSQL (TigerGraph) | **Secondary.** Heavy aggregation; needs temporal functions and `CALL {}`. |
| SNB Interactive v1 | Predecessor of v2 (no deletes, SF ≤ 1,000). Audits still use it. | Cypher, SQL, … | Superseded for our purpose. The IC queries are near-identical to v2. |
| FinBench | Financial-transaction workload (2023+), different schema. | Cypher (Neo4j, Galaxybase, TuGraph) | Future candidate (§9). |
| Graphalytics, SPB | Graph algorithms; RDF/SPARQL publishing workload. | — / SPARQL | Out of scope (SPB may interest `arango-sparql-py`). |

Reference repositories examined (cloned 2026-10-23):

| Repo | Commit | Cypher files |
| --- | --- | --- |
| `ldbc/ldbc_snb_interactive_v2_impls` | `d05cc76` (2026-04-06) | `cypher/queries/*.cypher` (37) |
| `ldbc/ldbc_snb_bi` | `47dd38b` (2026-04-06) | `neo4j/queries/*.cypher` (20 + 6 GDS helpers) |
| `ldbc/ldbc_snb_interactive_v1_impls` | `11db98c` (2026-03-26) | `cypher/queries/*.cypher` (29) |
| `ldbc/ldbc_finbench_transaction_impls` | `5a4f9d7` (2025-09-23) | `neo4j/queries/*.cypher` (36) |

### 2.2 Schema

- **Concrete node labels (11):** `Person`, `Forum`, `Post`, `Comment`, `Tag`,
  `TagClass`, `City`, `Country`, `Continent`, `University`, `Company`.
- **Supertype labels (3):** `Message` (`Post` | `Comment`); `Place` (`City` |
  `Country` | `Continent`); `Organisation` (`University` | `Company`). The Neo4j
  reference loads a comment as `:Message:Comment`, and the queries match on
  either label.
- **Relationship types (15):** `KNOWS`, `HAS_CREATOR`, `REPLY_OF`,
  `CONTAINER_OF`, `HAS_MEMBER`, `HAS_MODERATOR`, `HAS_TAG`, `HAS_INTEREST`,
  `HAS_TYPE`, `IS_SUBCLASS_OF`, `IS_LOCATED_IN`, `IS_PART_OF`, `STUDY_AT`,
  `WORK_AT`, `LIKES`. Several are **polymorphic**:
  - `IS_LOCATED_IN` connects Person→City, Message→Country and Organisation→Place.
  - `HAS_TAG` connects Message and Forum to Tag.
  - `REPLY_OF` connects Comment to Message.
- **Properties:** `id` (int64, unique per label, and unique across a supertype's
  members) and timestamps (`creationDate`, `birthday`).
  - `KNOWS.creationDate`, `STUDY_AT.classYear` and `WORK_AT.workFrom` are edge
    properties.
  - `speaks` and `email` are string arrays.

### 2.3 Data

- **Generator:** `ldbc_snb_datagen_spark`. The Neo4j references consume the
  `composite-projected-fk` CSV layout: pipe-delimited, gzip, one directory per
  entity or edge type, with `initial_snapshot/`, `inserts/` and `deletes/`.
- **Timestamp encoding differs per workload, and it matters:**
  - **Interactive v2 Neo4j:** timestamps are **epoch-milliseconds `LONG`**
    (`creationDate:LONG`). The queries rely on this. IC7 subtracts timestamps
    arithmetically, and IC10 calls `datetime({epochMillis: …}).month`.
  - **BI Neo4j:** timestamps are native **`DATETIME` / `DATE`**. The queries use
    `datetime('…')` parameters, `.year`/`.month` accessors and `duration()`.
- **Sample data sets (published by LDBC, verified reachable 2026-10-23):**
  - `social-network-sf0.003-bi-composite-projected-fk-neo4j-compressed-epoch-millis.zip`
    (2.4 MB, used by the Interactive reference CI)
  - `social-network-sf0.003-bi-composite-projected-fk-neo4j-compressed.zip`
    (2.4 MB, ISO-8601 timestamps, used by the BI reference CI)
- **SF0.003 contents (counted):**
  - Nodes: 50 Person, 381 Forum, 3,189 Post, 471 Comment, 16,080 Tag,
    71 TagClass, 1,460 Place, 7,955 Organisation.
  - Edges: about 37 k in total, 83 of them `KNOWS`.
  - The sample includes `inserts/` and `deletes/` streams.
- **Larger SFs:** SF1 is 318 MB compressed (`bi-sf1-composite-projected-fk.tar.zst`
  on `datasets.ldbcouncil.org`). SF10 to SF10,000 are published as pre-generated
  sets. Substitution parameters for SF ≥ 1 come from LDBC's `paramgen`, run over
  the published factor tables.

### 2.4 Correctness oracles LDBC provides

- **Interactive driver (Java):** has `create-validation-parameters` and
  `validate` modes, which produce and check a `validation_params.json` of
  expected results. LDBC does not publish these files. They are generated by
  running a reference implementation.
- **BI repository:** cross-validates two systems by running both in `--validate`
  mode and diffing their outputs (`scripts/cross-validate.sh`).

Either way, **the oracle is a reference system we run ourselves.** We already
operate Neo4j for cross-validation (`docker-compose.neo4j.yml`), so Neo4j
running the *verbatim* reference Cypher is the natural oracle (§4, D4).

### 2.5 Cypher features the reference queries use

Pattern-matched across the 57 operations (37 Interactive v2 + 20 BI):

| Feature | Queries |
| --- | --- |
| Supertype labels `:Message` / `:Place` / `:Organisation` | IC2 IC7 IC8 IC9 IC14 IS4–IS7 INS6 INS7 DEL1 DEL4 DEL6 BI1–BI10 BI12–BI17 (30) |
| `shortestPath` / `allShortestPaths` | IC1 IC13 IC14 BI15 BI19 BI20 |
| Temporal (`datetime()`, `date()`, `duration()`, `.year`, `.month`) | IC10 BI1 BI2 BI13 BI15 BI16 BI17 |
| `CALL { … }` subqueries (with `UNION` inside) | BI4 BI15 BI16 |
| Neo4j GDS / APOC procedures | IC14 BI10 BI15 BI19 BI20 |
| Multi-type hop `[:A\|B*0..]` | IC12 |
| Aggregate nested in an expression (`head(collect(…))`) | IC7 BI8 |
| Variable-length `*0..`, `*1..2`, unbounded `*` | IS2 IS6 IC3 IC5 IC6 IC9 IC11 IC12 DEL1 DEL4 DEL6 DEL7 BI3 BI4 BI9 BI12 BI15 BI17 … |
| Write pipelines (`MATCH … CREATE … WITH … UNWIND … MATCH … CREATE`) | INS1 INS4 INS6 INS7 |
| `OPTIONAL MATCH`-bound `DETACH DELETE` | DEL1 DEL4 |

### 2.6 Fair-use constraints (LDBC policy)

- "LDBC BENCHMARK" is a registered trademark. Only audited results inside an
  official results set may be called **"LDBC Benchmark Results"**.
- Any number we publish MUST carry a disclaimer, such as "These are not LDBC
  Benchmark Results", and attribute the source (CC-BY 4.0 style).
- We MUST NOT describe the harness as "an implementation of the LDBC SNB
  benchmark". It does not run the LDBC driver's scheduled, time-compressed
  workload, and it does not satisfy the audit rules.
- The reference queries are Apache-2.0. If we vendor them, they keep their
  license header, and our `NOTICE` names the upstream repository and the pinned
  commit.

---

## 3. Baseline probe (2026-10-23, no code changes)

Every reference query was translated with `arango_cypher.translate()` against two
provisional mappings built from the schema above:

- **PG:** one collection per concrete label, plus one `DEDICATED_COLLECTION` per
  relationship type.
- **LPG:** `nodes` with `type`, plus `edges` with `type`.

The probe script lives outside the repo. WP-32 makes it a committed, ratcheted
test.

### 3.1 Translation outcome (57 benchmark operations)

| Outcome | PG | LPG |
| --- | --- | --- |
| Translates | 28 | 30 |
| `UNSUPPORTED` (fail-closed) | 21 | 19 |
| `CYPHER_SYNTAX_ERROR` (grammar) | 8 | 8 |

### 3.2 "Translates" is not "correct": silent-wrong findings

These are the most valuable findings, and they are why execution against an
oracle is mandatory (§4, D4):

1. **Supertype labels.** *Corrected 2026-10-24.* The first probe gave
   `Message` its own physical mapping. Against that mapping the transpiler
   emits AQL that matches nothing:
   - **PG:** `MATCH (m:Message)` scans a `Message` collection.
   - **LPG:** it emits `FILTER m.type == "Message"`, and no document holds
     that type.

   The committed fixtures (WP-32) **leave the supertypes out** instead. Against
   them, every one of the 19 (PG) / 20 (LPG) queries that name a supertype
   fails closed with `MAPPING_NOT_FOUND`, and none translates. So the
   transpiler is not at fault here; the hazard is a mapping that misdescribes a
   label hierarchy. The real fix remains WP-35: let the mapping express the
   hierarchy.

   Under the fixtures, **13 (PG) and 14 (LPG)** operations translate. That
   matches the first probe's estimate of the most that could be correct.
2. **Temporal accessors on stored timestamps silently return `null`.**
   `m.creationDate.year` became `m.creationDate.year`, which is attribute
   access on a number or string. BI1 and BI13 would "translate" this way once
   their supertypes resolve.
   - **Fixed in WP-32 (fail closed).** An accessor on a property the mapping
     declares temporal is refused (`_translate_v0/temporal_guard.py`).
   - **Still open:** a mapping that types the property as a plain number or
     string keeps attribute access until WP-41.

Silent wrong results violate the PRD's determinism and safety intent (§1.1). An
oracle mismatch is recorded as `translates_wrong`, never `translatable`.

### 3.3 Root causes of the explicit failures

| Gap | Error today | Queries blocked (first failure) | Raise site |
| --- | --- | --- | --- |
| Comma-separated pattern parts in a `MATCH` after `WITH` | `Only a single pattern part is supported in v0` | IS2 IC3 IC4 IC6 IC10 BI10 BI11 BI14 | `_translate_v0/core.py:2314`, `:2529` |
| `shortestPath()` / `allShortestPaths()` syntax | `CYPHER_SYNTAX_ERROR` | IC1 IC13 IC14 | grammar |
| `CALL { … }` subquery syntax | `CYPHER_SYNTAX_ERROR` | BI4 BI16 (BI15) | grammar |
| GDS procedures (`CALL gds.…` with dotted `YIELD`) | `CYPHER_SYNTAX_ERROR` / `UNSUPPORTED` | IC14 BI15 BI19 BI20 | grammar / procedure registry |
| Multi-type hop `[:A\|B]` | `Exactly one relationship type is required` | IC12 | `core.py:4016` |
| Aggregate inside an expression | `Unsupported function in v0: collect` | IC7 BI8 | `core.py:5316` |
| Updating clause after `WITH … UNWIND … MATCH` | `Updating clauses are not supported in v0` | INS1 INS4 INS6 INS7 | `core.py:1378`, `:1421` |
| `DETACH DELETE` of `OPTIONAL MATCH` variables | `DELETE targets 'x', which is not bound by the first MATCH pattern` | DEL1 DEL4 | `writes.py` |
| Unlabeled start node with an ambiguous PG domain | `A single label is required` | IC5 IC9 (PG only) BI2 | `core.py:3796` |
| Unlabeled endpoint across a polymorphic edge (PG), e.g. `(c:Comment)-[:REPLY_OF]->(m)`, whose range is a union | `A single label is required` | IS2 IC5 IC9 (PG only, found 2026-10-24) | `core.py:3796`; `MappingResolver.infer_endpoint_label` returns `None` for unions |
| `datetime()` / `duration()` / `date()` | `Unsupported function in v0: datetime` | IC10 BI2 BI17 … | `core.py:5316` |

Constructs that **already translate** and need only execution evidence:

- unbounded and zero-hop variable length (`*`, `*0..`)
- pattern predicates in `WHERE NOT (…)` and as values in `RETURN`
- label-predicate expressions (`m:Comment AS isComment`)
- list comprehension over `nodes(path)`
- disconnected comma patterns in the *first* `MATCH`

### 3.5 Defects found by execution against the oracle (2026-10-24)

The execution tier (WP-33) runs every read the transpiler accepts against the
Neo4j oracle's snapshots. Its first run:

- **IS1, IS3 and IC11 match** the oracle on both layouts, for every binding.
- **BI18 returns wrong rows on both layouts (`translates_wrong`).** The
  pattern predicate `NOT (person1)-[:KNOWS]-(person2)` was lowered to
  "person1 has *any* KNOWS edge":
  - `_compile_pattern_predicate` (`_translate_v0/core.py`) discards the end
    node's variable, so a bound `person2` never constrains the existence
    subquery. Every `person1` has friends, so the result is empty.
  - The same function also ignores every relationship after the first, a PG
    target label, and properties on the target node.
  - This is a silent wrong result of the kind the translation tier cannot see.
  - **Fixed 2026-10-24.** `_compile_pattern_predicate` now compiles every
    hop, correlates a bound end node, checks end and start labels in both
    styles, and filters node and relationship properties. BI18 now matches the
    oracle on both layouts. Seven TCK negative scenarios
    (`Pattern1/[10]`, `List6/[6]`) moved from `translatable` to
    `correct_rejection`.
  - **Open:** the EXISTS/COUNT subquery compiler (`_compile_subquery_body`)
    shares the first-hop-only shape, and ignores a labelled or propertied
    named target's properties. It is not exercised by an LDBC read yet.
- **IC5 on LPG emits AQL that ArangoDB rejects (`execution_error`, ERR 1511:
  `forum` assigned twice).** The `WITH DISTINCT … collect() … OPTIONAL MATCH`
  pipeline is mis-lowered: the `OPTIONAL MATCH` becomes an inner `FOR`, and
  the aggregation boundaries are stacked. PG refuses the same query
  (`fails_closed`), and LPG should too.
  - **Fixed 2026-10-26**, with the OPTIONAL MATCH defects below. IC5 on LPG
    now matches the oracle.

**Writes (WP-33 part 3, 2026-10-24).** Each write's post-state delta is
compared with the oracle's:

- **INS2, INS3, INS5 and INS8 match on both layouts.**
- **DEL2, DEL3, DEL5 and DEL8 lose the end node of the pattern, which
  destroys data.**
  - For example, `MATCH (:Person {id: $personId})-[likes:LIKES]->(:Post {id:
    $postId}) DELETE likes` drops both the `{id: $postId}` filter and the
    `:Post` label.
  - The AQL then removes *every* LIKES edge of the person, to posts and
    comments.
  - It surfaced only as `ERR 1552`, because the harness passes `postId` as a
    bind variable that the AQL never declares. Without that tripwire the
    delete "succeeds".
  - **Fixed 2026-10-24.** `_translate_mutating_query`
    (`_translate_v0/writes.py`) now filters end-node labels and properties
    and relationship properties, as the read path does. It refuses
    (`UNSUPPORTED`) a write after more than one reading clause, or after a
    `MATCH` with several pattern parts, instead of ignoring them.
    - DEL2, DEL3, DEL5 and DEL8 now match the oracle on both layouts.
    - DEL1 and DEL4 still fail closed, now on that refusal.
- **DEL7 is wrong in two ways.**
  - The unbounded `*0..` is silently capped at `0..10`.
  - The `REMOVE` after the traversal then fails with ERR 1579 on PG, and on
    LPG it leaves the comment in place (`translates_wrong`).

**Exposed by WP-36 (2026-10-26).** Once multi-part MATCH translated, these
reads got past their first blocker. The execution tier shows they then meet
**pre-existing** defects. All of them fail loudly (`execution_error`), not
silently:

- **LPG variable-length relationships check only the last edge's type.**
  This affects every LPG mapping, not just LDBC.
  - `[:REPLY_OF*0..]` compiles to `FOR v, r IN 0..10 … @@edges FILTER
    r.type == …`, so the traversal walks every relationship type in the
    shared edge collection.
  - The result is wrong paths, and an exponential blow-up: IS2 on LPG is
    killed at the 30 s cap.
  - `*0..` is also silently capped at 10 hops, and the zero-hop row is
    filtered out, because `r` is null there.
  - **Highest priority:** where such a query finishes, it returns silently
    wrong rows.
  - **Fixed 2026-10-26.** A post-pass
    (`_translate_v0/formatting.py::_guard_varlength_edge_types`) adds
    `PRUNE r != null AND r[@tf] != @tv` to every variable-length traversal
    whose edge carries a type filter, and lets the zero-hop row (`r == null`)
    through.
    - IS2 on LPG no longer times out. Its rows then exposed `coalesce()`
      (next item).
    - The silent 10-hop cap on open-ended ranges is kept, but every capped
      translation now carries a warning.
- **`coalesce()` returned its first argument, null or not.** It was emitted
  as `FIRST(REMOVE_VALUES([a, b], null))`, but `REMOVE_VALUES` takes an
  array of values, so nothing was removed.
  - Every `coalesce` with a null first argument was wrong, e.g. IS2's
    `coalesce(message.imageFile, message.content)` on text posts.
  - **Fixed 2026-10-26** with AQL's `NOT_NULL()`. IS2 on LPG now matches the
    oracle, and no `translates_wrong` cell remains.
  - DEL7 on LPG now fails loudly (ERR 1202) instead of leaving the comment
    behind.
- **A `CASE` in a `WITH` projection is emitted as raw `CASE WHEN … END` in a
  `LET`.** AQL has no such expression (ERR 1501; IC3 and IC4).
  - **Fixed 2026-10-27:** every `CASE` was affected, not only those in a `WITH`
    (IS7 has one in its `RETURN`). `CASE` now compiles to nested ternaries.
- **A variable is used after a `COLLECT` has dropped it, or before its `LET`.**
  - IC4 groups by `tag` after `COLLECT tag_1 = tag`.
  - IC5 sorts by `forum.id` after `COLLECT`, and filters on `otherPersons`
    before defining it.
  - AQL then reads the name as a collection (ERR 1203; IC5, IC6, BI11).
  - BI11 and IC5 (LPG) are fixed by compiling the pipeline in query order
    (2026-10-26, next block). IC4 and IC6 remain.

**Found by BI12 on LPG (2026-10-26).** BI12 became translatable once LPG
supertypes landed (WP-35 stage 1), and the oracle showed 9 rows where Neo4j
returns 10. The cause was `OPTIONAL MATCH`, and fixing it uncovered three more
silent-wrong defects. All four are fixed, and each has a live regression test
(`tests/integration/test_optional_match_live.py`):

- **An `OPTIONAL MATCH` after `WITH`, or between `MATCH` and `WITH`, was an
  inner join.** It became a plain `FOR`, so a person with no message vanished.
  BI12 lost its `messageCount = 0` group.
- **Everywhere else, `OPTIONAL MATCH` kept only its first match.** It compiled
  to `LET f = FIRST(...)`.
  - `MATCH (u) OPTIONAL MATCH (u)-[:FOLLOWS]->(f) RETURN u, f` returned one
    followee per user, and `count(f)` was at most 1.
  - A node-only `OPTIONAL MATCH` added `LIMIT 1`.
  - A multi-part optional pattern took independent `FIRST()`s, not one joint
    match.
  - **Fix:** the pattern compiles through the bound-MATCH compiler into a
    subquery of match rows. The outer query iterates those rows, or one empty
    row when there are none:
    `FOR _opt_row IN (LENGTH(_opt_rows) > 0 ? _opt_rows : [{}])`.
- **`count(x)`, `count(DISTINCT x)` and `collect(x)` counted and kept nulls,
  and `sum()` over no rows was null.** AQL's `COUNT`/`COUNT_DISTINCT` count
  nulls, so `count(u.age)` counted users without an age.
  - **Fix:** `_translate_v0/aggregates.py` lowers `count(x)` to
    `SUM(x == null ? 0 : 1)`, falling back to 0. `count(DISTINCT x)` subtracts
    the one distinct null, and `collect()` drops nulls.
- **A `MATCH` between two `WITH`s was compiled above both.**
  - `MATCH (u) WITH u LIMIT 1 MATCH (u)--(f) WITH f …` applied the `LIMIT` to
    the traversal's rows.
  - An `UNWIND` there was dropped.
  - A second `WITH` keyed its variables by their AQL names (`country_1`), so a
    later `MATCH` saw them as unbound.
  - **Fix:** the pipeline now compiles clause by clause, in query order. BI11
    now matches the oracle on both layouts.
  - Fourteen TCK scenarios had been counted as translatable while silently
    mistranslated. In seven, a leading `OPTIONAL MATCH … WITH` became a plain
    `MATCH`. In the other seven, an `UNWIND` was dropped. All fourteen now
    translate correctly.
- **Executing TCK: 1,700 scenarios pass, against 1,670 on `main`, and none is
  lost.** The comparison also exposed three RETURN defects that `FIRST()` had
  hidden, all now fixed:
  - An unaliased variable bound under another AQL name came back under that
    name (`r_1`, TCK Match7[26]).
  - A lone `RETURN type(r)` came back as an object `{type: …}` (Graph4[1],
    Graph4[4]).
  - An undirected self-loop matched twice (Match7[24]).
- **Open, found while fixing the above. Neither is exercised by LDBC, which
  binds parameters.**
  - A string literal in an inline property map matches case-insensitively.
    `(t:Tag {name: 'x'})` compiles to `LOWER(t.name) == LOWER('x')`
    (`_compile_node_pattern_properties`), so it matches more nodes than Neo4j.
  - `RETURN f.name AS f ORDER BY f` sorts by the node `f`, not by the
    projected alias.

### 3.4 Upstream reference quirks (to report to LDBC, not to fix here)

**INS1** (`interactive-update-1.cypher`) creates
`[:WORKS_AT]` (the schema says `WORK_AT`) and a property `languages` (the schema
says `speaks`). Neo4j accepts both silently. A mapping-driven transpiler rejects
the first (`MAPPING_NOT_FOUND`), which is *correct* behaviour. We keep the
verbatim file, add a corrected variant, and file an issue upstream (WP-32).

---

## 4. Scope decisions (recommendations for review)

| # | Decision | Recommendation | Alternatives |
| --- | --- | --- | --- |
| D1 | Workloads | **Interactive v2 (all 37 ops) first, then BI (20).** Interactive v1 is skipped. | BI first (more aggregation coverage, but blocked on temporal and `CALL {}`). |
| D2 | Physical layouts | **PG and LPG, both mandatory** (as requested). Hybrid is optional, in Phase 3 (§9). **Decided 2026-10-26: hybrid is added after WP-35.** | Add hybrid now. It mirrors LDBC's CSV split: `Place`/`Organisation` as typed collections, `Post`/`Comment` separate. |
| D3 | Query text | **Vendor the reference Cypher verbatim**, pinned to the upstream commit. Never edit it. Adaptations (GDS replacements, upstream-bug corrections) go in separate `variants/` files with a stated reason. The ledger reports verbatim and variant outcomes separately. | Rewrite into a portable subset. Rejected: it hides exactly the gaps we want to find. |
| D4 | Oracle | **Neo4j 5.x, pinned to `5.26.27-community`**, runs the verbatim queries on the same data and parameters. Results are **recorded as committed snapshots** for SF0.003, so the integration tier needs no Neo4j. A `RUN_CROSS` job re-verifies them against live Neo4j. | (a) LDBC driver validation mode: Java, heavy, and still needs a reference run. (b) PostgreSQL reference: an independent second oracle, worth adding for disputed cases (§9). |
| D5 | Timestamp encoding | **Load each workload in its reference encoding.** Interactive uses epoch-ms numbers; BI uses ISO-8601 strings, declared temporal in the mapping. Neo4j gets the same encoding the reference expects. | One encoding for both. Rejected: the Interactive queries do arithmetic on timestamps and the BI queries use temporal accessors. |
| D6 | Data in repo | **Download on demand, verified against pinned content digests** (SHA-256 over each entity's sorted, decompressed rows; LDBC republishes the archives with new part-file names, so archive hashes are not stable), cached under `~/.cache/arango-cypher/ldbc/` and `actions/cache` in CI. Only mappings, parameters and snapshots are committed. | Vendor the 2.4 MB sample. Smaller CI surface, but it is a generated artifact with its own provenance. |
| D7 | Scale factors | **SF0.003 in CI** (integration tier). **SF0.1 and SF1 on the benchmark VM** for performance (decisions 11, 12; SF0.1 nightly was the original plan). SF10 runs ad hoc on dedicated hardware. | SF0.1 in CI (~30 MB; slower to load). |
| D8 | Supertype modeling (the biggest design choice) | Extend the **conceptual** schema with `supertypes` / `subtypeOf` **in arango-query-core** (fix upstream, don't fork). Physically: **LPG** uses a `labelsField` array (`"Message" IN n.labels`) with an array index. **PG** treats the supertype as the union of its members' collections: an `IS_SAME_COLLECTION` any-of filter on traversal targets, and a branch union for scan starts. The benchmark then *measures* what that union costs. **Decided 2026-10-26: this, staged (LPG first, then the PG union).** | (a) Require a hybrid layout for supertypes (a `message` collection plus a type field), which dodges the PG question. (b) Expand supertype queries into `UNION ALL` at the Cypher level. |
| D9 | GDS / APOC queries (IC14 BI10 BI15 BI19 BI20) | **Report as out-of-subset verbatim.** Add `arango` dialect variants (weighted shortest path via `K_SHORTEST_PATHS`/`arango.shortest_path`) with *results* checked against a pure-Cypher or SQL reference where feasible. | Exclude entirely. |
| D10 | Performance comparison | Report per query × layout × SF: translation time, plus Arango execution p50/p95/p99. Neo4j on identical container limits is **indicative only** and carries the disclaimer. A hand-written AQL baseline for a subset measures the "transpilation tax" (WP-44). **Decided 2026-10-26:** SF1+ runs on an Arango Platform deployment or a cloud VM on 3.12, and the numbers stay internal. | Arango only. |

---

## 5. Architecture

### 5.1 Layout (mirrors the `tests/tck/` precedent)

```
tests/ldbc/
  queries/
    UPSTREAM.md            # repo URLs, pinned commits, refresh procedure
    LICENSE, NOTICE        # Apache-2.0 text and attribution, with the queries
    interactive_v2/        # verbatim *.cypher (37)
    bi/                    # verbatim *.cypher (20)
    variants/              # adapted copies: <op>.<reason>.cypher, each with a header comment
  manifest.yml             # op id → file, workload, kind, sha256 pin, out_of_subset
  corpus.py                # manifest loader, hash check, --rehash
  schema.py                # LDBC labels, supertypes, CSV files and column orders (single source)
  datasets.py              # fetch or generate + content-digest verification + cache
  datagen.py               # LDBC Datagen in Docker, pinned commit (SF0.1; decision 12)
  loader.py                # LDBC CSV → Arango PG | LPG (the Neo4j loader is WP-33)
  params.py                # deterministic picker for SF ≤ 0.1: python -m tests.ldbc.params --scale sf0.1
  params/
    sf0.003.yml, sf0.1.yml # committed bindings (several per op)
  expected/sf0.003/<layout-independent>/<op>/<binding>.json   # Neo4j snapshots
  ledger.py                # translation tier → translation_ledger.tsv (reuses the TCK diff)
  translation_ledger.tsv   # op × layout: translatable | fails_closed | syntax_error | out_of_subset
  execution_ledger.tsv     # WP-33: op × layout: executes | matches_oracle | translates_wrong …
  test_ldbc_*_unit.py      # default suite, no DB (loader, ratchet, …)
  bench.py                 # perf runner CLI: python -m tests.ldbc.bench --sf 0.1 --layout pg,lpg
  report.py                # results JSON → docs/ldbc_results.md (with disclaimer)
tests/integration/        # DB-backed suites live here, so they inherit the
  test_ldbc_load.py        #   local-database guard in tests/integration/conftest.py
  test_ldbc_execution.py   # RUN_INTEGRATION: execute + compare to snapshots
  test_ldbc_crossvalidate.py  # RUN_CROSS: live Neo4j re-verification
tests/fixtures/mappings/
  ldbc_pg.export.json
  ldbc_lpg.export.json
```

### 5.2 The outcome model

This extends the TCK's four-outcome contract (`cypher_capability_matrix.md`).
For each operation × layout:

1. `parses`
2. `translates`
3. `executes`
4. `matches_oracle`

There are three non-passing terminal states. Each must state its reason:

- `fails_closed` (an explicit `UNSUPPORTED`, the acceptable failure)
- `translates_wrong` (an oracle mismatch, which is a **defect**)
- `out_of_subset` (GDS or APOC)

The two tiers are kept in two ledgers, as the TCK keeps its dry run apart from
its execution run:
- **The translation ledger** needs no database. It runs in the default suite.
- **The execution ledger** runs in the integration tier.

As in the TCK, the ratchet fails CI if any cell **leaves** a passing state or an
improvement goes unrecorded. A `translates_wrong` cell can never be grandfathered
as passing.

### 5.3 Result comparison

- Reuse the normalisation in `tests/integration/test_movies_crossvalidate.py`:
  int/float folding, null-versus-missing, and recursive normalisation. Lift it
  into a shared helper rather than copying it.
- **Ordering contract.** Every LDBC read ends in `ORDER BY … LIMIT` with
  tie-breakers. Compare *ordered* when the manifest marks the ordering total.
  Otherwise compare as a multiset: the canonical-row SHA-256 multiset signature
  pattern, plus a diff.
- Nodes are never returned whole by LDBC reads (they project properties), so no
  `_id`/`_key` normalisation is needed. Write operations are compared by their
  **post-state**: a fixed set of read-back queries run on both systems after the
  operation.

### 5.4 Loader

- **PG.** One vertex collection per concrete label (`person`, `post`,
  `comment`, …) and one edge collection per relationship type.
  - `_key` is `str(id)`.
  - `_from`/`_to` resolve to the concrete member collection.
  - Polymorphic endpoints come from the CSV file name, e.g.
    `Comment_replyOf_Post`.
- **LPG.** All nodes go in `nodes`; all edges go in `edges`.
  - Each node gets `type` (its concrete label) and `labels` (all of its labels,
    e.g. `["Message", "Comment"]`).
  - `_key` is `"<Type>_<id>"`, because ids collide across labels.
  - Each edge gets `type`.
- **Indexes (both layouts)** mirror the reference DDL:
  - Unique `id` per label: for PG, a unique index per collection; for LPG, a
    persistent index on `[type, id]`.
  - `firstName`, `name` and `creationDate`.
  - LPG also gets edge VCIs on `[_from, type]` and `[_to, type]` (the
    translator's traversal hint picks the one led by the traversal's anchor),
    a `labels[*]` array index, and type-led `[type, …]` node indexes.
- **Arrays.** `speaks` and `email` are split on `;` into arrays.
- **Empty fields** are omitted, not stored as `""`, matching the reference
  import's `--ignore-empty-strings=true`, so `coalesce(m.imageFile, m.content)`
  behaves identically on both engines.
- **Endpoints.** A `Place` or `Organisation` endpoint is resolved to its
  concrete label per row. A dangling endpoint, in the CSV or in the loaded
  database, fails the load.
- **Integrity.** The loader is idempotent, reports row counts per
  entity, and asserts that **PG count == LPG count == CSV rows** for every
  entity. A mismatch fails loudly; nothing is dropped silently.
- **Bulk path.** `import_bulk` in batches. `arangoimport` is optional for
  SF ≥ 1.
- **Neo4j (WP-33).** Batched `UNWIND` for SF ≤ 0.1, mirroring the reference's labels
  (`:Message:Comment`). `neo4j-admin import`, with the reference's headers, for
  SF ≥ 1.

### 5.5 Mapping acquisition is also under test

PRD §12 requires live-database coverage for §7. After loading, the harness runs
`acquire_mapping` against both layouts and **diffs the acquired bundle against
the committed fixture.** The committed fixture is the contract. A difference is
either an analyzer defect (filed upstream against `arangodb-schema-analyzer`) or
a fixture error. The benchmark therefore also measures whether the analyzer can
detect LDBC's label hierarchy, which today it cannot.

**Status (2026-10-29): implemented** (`tests/ldbc/mapping_diff.py`,
`tests/integration/test_ldbc_mapping_acquisition.py`), on both data sets and
both layouts, with analyzer 0.15.1 and 0.17.0. Labels, collections, styles,
type values and relationship types agree. The remaining differences are
filed: arangodb-schema-analyzer #21 (every property typed `string`), #43
(supertypes not detected), #44 (no domain/range for relationships between
several labels) and #46 (a property on only some documents is dropped). The
`string` typing also led to arango-cypher #76: declared property types no
longer choose what an operator computes.

---

## 6. Work packages

Sizes: **S** ≤ 2 days · **M** ≤ 1 week · **L** 1–2 weeks. ⚡ marks critical-path
packages. Each WP ships as one to three PRs of five files or fewer, each green
on its own (incremental-over-atomic).

### Phase 0 — Harness and baseline (no transpiler changes)

#### WP-31: Data sets and loader ⚡ (M)
- **Scope:**
  - `tests/ldbc/schema.py`: labels, supertypes, files and column orders.
  - `tests/ldbc/datasets.py`: SF0.003 sample URLs (both encodings),
    content-digest pins and cache.
  - `tests/ldbc/loader.py`: PG and LPG loaders. The Neo4j loader moved to
    WP-33, which owns the oracle.
- **Tests:**
  - Unit tests for CSV parsing: quoting, `;` arrays, both timestamp encodings,
    and polymorphic endpoint resolution.
  - An integration test that loads SF0.003 into both layouts and asserts the
    per-entity counts from §2.3.
  - An idempotence test (load twice, same counts).
- **Acceptance:** `RUN_INTEGRATION=1 pytest tests/integration/test_ldbc_load.py`
  is green. Load time for SF0.003 is recorded.
- **Status (2026-10-24): implemented.**
  - 48 unit tests in `tests/ldbc/test_ldbc_loader_unit.py`; 18 integration
    tests in `tests/integration/test_ldbc_load.py`.
  - SF0.003 loads in about 2–3 s per layout (local Arango 3.11, Apple
    silicon).
  - A mutation check (dropping one document) is caught by the count guard in
    both layouts.
  - CI caches the samples (`actions/cache`, keyed on the catalog).

#### WP-32: Corpus, mappings and translation ledger ⚡ (M)
- **Scope:**
  - Vendor the queries with `UPSTREAM.md` and `NOTICE`.
  - Write `manifest.yml`.
  - Write `ldbc_pg` and `ldbc_lpg` mapping fixtures, generated from
    `schema.py` and the loader's index specs (`tests/ldbc/mappings.py`), with
    union domain/range for polymorphic edges. Supertypes have no mapping, so
    they fail closed (`MAPPING_NOT_FOUND`); no translator change is needed for
    that (§3.2).
  - The translator refuses temporal accessors on properties declared temporal
    (`_translate_v0/temporal_guard.py`).
  - `ledger.py`, `conformance_ledger.tsv` and `test_ldbc_ratchet.py` (default
    suite, no DB).
  - The INS1 corrected variant, and the upstream issue filed.
- **Acceptance:**
  - The ratchet runs in under 5 s.
  - The ledger reproduces §3.1, minus the silent-wrong cells, which now show
    `fails_closed`.
- **Status (2026-10-24): part 1 implemented** (the fixtures and the temporal
  guard).
  - `tests/ldbc/test_ldbc_mappings_unit.py` checks that the committed fixtures
    equal the generated ones, and that supertypes fail closed.
  - `tests/test_translate_temporal_guard.py` covers the guard. A mutation
    check (guard removed) fails 10 of its cases.
  - The integration suite checks that every loaded edge's endpoint labels fall
    within the declared domain and range.
  - **Part 2 (2026-10-24): implemented.**
    - The 57 operations are vendored verbatim with LICENSE and NOTICE, and
      pinned by SHA-256 in `manifest.yml`.
    - Out of subset: IC14, BI10, BI15, BI19, BI20.
    - The INS1 `schema-fix` variant is added.
    - `translation_ledger.tsv` and `test_ldbc_ratchet.py` reuse the TCK diff
      (`diff_ledger(passing=…)`). The ledger builds in under a second.
    - Mutation checks: an edited vendored file and a fabricated regression
      both fail the suite.
  - **Not done:** the upstream LDBC issue for INS1 is drafted, but awaits
    approval before filing.

#### WP-33: Oracle snapshots and execution tier ⚡ (M)
- **Scope:**
  - **Part 1:** pin Neo4j, and load the oracle (`tests/ldbc/neo4j_loader.py`).
  - A snapshot recorder (`python -m tests.ldbc.record --sf 0.003`).
  - `test_ldbc_execution.py`: Arango PG and LPG versus the snapshots.
  - `test_ldbc_crossvalidate.py`: live Neo4j re-verification.
  - Lift the shared result-equivalence helper.
  - Write operations: reset per test (a reload of SF0.003 takes seconds), then
    post-state comparison.
- **Acceptance:**
  - All four outcome columns are populated for every operation × layout.
  - A deliberately corrupted snapshot makes the test fail. This is a
    mutation check, so the gate is shown not to be vacuous.
- **Status (2026-10-24): part 1 implemented.**
  - **Neo4j is pinned to `5.26.27-community`.** That is the 5.26 LTS build the
    floating `5-community` tag already resolved to, locally and in CI. The plan
    had said 5.20, the reference implementations' version. Pinning down to it
    would have downgraded the engine the existing suites run on. Cypher is
    backward compatible within 5.x, and Movies plus Northwind cross-validation
    pass 34/34 on the pinned build.
  - **The Neo4j loader** reads the same rows as the ArangoDB loader
    (`loader.iter_graph`). It applies the reference's supertype labels and DDL,
    and keeps epoch-ms timestamps as integers but ISO ones as
    `DATETIME`/`DATE`. It checks Neo4j's write counters on every batch and the
    per-label totals at the end.
  - SF0.003 loads in 3–4 s.
- **Status (2026-10-24): part 2 implemented** (the oracle snapshots and the
  read execution tier).
  - `tests/ldbc/oracle.py` records the oracle's rows for all 120 read
    bindings into `expected/sf0.003/<op>.json`, together with each query's
    SHA-256. Recording is deterministic.
  - `collect()`-built columns (IC1, IC12) are declared `unordered_columns` in
    the manifest and compared as multisets. Their element order is undefined
    in Cypher; Neo4j's varied between runs.
  - `tests/ldbc/execution.py` and `execution_ledger.tsv` hold the per-cell
    outcomes. They are ratcheted by `tests/integration/test_ldbc_execution.py`,
    which needs ArangoDB only.
  - `tests/integration/test_ldbc_crossvalidate.py` (`RUN_CROSS`) re-records
    from live Neo4j and fails on drift.
  - The comparison moved to `tests/helpers/result_equivalence.py`, shared with
    the Movies and Northwind suites, which still pass 34/34.
  - The first findings are in §3.5.
- **Status (2026-10-24): part 3 implemented** (writes, compared by
  post-state).
  - **Bindings.** `tests/ldbc/writes.py` picks one binding per insert and
    delete over the initial snapshot: existing endpoints, and fresh ids for
    new entities. They are chosen on the oracle so that each one changes the
    graph. The update streams are not replayed: at SF0.003 several deletes
    target entities only the insert stream creates.
  - **Comparison.** Each write runs in a rolled-back transaction on both
    engines. The canonical delta (nodes and edges added or removed) is
    recorded for Neo4j and compared for ArangoDB.
  - **Findings** are in §3.5.
  - **Local runs.** Neo4j and ArangoDB each need about 1.1 GB with the repo's
    settings. On a Docker VM shared with many other containers, both were
    OOM-killed (exit 137) during runs. That was host contention, not a query.
    **Superseded 2026-10-26** by the LDBC-9 lifecycle: `tests/ldbc/stack.py`
    starts `docker-compose.ldbc.yml` (capped memory, own ports) around each
    run and removes it after. The manual overrides below were the stopgap:
    - Neo4j: `NEO4J_server_memory_heap_max__size=512m` and
      `..._pagecache_size=128m`;
    - ArangoDB: `ARANGODB_OVERRIDE_DETECTED_TOTAL_MEMORY=1536M`.

#### WP-34: Substitution parameters (S)
- **Scope:**
  - `params/generate.py`: a seeded, deterministic picker for SF ≤ 0.1. It
    chooses bindings that yield non-empty results, plus at least one
    empty-result binding per operation, because the empty case is an edge case
    too.
  - Commit `params/sf0.003/*.yml`.
  - Document how to drive LDBC `paramgen` for SF ≥ 1 (consumed by WP-43).
- **Acceptance:** at least 3 bindings per read; regeneration is byte-identical.
- **Status (2026-10-24): reads implemented** (`tests/ldbc/params.py`,
  `params/sf0.003.yml`).
  - The 36 reads the oracle can run each have their bindings: 35 have 3
    non-empty bindings, plus an empty one where one exists.
  - **BI17 is empty for every binding at SF0.003.** All 422 comment tags were
    tried. That is recorded, with how it was established, in `EMPTY_AT_SCALE`.
  - Generic sources missed IC3 and BI16, so they get grouped sources:
    - IC3: a friend who posted from two foreign countries;
    - BI16: one person's two tag/day pairs.
  - Regeneration is byte-identical.
  - **SF0.1 (2026-10-30, decision 12):** `params/sf0.1.yml`, chosen over the
    Datagen-generated data sets with `--scale sf0.1`. All 36 reads have 3
    non-empty bindings; BI17, empty at SF0.003, has rows at SF0.1. Loading
    SF0.1 into the oracle needed the database wipe batched (relationships
    first, `LIMIT` per transaction): the `IN TRANSACTIONS` form exhausted
    Neo4j's transaction memory pool at the 512 MB heap.
  - **Write bindings** come from the `inserts/` and `deletes/` streams, and
    land with the post-state comparison (WP-33 part 3).

**Milestone M1 (end of Phase 0).** We have a reproducible, ratcheted baseline for
57 operations × 2 layouts at SF0.003, with every failure filed as an issue tagged
`ldbc`. This alone yields a correctness report. It needs no transpiler work
beyond the fail-closed guard.

### Phase 1 — Correctness gaps, ordered by operations unlocked

#### WP-35: Label hierarchies (supertypes) ⚡ (L) — implemented 2026-10-26 (LPG) and 2026-10-27 (PG)
- **Status:**
  - **Core.** arango-query-core 0.2.5 carries the hierarchy: `subtypes`,
    `SUPERTYPE_STYLES` (`MULTI_LABEL`, `UNION`), `is_supertype` and
    `supertype_members`.
  - **LPG (stage 1).** Supertypes map as `MULTI_LABEL` over the labels array.
    LPG translatable went from 19 to 33, and LPG matches_oracle from 15 to 25,
    including BI12, BI5 and IC9. No cell is `translates_wrong`.
  - **PG (stage 2, 2026-10-27).** Supertypes map as `UNION` over the members'
    collections (`arango_cypher/_translate_v0/supertypes.py`). A scan becomes
    a `UNION` of per-member subqueries, with the inline properties pushed into
    each branch. A traversal target tests membership unless the edge already
    guarantees it. PG translatable went from 16 to 29, and PG matches_oracle
    from 13 to 23 (BI5, BI7, BI9, BI12, IC2, IC8, IC9, IS4, IS5, IS6). No cell
    is `translates_wrong`.
  - **Newly loud:** BI3 (ERR 1511), BI6 (`like` is an AQL keyword) and IS7
    (raw `CASE WHEN`), on both layouts. DEL6 fails with ERR 1202 on LPG; on PG
    it is refused, because a write to a union-matched variable names no single
    collection.
  - **The three loud cells, fixed 2026-10-27.** BI3, BI6 and IS7 now match the
    oracle on both layouts, as do IC4 on both layouts and IC3 on LPG. PG
    matches_oracle went from 23 to 27 and LPG from 25 to 30, and no cell is
    `translates_wrong`. The causes:
    - `CASE` was emitted verbatim (IS7, IC3, IC4).
    - Every unnamed node was called `v`, so two of them clashed (BI3). A node
      the user named `v`, `u` or `r` later in a pattern joined the unnamed
      one, which silently returned no rows; there was no LDBC cell for this.
    - The grouped `RETURN forum.id, person.id` emitted two `id` keys, and its
      `ORDER BY forum.id` sorted by `person.id` (BI3). Neither raised an error.
    - Names AQL reserves (`like` in BI6, but also `filter`, `sort`, `in`, …)
      were emitted bare.
  - **Fixed on the way:** with a `COLLECTION` primary label, an extra supertype
    label was only warned about, so `(x:Person:Message)` returned every Person.
    It is now tested, and an extra supertype the primary implies adds no filter.
- **Upstream (`arango-query-core`):**
  - Conceptual `subtypeOf` / `supertypes` in `MappingBundle`.
  - A physical `labelsField` for LPG.
  - Resolver APIs to enumerate member collections and labels.
  - A pin bump in this repo.
- **Upstream (`arangodb-schema-analyzer`):** detect a labels array or
  `_from`-type co-occurrence and emit the hierarchy. Filed as an issue, not
  blocking, because the fixtures carry the contract.
- **Transpiler:**
  - LPG: `"Message" IN n.labels`.
  - PG: the union lowering described in D8.
  - The label-predicate expression `m:Comment` is lowered against the labels
    field rather than the type field.
  - `WITH` collection declarations cover all member collections.
- **Tests:**
  - Goldens for scan, traversal source and target, and label predicate, in both
    layouts.
  - Execution evidence from the ledger.
- **Unlocks:** the 30 operations in §2.5. This WP gates most of the rest.

#### WP-36: Multi-part `MATCH` after `WITH` (M) — implemented 2026-10-26
- **Scope:** comma-separated pattern parts in a bound `MATCH`
  (`_compile_match_from_bound`, `core.py:2295`), with shared variables joined.
- **Unlocks:** IS2 IC3 IC4 IC6 IC10 BI10 BI11 BI14 (first-failure basis).
- **Status (2026-10-26): implemented.**
  - A `MATCH` after `WITH` compiles each comma-separated part in order.
  - A multi-part first `MATCH` may precede `WITH`; it reuses the
    disconnected-parts compiler.
  - A hop ending at an already-bound variable is correlated by `_id` instead
    of rebinding it. That removes IC5's ERR 1511.
  - An `ORDER BY` after `WITH` that names an out-of-scope variable is refused.
    Before, the translation sorted by it, because AQL never drops it from
    scope.
  - **TCK:** 9 scenarios improve, and 30 negative scenarios
    (`WithOrderBy3/[8]`) that used to "translate" are now correctly rejected.
  - **LDBC:** IC4 (both layouts), and IS2, IC3, IC6 and BI11 (LPG; BI11 also
    on PG) now translate. They then meet the defects in §3.5.

#### WP-37: Aggregates nested in expressions (S) — implemented 2026-10-28
- **Scope:** `head(collect(…))`, `size(collect(…))` and similar. Hoist the
  aggregate into `COLLECT … AGGREGATE` / `INTO`, then apply the function.
- **Unlocks:** IC7 BI8.
- **Status (2026-10-28): implemented.** Each nested aggregate is a COLLECT
  variable and the expression a post-COLLECT `LET`; `collect()` beside another
  aggregate groups `INTO` rows (`PUSH` in `AGGREGATE` was ERR 1574). Executing
  IC7 and BI8 against the oracle found more defects, fixed alongside:
  `toInteger()` did not truncate (#58); pattern comprehensions compiled only
  their first hop, ignored PG end labels and redeclared bound nodes (#59).

#### WP-38: Multi-type relationship hops `[:A|B]` (M) — implemented 2026-10-28
- **Scope:** closes the PRD §8.4 gap.
  - PG: multiple edge collections in one traversal.
  - LPG: `r.type IN [...]` with the VCI.
  - Also with variable length.
- **Unlocks:** IC12. Broadly useful beyond LDBC.
- **Status (2026-10-28): implemented** (`_translate_v0/multi_type.py`). IC12 also
  needed an unnamed start node and four hops in a MATCH after WITH; the
  four-hop limit there is gone (which also unblocks BI14), and those hops now
  keep their relationships distinct.

#### WP-39: `shortestPath()` / `allShortestPaths()` (M) — `shortestPath()` implemented 2026-10-28
- **Scope:**
  - Add the grammar rule (regenerate ANTLR).
  - Lower to AQL `SHORTEST_PATH` / `ALL_SHORTEST_PATHS` / `K_SHORTEST_PATHS`.
  - Support `length(path)` and `nodes(path)`, and the "no path" case
    (`-1` / empty).
- **Unlocks:** IC1 IC13, and the IC14 existence check.
- **Status (2026-10-28):** `shortestPath()` between bound nodes is a
  breadth-first traversal (`_translate_v0/shortest_path.py`); IC1 and IC13 match
  the oracle on both layouts. `allShortestPaths()` is still refused.

#### WP-40: Write pipelines (M) — implemented 2026-10-28
- **Status (2026-10-28): the deletes are done.** A DELETE-only query reads every
  matched row first, then writes each collection once (PRD §8.3). DEL1, DEL4,
  DEL6 and DEL7 match the oracle on both layouts. The inserts are done too
  (decided 2026-10-28: read first, one INSERT per collection, keys generated
  up front): INS1~schema-fix, INS4, INS6 and INS7 match on both layouts; INS1
  itself stays refused because it names `WORKS_AT`, which the schema lacks
  (the upstream bug, ldbc/ldbc_snb_interactive_v2_impls#10).
- **Scope:**
  - `MATCH … CREATE … WITH … UNWIND … MATCH … CREATE` (INS1 INS4 INS6 INS7).
  - `DETACH DELETE` over `OPTIONAL MATCH` variables (DEL1 DEL4).
- **LPG warning:** LPG puts every node in one collection and every edge in
  another. Multi-statement writes therefore hit **ERR 1579 (same-collection
  multi-write)** far more often than PG does. This is a genuine layout-dependent
  limitation, and it feeds PRD §16's "multi-statement execution model" question
  with concrete data.

#### WP-41: Temporal subset (L)
- **Scope:**
  - The mapping declares a temporal property type and its encoding: `epoch_ms`
    or `iso8601`.
  - The translator lowers:
    - `datetime({epochMillis:})` and `datetime('…')` parameters;
    - `.year`, `.month`, `.day` to `DATE_YEAR`, `DATE_MONTH`, `DATE_DAY` (AQL's
      `DATE_*` functions accept both encodings);
    - `duration({days:})` arithmetic to `DATE_ADD`;
    - `date()` truncation.
  - Accessors on non-temporal properties **fail closed** (closing §3.2 item 2).
- **Unlocks:** IC10 BI1 BI2 BI13 BI16 BI17. It also moves the TCK temporal
  category (14.6% today).
- **Status (2026-10-29), first half:** temporal values, functions and accessors
  (`_translate_v0/temporal.py`): `datetime()` / `date()` / `localdatetime()`,
  `.year` … `.epochMillis` on declared temporal properties, temporal functions,
  WITH variables and parameters (run-time choice: a parameter may be a map).
  An accessor on a property the mapping types as a string or number fails
  closed (§3.2 item 2). BI1, BI13, BI16 and IC10 match on both layouts (BI1 on
  PG also needed a later supertype scan to stay below the variables bound
  before it). arango-query-core 0.2.6 adds `PropertyInfo.encoding`
  (`epoch_ms` / `iso8601`).
- **Status (2026-10-29), second half:** `duration({…})` values and
  `t ± duration({…})`; comparisons between computed and stored temporal values
  in epoch milliseconds, index-friendly on an `epoch_ms` property; the
  execution harness declares each data set's encoding
  (`tests/ldbc/mappings.with_encoding`). BI17 matches on both layouts. BI2 also
  needed its OPTIONAL MATCH read from the bound `tag`
  (`_translate_v0/pattern_reversal.py`): it scanned every message per tag and
  ran past 30 s. WP-41 is complete for the LDBC queries.

#### WP-42: `CALL { … }` subqueries (M) — implemented 2026-10-29
- **Scope:**
  - Grammar support.
  - Lower to an AQL subquery with an imported-variable `WITH`.
  - `UNION ALL` inside the subquery body.
- **Unlocks:** BI4 BI16 (BI15 also needs GDS).
- **Status (2026-10-29):** `CALL { … }` compiles to an AQL subquery over the
  outer variables (`_translate_v0/subqueries.py`), with `UNION` / `UNION ALL`
  bodies. BI4 matches the oracle on both layouts; it also needed `DISTINCT`
  to keep the order of its rows (the top 100 forums). BI16 now parses and
  fails closed on `date(message.creationDate)` until WP-41.

**Milestone M2 (end of WP-35 to WP-40).** All 21 Interactive reads (except
IC10 and the GDS part of IC14) and all 16 updates reach `matches_oracle` on
**both** layouts. **Achieved 2026-10-29**, IC10 included. Verbatim INS1 fails
closed on its upstream schema bug and its schema-fix variant matches, which
M2 accepts (decision 7 in §10).

**Milestone M3 (after WP-41 and WP-42).** All BI operations without GDS or APOC
reach `matches_oracle`. **Achieved 2026-10-29.**

### Phase 2 — Performance

#### WP-43: Benchmark runner and report (M). It can start right after M1, on whatever passes.
- **Measurements:**
  - **Translation:** `translate()` with the cache cleared and with it warm;
    p50 and p95 per operation. The PRD v0.1 criterion of P95 < 50 ms for a
    single hop is re-checked on real queries.
  - **Execution:** per operation × layout × SF. Discard K warm-up runs, then
    take N bindings × R repeats. Report p50/p95/p99, mean, row count, and
    server-side `executionTime` from cursor stats (which excludes the client),
    with a per-query timeout recorded as `TIMEOUT`.
  - **Plan digest:** from `EXPLAIN`. Full collection scans, index usage and
    traversal options are stored, so plan regressions are diffable.
- **Environment capture:**
  - Arango and Neo4j versions, image digests, CPU, RAM and container limits;
  - the SF and its data set checksum;
  - the git SHA and the mapping fixture hash.
- **Output:** `results/<date>-<sf>-<host>.json` (a CI artifact, not committed),
  plus `docs/ldbc_results.md`, generated by `report.py`. It carries the §2.6
  disclaimer and attribution. A conformance test checks that every number in
  the doc matches a committed summary JSON (the published-results conformance
  pattern), so the prose cannot drift.
- **Where it runs:** the dedicated benchmark VM (decisions 9 and 11). CI
  only runs it as a smoke test.

- **Status (2026-10-29): the runner is implemented** (`tests/ldbc/bench.py`,
  `tests/ldbc/report.py`).
  - It times translation (cold and warm) and execution (server-side from the
    cursor statistics, and client) per read operation × layout × binding,
    after warm-up, with `TIMEOUT` past `--timeout`. The query results cache is
    off for the timed runs.
  - Each binding's row count is checked against the oracle's; a cell that
    differs is `rows_differ`, never timed numbers for a wrong answer.
  - It records the plan shape and digest, and the environment (server
    version and image, host, packages, data set digests, git SHA, mapping
    hashes, settings).
  - `report.py` renders Markdown with the disclaimer and attribution, checks
    a document against its run (conformance), and compares two runs: plan
    changes, server p50 moves beyond ×1.5 above 5 ms, and outcome changes.
  - A nightly GitHub workflow ran it twice on ArangoDB 3.12.12 (72 of 72
    cells timed). It was removed on 2026-10-30 (decision 11): this repository
    is public, so its artifacts would have published the numbers; those two
    runs were deleted.

#### WP-44: Optimisation loop and baselines (L, open-ended)
- **Scope:**
  - Triage the slowest operations per layout from the plan digests: missing
    indexes, scans that should be traversals, and filter placement (§7.2
    statistics).
  - Fix each in the transpiler with a golden plus a plan-shape assertion.
  - Optional hand-written AQL for IS1–IS7, IC2, IC8, IC9 and IC11 quantifies
    the transpilation tax.
  - Optional Neo4j timings on identical limits (indicative).
- **Acceptance:** each optimisation lands with a before and after row in the
  report, and a regression bound.
- **Done:**
  - *LPG supertype scans (2026-11).* `(m:Message {id: $id})` on LPG tested only
    the labels array, which only the labels array index serves, so a lookup by
    id read every Message (IS4–IS7 start this way). The members' type field is
    now tested beside it and the `(type, id)` index answers. The plan-shape
    regression bound is `tests/integration/test_supertype_lpg_index_live.py`;
    the before and after timings stay internal (decision 11).
  - *Anchor selection (2026-11).* A pattern compiled from its first node, so
    `(p:Person)<-[:HAS_CREATOR]-(m:Message)-[:HAS_TAG]->(t:Tag {name: $tag})`
    scanned every Person when the Tag's name index finds the one tag the rows
    start from. A pattern whose last node alone has an inline equality on an
    indexed property is now read from that end, decided from the mapping's
    indexes (BI2, BI11, BI16 change). The plan-shape regression bound is
    `tests/integration/test_anchor_selection_live.py`. Not yet: an anchor in
    the middle of a pattern, WHERE equalities and IN lists (IC3), and range
    anchors (BI9, BI12).
  - *shortestPath on PG (2026-11).* A breadth-first traversal per pair
    re-explored the start's neighbourhood for every target (IC1: one per
    candidate friend). Edges in their own collection now use AQL's
    `SHORTEST_PATH`, which searches from both ends; the plan-shape bound is
    in `tests/integration/test_shortest_path_live.py`. LPG keeps the
    traversal, since `SHORTEST_PATH` cannot filter edges.
  - *shortestPath on LPG (2026-11).* The variable-length type guard, a
    PRUNE, keeps the `(_from, type)` index out, so the search read every
    edge of every person it reached. The search now also tests its path
    (`p.edges[*][@tf] ALL == @tv`), which the index serves (IC1, IC13).
    The bound is the index entries read, in
    `tests/integration/test_shortest_path_live.py`.
  - *Not done: the same path condition on every LPG variable-length hop.*
    It helps where vertices have many edges of other types (IC1, IC9,
    IC11) and hurts where they have few (BI12's `REPLY_OF*0..` about 2.5×
    slower, IC12 about a quarter): the `(_from, type)` index has no
    in-memory cache, the edge index has. The mapping holds no fan-out
    statistics to choose by.

---

## 7. Sequencing

```
WP-31 ─┬─ WP-32 ─┬─ WP-33 ──► M1 ──► WP-43 (perf on passing subset)
       │         │
       └─ WP-34 ─┘
M1 ─► WP-35 ⚡ ─┬─ WP-36 ─┬─ WP-40 ──► M2
               ├─ WP-37 ─┤
               ├─ WP-38 ─┤
               └─ WP-39 ─┘
M2 ─► WP-41 ─┬─ WP-42 ──► M3 ──► WP-44
```

WP-36 to WP-39 are independent of each other and can run in parallel. WP-35's
upstream half (arango-query-core) should start during Phase 0, because it
crosses repositories.

---

## 8. Risks

| Risk | Mitigation |
| --- | --- |
| The PG union lowering for supertypes is slow, because a scan start becomes a multi-branch subquery | That is a finding, not a failure. Measure it (WP-43) and compare against LPG and the optional hybrid. D8 keeps the lowering behind the resolver, so it can change. |
| Neo4j results are non-deterministic under ties | The manifest states each query's ordering contract. Compare as a multiset when the order is not total. |
| Neo4j version drift changes snapshots | Pin the image (WP-33). Re-record only through the explicit `record` command, with the diff reviewed. |
| CI time | SF0.003 loads in seconds. The ratchet tier needs no DB. SF ≥ 0.1 stays out of PR CI. |
| The upstream mapping change stalls WP-35 | The fixtures and the fail-closed guard (WP-32) keep `main` safe. Only the unlock waits. Any interim workaround carries an `# upstream:` marker. |
| Unaudited numbers are read as LDBC results | Generated reports carry the disclaimer. The conformance test fails if it is missing. |

---

## 9. Out of scope / future

- **Hybrid layout.** A third mapping that mirrors LDBC's CSV split. It is cheap
  once the loader exists, and it gives a three-way performance comparison.
- **A second oracle.** The PostgreSQL SQL reference, for cases where Neo4j and
  we disagree and the Cypher semantics are disputed.
- **FinBench.** A different schema with heavier temporal and window
  predicates. Reuse the harness.
- **The LDBC Java driver** (scheduled workload, throughput, the 95% on-time
  rule). That is needed only to pursue an audit.
- **NL → Cypher.** LDBC queries come with English descriptions in the spec,
  which could become a harder NL eval set (`tests/nl2cypher/eval`).

---

## 10. Decisions (2026-10-26) and remaining open questions

Decided by the maintainer on 2026-10-26:

1. **D8, supertype modeling: Option A, staged.**
   - **Where:** subtypes are modeled in arango-query-core's mapping model (fix
     upstream, don't fork).
   - **Stage 1, LPG:** a labels-array field (`"Message" IN n.labels`).
   - **Stage 2, PG:** the member-collection union. The benchmark measures its
     cost.
   - This is WP-35.
2. **D2, hybrid layout:** added as a third layout once WP-35 lands.
3. **Targets:** M2 and M3 become PRD success criteria (PRD §3).
4. **Performance environment:**
   - SF1 and above run on an Arango Platform deployment or a cloud VM, on
     ArangoDB 3.12.
   - CI's correctness tiers stay on 3.11.
   - This machine's 8 GB, shared Docker VM is too small for SF1.
5. **Publication:**
   - Benchmark numbers stay internal, as CI artifacts.
   - Only the method is published, until product signs off.
   - `docs/ldbc_results.md` is not committed until then.
6. **The INS1 upstream bug** is reported:
   [ldbc/ldbc_snb_interactive_v2_impls#10](https://github.com/ldbc/ldbc_snb_interactive_v2_impls/issues/10).
   v1 has the same bug.

**Correctness on ArangoDB 3.12 (2026-10-29).** The execution ledger, the
integration suite and the TCK were run against `arangodb:3.12.12`: identical
to 3.11 (PG 52 / LPG 52; same 1,861 TCK scenarios pass).

Decided by the maintainer on 2026-10-29, closing out correctness:

7. **INS1 and M2.** Verbatim INS1 counts toward M2 when it fails closed on
   the upstream schema bug and its labelled schema-fix variant matches (PRD §3).
8. **D9, GDS/APOC queries: deferred.** IC14, BI10, BI15, BI19 and BI20 stay
   `out_of_subset`. Arango-dialect variants would test the extension functions
   and ArangoDB's path engine, not the transpiler, and have no Neo4j oracle.
   Revisit only for a weighted-path performance comparison (IC14 first).
9. **Performance environment.** SF1 runs on a dedicated cloud VM, both engines
   under identical limits, for reproducible comparative numbers; one run on an
   Arango Platform deployment checks the result. (SF0.1 nightly in CI was
   withdrawn by decision 11.)
10. **Hybrid layout (D2): after the performance work.** It is not part of the
    PRD's LDBC-2 (PG and LPG), so LDBC closes without it; it then adds a third
    layout to the benchmark and its own correctness ledger column.

Decided by the maintainer on 2026-10-30:

11. **Benchmark numbers on the VM only, for now.** The repository is public,
    so CI artifacts are readable by any signed-in GitHub user. Timed runs
    happen on the benchmark VM; CI runs the runner only as a smoke test.
12. **SF0.1 is generated with LDBC's data generator** (Datagen, pinned to the
    commit of the SF0.003 samples), since LDBC publishes no SF0.1 BI data set.
    It is cached and verified by content digest; its bindings are chosen
    against Neo4j and committed, like SF0.003's.
13. **CI installs the schema analyzer** in the integration job, so the live
    acquisition tests and the LDBC live mapping diff run on every push.
