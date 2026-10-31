"""Writes that need their reads (and other writes) sequenced inside one AQL query.

Live behaviour: ``tests/integration/test_pipeline_deletes_live.py``,
``tests/integration/test_write_clauses_live.py`` and the TCK (Delete5,
Merge6, Merge7) through the execution ratchet.
"""

from __future__ import annotations

import pytest
from arango_query_core import CoreError

from arango_cypher import translate
from arango_cypher._translate_v0.merge_bindings import endpoint_action_lines
from arango_cypher._translate_v0.merge_docs import MapAction, insert_document, update_clause, upsert_options
from arango_cypher._translate_v0.pipeline_deletes import needs_pipeline_delete
from arango_cypher._translate_v0.pipeline_writes import nullable_vars
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


# -- What a MERGE binds besides the entity it writes ---------------------------


def test_merge_binds_a_named_path_to_the_node() -> None:
    aql = _aql("MERGE p = (n:User {name: 'z'}) RETURN p")
    assert "LET n = NEW\nLET p = {nodes: [n], edges: []}" in aql


def test_merge_binds_a_named_path_to_one_hop_in_pattern_order() -> None:
    aql = _aql("MATCH (a:User), (b:User) MERGE p = (a)<-[r:FOLLOWS]-(b) RETURN p")
    assert "UPSERT {_from: b._id, _to: a._id}" in aql
    assert "LET p = {nodes: [a, b], edges: [r]}" in aql


@pytest.mark.parametrize(
    "cypher",
    [
        "MERGE p = (a:User)-[:FOLLOWS]->(b:User)-[:FOLLOWS]->(c:User) RETURN p",
        "MERGE p = (a:User {name: 'x'}) MERGE (b:User {name: 'y'}) RETURN p",
    ],
)
def test_a_named_path_the_merge_would_not_bind_is_refused(cypher: str) -> None:
    with pytest.raises(CoreError, match="named path"):
        _aql(cypher)


def test_on_create_set_on_an_endpoint_updates_it_after_the_upsert() -> None:
    aql = _aql("MATCH (a:User), (b:User) MERGE (a)-[:FOLLOWS]->(b) ON CREATE SET b.created = 1")
    lines = [line.strip() for line in aql.splitlines()]
    upsert_end = lines.index("IN @@edgeCollection")
    assert lines[upsert_end + 1] == "LET _merge_old = OLD"
    # Nothing follows: the endpoint UPDATE ends the query.
    assert lines[-1].startswith("FOR _w IN [{d: b, p: (_merge_old == null ? {created: 1} : null)}]")
    assert lines[-1].endswith("UPDATE _w.d WITH _w.p IN @@collection2")  # b's binding


def test_endpoint_sets_before_a_return_are_a_subquery() -> None:
    aql = _aql(
        "MATCH (a:User), (b:User) MERGE (a)-[r:FOLLOWS]->(b) ON MATCH SET a.seen = 1, b.seen = 2 "
        "RETURN count(r) AS c"
    )
    # a and b are both users: one UPDATE writes both (a second would be ERR 1579).
    assert aql.count("UPDATE _w.d") == 1
    assert (
        "LET _endpoint_sets0 = (FOR _w IN [{d: a, p: (_merge_old != null ? {seen: 1} : null)}, {d: b," in aql
    )


def test_a_return_of_an_endpoint_the_merge_set_is_refused() -> None:
    with pytest.raises(CoreError, match="without the SET"):
        _aql("MATCH (a:User), (b:User) MERGE (a)-[:FOLLOWS]->(b) ON CREATE SET b.created = 1 RETURN b")


def test_endpoint_action_lines_needs_the_endpoint_collection() -> None:
    with pytest.raises(CoreError, match="not known here"):
        endpoint_action_lines({("b", True): ["x: 1"]}, {}, {}, final=True)


def test_a_relationship_merge_matches_on_the_relationship_properties() -> None:
    aql = _aql("MATCH (a:User), (b:User) MERGE (a)-[r:FOLLOWS {w: 6}]->(b) RETURN r")
    assert "UPSERT {_from: a._id, _to: b._id, w: 6}" in aql


# -- MERGE of a relationship whose endpoints nothing binds ---------------------


def test_an_unbound_pattern_merge_reads_the_whole_pattern_then_inserts() -> None:
    aql = _aql("MERGE (a:User {name: 'x'})-[r:FOLLOWS {w: 1}]->(b:User {name: 'y'}) RETURN r")
    find = aql.index("LET _merge_found = FIRST(")
    assert find < aql.index("INSERT _d IN @@collection") < aql.index("INSERT _d IN @@edgeCollection")
    assert "MATCHES(_mr, {w: 1})" in aql
    # Both users are one collection: one INSERT writes both (ERR 1579 otherwise).
    assert aql.count("INSERT _d IN @@collection RETURN NEW") == 1
    assert "LET a = _merge_found != null ? _merge_found.s : _merge_new0[0]" in aql
    assert "LET r = _merge_found != null ? _merge_found.r : _merge_new_rel[0]" in aql


def test_an_unbound_pattern_merge_without_return_ends_on_the_edge_insert() -> None:
    aql = _aql("MERGE (:User {name: 'x'})<-[:FOLLOWS]-(:User {name: 'y'})")
    last = aql.splitlines()[-1].strip()
    assert last.startswith("FOR _d IN (_merge_found == null ? [{_from: _mergeEnd._id, _to: _mergeStart._id")
    assert last.endswith("INSERT _d IN @@edgeCollection")


def test_an_unbound_self_loop_merge_creates_one_node() -> None:
    aql = _aql("MERGE p = (a:User {name: 'x'})-[:FOLLOWS]->(a) RETURN p")
    assert "[{name: 'x'}]" in aql or "[{name: @" in aql
    assert "LET p = {nodes: [a, a], edges: [_mergeRel]}" in aql


@pytest.mark.parametrize(
    ("cypher", "match"),
    [
        ("MATCH (a:User) MERGE (a)-[:FOLLOWS]->(b:User)", "only as the query's first clause"),
        ("MERGE (a:User)-[:FOLLOWS]->(b:User)-[:FOLLOWS]->(c:User)", "multi-hop"),
        ("MERGE (a:User)-[:FOLLOWS]->(b:User) ON CREATE SET a.x = 1", "ON CREATE / ON MATCH"),
        ("MERGE (a:User)-[:FOLLOWS]->(a:User {name: 'z'})", "VariableAlreadyBound"),
    ],
)
def test_unbound_endpoint_merges_it_does_not_cover_are_refused(cypher: str, match: str) -> None:
    with pytest.raises(CoreError, match=match):
        _aql(cypher)


# -- SET / REMOVE after several reading clauses --------------------------------


def test_remove_after_optional_match_skips_the_null_rows() -> None:
    aql = _aql("MATCH (n:User) OPTIONAL MATCH (n)-[r:FOLLOWS]->() REMOVE r.w RETURN n")
    assert "LET _set_r = (FOR _w IN (r == null ? [] : [r]) UPDATE _w WITH" in aql
    assert aql.rstrip().endswith("RETURN n")


def test_set_after_two_matches_writes_in_the_inner_loop() -> None:
    aql = _aql("MATCH (a:User) MATCH (b:Doc) SET a.x = b.x")
    assert aql.index("FOR b IN") < aql.index("UPDATE a WITH {x: b.x}")


def test_nullable_vars_follows_optional_bindings_and_aliases() -> None:
    lines = ["FOR n IN c", "  LET r = _opt_row.r", "  LET m = _opt_row2.m", "  LET x = r", "  LET y = n"]
    assert nullable_vars(lines) == {"r", "m", "x"}
