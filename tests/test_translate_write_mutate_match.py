"""The MATCH before SET / REMOVE / DELETE compiles as any MATCH does.

``write_mutate`` used to compile that MATCH itself: a named path was never
bound (``p`` reached ArangoDB as a collection name, ERR 1203), an unnamed node
was named ``n`` or ``v`` beside a user's ``n`` or ``v`` (ERR 1511), several
pattern parts were refused, a self-loop re-declared its node, one edge could
match two relationships, and a variable-length relationship's properties were
checked on its last edge. It now takes its lines from the shared compiler.
``tests/integration/test_write_mutate_match_live.py`` runs these on ArangoDB.
"""

from __future__ import annotations

import pytest
from arango_query_core import CoreError

from arango_cypher import translate
from arango_cypher._translate_v0.naming import _declared_aql_vars
from tests.helpers.mapping_fixtures import mapping_bundle_for


def _aql(cypher: str, mapping: str = "pg") -> tuple[str, dict]:
    q = translate(cypher, mapping=mapping_bundle_for(mapping))
    return q.aql, q.bind_vars


def _declares_once(aql: str) -> bool:
    names: list[str] = []
    for line in aql.splitlines():
        names += sorted(_declared_aql_vars([line]))
    return len(names) == len(set(names))


def test_a_named_path_is_bound_before_the_write():
    aql, _ = _aql("MATCH p = (a:User)-[:FOLLOWS]->(b:User) SET b.hops = length(p)")
    assert "LET p = " in aql
    assert aql.index("LET p = ") < aql.index("UPDATE b")


def test_a_named_path_is_kept_for_return_after_optional_match():
    aql, _ = _aql("OPTIONAL MATCH p = (a:User)-[:FOLLOWS]->(b:User) SET b.x = 1 RETURN p")
    assert "p: p" in aql  # carried out of the null-extending subquery
    assert "LET p = _opt_row.p" in aql


@pytest.mark.parametrize(
    "cypher",
    [
        "MATCH (:User {name: 'a'})-[:FOLLOWS]->(n:User) SET n.hit = true",
        "MATCH (v:User)-[:FOLLOWS]->() SET v.hit = true",
        "MATCH (a:User)-[r:FOLLOWS]-(a) SET r.loop = true",
    ],
)
def test_no_variable_is_declared_twice(cypher):
    aql, _ = _aql(cypher)
    assert _declares_once(aql), aql


def test_several_pattern_parts_are_matched_and_written():
    aql, bv = _aql("MATCH (a:User {name: 'a'}), (b:User {name: 'b'}) SET a.x = b.name")
    assert aql.count(" IN @@") >= 3  # two scans and the UPDATE
    assert "UPDATE a WITH {x: b.name}" in aql


def test_relationships_are_distinct_across_hops():
    aql, _ = _aql("MATCH (a:User)-[r1:FOLLOWS]-(b:User)-[r2:FOLLOWS]-(c:User) SET b.hit = true")
    assert "r1._id != r2._id" in aql


def test_variable_length_properties_hold_on_every_edge():
    aql, _ = _aql("MATCH (a:User)-[:FOLLOWS*1..2 {w: 2}]->(b:User) SET b.hit = true")
    assert "[* FILTER NOT ((CURRENT.w == 2))]) == 0" in aql


def test_only_written_variables_get_a_collection_bind():
    aql, bv = _aql("MATCH (a:User)-[r:FOLLOWS]->(d:Doc) SET d.hit = true")
    written = [k for k in bv if k.startswith("@collection")]
    assert [bv[k] for k in written] == ["docs"], bv
    assert "UPDATE d WITH {hit: true} IN @@collection" in aql


def test_a_relationship_is_written_in_its_edge_collection():
    aql, bv = _aql("MATCH (a:User)-[r:FOLLOWS]->(b:User) SET r.since = 2020")
    assert "UPDATE r WITH {since: 2020} IN @@edgeCollection" in aql
    assert bv["@edgeCollection"] == "follows"


@pytest.mark.parametrize(
    ("cypher", "message"),
    [
        ("MATCH (a:User)-[r:FOLLOWS*1..2]->(b:User) SET r.x = 1", "variable-length"),
        ("MATCH (a:User)-[r:FOLLOWS*1..1]->(b:User) SET r.x = 1", "variable-length"),
        ("MATCH p = (a:User)-[:FOLLOWS]->(b:User) SET p.x = 1", "not a node or relationship"),
    ],
)
def test_what_cannot_be_written_fails_closed(cypher, message):
    with pytest.raises(CoreError) as exc:
        _aql(cypher)
    assert exc.value.code == "UNSUPPORTED"
    assert message in str(exc.value)
