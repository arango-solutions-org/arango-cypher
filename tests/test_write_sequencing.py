"""Writes that need their reads (and other writes) sequenced inside one AQL query.

Live behaviour: ``tests/integration/test_pipeline_deletes_live.py``,
``tests/integration/test_write_clauses_live.py`` and the TCK (Delete5,
Merge6, Merge7) through the execution ratchet.
"""

from __future__ import annotations

import pytest
from arango_query_core import CoreError

from arango_cypher import translate
from arango_cypher._translate_v0.merge_docs import MapAction, insert_document, update_clause, upsert_options
from arango_cypher._translate_v0.pipeline_deletes import needs_pipeline_delete
from arango_cypher.parser import parse_cypher
from tests.helpers.mapping_fixtures import mapping_bundle_for


def _aql(cypher: str, params: dict | None = None) -> str:
    return translate(cypher, mapping=mapping_bundle_for("pg"), params=params).aql


# -- DELETE of an expression after WITH ----------------------------------------


def test_a_delete_of_a_list_element_reads_every_row_before_removing() -> None:
    aql = _aql("MATCH (:User)-[:FOLLOWS]->(n:User) WITH collect(n) AS fs DETACH DELETE fs[$i]", {"i": 1})
    assert aql.index("LET _matched = (") < aql.index("REMOVE")
    assert "_targets: [(fs)[@i]]" in aql
    assert "LET _detach0" in aql  # DETACH: the edges touching the deleted nodes go too
    assert aql.count("REMOVE PARSE_IDENTIFIER") == len(["follows", "docs", "persons", "places", "users"])


def test_a_bare_variable_keeps_the_in_loop_remove() -> None:
    aql = _aql("MATCH (n:User) WITH n DELETE n")
    assert "LET _matched" not in aql and "REMOVE n IN @@collection" in aql


def test_a_delete_after_with_can_return_the_rows_it_read() -> None:
    aql = _aql("MATCH (n:User) WITH collect(n) AS ns DETACH DELETE ns[0] RETURN size(ns) AS c")
    tail = aql[aql.index("FOR _row IN _matched") :]
    assert "LET ns = _row.ns" in tail and "RETURN" in tail


@pytest.mark.parametrize(
    ("cypher", "expected"),
    [("MATCH (n) WITH n DELETE n", False), ("MATCH (n) WITH {k: n} AS m DELETE m.k", True)],
)
def test_needs_pipeline_delete(cypher: str, expected: bool) -> None:
    tree = parse_cypher(cypher).tree
    mpq = tree.oC_Statement().oC_Query().oC_RegularQuery().oC_SingleQuery().oC_MultiPartQuery()
    deletes = [uc.oC_Delete() for uc in mpq.oC_SinglePartQuery().oC_UpdatingClause()]
    assert needs_pipeline_delete(deletes) is expected


# -- MERGE actions that set a whole map ----------------------------------------


def test_on_create_set_a_whole_node_copies_its_properties_onto_the_relationship() -> None:
    aql = _aql("MATCH (a:User), (b:User) MERGE (a)-[r:FOLLOWS]->(b) ON CREATE SET r = a")
    insert = next(line.strip() for line in aql.splitlines() if line.strip().startswith("INSERT"))
    assert 'MERGE(KEEP({_from: a._id, _to: b._id}, "_from", "_to")' in insert
    assert "UNSET(a, [" in insert  # a's properties, not its system fields


def test_on_match_merging_a_map_replaces_old_with_it() -> None:
    aql = _aql("MATCH (a:User), (b:User) MERGE (a)-[r:FOLLOWS]->(b) ON MATCH SET r += {w: 1, x: null}")
    assert "REPLACE FIRST(LET _doc = MERGE(OLD, " in aql
    assert "UPDATE" not in aql


def test_a_whole_map_set_on_a_node_merge_is_still_refused() -> None:
    with pytest.raises(CoreError, match="whole map"):
        _aql("MERGE (n:User {id: 1}) ON CREATE SET n = {x: 1}")


def test_merge_docs_keep_plain_documents_for_property_actions() -> None:
    assert insert_document(["_from: a"], ["x: 1"], ["_from"]) == "{_from: a, x: 1}"
    assert update_clause(["y: 2"], ["_from"]) == "UPDATE {y: 2}"
    assert upsert_options(["y: 2"]) == ""


def test_a_literal_null_is_not_inserted_and_is_removed_on_update() -> None:
    assert insert_document(["_from: a"], ["name: null"], ["_from"]) == "{_from: a}"
    assert upsert_options(["name: null"]) == " OPTIONS {keepNull: false}"


def test_map_actions_apply_in_order() -> None:
    doc = insert_document(["_from: a"], ["x: 1", MapAction("m", replaces=True), "y: 2"], ["_from"])
    assert doc.startswith(
        'FIRST(LET _doc = MERGE(MERGE(KEEP(MERGE({_from: a}, {x: 1}), "_from"), m), {y: 2})'
    )
