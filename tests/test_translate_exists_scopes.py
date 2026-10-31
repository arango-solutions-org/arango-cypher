"""EXISTS { … } subqueries: a name correlates only when the outer query binds it.

Live behaviour: TCK ExistentialSubquery1 [2]/[4], ExistentialSubquery3 [1]/[3]
in ``tests/tck/execution_ledger.tsv``.
"""

from __future__ import annotations

import pytest
from arango_query_core import CoreError

from arango_cypher import translate
from arango_cypher._translate_v0.scopes import outer_bound_names
from arango_cypher.parser import parse_cypher
from tests.helpers.mapping_fixtures import mapping_bundle_for


def _aql(q: str) -> str:
    return translate(q, mapping=mapping_bundle_for("lpg")).aql


def test_fresh_target_is_the_loop_variable():
    aql = _aql("MATCH (n) WHERE exists { (n)-->(m) WHERE n.prop = m.prop } RETURN n")
    assert "FOR m, _sq_e IN 1..1 OUTBOUND n" in aql
    assert "m._id" not in aql


def test_outer_target_is_correlated_by_id():
    aql = _aql("MATCH (n), (m) WHERE exists { (n)-->(m) } RETURN n")
    assert "_sq_v._id == m._id" in aql


def test_named_relationship_is_the_edge_variable():
    aql = _aql("MATCH (n) WHERE exists { (n)-[r]->() WHERE r.w > 1 } RETURN n")
    assert "FOR _sq_v, r IN 1..1 OUTBOUND n" in aql


def test_fresh_single_node_scans_and_outer_one_is_probed():
    nested = _aql("MATCH (n) WHERE exists { MATCH (m) WHERE m.prop = n.prop RETURN true } RETURN n")
    assert "FOR m IN @@sqNodes" in nested
    probe = _aql("MATCH (n) WHERE exists { MATCH (n) WHERE n.prop = 1 RETURN true } RETURN n")
    assert "FOR _sq_probe_n IN [n]" in probe


def test_multi_hop_subquery_tests_every_hop():
    """Both hops are traversed, from the outer ``n``, edges kept distinct."""
    aql = _aql("MATCH (n) WHERE exists { MATCH (l)<-[:FOLLOWS]-(n)-[:FOLLOWS]->(m) RETURN true } RETURN n")
    assert aql.count(" IN 1..1 ") == 2
    assert "._id != " in aql


def test_multi_hop_subquery_with_aggregation_fails_closed():
    with pytest.raises(CoreError):
        _aql(
            "MATCH (n) WHERE exists { MATCH (l)<-[:FOLLOWS]-(n)-[:FOLLOWS]->(m) WITH count(*) AS c RETURN c } "
            "RETURN n"
        )


def test_outer_bound_names_excludes_the_subquery_itself():
    tree = parse_cypher("MATCH (n) UNWIND [1] AS x WITH n, x AS y MATCH (m) RETURN n").tree

    def find(node, kind):
        if isinstance(node, kind):
            return node
        for i in range(node.getChildCount() if hasattr(node, "getChildCount") else 0):
            hit = find(node.getChild(i), kind)
            if hit is not None:
                return hit
        return None

    from arango_cypher._antlr.CypherParser import CypherParser

    query = tree.oC_Statement().oC_Query()
    assert outer_bound_names(query) == set()
    unwind = find(query, CypherParser.OC_UnwindContext)
    # Only what is declared before: the later MATCH (m) does not bind for it.
    assert outer_bound_names(unwind) == {"n"}
    later_match = find(find(query, CypherParser.OC_SinglePartQueryContext), CypherParser.OC_MatchContext)
    assert outer_bound_names(later_match) == {"n", "x", "y"}
