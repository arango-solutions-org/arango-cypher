"""A pattern is read from its bound end, or else from the end an index finds (``pattern_reversal``).

The behaviour tests run on ArangoDB in ``tests/integration/test_unlabeled_nodes_pg_live.py``
and ``tests/integration/test_temporal_arithmetic_live.py``; reading from the
indexed end in ``tests/integration/test_anchor_selection_live.py``.
"""

from __future__ import annotations

import re

import pytest

from arango_cypher import translate
from tests.helpers.mapping_fixtures import mapping_bundle_for


def _aql(cypher: str, mapping=None) -> str:
    return translate(cypher, mapping=mapping or mapping_bundle_for("ldbc_pg")).aql


@pytest.mark.parametrize("layout", ["ldbc_pg", "ldbc_lpg"])
def test_an_optional_match_is_read_from_its_bound_end(layout: str) -> None:
    aql = _aql(
        "MATCH (tag:Tag) OPTIONAL MATCH (m:Message)-[:HAS_TAG]->(tag) RETURN tag.name, count(m) AS n",
        mapping_bundle_for(layout),
    )
    assert "INBOUND tag" in aql
    assert "_id == tag._id" not in aql


def test_a_pattern_bound_at_its_start_is_unchanged() -> None:
    aql = _aql("MATCH (tag:Tag) OPTIONAL MATCH (tag)<-[:HAS_TAG]-(m:Message) RETURN tag.name, count(m) AS n")
    assert "INBOUND tag" in aql


def test_a_pattern_bound_at_neither_end_is_unchanged() -> None:
    aql = _aql("MATCH (tag:Tag) WITH tag MATCH (m:Message)-[:HAS_TAG]->(t:Tag) RETURN count(m) AS n")
    assert "INBOUND" not in aql


# ---------------------------------------------------------------------------
# From the end an index finds (WP-44): a first MATCH, a MATCH after WITH and a
# part of a multi-part MATCH start where an inline equality on an indexed
# property is, when the written start has none.
# ---------------------------------------------------------------------------

_BI16 = "MATCH (p:Person)<-[:HAS_CREATOR]-(m:Message)-[:HAS_TAG]->(t:Tag {name: $name})"


def _start(aql: str) -> str:
    """The variable the outermost FOR iterates."""
    m = re.search(r"^FOR (\w+) IN", aql, re.MULTILINE)
    assert m, aql
    return m.group(1)


@pytest.mark.parametrize("layout", ["ldbc_pg", "ldbc_lpg"])
def test_a_first_match_starts_at_its_indexed_end(layout: str) -> None:
    # LPG's (type, name) index counts: the label test fixes its leading type field.
    aql = _aql(f"{_BI16} RETURN p.id AS p", mapping_bundle_for(layout))
    assert _start(aql) == "t", aql
    assert "INBOUND t" in aql


def test_an_indexed_start_is_kept() -> None:
    assert (
        _start(
            _aql(
                "MATCH (p:Person {id: 1})<-[:HAS_CREATOR]-(m:Message)-[:HAS_TAG]->(t:Tag {name: 'x'}) RETURN m.id"
            )
        )
        == "p"
    )


def test_an_equality_on_an_unindexed_property_is_no_anchor() -> None:
    assert (
        _start(
            _aql("MATCH (p:Person)<-[:HAS_CREATOR]-(m:Message)-[:HAS_TAG]->(t:Tag {url: 'x'}) RETURN p.id")
        )
        == "p"
    )


def test_a_named_path_keeps_its_order() -> None:
    aql = _aql(
        "MATCH path = (p:Person)<-[:HAS_CREATOR]-(m:Message)-[:HAS_TAG]->(t:Tag {name: 'x'}) RETURN path"
    )
    assert _start(aql) == "p", aql


def test_a_variable_length_relationship_variable_keeps_its_order() -> None:
    # k lists the relationships in path order, so reading backwards would reverse it.
    aql = _aql("MATCH (a:Person)-[k:KNOWS*1..2]-(b:Person {id: 1}) RETURN size(k) AS n")
    assert _start(aql) == "a", aql
    assert _start(_aql("MATCH (a:Person)-[:KNOWS*1..2]-(b:Person {id: 1}) RETURN a.id")) == "b"


def test_a_match_after_with_starts_at_its_indexed_end() -> None:
    aql = _aql(
        f"MATCH (f:Forum {{id: 1}}) WITH f.title AS name {_BI16.replace('$name', 'name')} RETURN p.id AS p"
    )
    assert re.search(r"FOR t IN .*\n.*\n\s*FOR m, \w+ IN 1\.\.1 INBOUND t", aql), aql


def test_a_part_of_a_multi_part_match_starts_at_its_indexed_end() -> None:
    aql = _aql(f"{_BI16}, (f:Forum) RETURN p.id AS p, f.id AS f")
    assert _start(aql) == "t", aql


def test_a_part_joined_to_an_earlier_part_starts_there() -> None:
    # p is bound by the first part: the second starts from it, index or not.
    aql = _aql(
        "MATCH (p:Person {id: 1}), (p)<-[:HAS_CREATOR]-(m:Message)-[:HAS_TAG]->(t:Tag {name: 'x'}) RETURN m.id"
    )
    assert "INBOUND p" in aql, aql
