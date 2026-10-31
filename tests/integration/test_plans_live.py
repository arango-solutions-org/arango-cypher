"""Multi-statement plans run in one stream transaction on a real ArangoDB.

Each query here needs several AQL statements on the LPG mapping, where every
node lives in ``vertices``: a write then a read of it, DELETE beside MERGE,
CREATE beside MERGE. A failing statement aborts the transaction, so nothing
an earlier statement wrote is kept. Unit counterpart: ``tests/test_plans.py``.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import replace
from typing import Any

import pytest
from arango import ArangoClient
from arango_query_core import AqlQuery

from arango_cypher import PlanExecutionError, execute_plan, translate_plan
from arango_cypher.plan_exec import Statement, run_statements
from tests.helpers.mapping_fixtures import mapping_bundle_for

pytestmark = pytest.mark.integration

_DB = "arango_cypher_plans_it"
LPG = mapping_bundle_for("lpg")


@pytest.fixture
def db() -> Iterator[Any]:
    user = os.environ.get("ARANGO_USER", "root")
    password = os.environ.get("ARANGO_PASS", "openSesame")
    client = ArangoClient(hosts=os.environ.get("ARANGO_URL", "http://localhost:8529"))
    sys_db = client.db("_system", username=user, password=password)
    if sys_db.has_database(_DB):
        sys_db.delete_database(_DB)
    sys_db.create_database(_DB)
    try:
        database = client.db(_DB, username=user, password=password)
        database.create_collection("vertices")
        database.create_collection("edges", edge=True)
        database.collection("vertices").insert_many(
            [{"_key": "a", "type": "User", "name": "a"}, {"_key": "b", "type": "User", "name": "b"}]
        )
        yield database
    finally:
        if sys_db.has_database(_DB):
            sys_db.delete_database(_DB)


def _run(db: Any, cypher: str, params: dict[str, Any] | None = None) -> list[Any]:
    plan = translate_plan(cypher, mapping=LPG, params=params)
    assert len(plan.steps) > 1, "expected a multi-statement plan"
    return execute_plan(db, plan)


def _names(db: Any) -> list[str]:
    return sorted(d["name"] for d in db.collection("vertices").all())


def test_create_beside_merge_creates_then_merges_what_it_created(db: Any) -> None:
    rows = _run(
        db, "CREATE (a:User {name: 'x'}), (b:User {name: 'y'}) MERGE (a)-[:FOLLOWS]->(b) RETURN count(a) AS c"
    )
    assert rows in ([1], [{"c": 1}])
    assert _names(db) == ["a", "b", "x", "y"]
    (edge,) = list(db.collection("edges").all())
    assert edge["type"] == "FOLLOWS"


def test_a_read_after_a_write_sees_the_write(db: Any) -> None:
    rows = _run(
        db,
        "MATCH (a:User {name: 'a'}) MERGE (b:User {name: 'new'}) "
        "CREATE (a)-[:FOLLOWS]->(b) WITH a "
        "MATCH (a)-[:FOLLOWS]->(m:User) RETURN m.name AS name",
    )
    assert rows in (["new"], [{"name": "new"}])


def test_delete_beside_merge_does_not_match_the_deleted_node(db: Any) -> None:
    rows = _run(db, "MATCH (n:User {name: 'a'}) DELETE n MERGE (m:User {name: 'a'}) RETURN m.name AS name")
    assert rows in (["a"], [{"name": "a"}])
    assert _names(db) == ["a", "b"]
    assert db.collection("vertices").get("a") is None  # deleted; the merged one is new


def test_several_merges_on_one_collection_see_each_other(db: Any) -> None:
    _run(
        db,
        "UNWIND ['p', 'q', 'p'] AS name MERGE (m:User {name: 'movie'}) MERGE (u:User {name: name}) "
        "MERGE (u)-[:FOLLOWS]->(m)",
    )
    assert _names(db) == ["a", "b", "movie", "p", "q"]
    assert db.collection("edges").count() == 2


def test_a_failing_step_rolls_back_every_step(db: Any) -> None:
    plan = translate_plan(
        "CREATE (a:User {name: 'x'}), (b:User {name: 'y'}) MERGE (a)-[:FOLLOWS]->(b) RETURN count(a) AS c",
        mapping=LPG,
    )
    broken = replace(
        plan.steps[1],
        query=replace(
            plan.steps[1].query,
            aql="FOR _r IN @_step1_rows RETURN FAIL('boom')",
            bind_vars={"_step1_rows": []},
        ),
    )
    plan = replace(plan, steps=(plan.steps[0], broken))
    with pytest.raises(PlanExecutionError, match="step 2 of 2 failed") as exc:
        execute_plan(db, plan)
    assert exc.value.step == 2 and "boom" in str(exc.value.__cause__)
    assert _names(db) == ["a", "b"]  # step 1's CREATE was undone


def test_run_statements_feeds_each_statement_the_rows_before_it(db: Any) -> None:
    rows = run_statements(
        db,
        [
            Statement(AqlQuery("FOR v IN vertices SORT v.name RETURN {n: v.name}", {})),
            Statement(
                AqlQuery(
                    "FOR r IN @rows INSERT {type: 'User', name: CONCAT(r.n, '2')} INTO vertices RETURN NEW.name",
                    {},
                ),
                input_param="rows",
            ),
        ],
        write_collections=["vertices"],
    )
    assert rows == ["a2", "b2"]
    assert _names(db) == ["a", "a2", "b", "b2"]
