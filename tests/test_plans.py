"""Multi-statement plans (``arango_cypher.plans``): how a query is cut into steps.

Live execution, rollback included: ``tests/integration/test_plans_live.py``.
"""

from __future__ import annotations

import pytest
from arango_query_core import CoreError

from arango_cypher import translate, translate_plan
from arango_cypher._translate_v0.plan_scope import Binding, PlanUnsupported, query_clauses, scope_after
from arango_cypher.parser import parse_cypher
from arango_cypher.plans import ROW_MARKER, rows_param
from tests.helpers.mapping_fixtures import mapping_bundle_for

LPG = mapping_bundle_for("lpg")
TWO_STEPS = "CREATE (a:User {name: 'x'}), (b:User {name: 'y'}) MERGE (a)-[:FOLLOWS]->(b) RETURN count(a) AS c"


def test_a_query_that_fits_one_statement_is_a_one_step_plan_identical_to_translate() -> None:
    cypher = "MATCH (u:User) WHERE u.name = $n RETURN u.name AS name"
    plan = translate_plan(cypher, mapping=LPG, params={"n": "x"})
    single = translate(cypher, mapping=LPG, params={"n": "x"})
    assert plan.is_single_statement and plan.result_step == 0
    (step,) = plan.steps
    assert (step.aql, step.bind_vars) == (single.aql, single.bind_vars)
    assert step.input_param is None and step.output_param is None


def test_create_beside_merge_on_one_collection_is_two_steps_handing_rows_on() -> None:
    plan = translate_plan(TWO_STEPS, mapping=LPG)
    first, second = plan.steps
    assert plan.result_step == 1
    assert first.input_param is None and first.output_param == second.input_param == rows_param(1)
    assert first.aql.rstrip().endswith("RETURN {a: a, b: b}")  # object rows, keyed by variable
    assert second.aql.startswith(f"FOR _row IN @{rows_param(1)}")
    assert "LET a = DOCUMENT(@@stepCollection, _row.a._key)" in second.aql  # read again: sees step 1
    assert second.bind_vars[rows_param(1)] == []  # the executor sets the rows
    assert "UPSERT" in second.aql and "INSERT" not in first.aql.split("FOR _r IN")[1]
    assert {"vertices", "edges"} <= plan.collections()


def test_translate_names_translate_plan_for_a_query_that_needs_several_statements() -> None:
    with pytest.raises(CoreError) as exc:
        translate(TWO_STEPS, mapping=LPG)
    assert "translate_plan()" in str(exc.value) and "2 AQL statements" in str(exc.value)
    assert exc.value.code == "UNSUPPORTED"  # the original error's code


def test_read_after_write_reads_in_a_later_step() -> None:
    plan = translate_plan(
        "MATCH (a:User) MERGE (b:User {name: 'n'}) WITH * OPTIONAL MATCH (a)-[:FOLLOWS]->(b) RETURN count(*) AS c",
        mapping=LPG,
    )
    assert len(plan.steps) == 2
    assert "UPSERT" in plan.steps[0].aql and "_opt_rows" in plan.steps[1].aql


def test_a_step_with_nothing_in_scope_still_hands_on_one_row_per_row() -> None:
    plan = translate_plan("CREATE (:User) CREATE (:User) MERGE (:User {name: 'z'})", mapping=LPG)
    assert plan.steps[0].aql.rstrip().endswith(f"RETURN {{{ROW_MARKER}: 1}}")
    assert f"LET {ROW_MARKER} = _row.{ROW_MARKER}" in plan.steps[1].aql


def test_a_step_binds_only_the_parameters_its_aql_reads() -> None:
    plan = translate_plan(
        "CREATE (a:User {name: $first}) WITH a MATCH (b:User {name: $second}) CREATE (a)-[:FOLLOWS]->(b)",
        mapping=LPG,
        params={"first": "x", "second": "y"},
    )
    assert len(plan.steps) == 2
    assert "first" in plan.steps[0].bind_vars and "second" not in plan.steps[0].bind_vars
    assert "second" in plan.steps[1].bind_vars and "first" not in plan.steps[1].bind_vars


@pytest.mark.parametrize(
    "cypher",
    [
        "MATCH (a:User) CREATE (a)",  # semantic error: an invalid query never becomes a plan
        "CREATE (:User) MERGE (:User) RETURN 1 AS x UNION RETURN 2 AS x",  # a UNION
        "MERGE (a) MERGE (b) MERGE (a)-[:FOLLOWS*2]->(b)",  # a step no statement can run
    ],
)
def test_a_query_no_plan_can_run_raises_the_translate_error(cypher: str) -> None:
    with pytest.raises(CoreError) as plan_exc:
        translate_plan(cypher, mapping=LPG)
    with pytest.raises(CoreError) as one_exc:
        translate(cypher, mapping=LPG)
    assert str(plan_exc.value) == str(one_exc.value) and "translate_plan" not in str(one_exc.value)


def test_a_read_only_query_is_never_cut() -> None:
    cypher = "MATCH (a:User), (b:User) WITH a, b MATCH p = allShortestPaths((a)-[*]-(b)) RETURN p"
    with pytest.raises(CoreError, match="allShortestPaths"):
        translate_plan(cypher, mapping=LPG)


def test_the_rows_parameter_is_reserved() -> None:
    with pytest.raises(CoreError):
        translate_plan(TWO_STEPS + " , $_step1_rows AS r", mapping=LPG, params={"_step1_rows": 1})


# -- scope between clauses ------------------------------------------------------


def _scopes(cypher: str) -> list[dict[str, Binding]]:
    tree = parse_cypher(cypher).tree
    clauses = query_clauses(tree.oC_Statement().oC_Query().oC_RegularQuery().oC_SingleQuery())
    scopes: list[dict[str, Binding]] = [{}]
    for clause in clauses:
        scopes.append(scope_after(clause, scopes[-1]))
    return scopes


def test_scope_tracks_kinds_labels_and_with_projections() -> None:
    scopes = _scopes(
        "MATCH p = (a:User)-[r:FOLLOWS]->(b), (c)-[v:FOLLOWS*2]->(d) WITH a AS x, r, b.name AS name RETURN x"
    )
    assert scopes[1]["a"] == Binding("node", ("User",))
    assert scopes[1]["r"] == Binding("relationship", ("FOLLOWS",))
    assert scopes[1]["p"].kind == "path" and scopes[1]["v"].kind == "value"
    assert scopes[2] == {
        "x": Binding("node", ("User",)),
        "r": Binding("relationship", ("FOLLOWS",)),
        "name": Binding("value"),
    }


def test_a_variable_that_is_no_plain_aql_name_cannot_cross_steps() -> None:
    with pytest.raises(PlanUnsupported):
        _scopes("CREATE (`filter`:User) RETURN 1")
