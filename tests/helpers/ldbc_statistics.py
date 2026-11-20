"""Cardinality statistics in the proportions of LDBC SNB SF0.1 (``metadata.statistics``).

Average degrees as ``schema_acquire.compute_statistics`` reports them on the
SF0.1 databases (rounded), for tests of statistics-driven translation that
need no database. Degrees are properties of the data set, not timings.
"""

from __future__ import annotations

from typing import Any


def _rel(edges: int, out: float, in_: float) -> dict[str, Any]:
    return {
        "edge_count": edges,
        "source_count": 1,
        "target_count": 1,
        "avg_out_degree": out,
        "avg_in_degree": in_,
    }


SF0_1_STATISTICS: dict[str, Any] = {
    "relationships": {
        "CONTAINER_OF": _rel(161074, 10.12, 1.0),
        "HAS_CREATOR": _rel(324221, 1.0, 197.82),
        "HAS_INTEREST": _rel(37746, 23.03, 2.35),
        "HAS_MEMBER": _rel(241175, 15.15, 147.15),
        "HAS_MODERATOR": _rel(15916, 1.0, 9.71),
        "HAS_TAG": _rel(311990, 0.92, 19.4),
        "HAS_TYPE": _rel(16080, 1.0, 226.48),
        "IS_LOCATED_IN": _rel(333815, 1.0, 229.58),
        "IS_PART_OF": _rel(1454, 1.0, 12.43),
        "IS_SUBCLASS_OF": _rel(70, 0.99, 0.99),
        "KNOWS": _rel(14368, 8.77, 8.77),
        "LIKES": _rel(135045, 82.39, 0.42),
        "REPLY_OF": _rel(163147, 1.0, 0.5),
        "STUDY_AT": _rel(1323, 0.81, 0.21),
        "WORK_AT": _rel(3502, 2.14, 2.22),
    }
}
