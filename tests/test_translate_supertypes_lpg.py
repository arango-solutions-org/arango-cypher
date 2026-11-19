"""Supertypes on LPG: MULTI_LABEL entities (WP-35 stage 1; docs/ldbc_benchmark_plan.md D8).

LDBC's ``Message`` is every ``Post`` and every ``Comment``. On the LPG layout
each node's ``labels`` array lists its supertype, so ``(m:Message)`` becomes
``@labelValue IN m[@labelsField]``. Creating a member writes that array, and a
replacement keeps it, so supertype queries see new nodes too.
"""

from __future__ import annotations

import copy
import dataclasses
import re

import pytest
from arango_query_core import CoreError, MappingBundle

from arango_cypher import translate
from tests.helpers.mapping_fixtures import mapping_bundle_for


def _t(cypher: str, **params):
    return translate(cypher, mapping=mapping_bundle_for("ldbc_lpg"), params=params or None)


def _multi_label(q, var: str) -> tuple[str, str]:
    m = re.search(rf"@(\w+) IN {var}\[@(\w+)\]", q.aql)
    assert m, q.aql
    return q.bind_vars[m.group(1)], q.bind_vars[m.group(2)]


def test_scan_start_filters_on_the_labels_array():
    q = _t("MATCH (m:Message {id: 1}) RETURN m.id")
    assert _multi_label(q, "m") == ("Message", "labels")


def test_traversal_target_filters_on_the_labels_array():
    q = _t("MATCH (p:Person {id: 1})<-[:HAS_CREATOR]-(m:Message) RETURN m.id")
    assert _multi_label(q, "m") == ("Message", "labels")


def test_label_predicate_on_a_supertype():
    q = _t("MATCH (p:Person {id: 1})<-[:HAS_CREATOR]-(m) RETURN m:Message AS isMessage")
    assert _multi_label(q, "m") == ("Message", "labels")


def test_a_concrete_label_is_preferred_as_primary():
    q = _t("MATCH (m:Comment:Message {id: 1}) RETURN m.id")
    assert re.search(r"m\[@\w+\] == @\w+", q.aql), q.aql  # Comment's type-field filter
    assert "Message" not in q.aql and "Message" not in q.bind_vars.values()  # every Comment is a Message


def test_a_supertype_the_primary_does_not_imply_filters_on_its_labels_field():
    q = _t("MATCH (m:Person:Message {id: 1}) RETURN m.id")
    assert _multi_label(q, "m") == ("Message", "labels")


def test_place_and_organisation_are_supertypes_too():
    assert _multi_label(_t("MATCH (x:Place {id: 1}) RETURN x.name"), "x") == ("Place", "labels")
    assert _multi_label(_t("MATCH (x:Organisation {id: 1}) RETURN x.name"), "x") == ("Organisation", "labels")


def test_create_writes_the_supertype_labels():
    q = _t("CREATE (p:Post {id: 9})")
    labels = [v for v in q.bind_vars.values() if isinstance(v, list)]
    assert ["Message", "Post"] in labels, q.bind_vars
    assert re.search(r"labels: @\w+", q.aql), q.aql


def test_create_by_a_supertype_is_refused():
    with pytest.raises(CoreError) as exc:
        _t("CREATE (m:Message {id: 9})")
    assert exc.value.code == "UNSUPPORTED"
    assert "supertype" in str(exc.value)


def test_merge_on_a_member_label_is_refused():
    with pytest.raises(CoreError) as exc:
        _t("MERGE (p:Post {id: 9})")
    assert exc.value.code == "UNSUPPORTED"


def test_a_label_without_supertypes_creates_as_before():
    q = _t("CREATE (t:Tag {id: 9})")
    assert not [v for v in q.bind_vars.values() if isinstance(v, list)], q.bind_vars


def test_delete_through_a_supertype_filters_on_the_labels_array():
    q = _t("MATCH (p:Person {id: 1})<-[r:HAS_CREATOR]-(m:Message {id: 2}) DELETE r")
    assert _multi_label(q, "m") == ("Message", "labels")


def test_replace_keeps_the_labels_array():
    q = _t("MATCH (p:Post {id: 1}) SET p = {id: 1, content: 'x'}")
    assert '"labels"' in q.aql, q.aql


# ---------------------------------------------------------------------------
# The members' type field beside the labels array (WP-44): a scan can use a
# type-led index instead of reading every member (LDBC IS4 on LPG).
# ---------------------------------------------------------------------------


def _member_types(q, var: str) -> tuple[str, list[str]] | None:
    m = re.search(rf"{var}\[@(\w+)\] IN @(\w+)", q.aql)
    return (q.bind_vars[m.group(1)], q.bind_vars[m.group(2)]) if m else None


def _with_entities(**changes) -> MappingBundle:
    """The LDBC LPG bundle with each named entity's mapping updated by *changes*."""
    bundle = mapping_bundle_for("ldbc_lpg")
    physical = copy.deepcopy(bundle.physical_mapping)
    for name, patch in changes.items():
        physical["entities"][name].update(patch)
    return dataclasses.replace(bundle, physical_mapping=physical)


def test_scan_start_also_tests_the_members_type_field():
    q = _t("MATCH (m:Message {id: 1}) RETURN m.id")
    assert _member_types(q, "m") == ("type", ["Post", "Comment"])
    assert _multi_label(q, "m") == ("Message", "labels")  # still tested: the rows do not change


def test_every_member_of_place_is_named():
    q = _t("MATCH (x:Place {id: 1}) RETURN x.name")
    assert _member_types(q, "x") == ("type", ["City", "Country", "Continent"])


def test_traversal_target_tests_the_members_type_field_too():
    q = _t("MATCH (p:Person {id: 1})<-[:HAS_CREATOR]-(m:Message) RETURN m.id")
    assert _member_types(q, "m") == ("type", ["Post", "Comment"])


def test_a_label_predicate_keeps_the_two_tests_together():
    q = _t("MATCH (p:Person {id: 1})<-[:HAS_CREATOR]-(m) RETURN NOT m:Message AS notMessage")
    # One parenthesised conjunction, so NOT applies to both tests.
    assert re.search(r"\(m\[@\w+\] IN @\w+ AND @\w+ IN m\[@\w+\]\)", q.aql), q.aql


@pytest.mark.parametrize(
    "changes",
    [
        pytest.param({"Post": {"labelsField": "labels"}}, id="member-with-its-own-labels-array"),
        pytest.param({"Comment": {"collectionName": "comments"}}, id="member-in-another-collection"),
        pytest.param({"Comment": {"typeField": "kind"}}, id="members-with-different-type-fields"),
        pytest.param({"Comment": {"style": "GENERIC_WITH_TYPE"}}, id="member-not-mapped-label"),
    ],
)
def test_the_labels_array_alone_when_the_type_field_cannot_decide_membership(changes):
    q = translate("MATCH (m:Message {id: 1}) RETURN m.id", mapping=_with_entities(**changes))
    assert _member_types(q, "m") is None, q.aql
    assert _multi_label(q, "m") == ("Message", "labels")


# ---------------------------------------------------------------------------
# NOT over a labels-array test: AQL parses NOT (x IN a) as x NOT IN a, which is
# false when a is not an array. A label predicate, which NOT can wrap, adds
# IS_ARRAY(a); a pattern filter, never negated, keeps the bare test.
# ---------------------------------------------------------------------------

_LABELS_ONLY = {"Post": {"labelsField": "labels"}}  # the type field cannot decide membership


def test_a_label_predicate_on_a_labels_only_supertype_guards_the_array():
    q = translate(
        "MATCH (p:Person {id: 1})<-[:HAS_CREATOR]-(m) RETURN NOT m:Message AS notMessage",
        mapping=_with_entities(**_LABELS_ONLY),
    )
    assert re.search(r"\(@\w+ IN m\[@(\w+)\] AND IS_ARRAY\(m\[@\1\]\)\)", q.aql), q.aql


def test_a_pattern_filter_on_a_labels_only_supertype_keeps_the_bare_test():
    q = translate("MATCH (m:Message {id: 1}) RETURN m.id", mapping=_with_entities(**_LABELS_ONLY))
    assert "IS_ARRAY" not in q.aql, q.aql
    assert _multi_label(q, "m") == ("Message", "labels")
