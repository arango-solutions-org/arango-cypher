"""A hop that closes a pattern at a bound node checks the bound node's neighbours first (WP-44).

``_translate_v0/closing_hop.py`` adds ``FILTER open._id IN (<bound's neighbours>)``
before the walk when the statistics say the bound end has no more edges of the
type than the open end. The live test
(``tests/integration/test_closing_hop_prefilter_live.py``) shows the rows do
not change; these check where the filter appears and where it does not.
"""

from __future__ import annotations

import dataclasses
import re

import pytest
from arango_query_core import MappingBundle, MappingResolver

from arango_cypher import translate
from arango_cypher._translate_v0.closing_hop import prefers_neighbour_list
from tests.helpers.ldbc_statistics import SF0_1_STATISTICS
from tests.helpers.mapping_fixtures import mapping_bundle_for

#: LDBC BI18 (friend recommendation): the last hop closes at ``tag``.
_BI18 = (
    "MATCH (tag:Tag {name: $tag})<-[:HAS_INTEREST]-(person1:Person)-[:KNOWS]-(mutualFriend:Person)"
    "-[:KNOWS]-(person2:Person)-[:HAS_INTEREST]->(tag) "
    "WHERE person1 <> person2 "
    "RETURN person1.id AS p1, person2.id AS p2, count(DISTINCT mutualFriend) AS n"
)

_PREFILTER = re.compile(r"FILTER (\w+)\._id IN (APPEND\()?\(FOR _cl_e IN ")


def _bundle(layout: str, stats: dict | None = SF0_1_STATISTICS) -> MappingBundle:
    bundle = mapping_bundle_for(f"ldbc_{layout}")
    if stats is None:
        return bundle
    return dataclasses.replace(bundle, metadata={**bundle.metadata, "statistics": stats})


def _prefiltered(cypher: str, bundle: MappingBundle) -> list[str]:
    """The open variables that get a neighbour-list filter, in order."""
    aql = translate(cypher, mapping=bundle, params={"tag": "Frank_Sinatra"}).aql
    return [m.group(1) for m in _PREFILTER.finditer(aql)]


# --- the rule --------------------------------------------------------------


@pytest.mark.parametrize(
    ("rel_type", "direction", "expected"),
    [
        # a person has about 23 interests, a tag about 2 interested people
        ("HAS_INTEREST", "OUTBOUND", True),
        ("HAS_INTEREST", "INBOUND", False),
        # a message has about 1 tag, a tag about 19 messages
        ("HAS_TAG", "OUTBOUND", False),
        ("HAS_TAG", "INBOUND", True),
        # undirected: the same edges at either end
        ("KNOWS", "ANY", True),
    ],
)
def test_the_list_is_used_where_it_is_no_longer_than_the_walk(
    rel_type: str, direction: str, expected: bool
) -> None:
    resolver = MappingResolver(_bundle("pg"))
    assert prefers_neighbour_list(resolver, rel_type, direction) is expected


def test_no_statistics_no_list() -> None:
    assert prefers_neighbour_list(MappingResolver(_bundle("pg", None)), "HAS_INTEREST", "OUTBOUND") is False


def test_no_edges_no_list() -> None:
    stats = {
        "relationships": {
            "HAS_INTEREST": {**SF0_1_STATISTICS["relationships"]["HAS_INTEREST"], "avg_out_degree": 0.0}
        }
    }
    assert prefers_neighbour_list(MappingResolver(_bundle("pg", stats)), "HAS_INTEREST", "OUTBOUND") is False


# --- where the filter appears ------------------------------------------------


@pytest.mark.parametrize("layout", ["pg", "lpg"])
def test_bi18_checks_person2_against_the_tags_neighbours(layout: str) -> None:
    aql = translate(_BI18, mapping=_bundle(layout), params={"tag": "Frank_Sinatra"}).aql
    lines = [line.strip() for line in aql.splitlines()]
    i = next(i for i, line in enumerate(lines) if line.startswith("FILTER person2._id IN"))
    assert "FILTER _cl_e._to == tag._id" in lines[i] and "RETURN _cl_e._from" in lines[i], lines[i]
    # It comes right before the walk it saves, which still binds the edge.
    assert lines[i + 1].startswith("FOR tag_m, ") and " OUTBOUND person2 " in lines[i + 1], lines[i + 1]


def test_lpg_list_reads_only_the_relationships_type() -> None:
    out = translate(_BI18, mapping=_bundle("lpg"), params={"tag": "Frank_Sinatra"})
    assert "FILTER _cl_e._to == tag._id FILTER _cl_e[@clTypeField] == @clTypeValue" in out.aql
    assert out.bind_vars["clTypeField"] == "type"
    assert out.bind_vars["clTypeValue"] == "HAS_INTEREST"


@pytest.mark.parametrize("layout", ["pg", "lpg"])
def test_without_statistics_nothing_changes(layout: str) -> None:
    assert _prefiltered(_BI18, _bundle(layout, None)) == []


@pytest.mark.parametrize("layout", ["pg", "lpg"])
@pytest.mark.parametrize(
    "cypher",
    [
        # several pattern parts
        "MATCH (tag:Tag {name: $tag})<-[:HAS_INTEREST]-(p:Person), (p)-[:KNOWS]-(q:Person)-[:HAS_INTEREST]->(tag) "
        "RETURN q.id AS q",
        # a MATCH after WITH, and an OPTIONAL MATCH, from a bound tag
        "MATCH (tag:Tag {name: $tag}) WITH tag "
        "MATCH (tag)<-[:HAS_INTEREST]-(p:Person)-[:KNOWS]-(q:Person)-[:HAS_INTEREST]->(tag) RETURN q.id AS q",
        "MATCH (tag:Tag {name: $tag}) "
        "OPTIONAL MATCH (tag)<-[:HAS_INTEREST]-(p:Person)-[:KNOWS]-(q:Person)-[:HAS_INTEREST]->(tag) RETURN q.id AS q",
        # the leading MATCH of a multi-part query
        "MATCH (tag:Tag {name: $tag})<-[:HAS_INTEREST]-(p:Person)-[:KNOWS]-(q:Person)-[:HAS_INTEREST]->(tag) "
        "WITH q RETURN q.id AS q",
    ],
)
def test_every_match_compiler_adds_it(layout: str, cypher: str) -> None:
    assert _prefiltered(cypher, _bundle(layout)) == ["q"]


def test_an_inbound_closing_hop_lists_the_sources() -> None:
    cypher = (
        "MATCH (m:Post {id: 1})-[:HAS_CREATOR]->(p:Person)-[:LIKES]->(n:Post)-[:HAS_TAG]->(t:Tag)<-[:HAS_TAG]-(m) "
        "RETURN n.id AS n"
    )
    aql = translate(cypher, mapping=_bundle("pg")).aql
    # (t)<-[:HAS_TAG]-(m) walks INBOUND from t to m: t has about 19 messages,
    # m about one tag, so m's list is the shorter one.
    assert re.search(
        r"FILTER t\._id IN \(FOR _cl_e IN @@\w+ FILTER _cl_e\._from == m\._id RETURN _cl_e\._to\)", aql
    ), aql


def test_an_undirected_closing_hop_lists_both_orientations() -> None:
    cypher = "MATCH (a:Person {id: 1})-[:KNOWS]-(b:Person)-[:KNOWS]-(c:Person)-[:KNOWS]-(a) RETURN c.id AS c"
    aql = translate(cypher, mapping=_bundle("pg")).aql
    assert re.search(
        r"FILTER c\._id IN APPEND\("
        r"\(FOR _cl_e IN (@@\w+) FILTER _cl_e\._to == a\._id RETURN _cl_e\._from\), "
        r"\(FOR _cl_e IN \1 FILTER _cl_e\._from == a\._id RETURN _cl_e\._to\)\)",
        aql,
    ), aql


@pytest.mark.parametrize(
    "cypher",
    [
        # a message has fewer tags than the tag has messages: the walk is shorter
        "MATCH (t:Tag {name: $tag})<-[:HAS_TAG]-(m:Post)-[:HAS_CREATOR]->(p:Person)<-[:HAS_CREATOR]-(c:Comment)"
        "-[:HAS_TAG]->(t) RETURN c.id AS c",
        # a hop back to the node it starts from
        "MATCH (p:Person {id: 1})-[:KNOWS]-(p) RETURN p.id AS p",
        # variable length
        "MATCH (a:Person {id: 1})-[:KNOWS]-(b:Person)-[:KNOWS*1..2]-(a) RETURN b.id AS b",
        # several types
        "MATCH (tag:Tag {name: $tag})<-[:HAS_INTEREST]-(p:Person)-[:KNOWS]-(q:Person)"
        "-[:HAS_INTEREST|HAS_TAG]->(tag) RETURN q.id AS q",
    ],
)
@pytest.mark.parametrize("layout", ["pg", "lpg"])
def test_no_list_where_it_cannot_pay_or_does_not_apply(cypher: str, layout: str) -> None:
    assert _prefiltered(cypher, _bundle(layout)) == []
