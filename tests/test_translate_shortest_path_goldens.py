"""Golden AQL for shortestPath() between bound nodes (WP-39), PG and LPG.

The behaviour tests are ``tests/integration/test_shortest_path_live.py``.
"""

from __future__ import annotations

import pytest

from arango_cypher import translate
from tests.helpers.mapping_fixtures import mapping_bundle_for


@pytest.mark.parametrize("case_id", [f"C{n}" for n in range(860, 862)])
def test_translate_shortest_path_goldens(corpus_cases, case_id: str):
    case = next(c for c in corpus_cases if c.id == case_id)
    mapping = mapping_bundle_for(case.mapping_fixture)

    out = translate(case.cypher, mapping=mapping, params=case.params or None)

    assert case.expected_aql is not None, f"Golden AQL must be filled for {case_id}"
    assert out.aql.strip() == case.expected_aql.strip()
    assert out.bind_vars == case.expected_bind_vars
    # An unbounded * is capped, and the translation says so.
    assert all("limited to" in w["message"] for w in out.warnings)


def test_an_lpg_search_tests_every_edge_of_its_path():
    # The PRUNE alone keeps a (_from, type) index out; the path condition lets it serve the search.
    aql = translate(
        "MATCH (a:Person {id: 1}), (b:Person {id: 2}), p = shortestPath((a)-[:KNOWS*]-(b)) RETURN length(p)",
        mapping=mapping_bundle_for("ldbc_lpg"),
    ).aql
    assert "FILTER _sp_p.edges[*][@spRelTF] ALL == @spRelTV" in aql, aql


def test_an_lpg_search_over_several_types_tests_each_edge_against_the_list():
    aql = translate(
        "MATCH (a:Person {id: 1}), (b:Person {id: 2}), p = shortestPath((a)-[:KNOWS|LIKES*]-(b)) RETURN length(p)",
        mapping=mapping_bundle_for("ldbc_lpg"),
    ).aql
    assert "ALL IN @spRelTV" in aql and "ALL == @" not in aql, aql
