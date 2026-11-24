"""EXISTS { } and COUNT { } bodies mean what the same MATCH means elsewhere.

The body used to be compiled by a single-hop copy of the MATCH compiler that
dropped the target's labels and properties, the relationship's properties and
every MATCH after the first; it now goes through the compiler for a MATCH that
starts at bound variables. Each test names a pattern element and checks that
the AQL tests it. ``tests/integration/test_exists_count_subqueries_live.py``
runs the same questions on ArangoDB.
"""

from __future__ import annotations

import pytest
from arango_query_core import CoreError

from arango_cypher import translate
from tests.helpers.mapping_fixtures import mapping_bundle_for


def _sub(cypher: str, mapping: str = "pg") -> tuple[str, dict]:
    """The AQL of the (first) subquery, and the query's bind variables."""
    q = translate(cypher, mapping=mapping_bundle_for(mapping))
    start = q.aql.index("LENGTH(FOR")
    depth, end = 0, start
    for end in range(start, len(q.aql)):
        depth += {"(": 1, ")": -1}.get(q.aql[end], 0)
        if depth == 0 and q.aql[end] == ")":
            break
    return q.aql[start : end + 1], q.bind_vars


def test_the_target_label_is_checked():
    sub, bv = _sub("MATCH (u:User) WHERE EXISTS { (u)-[:FOLLOWS]->(:Doc) } RETURN u")
    assert "IS_SAME_COLLECTION(@vCollection, _sq_v)" in sub
    assert bv["vCollection"] == "docs"


def test_the_target_properties_are_checked():
    sub, _ = _sub("MATCH (u:User) WHERE EXISTS { (u)-[:FOLLOWS]->(m {name: 'c'}) } RETURN u")
    assert "_sq_m.name == 'c'" in sub


def test_the_relationship_properties_are_checked():
    sub, _ = _sub("MATCH (u:User) WHERE EXISTS { (u)<-[:FOLLOWS {w: 1}]-() } RETURN u")
    assert "INBOUND u" in sub
    assert "_sq_r.w == 1" in sub


def test_the_start_node_labels_and_properties_are_checked():
    sub, _ = _sub("MATCH (u:User) WHERE EXISTS { (u:User {name: 'a'})-[:FOLLOWS]->() } RETURN u", "lpg")
    assert "u.name == 'a'" in sub
    assert "u[@uLabelField] == @uLabelValue" in sub


def test_variable_length_properties_hold_on_every_edge():
    sub, _ = _sub("MATCH (u:User) WHERE EXISTS { (u)-[:FOLLOWS*2 {w: 1}]->() } RETURN u")
    assert "LENGTH(_sq__hop_path.edges[* FILTER NOT ((CURRENT.w == 1))]) == 0" in sub


def test_a_fresh_start_node_is_scanned():
    sub, bv = _sub("MATCH (u:User) WHERE EXISTS { (x:User {name: 'b'})-[:FOLLOWS]->(u) } RETURN u")
    # Read from the bound end, u; x is the subquery's own.
    assert "INBOUND u" in sub
    assert "_sq_x.name == 'b'" in sub


def test_a_bound_target_with_a_label_is_correlated_not_rebound():
    sub, _ = _sub("MATCH (u:User), (v:User) WHERE EXISTS { (u)-[:FOLLOWS]->(v:User) } RETURN u")
    assert "._id == v._id" in sub
    assert "FOR v" not in sub


def test_every_match_clause_is_compiled():
    sub, _ = _sub(
        "MATCH (u:User) WHERE EXISTS { MATCH (u)-[:FOLLOWS]->(x) MATCH (x)-[:FOLLOWS]->(:Doc) } RETURN u"
    )
    assert sub.count(" IN 1..1 ") == 2
    assert "OUTBOUND _sq_x" in sub


def test_every_comma_part_is_compiled():
    sub, _ = _sub("MATCH (u:User) WHERE EXISTS { (u)-[:FOLLOWS]->(:User), (u)-[:FOLLOWS]->(:Doc) } RETURN u")
    assert sub.count("OUTBOUND u") == 2


def test_a_bound_node_alone_is_its_filters_over_one_row():
    sub, _ = _sub("MATCH (u:User) WHERE EXISTS { (u {name: 'b'}) } RETURN u")
    assert sub == "LENGTH(FOR _sq_row IN [1] FILTER (u.name == 'b') RETURN 1)"


def test_a_fresh_node_with_several_labels_scans_one_and_filters_the_others():
    sub, _ = _sub("MATCH (u:User) WHERE EXISTS { (x:User:Doc) } RETURN u", "lpg_labels")
    assert "FOR _sq_x IN @@tailCollection" in sub
    assert '"User" IN _sq_x.labels' in sub


@pytest.mark.parametrize(
    "cypher",
    [
        # Unnamed variables of the outer query and of the subquery share default names.
        "MATCH (p:User)-[:FOLLOWS]->(m) WHERE EXISTS { (p)-[:FOLLOWS]->(m) } RETURN p",
        "MATCH (p:User)-[:FOLLOWS]->() WHERE EXISTS { (p)-[:FOLLOWS]->()-[:FOLLOWS]->() } RETURN p",
        "MATCH (u:User) WHERE EXISTS { MATCH (u)-[:FOLLOWS]->() WHERE EXISTS { (u)-[:FOLLOWS]->() } } RETURN u",
    ],
)
def test_the_subquery_declares_no_variable_of_the_outer_query(cypher):
    q = translate(cypher, mapping=mapping_bundle_for("pg"))
    outer, _, rest = q.aql.partition("LENGTH(")
    from arango_cypher._translate_v0.naming import _declared_aql_vars

    assert not _declared_aql_vars([outer]) & _declared_aql_vars([rest]), q.aql


def test_a_count_grouped_by_the_outer_node_keeps_no_empty_group():
    sub, _ = _sub(
        "MATCH (u:User) WHERE EXISTS { MATCH (u)-[:FOLLOWS]->(:Doc) WITH u, count(*) AS c RETURN true } RETURN u"
    )
    assert sub.endswith("COLLECT WITH COUNT INTO _sq_c FILTER _sq_c > 0 RETURN 1)")


def test_a_count_with_no_key_always_has_its_row():
    sub, _ = _sub(
        "MATCH (u:User) WHERE EXISTS { MATCH (u)-[:FOLLOWS]->(:Doc) WITH count(*) AS c RETURN c } RETURN u"
    )
    assert sub.endswith("COLLECT WITH COUNT INTO _sq_c RETURN 1)")


@pytest.mark.parametrize(
    "body",
    [
        "MATCH (u)-[:FOLLOWS]->(x) OPTIONAL MATCH (x)-[:FOLLOWS]->(y)",
        "MATCH (u)-[:FOLLOWS]->(x) UNWIND [1, 2] AS i",
    ],
)
def test_a_reading_clause_other_than_match_fails_closed(body):
    with pytest.raises(CoreError) as exc:
        translate(f"MATCH (u:User) WHERE EXISTS {{ {body} }} RETURN u", mapping=mapping_bundle_for("pg"))
    assert exc.value.code == "NOT_IMPLEMENTED"
