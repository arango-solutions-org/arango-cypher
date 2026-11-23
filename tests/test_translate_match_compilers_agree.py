"""The MATCH compilers apply the same pattern rules.

A single-pattern ``MATCH … RETURN`` (``_translate_match_body``) had fixes the
other compilers lacked: a MATCH before WITH or a write, comma-separated parts,
a MATCH after WITH or an OPTIONAL MATCH, and pattern predicates. Each case
below was wrong in one of them:

- an unnamed node or relationship took a name the query gives another
  variable, and was silently joined with it (``MATCH (:User), (n1:User)``);
- an undirected hop back to its own start (``(a)-[r]-(a)``) matched each
  self-loop twice;
- a variable-length relationship's properties were checked on its last edge
  only, and ``r`` was that edge, not the list;
- one relationship could match twice across the hops of a pattern before WITH.

The live counterpart is ``tests/integration/test_match_compilers_agree_live.py``.
"""

from __future__ import annotations

import re

import pytest

from arango_cypher import translate
from tests.helpers.mapping_fixtures import mapping_bundle_for

_EVERY_EDGE = re.compile(r"LENGTH\((\w+)\.edges\[\* FILTER NOT \(\(CURRENT\.w == 1\)\)\]\) == 0")


def _aql(cypher: str, fixture: str = "pg") -> str:
    return translate(cypher, mapping=mapping_bundle_for(fixture)).aql


# --- unnamed variables never take a name the query uses ------------------------


@pytest.mark.parametrize(
    "cypher",
    [
        "MATCH (:User), (n1:User) RETURN count(*)",
        "MATCH (:User)-[:FOLLOWS]->(:User), (u1:User) RETURN count(*)",
        "MATCH (a:User)-[:FOLLOWS]->(), (v1_1:User) RETURN count(*)",
    ],
)
def test_an_unnamed_node_in_comma_parts_is_not_joined_with_a_named_one(cypher: str) -> None:
    loops = re.findall(r"FOR (\w+)(?:, \w+)? IN", _aql(cypher))
    assert len(loops) == len(set(loops)), loops
    assert len(loops) == cypher.count("(") - cypher.count("count(")


def test_an_unnamed_relationship_in_comma_parts_does_not_take_a_named_ones_name() -> None:
    aql = _aql("MATCH (a:User)-[:FOLLOWS]->(b:User), (c:User)-[r:FOLLOWS]->(d:User) RETURN r")
    assert re.search(r"FOR b, (\w+) IN", aql).group(1) != "r"
    assert "FOR d, r IN" in aql


# --- an undirected hop back to its start walks one way --------------------------


@pytest.mark.parametrize(
    "cypher",
    [
        "MATCH (a:User)-[r:FOLLOWS]-(a) RETURN count(r)",
        "MATCH (a:User), (a)-[r:FOLLOWS]-(a) RETURN count(r)",
        "MATCH (a:User) WITH a MATCH (a)-[r:FOLLOWS]-(a) RETURN count(r)",
    ],
)
def test_an_undirected_self_loop_is_matched_once_by_every_compiler(cypher: str) -> None:
    aql = _aql(cypher)
    assert "1..1 OUTBOUND a" in aql
    assert " ANY " not in aql


# --- a variable-length relationship is checked on every edge --------------------


@pytest.mark.parametrize(
    "cypher",
    [
        "MATCH (a:User)-[r:FOLLOWS*1..2 {w: 1}]->(b) RETURN count(*)",
        "MATCH (a:User), (a)-[r:FOLLOWS*1..2 {w: 1}]->(b) RETURN count(*)",
        "MATCH (a:User)-[r:FOLLOWS*1..2 {w: 1}]->(b) WITH b RETURN count(*)",
        "MATCH (a:User) WITH a MATCH (a)-[r:FOLLOWS*1..2 {w: 1}]->(b) RETURN count(*)",
        "MATCH (a:User) OPTIONAL MATCH (a)-[r:FOLLOWS*1..2 {w: 1}]->(b) RETURN count(b)",
        "MATCH (a:User) WHERE (a)-[:FOLLOWS*1..2 {w: 1}]->() RETURN count(*)",
        "MATCH (a:User) RETURN [(a)-[:FOLLOWS*1..2 {w: 1}]->(b) | b.name] AS names",
    ],
)
def test_variable_length_properties_hold_on_every_edge_in_every_compiler(cypher: str) -> None:
    aql = _aql(cypher)
    assert _EVERY_EDGE.search(aql), aql
    assert not re.search(r"\b(?!CURRENT)\w+\.w == 1", aql), aql


@pytest.mark.parametrize(
    "cypher",
    [
        "MATCH (a:User)-[r:FOLLOWS*1..2]->(b) WITH r RETURN r",
        "MATCH (a:User), (a)-[r:FOLLOWS*1..2]->(b) RETURN r",
    ],
)
def test_a_used_variable_length_relationship_is_its_list_of_edges(cypher: str) -> None:
    aql = _aql(cypher)
    path = re.search(r"FOR b, \w+, (\w+) IN 1\.\.2", aql)
    assert path is not None, aql
    assert f"LET r = {path.group(1)}.edges" in aql


# --- one relationship is never matched twice within a pattern -------------------


def test_relationship_uniqueness_holds_before_with() -> None:
    aql = _aql("MATCH (a:User)-[r1:FOLLOWS]-(b)-[r2:FOLLOWS]-(c) WITH c RETURN count(*)")
    assert "FILTER r1._id != r2._id" in aql


def test_a_variable_length_hop_joins_the_uniqueness_check_across_comma_parts() -> None:
    aql = _aql("MATCH (a:User)-[:FOLLOWS*1..2]->(b), (b)-[s:FOLLOWS]->(c) RETURN count(*)")
    assert re.search(r"FILTER s\._id NOT IN \w+\.edges\[\*\]\._id", aql), aql


def test_a_single_variable_length_part_needs_no_path() -> None:
    aql = _aql("MATCH (a:User), (a)-[:FOLLOWS*1..2]->(b) RETURN count(*)")
    assert re.search(r"FOR b, \w+ IN 1\.\.2", aql), aql
