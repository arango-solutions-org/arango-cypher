"""Golden AQL for multi-label LPG nodes (``labelsField``), keys()/properties(),
SET … RETURN and multi-node CREATE (PRD §6.3, §8.3).

Live behaviour is covered by the openCypher TCK execution ledger
(``tests/tck/execution_ledger.tsv``: Match1, Graph3, Graph5, Set3, Remove2,
Graph9, Set1–Set6, Create1–Create6).
"""

from __future__ import annotations

import pytest

from arango_cypher import translate
from tests.helpers.mapping_fixtures import mapping_bundle_for


@pytest.mark.parametrize("case_id", [f"C{n}" for n in range(1101, 1114)])
def test_translate_lpg_labels_array_goldens(corpus_cases, case_id: str):
    case = next(c for c in corpus_cases if c.id == case_id)
    mapping = mapping_bundle_for(case.mapping_fixture)

    out = translate(case.cypher, mapping=mapping, params=case.params or None)

    assert case.expected_aql is not None, f"Golden AQL must be filled for {case_id}"
    assert out.aql.strip() == case.expected_aql.strip()
    assert out.bind_vars == case.expected_bind_vars


def test_label_test_without_labels_field_is_the_type_field_alone():
    """A mapping that names no labels field keeps the single type-field test."""
    out = translate("MATCH (n:User) RETURN n", mapping=mapping_bundle_for("lpg"))
    assert "FILTER n[@typeField] == @typeValue" in out.aql
    assert "labelsField" not in out.bind_vars


def test_create_refuses_undirected_and_variable_length_relationships():
    from arango_query_core import CoreError

    for q in ("CREATE (a)-[:FOLLOWS]-(b)", "CREATE (a)<-[:FOLLOWS]->(b)", "CREATE ()-[:FOLLOWS*2]->()"):
        with pytest.raises(CoreError):
            translate(q, mapping=mapping_bundle_for("lpg"))


def test_create_refuses_relabelling_a_bound_node():
    from arango_query_core import CoreError

    for q in (
        "CREATE (n:User), (n:Doc)",
        "MATCH (n) CREATE (n {name: 'x'})",
        "CREATE (n:User) CREATE (n:Doc)",
    ):
        with pytest.raises(CoreError):
            translate(q, mapping=mapping_bundle_for("lpg"))


def test_set_map_with_null_drops_null_attributes():
    out = translate(
        "MATCH (n:User) SET n = {name: 'a', gone: null} RETURN n", mapping=mapping_bundle_for("lpg")
    )
    assert "ATTRIBUTES(_p)[* FILTER _p[CURRENT] == null]" in out.aql
    literal = translate("MATCH (n:User) SET n = {name: 'a'}", mapping=mapping_bundle_for("lpg"))
    assert "FILTER _p[CURRENT] == null" not in literal.aql


def test_merge_refusals():
    """openCypher VariableAlreadyBound, MergeReadOwnWrites, UndefinedVariable; a
    whole-map SET in a node MERGE action fails closed instead of being dropped
    (a relationship MERGE compiles it: merge_docs)."""
    from arango_query_core import CoreError

    for q in (
        "MATCH (a) MERGE (a)",
        "MERGE ({num: null})",
        "MERGE (n) ON CREATE SET x.num = 1",
        "MERGE (n:User {id: 1}) ON CREATE SET n = {x: 1}",
    ):
        with pytest.raises(CoreError):
            translate(q, mapping=mapping_bundle_for("lpg"))
    # A CREATE beside a MERGE used to be dropped from the AQL: refused now.
    with pytest.raises(CoreError, match="CREATE beside MERGE"):
        translate(
            "MERGE (t:User {id: 42}) CREATE (f:Doc) CREATE (t)-[:FOLLOWS]->(f)",
            mapping=mapping_bundle_for("lpg"),
        )


@pytest.mark.usefixtures("without_constant_folding")
def test_keys_hides_fields_only_of_stored_documents():
    """A map (no ``_id``) keeps every key, ``type`` included; a document hides its fields.

    Folding is off so the literal map exercises the run-time lowering a
    data-dependent map takes; folded, ``keys({a: 1, type: 2})`` is ``['a', 'type']``.
    """
    out = translate("RETURN keys({a: 1, type: 2}) AS k", mapping=mapping_bundle_for("lpg"))
    assert "HAS(_v, '_id') ? ATTRIBUTES(_v)[* FILTER CURRENT NOT IN [" in out.aql
    assert ": ATTRIBUTES(_v))" in out.aql


def test_keys_of_a_literal_map_folds_to_every_key():
    out = translate("RETURN keys({a: 1, type: 2}) AS k", mapping=mapping_bundle_for("lpg"))
    assert out.bind_vars["cv0"] == ["a", "type"]
