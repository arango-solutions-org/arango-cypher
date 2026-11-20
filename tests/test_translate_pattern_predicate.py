"""Pattern predicates honour the whole pattern (docs/ldbc_benchmark_plan.md §3.5).

Found by the LDBC execution tier: ``NOT (person1)-[:KNOWS]-(person2)`` in BI18
was lowered to "person1 has any KNOWS edge" because the end node's variable
was dropped, so the query returned no rows where Neo4j returned six. The same
compiler also dropped every hop after the first, a collection-style label on
the end node, and node/relationship properties.
"""

from __future__ import annotations

import re

import pytest
from arango_query_core import CoreError

from arango_cypher import translate
from tests.helpers.mapping_fixtures import mapping_bundle_for


def _aql(cypher: str, layout: str) -> tuple[str, dict]:
    q = translate(cypher, mapping=mapping_bundle_for(f"ldbc_{layout}"))
    return q.aql, q.bind_vars


@pytest.mark.parametrize("layout", ["pg", "lpg"])
def test_a_bound_end_node_constrains_the_predicate(layout):
    aql, _ = _aql(
        "MATCH (a:Person)-[:KNOWS]-(m:Person)-[:KNOWS]-(b:Person) WHERE NOT (a)-[:KNOWS]-(b) RETURN a.id, b.id",
        layout,
    )
    # Both ends bound: the edge is looked up by them, either way round (ANY).
    assert (
        "(_pp_e._from == a._id AND _pp_e._to == b._id) OR (_pp_e._from == b._id AND _pp_e._to == a._id)"
        in aql
    )


def test_an_anonymous_end_node_only_needs_some_neighbour():
    aql, _ = _aql("MATCH (a:Person) WHERE (a)-[:KNOWS]->() RETURN a.id", "pg")
    assert "FOR _pp_v, _pp_e IN 1..1 OUTBOUND a @@ppEdge LIMIT 1 RETURN 1" in aql


@pytest.mark.parametrize("layout", ["pg", "lpg"])
def test_every_hop_is_compiled(layout):
    aql, binds = _aql(
        "MATCH (a:Person), (b:Comment) WHERE (a)<-[:HAS_CREATOR]-(:Post)<-[:REPLY_OF]-(b) RETURN a.id",
        layout,
    )
    first = re.search(r"FOR _pp_v, _pp_e IN 1\.\.1 INBOUND a @@(\w+)", aql)
    # The last hop ends at the bound b: an edge lookup from b to the Post.
    second = re.search(
        r"FOR _pp_e_1 IN @@(\w+) FILTER \(_pp_e_1\._from == b\._id AND _pp_e_1\._to == _pp_v\._id\)", aql
    )
    assert first and second, aql
    if layout == "pg":
        assert binds[f"@{first.group(1)}"] == "has_creator"
        assert binds[f"@{second.group(1)}"] == "reply_of"
    assert "_pp_e_1._id != _pp_e._id" in aql


def test_collection_style_end_label_is_checked():
    aql, binds = _aql("MATCH (c:Comment) WHERE (c)-[:REPLY_OF]->(:Post) RETURN c.id", "pg")
    m = re.search(r"IS_SAME_COLLECTION\(@(\w+), _pp_v\)", aql)
    assert m, aql
    assert binds[m.group(1)] == "post"


def test_label_style_end_label_is_checked():
    aql, binds = _aql("MATCH (c:Comment) WHERE (c)-[:REPLY_OF]->(:Post) RETURN c.id", "lpg")
    m = re.search(r"_pp_v\[@(\w+)\] == @(\w+)", aql)
    assert m, aql
    assert binds[m.group(2)] == "Post"


def test_end_node_and_relationship_properties_are_filters():
    aql, _ = _aql(
        "MATCH (p:Person) WHERE (p)-[:STUDY_AT {classYear: 2004}]->(:University {name: 'X'}) RETURN p.id",
        "pg",
    )
    assert "(_pp_e.classYear == 2004)" in aql
    assert "(_pp_v.name == 'X')" in aql or '(_pp_v.name == "X")' in aql


def test_start_node_labels_are_checked_on_the_bound_variable():
    aql, binds = _aql(
        "MATCH (f:Forum)-[:CONTAINER_OF]->(x:Post) WHERE (x:Post)-[:HAS_TAG]->() RETURN f.id", "pg"
    )
    m = re.search(r"\(IS_SAME_COLLECTION\(@(\w+), x\)\) AND \(LENGTH\(FOR _pp_v", aql)
    assert m, aql
    assert binds[m.group(1)] == "post"


def test_an_anonymous_start_node_is_refused():
    with pytest.raises(CoreError) as exc:
        _aql("MATCH (b:Person) WHERE ()-[:KNOWS]->(b) RETURN b.id", "pg")
    assert exc.value.code == "UNSUPPORTED"
    assert "bound earlier" in str(exc.value)
