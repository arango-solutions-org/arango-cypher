"""UNWIND, OPTIONAL MATCH and write translations that ran but were wrong.

Each case compiled to AQL that ArangoDB rejected or that returned or wrote the
wrong thing:

- ``UNWIND`` before a ``MATCH``, ``CREATE`` or ``MERGE`` looped over its value
  as it is, so a scalar or null was ERR 1563 (``unwind_source`` was skipped);
- an ``UNWIND`` after the MATCH was dropped when one also came before it;
- ``UNWIND``/``CALL`` then ``OPTIONAL MATCH`` null-extended the whole result
  once instead of each row, losing rows;
- ``OPTIONAL MATCH … SET`` with no RETURN ended on a ``LET`` (ERR 1501);
- a relationship MERGE after two node MERGEs left the pattern's properties out
  of what it matches, and a node MERGE stored a property set to null and wrote
  ``n.a.b`` to ``n.b``.

The live counterpart is ``tests/integration/test_unwind_optional_writes_live.py``.
"""

from __future__ import annotations

import re

import pytest
from arango_query_core import CoreError, ExtensionPolicy, ExtensionRegistry

from arango_cypher import translate
from arango_cypher.procedures import parse_signature, register_table_procedure
from tests.helpers.mapping_fixtures import mapping_bundle_for


def _aql(cypher: str, fixture: str = "pg", **kwargs) -> str:
    return translate(cypher, mapping=mapping_bundle_for(fixture), **kwargs).aql


def _names_registry() -> ExtensionRegistry:
    registry = ExtensionRegistry(policy=ExtensionPolicy(enabled=True))
    register_table_procedure(
        registry, parse_signature("test.names() :: (name :: STRING?)"), [{"name": "alice"}, {"name": "zed"}]
    )
    return registry


# --- UNWIND -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("cypher", "loop"),
    [
        ("UNWIND 5 AS x MATCH (u:User) RETURN x", "FOR x IN [5]"),
        ("UNWIND null AS x MATCH (u:User) RETURN x", "FOR x IN []"),
        ("UNWIND null AS x CREATE (:User {name: x})", "FOR x IN []"),
        ("UNWIND 1 AS x MERGE (:User {name: toString(x)})", "FOR x IN [1]"),
    ],
)
def test_unwind_of_a_scalar_or_null_before_a_match_or_write_loops_over_a_list(cypher: str, loop: str) -> None:
    assert loop in _aql(cypher)


def test_unwind_of_a_list_is_not_wrapped() -> None:
    assert "FOR x IN [1,2]" in _aql("UNWIND [1,2] AS x MATCH (u:User) RETURN x")


def test_unwind_after_the_match_is_kept_when_one_comes_before_it() -> None:
    aql = _aql("UNWIND [1,2] AS x MATCH (u:User) UNWIND [3,4] AS y RETURN x, y, u.name")
    assert aql.index("FOR x IN [1,2]") < aql.index("FOR u IN") < aql.index("FOR y IN [3,4]")


# --- OPTIONAL MATCH with no MATCH --------------------------------------------


def test_unwind_then_optional_match_null_extends_each_row() -> None:
    aql = _aql("UNWIND ['alice', 'zed'] AS name OPTIONAL MATCH (u:User {name: name}) RETURN name, u.name")
    assert "LET _rows" not in aql
    # The OPTIONAL MATCH runs inside the UNWIND loop, once per row.
    assert aql.index("FOR name IN") < aql.index("LET _opt_rows = (") < aql.index("FOR _opt_row IN")


def test_call_then_optional_match_null_extends_each_row() -> None:
    aql = _aql(
        "CALL test.names() YIELD name OPTIONAL MATCH (u:User {name: name}) RETURN name, u.name",
        registry=_names_registry(),
    )
    assert "LET _rows" not in aql
    assert aql.index("_call_row") < aql.index("LET _opt_rows = (")


def test_clauses_between_optional_matches_run_in_source_order() -> None:
    aql = _aql(
        "OPTIONAL MATCH (u:User) UNWIND [1, 2] AS y OPTIONAL MATCH (p:Person {name: u.name}) RETURN y, p.name"
    )
    first, unwind, second = (
        aql.index("LET _opt_rows = ("),
        aql.index("FOR y IN"),
        aql.index("LET _opt_rows_1"),
    )
    assert first < unwind < second


def test_optional_match_with_no_input_still_projects_one_row() -> None:
    aql = _aql("OPTIONAL MATCH (u:User {name: 'nobody'}) RETURN count(u) AS n")
    assert "LENGTH(_opt_rows) > 0 ? _opt_rows : [{}]" in aql


# --- OPTIONAL MATCH … SET / DELETE with no RETURN -------------------------------


@pytest.mark.parametrize("fixture", ["pg", "lpg_labels"])
@pytest.mark.parametrize(
    "cypher",
    [
        "OPTIONAL MATCH (u:User) SET u.x = 1",
        "OPTIONAL MATCH (u:User) SET u.x = 1, u.y = 2 REMOVE u.z",
    ],
)
def test_optional_match_write_with_no_return_ends_on_the_write(fixture: str, cypher: str) -> None:
    last = _aql(cypher, fixture).splitlines()[-1].strip()
    assert last.startswith("FOR _w IN (u == null ? [] : [u]) UPDATE _w WITH"), last


def test_optional_match_write_with_a_return_reads_the_new_document() -> None:
    aql = _aql("OPTIONAL MATCH (u:User) SET u.x = 1 RETURN u")
    assert "LET u_new = FIRST(FOR _w IN (u == null ? [] : [u]) UPDATE _w WITH {x: 1}" in aql
    assert aql.splitlines()[-1].strip() == "RETURN u_new"


# --- MERGE ----------------------------------------------------------------------


def test_relationship_merge_after_node_merges_matches_the_pattern_properties() -> None:
    aql = _aql("MERGE (a:Person {id: 1}) MERGE (b:Place {id: 2}) MERGE (a)-[r:FOLLOWS {since: 2020}]->(b)")
    assert re.search(r"LET r = FIRST\(UPSERT \{_from: a\._id, _to: b\._id, since: 2020\} ", aql), aql


def test_node_merge_leaves_a_null_on_create_property_out() -> None:
    aql = _aql("MERGE (u:User {name: 'n'}) ON CREATE SET u.nick = null, u.age = 3")
    assert "INSERT {name: 'n', age: 3}" in aql


def test_node_merge_removes_a_property_set_to_null_on_match() -> None:
    aql = _aql("MERGE (u:User {name: 'n'}) ON MATCH SET u.age = null")
    assert "UPDATE {age: null}" in aql
    assert "IN @@collection OPTIONS {keepNull: false}" in aql


def test_node_merge_with_no_null_action_has_no_options() -> None:
    aql = _aql("MERGE (u:User {name: 'n'}) ON MATCH SET u.age = 3")
    assert "OPTIONS" not in aql


def test_multi_merge_removes_a_property_set_to_null_on_match() -> None:
    aql = _aql("MERGE (a:Person {id: 1}) ON MATCH SET a.age = null MERGE (b:Place {id: 2})")
    assert "UPDATE {age: null} IN @@collection OPTIONS {keepNull: false} RETURN NEW)" in aql


@pytest.mark.parametrize(
    "cypher",
    [
        "MERGE (u:User {name: 'n'}) ON CREATE SET u.a.b = 1",
        "MERGE (u:User {name: 'n'}) ON MATCH SET u.a.b = 1",
    ],
)
def test_node_merge_refuses_a_nested_property_action(cypher: str) -> None:
    with pytest.raises(CoreError) as exc:
        _aql(cypher)
    assert exc.value.code == "UNSUPPORTED"
    assert "nested property" in str(exc.value)
