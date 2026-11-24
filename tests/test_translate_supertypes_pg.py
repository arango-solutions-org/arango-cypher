"""Supertypes on PG: UNION entities (WP-35 stage 2; docs/ldbc_benchmark_plan.md D8).

LDBC's ``Message`` is every ``Post`` and every ``Comment``. On the PG layout the
members keep their own collections, so ``(m:Message)`` scans a ``UNION`` of one
branch per member (with the node's inline properties pushed into each) and a
traversal target or label predicate tests that the document is in one of the
member collections. The live counterpart is
``tests/integration/test_supertypes_pg_live.py``.
"""

from __future__ import annotations

import copy
import re

import pytest
from arango_query_core import CoreError, MappingBundle

from arango_cypher import translate
from tests.helpers.mapping_fixtures import mapping_bundle_for


def _t(cypher: str, mapping: MappingBundle | None = None, **params):
    return translate(cypher, mapping=mapping or mapping_bundle_for("ldbc_pg"), params=params or None)


def _members_test(q, var: str) -> list[str]:
    m = re.search(rf"PARSE_IDENTIFIER\({var}\)\.collection IN @(\w+)", q.aql)
    assert m, q.aql
    return q.bind_vars[m.group(1)]


def _scan_branches(q, var: str) -> list[str]:
    branches = re.findall(
        rf"\(FOR {var} IN @@(\w+)(?: FILTER [^()]*(?:\([^()]*\))?[^()]*)* RETURN {var}\)", q.aql
    )
    assert branches, q.aql
    return [q.bind_vars[f"@{b}"] for b in branches]


# ── scan start ────────────────────────────────────────────────────────


def test_scan_start_unions_the_member_collections():
    q = _t("MATCH (m:Message) RETURN m.id")
    assert q.aql.startswith("FOR m IN UNION((FOR m IN @@"), q.aql
    assert _scan_branches(q, "m") == ["post", "comment"]


def test_inline_properties_filter_inside_each_branch():
    q = _t("MATCH (m:Message {id: $id}) RETURN m.content", id=1)
    assert q.aql.count("FILTER (@id != null AND m.id == @id)") == 2, q.aql
    assert "\n  FILTER (@id != null AND m.id == @id)" not in q.aql  # not repeated after the union
    assert "indexHint" not in q.aql  # a hint names one collection's index; the branches have no OPTIONS


def test_where_stays_outside_the_union():
    q = _t("MATCH (m:Message) WHERE m.length > 10 RETURN m.id")
    head, _, tail = q.aql.partition("\n")
    assert "length" not in head and "m.length > 10" in tail, q.aql


def test_a_supertype_start_node_of_a_traversal():
    q = _t("MATCH (m:Message {id: 1})-[:HAS_CREATOR]->(p:Person) RETURN p.id")
    assert _scan_branches(q, "m") == ["post", "comment"]
    assert "OUTBOUND m @@" in q.aql


def test_place_and_organisation_are_unions_too():
    assert _scan_branches(_t("MATCH (x:Place) RETURN x.name"), "x") == ["city", "country", "continent"]
    assert _scan_branches(_t("MATCH (x:Organisation) RETURN x.name"), "x") == ["university", "company"]


# ── traversal target ──────────────────────────────────────────────────


def test_a_target_the_edge_guarantees_needs_no_filter():
    """HAS_CREATOR's domain is [Post, Comment]: whatever it reaches is a Message."""
    q = _t("MATCH (p:Person {id: 1})<-[:HAS_CREATOR]-(m:Message) RETURN m.id")
    assert "PARSE_IDENTIFIER" not in q.aql and "IS_SAME_COLLECTION" not in q.aql, q.aql


def test_a_target_the_edge_does_not_guarantee_is_tested_for_membership():
    """HAS_TAG also leads from forums, which are not Messages."""
    q = _t("MATCH (t:Tag {id: 1})<-[:HAS_TAG]-(m:Message) RETURN m.id")
    assert _members_test(q, "m") == ["post", "comment"]


def test_an_optional_supertype_target_is_tested_inside_the_optional_subquery():
    q = _t("MATCH (t:Tag {id: 1}) OPTIONAL MATCH (t)<-[:HAS_TAG]-(m:Message) RETURN t.name, m.id")
    sub = q.aql[q.aql.index("LET _opt_rows = (") : q.aql.index("FOR _opt_row")]
    assert "PARSE_IDENTIFIER(m).collection IN @" in sub, q.aql


def test_a_supertype_after_with_is_scanned_as_a_union():
    q = _t("MATCH (p:Person {id: 1}) WITH p MATCH (m:Message {id: 2}) RETURN p.id, m.id")
    assert _scan_branches(q, "m") == ["post", "comment"]


# ── labels and predicates ─────────────────────────────────────────────


def test_a_member_label_implies_its_supertype():
    q = _t("MATCH (m:Post:Message {id: 1}) RETURN m.id")
    assert q.bind_vars["@collection"] == "post"
    assert ".labels" not in q.aql and "PARSE_IDENTIFIER" not in q.aql, q.aql


def test_a_supertype_the_primary_does_not_imply_is_tested():
    q = _t("MATCH (x:Person:Message {id: 1}) RETURN x.id")
    assert _members_test(q, "x") == ["post", "comment"]


def test_label_predicate_on_a_supertype():
    q = _t("MATCH (t:Tag {id: 1})<-[:HAS_TAG]-(x:Forum) RETURN x:Message AS isMessage")
    assert _members_test(q, "x") == ["post", "comment"]


def test_exists_subquery_over_a_supertype():
    q = _t("MATCH (p:Person) WHERE EXISTS { MATCH (m:Message {id: 1}) } RETURN p.id")
    assert _scan_branches(q, "_sq_m") == ["post", "comment"]


# ── writes ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "cypher",
    [
        "MATCH (m:Message {id: 1}) SET m.length = 0",
    ],
)
def test_writing_a_union_variable_fails_closed(cypher):
    with pytest.raises(CoreError) as exc:
        _t(cypher)
    assert exc.value.code == "UNSUPPORTED"
    assert "supertype 'Message'" in str(exc.value)


def test_a_write_to_another_variable_matches_through_the_union():
    q = _t("MATCH (m:Message {id: 1})<-[r:LIKES]-(p:Person {id: 2}) DELETE r")
    assert _scan_branches(q, "m") == ["post", "comment"]
    removal = q.aql.splitlines()[-1]
    assert q.bind_vars["@" + removal.rsplit("IN @@", 1)[1].strip()] == "likes", q.aql


def test_deleting_a_union_variable_removes_from_every_member_collection():
    q = _t("MATCH (m:Message {id: 1}) DETACH DELETE m")
    removed = {
        q.bind_vars[k]
        for k in re.findall(r"REMOVE PARSE_IDENTIFIER\(_id\)\.key IN @(@deleteNodes\w*)", q.aql)
    }
    assert removed == {"post", "comment"}, q.aql


@pytest.mark.parametrize(
    "cypher, reason",
    [
        ("CREATE (m:Message {id: 1})", "name a concrete label"),
        ("MERGE (m:Message {id: 1})", "members live in several collections"),
    ],
)
def test_creating_or_merging_a_supertype_fails_closed(cypher, reason):
    with pytest.raises(CoreError) as exc:
        _t(cypher)
    assert exc.value.code == "UNSUPPORTED"
    assert reason in str(exc.value)


def test_merging_a_member_is_unaffected():
    q = _t("MERGE (p:Post {id: 1})")
    assert q.bind_vars["@collection"] == "post"


# ── other member mappings ─────────────────────────────────────────────


def _with_message_members(**member_maps) -> MappingBundle:
    """The LDBC PG mapping with Post/Comment re-mapped (e.g. to a shared ``message`` collection)."""
    base = mapping_bundle_for("ldbc_pg")
    pm = copy.deepcopy(base.physical_mapping)
    pm["entities"].update(member_maps)
    return MappingBundle(
        conceptual_schema=base.conceptual_schema, physical_mapping=pm, metadata=base.metadata
    )


def test_label_members_in_a_shared_collection_carry_their_type_filter():
    """A hybrid layout: posts and comments share ``message``, told apart by ``type``."""
    shared = {"collectionName": "message", "style": "LABEL", "typeField": "type"}
    mb = _with_message_members(
        Post={**shared, "typeValue": "Post"}, Comment={**shared, "typeValue": "Comment"}
    )
    scan = _t("MATCH (m:Message {id: 1}) RETURN m.id", mb)
    assert scan.aql.count("FILTER m[@mMemberTypeField") == 2, scan.aql
    target = _t("MATCH (t:Tag {id: 1})<-[:HAS_TAG]-(m:Message) RETURN m.id", mb)
    assert target.aql.count("IS_SAME_COLLECTION(@mMemberColl") == 2, target.aql
    assert " OR " in target.aql


def test_a_single_member_scan_is_one_branch():
    mb = _with_message_members(Message={"style": "UNION", "members": ["Post"]})
    q = _t("MATCH (m:Message) RETURN m.id", mb)
    assert q.aql.startswith("FOR m IN (FOR m IN @@mMember RETURN m)"), q.aql


def test_a_member_of_an_unknown_style_fails_closed():
    mb = _with_message_members(Comment={"collectionName": "comment", "style": "EMBEDDED"})
    with pytest.raises(CoreError) as exc:
        _t("MATCH (m:Message) RETURN m.id", mb)
    assert exc.value.code == "INVALID_MAPPING"
    assert "'Comment' is mapped EMBEDDED" in str(exc.value)
