"""Cypher names AQL cannot spell, and ``startNode`` / ``endNode``.

AQL refuses a variable that does not start, after any underscores, with an
ASCII letter — ``_001`` (TCK Create4), ``ä`` — even in backticks (ERR 1501),
and an attribute key such as ``_001`` after a dot. Live:
``tests/integration/test_reserved_names_live.py``.
"""

from __future__ import annotations

import re

import pytest

from arango_cypher import translate
from arango_cypher._translate_v0.naming import _rename_invalid_aql_variables
from arango_cypher.parser import parse_cypher
from tests.helpers.mapping_fixtures import mapping_bundle_for

PG = mapping_bundle_for("pg")


def _aql(cypher: str, **kw: bool) -> str:
    return translate(cypher, mapping=PG, **kw).aql


def test_an_underscore_digit_variable_gets_a_fresh_name() -> None:
    aql = _aql("CREATE (_001:User {name: 'y'}) RETURN _001.name AS n")
    assert re.search(r"(?<!\w)_001\b", aql) is None and "v_001" in aql


def test_the_fresh_name_avoids_the_query_s_other_variables() -> None:
    aql = _aql("UNWIND [1] AS _1 WITH _1 AS v_1 RETURN v_1")
    declared = re.findall(r"(?:FOR|LET) (\w+)", aql)
    assert "v_1" in declared and "_1" not in declared
    assert len(declared) == len(set(declared))


def test_renaming_is_deterministic_and_consistent() -> None:
    tree = parse_cypher("MATCH (_9)-->(ä) RETURN _9, ä").tree
    _rename_invalid_aql_variables(tree)
    assert tree.getText().count("v_9") == 2 and tree.getText().count("v__") == 2


def test_a_name_that_starts_well_is_quoted_not_renamed() -> None:
    aql = _aql("MATCH (aä:User) RETURN aä.name AS n")
    assert "FOR `aä` IN" in aql


def test_an_unaliased_column_keeps_the_written_name() -> None:
    aql = _aql("MATCH (_001:User) RETURN _001", cypher_column_names=True)
    assert '"_001": v_001' in aql or "RETURN v_001" in aql


@pytest.mark.parametrize("cypher", ["MATCH (n:User) RETURN n._001 AS x", "MATCH (n:User) SET n._001 = 1"])
def test_a_property_key_aql_cannot_spell_bare_is_quoted(cypher: str) -> None:
    assert "`_001`" in _aql(cypher)


def test_start_and_end_node_read_the_edge_s_endpoints() -> None:
    aql = _aql("MATCH (:User)-[r:FOLLOWS]->() RETURN startNode(r) AS s, endNode(r) AS e")
    assert "(IS_OBJECT(r) ? DOCUMENT(r._from) : null)" in aql
    assert "(IS_OBJECT(r) ? DOCUMENT(r._to) : null)" in aql
    assert "RETURN {s: null}" in _aql("RETURN startNode(null) AS s")
