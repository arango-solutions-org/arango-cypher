"""The NL→AQL schema summary must say which way each edge points.

Regression: on schema-analyzer mappings the edge ends live only on the conceptual
relationship, so every edge printed as ``Connects: ?('?') -> ?('?')``, and the
"1:N → OUTBOUND from the '1' side" hint pointed child → parent edges the wrong way.
"""

from __future__ import annotations

import pytest
from arango_query_core.mapping import MappingBundle

from arango_cypher.nl2cypher._aql import _build_physical_schema_summary, nl_to_aql
from tests.helpers.mapping_fixtures import mapping_bundle_for

ENTITIES = {
    "Atom": {"collectionName": "atom", "style": "COLLECTION", "properties": {}},
    "Molecule": {"collectionName": "molecule", "style": "COLLECTION", "properties": {}},
}
CS = {"relationships": [{"type": "A_TO_M", "fromEntity": "Atom", "toEntity": "Molecule"}]}


def _summary(rel: dict, cs: dict = CS, metadata: dict | None = None) -> str:
    pm = {"entities": ENTITIES, "relationships": {"A_TO_M": {"edgeCollectionName": "a_to_m", **rel}}}
    bundle = MappingBundle(conceptual_schema=cs, physical_mapping=pm, metadata=metadata or {})
    return _build_physical_schema_summary(bundle).replace("`", "")


def test_ends_fall_back_to_the_conceptual_schema():
    assert "Connects: Atom('atom') -> Molecule('molecule')" in _summary({})
    assert "Connects: Atom('atom') -> Molecule('molecule')" in _summary({"domain": "Atom"})


def test_physical_ends_win_and_unknown_ends_stay_question_marks():
    assert "Connects: Molecule('molecule') -> Atom('atom')" in _summary(
        {"domain": "Molecule", "range": "Atom"}
    )
    assert "Connects: ?('?') -> ?('?')" in _summary({}, cs={})


def test_direction_hint_is_not_based_on_cardinality():
    summary = _summary({}, metadata={"statistics": {"entities": {"Atom": {"estimated_count": 9}}}})
    assert "from the '1' side" not in summary
    assert "OUTBOUND from an A document, INBOUND from a B document" in summary


def test_list_valued_conceptual_end_renders_each_member():
    # Polymorphic relationship: the analyzer exports the end as a list (LDBC HAS_CREATOR).
    pm = {
        "entities": {
            "Post": {"collectionName": "post"},
            "Comment": {"collectionName": "comment"},
            "Person": {"collectionName": "person"},
        },
        "relationships": {"HAS_CREATOR": {"edgeCollectionName": "has_creator"}},
    }
    cs = {"relationships": [{"type": "HAS_CREATOR", "fromEntity": ["Post", "Comment"], "toEntity": "Person"}]}
    bundle = MappingBundle(conceptual_schema=cs, physical_mapping=pm, metadata={})
    summary = _build_physical_schema_summary(bundle)
    assert "Connects: Post|Comment('post'|'comment') -> Person('person')" in summary

    class _Provider:
        def generate(self, system: str, user: str) -> tuple[str, dict]:  # noqa: ARG002
            return "```aql\nFOR p IN person RETURN p\n```", {}

    assert nl_to_aql("people", mapping=bundle, llm_provider=_Provider()).aql == "FOR p IN person RETURN p"


def test_analyzer_unresolved_end_is_unknown():
    assert "Connects: ?('?') -> Molecule('molecule')" in _summary(
        {}, cs={"relationships": [{"type": "A_TO_M", "fromEntity": "Any", "toEntity": "Molecule"}]}
    )
    summary = _build_physical_schema_summary(mapping_bundle_for("hybrid"))
    assert "Any(" not in summary
    assert "Connects: ?('?') -> ?('?')" in summary


@pytest.mark.parametrize("fixture", ["ldbc_pg", "ldbc_lpg"])
def test_list_valued_physical_end_renders_each_member(fixture):
    # Pre-existing crash: a list in the physical domain/range raised TypeError (unhashable list).
    summary = _build_physical_schema_summary(mapping_bundle_for(fixture))
    assert "Connects: Post|Comment(" in summary
    assert "Post|Comment --[" in summary
    assert "['Post', 'Comment']" not in summary
