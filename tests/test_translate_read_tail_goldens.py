"""Golden AQL for the read-side TCK long tail (PRD §8): variable-length
relationship lists and paths, empty intervals, UNWIND before a projection's
COLLECT, null CREATE properties.

Live behaviour: Match4, Match5 [11]-[13], Match6, Match9, Path2, Path3,
Graph8 and Unwind1 in ``tests/tck/execution_ledger.tsv``.
"""

from __future__ import annotations

import pytest

from arango_cypher import translate
from arango_cypher._translate_v0.formatting import empty_varlength_ranges
from tests.helpers.mapping_fixtures import mapping_bundle_for


@pytest.mark.parametrize("case_id", [f"C{n}" for n in range(1401, 1407)])
def test_read_tail_goldens(corpus_cases, case_id: str):
    case = next(c for c in corpus_cases if c.id == case_id)
    out = translate(case.cypher, mapping=mapping_bundle_for(case.mapping_fixture), params=case.params or None)
    assert case.expected_aql is not None, f"Golden AQL must be filled for {case_id}"
    assert out.aql.strip() == case.expected_aql.strip()
    assert out.bind_vars == case.expected_bind_vars


def _aql(q: str) -> str:
    return translate(q, mapping=mapping_bundle_for("lpg")).aql


def test_unused_variable_length_name_keeps_the_plain_traversal():
    """No path variable is added when nothing reads the relationship list."""
    aql = _aql("MATCH (a:User)-[r:FOLLOWS*1..2]->(b) RETURN b")
    assert "_vp_" not in aql and "FOR b, r IN 1..2" in aql


def test_fixed_length_star_range_is_still_a_list():
    """``[r*1..1]`` is variable-length syntax: ``r`` is a one-element list."""
    aql = _aql("MATCH (a:User)-[r:FOLLOWS*1..1]->(b) RETURN r")
    assert "LET r = _vp_r.edges" in aql


def test_empty_interval_rewrite():
    text = "FOR a IN c\n  FOR b, r IN 2..1 OUTBOUND a @@e\n    RETURN b"
    assert (
        empty_varlength_ranges(text)
        == "FOR a IN c\n  FOR b, r IN 0..0 OUTBOUND a @@e\n    FILTER false\n    RETURN b"
    )
    untouched = "FOR b, r IN 1..2 OUTBOUND a @@e"
    assert empty_varlength_ranges(untouched) == untouched


def test_unwind_variable_is_in_scope_for_return_star():
    aql = _aql("WITH [1, 2] AS list UNWIND list AS x RETURN *")
    assert "RETURN {list: list, x: x}" in aql


def test_edge_uniqueness_across_groups():
    from arango_cypher._translate_v0.core import edge_uniqueness_across

    assert edge_uniqueness_across([["_path.edges[0]", "r_2"]], singles=["r"]) == [
        "_path.edges[0]._id != r._id",
        "r_2._id != r._id",
    ]
    assert edge_uniqueness_across([["..._vp.edges"], ["e"]], singles=[]) == ["e._id NOT IN _vp.edges[*]._id"]
    assert edge_uniqueness_across([["...a.edges"], ["...b.edges"]], singles=[]) == [
        "LENGTH(INTERSECTION(a.edges[*]._id, b.edges[*]._id)) == 0"
    ]


def test_undirected_merged_hops_keep_edges_distinct_from_earlier_ones():
    aql = _aql("MATCH (n:User)-[:FOLLOWS]->(m)-[:FOLLOWS]-(o)-[:FOLLOWS]-(p) RETURN p")
    assert "._id != r._id" in aql
