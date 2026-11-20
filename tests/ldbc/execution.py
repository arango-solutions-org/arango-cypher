"""LDBC execution tier: translated AQL against the Neo4j oracle's snapshots (WP-33 part 2).

For each read operation × layout, every committed binding is translated with
its parameters, executed on the loaded ArangoDB layout, and compared with the
oracle's recorded rows (``expected/<scale>/<op>.json``). A cell's outcome:

* ``matches_oracle`` — every binding returns the oracle's rows;
* ``translates_wrong`` — a binding returns different rows: a **defect**, never
  a tolerated state (PRD §12.1 LDBC-5); the detail names the first difference;
* ``execution_error`` — ArangoDB rejected the AQL;
* ``fails_closed`` / ``syntax_error`` / ``out_of_subset`` — as in the
  translation ledger;
* ``no_oracle`` — no snapshot (an operation the oracle cannot run);
* ``crash`` — the harness or translator raised something unexpected.

Results go to ``execution_ledger.tsv``; ``tests/integration/test_ldbc_execution.py``
ratchets it. Regenerate (needs the local ArangoDB of the integration tier)::

    .venv/bin/python -m tests.ldbc.execution --write

(the databases are started and removed around the run: tests/ldbc/stack.py)
"""

from __future__ import annotations

import argparse
from typing import Any

from arango.exceptions import AQLQueryExecuteError, TransactionInitError

from arango_cypher import translate
from tests.helpers.mapping_fixtures import mapping_bundle_for
from tests.helpers.result_equivalence import result_difference
from tests.ldbc import oracle
from tests.ldbc import params as P
from tests.ldbc.corpus import LDBC_DIR, Operation, load
from tests.ldbc.datasets import DATASETS
from tests.ldbc.ledger import LAYOUTS, Cell, _detail, classify_error, load_ledger, write_cells
from tests.ldbc.mappings import with_encoding, with_statistics
from tests.ldbc.writes import arango_write_delta, delta_difference, dump_arango

EXEC_LEDGER_PATH = LDBC_DIR / "execution_ledger.tsv"

PASSING_OUTCOMES = frozenset({"matches_oracle"})
KNOWN_OUTCOMES = frozenset(
    {
        "matches_oracle",
        "translates_wrong",
        "execution_error",
        "fails_closed",
        "syntax_error",
        "out_of_subset",
        "no_oracle",
        "crash",
    }
)

#: Server-side cap per query; SF0.003 reads finish in milliseconds.
MAX_RUNTIME_S = 30.0


def dataset_for(op: Operation, scale: str = "sf0.003") -> str:
    return P.workload_datasets(scale)[op.workload]


def run_cell(
    db: Any, op: Operation, layout: str, snapshot: dict[str, Any] | None, before: Any = None
) -> Cell:
    """Translate, execute and compare every binding of *op*; the first failure decides.

    Reads compare rows; writes compare their post-state delta, taken inside a
    rolled-back transaction against *before* (the layout's initial dump).
    """
    if op.out_of_subset:
        return "out_of_subset", _detail(op.out_of_subset)
    if snapshot is None:
        return "no_oracle", "no oracle snapshot"
    if snapshot["sha256"] != op.sha256:
        return "crash", f"stale snapshot: recorded for {snapshot['sha256'][:12]}, query is {op.sha256[:12]}"

    mapping = with_statistics(
        with_encoding(mapping_bundle_for(f"ldbc_{layout}"), DATASETS[dataset_for(op)].encoding), db
    )
    for i, b in enumerate(snapshot["bindings"]):
        values = {k: P.to_arango(v) for k, v in b["binding"].items()}
        try:
            tq = translate(op.text(), mapping=mapping, params=values)
        except Exception as e:  # noqa: BLE001 — classified like the translation ledger
            return classify_error(e)
        if snapshot.get("kind") == "write":
            try:
                actual = arango_write_delta(db, layout, tq.aql, tq.bind_vars, before)
            except (AQLQueryExecuteError, TransactionInitError) as e:
                return "execution_error", _detail(f"ERR {e.error_code}: {e.error_message}")
            diff = delta_difference(b["delta"], actual)
            if diff is not None:
                return "translates_wrong", _detail(f"[{op.id}@{layout}] {diff}")
            continue
        try:
            rows = list(db.aql.execute(tq.aql, bind_vars=tq.bind_vars, max_runtime=MAX_RUNTIME_S))
        except AQLQueryExecuteError as e:
            return "execution_error", _detail(f"ERR {e.error_code}: {e.error_message}")
        columns = b["columns"]
        unordered = frozenset(j for j, c in enumerate(columns) if c in op.unordered_columns)
        diff = result_difference(
            b["rows"],
            rows,
            ordered=snapshot["ordered"],
            label=f"{op.id}@{layout} binding {i}",
            unordered_positions=unordered,
        )
        if diff is not None:
            return "translates_wrong", _detail(" ".join(diff.split()))
    return "matches_oracle", f"{len(snapshot['bindings'])} binding(s)"


def build_execution_ledger(dbs: dict[tuple[str, str], Any], scale: str = "sf0.003") -> dict[str, Cell]:
    """``{"<op>@<layout>": (outcome, detail)}``; *dbs* maps (data set, layout) to a loaded database."""
    out: dict[str, Cell] = {}
    befores: dict[tuple[str, str], Any] = {}
    for op in load():
        snapshot = oracle.read(op.id, scale)
        for layout in LAYOUTS:
            key = (dataset_for(op, scale), layout)
            if snapshot is not None and snapshot.get("kind") == "write" and key not in befores:
                befores[key] = dump_arango(dbs[key], layout)
            try:
                out[f"{op.id}@{layout}"] = run_cell(dbs[key], op, layout, snapshot, befores.get(key))
            except Exception as e:  # noqa: BLE001 — a harness failure is recorded, then fails the ratchet
                out[f"{op.id}@{layout}"] = ("crash", _detail(f"{type(e).__name__}: {e}"))
    return out


def write_execution_ledger(ledger: dict[str, Cell]) -> None:
    write_cells(
        ledger,
        EXEC_LEDGER_PATH,
        [
            "LDBC SNB execution ledger: translated AQL vs the Neo4j oracle's snapshots — regenerate with:",
            "  .venv/bin/python -m tests.ldbc.execution --write",
            "Format: <outcome>\\t<operation>@<layout>\\t<detail>, sorted by cell.",
            f"Passing outcomes: {', '.join(sorted(PASSING_OUTCOMES))}. translates_wrong is a defect, not a state.",
        ],
    )


def load_execution_ledger() -> dict[str, Cell]:
    return load_ledger(EXEC_LEDGER_PATH)


def loaded_databases(url: str | None = None, scale: str = "sf0.003") -> dict[tuple[str, str], Any]:
    """Load every data set × layout afresh on *url* (about ten seconds at SF0.003)."""
    from tests.ldbc.arango_dbs import load_fresh

    return {
        (dataset, layout): load_fresh(dataset, layout, url)[0]  # type: ignore[arg-type]
        for dataset in sorted(set(P.workload_datasets(scale).values()))
        for layout in LAYOUTS
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Measure or record the LDBC execution ledger.")
    ap.add_argument("--write", action="store_true", help=f"write {EXEC_LEDGER_PATH.name}")
    args = ap.parse_args(argv)
    from tests.ldbc.stack import ldbc_stack

    with ldbc_stack(arango=True) as endpoints:
        ledger = build_execution_ledger(loaded_databases(endpoints.arango_url))
    if args.write:
        write_execution_ledger(ledger)
        print(f"wrote {EXEC_LEDGER_PATH}")
    from tests.ldbc.ledger import summarize

    for layout, counts in summarize(ledger).items():
        print(f"{layout}: " + ", ".join(f"{o} {n}" for o, n in sorted(counts.items())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
