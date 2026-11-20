"""LDBC performance runner (plan WP-43; PRD §12.1 LDBC-6, LDBC-7, LDBC-8).

For each read operation × layout at one scale factor:

- **translation**: ``translate()`` with the cache cleared (cold) and with it
  warm, p50/p95 over ``--translate-repeats`` runs;
- **execution**: per parameter binding, ``--warmup`` discarded runs, then
  ``--repeats`` timed runs. Server-side time comes from the cursor statistics
  (the client is excluded); client time is reported beside it. p50/p95/p99,
  mean, min and max are taken over every binding × repeat. A run that passes
  ``--timeout`` is recorded as ``TIMEOUT``;
- **rows**: each binding's row count is checked against the oracle's, recorded
  in ``params/<scale>.yml``; a cell that differs is ``rows_differ``, not timed
  numbers for a wrong answer;
- **plan**: the EXPLAIN plan's shape (node types, full scans, indexes,
  traversal options, rules) and a digest of it, so a plan change is visible
  between runs (``report.compare``).

The environment is recorded with each run: server version, Python and package
versions, CPU and memory, the data sets' content digests, the git SHA and the
mapping fixtures' hashes. Output is JSON (``--out``); ``tests.ldbc.report``
renders it. The numbers are internal (plan §10, decision 5): a CI artifact,
never committed.

    .venv/bin/python -m tests.ldbc.bench --scale sf0.003 --out results/bench.json

Uses the server named by ``ARANGO_URL`` if set, else starts and removes the
LDBC stack (``tests/ldbc/stack.py``).
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.metadata
import json
import math
import os
import platform
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from arango.exceptions import AQLQueryExecuteError
from requests.exceptions import Timeout as HttpTimeout

from arango_cypher import translate
from arango_cypher.api import clear_translate_cache
from tests.helpers.mapping_fixtures import mapping_bundle_for
from tests.ldbc import params as P
from tests.ldbc.corpus import Operation
from tests.ldbc.datasets import DATASETS
from tests.ldbc.ledger import LAYOUTS
from tests.ldbc.mappings import fixture_path, with_encoding, with_statistics

SCHEMA_VERSION = 1
#: ArangoDB kills a query past its ``max_runtime`` with this error number.
_KILLED = 1500
#: Seconds the client waits beyond a query's ``max_runtime``, so the server's
#: kill (an ERR 1500 answer) arrives before the HTTP read times out.
CLIENT_TIMEOUT_MARGIN_S = 30.0
_REPO = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Settings:
    scale: str = "sf0.003"
    layouts: tuple[str, ...] = LAYOUTS
    operations: tuple[str, ...] = ()  # empty: every read operation
    warmup: int = 2
    repeats: int = 5
    translate_repeats: int = 20
    timeout_s: float = 60.0


# ---------------------------------------------------------------------------
# Statistics and plans
# ---------------------------------------------------------------------------


def percentiles(samples: Sequence[float]) -> dict[str, float | int] | None:
    """Nearest-rank p50/p95/p99, mean, min and max of *samples* (ms), or None."""
    if not samples:
        return None
    ordered = sorted(samples)

    def rank(p: float) -> float:
        return ordered[max(0, math.ceil(p / 100 * len(ordered)) - 1)]

    return {
        "n": len(ordered),
        "p50": round(rank(50), 3),
        "p95": round(rank(95), 3),
        "p99": round(rank(99), 3),
        "mean": round(sum(ordered) / len(ordered), 3),
        "min": round(ordered[0], 3),
        "max": round(ordered[-1], 3),
    }


def plan_shape(plan: dict[str, Any]) -> dict[str, Any]:
    """The parts of an EXPLAIN plan that say how a query runs, and a digest of them.

    Estimates and node ids are left out, so the digest changes only when the
    plan does: another node, another index, a scan that was a traversal.
    """
    nodes = plan.get("nodes") or []
    full_scans = sorted(n.get("collection", "") for n in nodes if n.get("type") == "EnumerateCollectionNode")
    indexes = sorted(
        f"{n.get('collection', '')}:{idx.get('name', idx.get('type', ''))}"
        for n in nodes
        if n.get("type") == "IndexNode"
        for idx in n.get("indexes") or []
    )
    traversals = [
        {k: (n.get("options") or {}).get(k) for k in ("order", "uniqueVertices", "uniqueEdges")}
        for n in nodes
        if n.get("type") in ("TraversalNode", "EnumeratePathsNode", "ShortestPathNode")
    ]
    shape = {
        "nodes": [n.get("type") for n in nodes],
        "full_scans": full_scans,
        "indexes": indexes,
        "traversals": traversals,
        "rules": sorted(plan.get("rules") or []),
    }
    digest = hashlib.sha256(json.dumps(shape, sort_keys=True).encode()).hexdigest()[:16]
    return {**shape, "digest": digest}


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------


def _git(*args: str) -> str:
    try:
        return subprocess.run(
            ["git", *args], cwd=_REPO, capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as e:
        return f"unavailable ({e.__class__.__name__})"


def _memory_bytes() -> int | None:
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (ValueError, OSError, AttributeError):
        return None


def _version(package: str) -> str:
    try:
        return importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        return "not installed"


def environment(db: Any, settings: Settings, datasets: dict[str, str]) -> dict[str, Any]:
    """What a run's numbers depend on (PRD LDBC-6)."""
    server = db.version(details=True)
    details = server.get("details", {}) if isinstance(server, dict) else {}
    engine = db.engine()  # 3.12's version details no longer name the storage engine
    return {
        "recorded_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "server": {
            "version": server.get("version") if isinstance(server, dict) else server,
            "engine": engine.get("name") if isinstance(engine, dict) else details.get("engine"),
            "mode": details.get("mode"),
            "image": os.environ.get("LDBC_BENCH_IMAGE", "unrecorded"),
        },
        "host": {
            "platform": platform.platform(),
            "machine": platform.machine(),
            "cpus": os.cpu_count(),
            "memory_bytes": _memory_bytes(),
            "runner": os.environ.get("RUNNER_NAME") or os.environ.get("HOSTNAME") or platform.node(),
        },
        "software": {
            "python": sys.version.split()[0],
            "arango-cypher-py": _version("arango-cypher-py"),
            "arango-query-core": _version("arango-query-core"),
            "python-arango": _version("python-arango"),
        },
        "data": {
            "scale": settings.scale,
            "datasets": {
                w: {"name": n, "content_digest": DATASETS[n].content_digest} for w, n in datasets.items()
            },
        },
        "git": {
            "sha": _git("rev-parse", "HEAD"),
            "dirty": bool(_git("status", "--porcelain", "--untracked-files=no")),
        },
        "mappings": {
            layout: hashlib.sha256(fixture_path(layout).read_bytes()).hexdigest()[:16]  # type: ignore[arg-type]
            for layout in settings.layouts
        },
        "settings": {k: getattr(settings, k) for k in Settings.__dataclass_fields__},
    }


# ---------------------------------------------------------------------------
# Timing
# ---------------------------------------------------------------------------


def time_translation(cypher: str, mapping: Any, values: dict[str, Any], repeats: int) -> dict[str, Any]:
    """Cold (cache cleared before each call) and warm ``translate()`` times, ms."""
    cold: list[float] = []
    warm: list[float] = []
    for _ in range(repeats):
        clear_translate_cache()
        start = time.perf_counter()
        translate(cypher, mapping=mapping, params=values)
        cold.append((time.perf_counter() - start) * 1000)
    for _ in range(repeats):
        start = time.perf_counter()
        translate(cypher, mapping=mapping, params=values)
        warm.append((time.perf_counter() - start) * 1000)
    return {"cold": percentiles(cold), "warm": percentiles(warm)}


def time_execution(
    db: Any, aql: str, bind_vars: dict[str, Any], *, warmup: int, repeats: int, timeout_s: float
) -> dict[str, Any]:
    """Server-side and client times (ms) and the row count of one binding."""
    server: list[float] = []
    client: list[float] = []
    rows = 0
    for i in range(warmup + repeats):
        start = time.perf_counter()
        try:
            # cache=False: a server with the query results cache on would answer repeats from it.
            cursor = db.aql.execute(aql, bind_vars=bind_vars, max_runtime=timeout_s, count=True, cache=False)
            result = list(cursor)
        except AQLQueryExecuteError as e:
            if e.error_code == _KILLED:
                return {"outcome": "TIMEOUT", "timeout_s": timeout_s}
            return {"outcome": "error", "detail": f"ERR {e.error_code}: {e.error_message}"[:300]}
        except HttpTimeout as e:
            # The server did not answer even after max_runtime: one cell's outcome, not the run's end.
            return {"outcome": "TIMEOUT", "timeout_s": timeout_s, "detail": f"client: {e}"[:300]}
        elapsed = (time.perf_counter() - start) * 1000
        if i >= warmup:
            client.append(elapsed)
            server.append(float(cursor.statistics().get("execution_time", 0.0)) * 1000)
        rows = len(result)
    return {"outcome": "ok", "rows": rows, "server": server, "client": client}


def _cell(
    db: Any, op: Operation, layout: str, bindings: list[dict[str, Any]], mapping: Any, settings: Settings
) -> dict[str, Any]:
    values = [{k: P.to_arango(v) for k, v in b["binding"].items()} for b in bindings]
    cell: dict[str, Any] = {"operation": op.id, "layout": layout, "bindings": len(values)}
    try:
        first = translate(op.text(), mapping=mapping, params=values[0])
    except Exception as e:  # noqa: BLE001 — a refused translation is a recorded outcome
        return {**cell, "outcome": "translate_failed", "detail": f"{type(e).__name__}: {e}"[:300]}
    cell["translation_ms"] = time_translation(op.text(), mapping, values[0], settings.translate_repeats)
    cell["plan"] = plan_shape(db.aql.explain(first.aql, bind_vars=first.bind_vars))
    server: list[float] = []
    client: list[float] = []
    rows: list[int] = []
    expected = [b.get("rows") for b in bindings]
    for value in values:
        tq = translate(op.text(), mapping=mapping, params=value)
        run = time_execution(
            db,
            tq.aql,
            tq.bind_vars,
            warmup=settings.warmup,
            repeats=settings.repeats,
            timeout_s=settings.timeout_s,
        )
        if run["outcome"] != "ok":
            return {**cell, **{k: v for k, v in run.items() if k != "outcome"}, "outcome": run["outcome"]}
        server += run["server"]
        client += run["client"]
        rows.append(run["rows"])
    if any(want is not None and want != got for want, got in zip(expected, rows, strict=True)):
        # Timing a query that returns other rows than the oracle measures nothing useful.
        return {**cell, "outcome": "rows_differ", "rows": rows, "rows_expected": expected}
    return {
        **cell,
        "outcome": "ok",
        "rows": rows,
        "server_ms": percentiles(server),
        "client_ms": percentiles(client),
    }


def scale_datasets(scale: str) -> dict[str, str]:
    """Workload → data set name for *scale*, as its parameter file records it."""
    doc = yaml.safe_load(P.params_path(scale).read_text(encoding="utf-8"))
    return dict(doc["datasets"])


def run(
    databases: dict[tuple[str, str], Any],
    settings: Settings,
    *,
    progress: Callable[[str], None] = lambda _msg: None,
) -> dict[str, Any]:
    """Benchmark every selected read operation on the loaded *databases*
    (keyed by ``(dataset, layout)``)."""
    datasets = scale_datasets(settings.scale)
    bindings = P.read(settings.scale)
    wanted = set(settings.operations)
    cells = []
    for op in P.read_operations():
        if wanted and op.id not in wanted:
            continue
        dataset = datasets[op.workload]
        for layout in settings.layouts:
            mapping = with_statistics(
                with_encoding(mapping_bundle_for(f"ldbc_{layout}"), DATASETS[dataset].encoding),
                databases[(dataset, layout)],
            )
            op_bindings = bindings.get(op.id) or []
            if not op_bindings:
                cells.append({"operation": op.id, "layout": layout, "outcome": "no_bindings"})
                continue
            progress(f"{op.id}@{layout}")
            cells.append(_cell(databases[(dataset, layout)], op, layout, op_bindings, mapping, settings))
    any_db = next(iter(databases.values()))
    return {
        "schema_version": SCHEMA_VERSION,
        "environment": environment(any_db, settings, datasets),
        "cells": cells,
    }


def load_databases(url: str, settings: Settings) -> dict[tuple[str, str], Any]:
    from tests.ldbc.arango_dbs import load_fresh

    return {
        (dataset, layout): load_fresh(  # type: ignore[arg-type]
            dataset, layout, url, request_timeout=settings.timeout_s + CLIENT_TIMEOUT_MARGIN_S
        )[0]
        for dataset in sorted(set(scale_datasets(settings.scale).values()))
        for layout in settings.layouts
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Time LDBC read operations (internal numbers; see the module doc)."
    )
    ap.add_argument("--scale", default=Settings.scale, help="a scale with a params/<scale>.yml file")
    ap.add_argument("--layouts", nargs="+", default=list(LAYOUTS), choices=list(LAYOUTS))
    ap.add_argument("--ops", default="", help="comma-separated operation ids (default: all reads)")
    ap.add_argument("--warmup", type=int, default=Settings.warmup)
    ap.add_argument("--repeats", type=int, default=Settings.repeats)
    ap.add_argument("--translate-repeats", type=int, default=Settings.translate_repeats)
    ap.add_argument("--timeout", type=float, default=Settings.timeout_s, help="seconds per query run")
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args(argv)
    if not P.params_path(args.scale).exists():
        ap.error(f"no bindings for {args.scale}: {P.params_path(args.scale)} is missing")
    settings = Settings(
        scale=args.scale,
        layouts=tuple(args.layouts),
        operations=tuple(o for o in args.ops.split(",") if o),
        warmup=args.warmup,
        repeats=args.repeats,
        translate_repeats=args.translate_repeats,
        timeout_s=args.timeout,
    )
    from tests.ldbc.stack import ldbc_stack

    with ldbc_stack(arango=True) as endpoints:
        result = run(
            load_databases(endpoints.arango_url, settings),
            settings,
            progress=lambda msg: print(msg, file=sys.stderr, flush=True),
        )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=1, sort_keys=True), encoding="utf-8")
    ok = sum(1 for c in result["cells"] if c["outcome"] == "ok")
    print(f"wrote {args.out}: {ok} of {len(result['cells'])} cells timed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
