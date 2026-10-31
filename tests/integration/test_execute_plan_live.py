"""``POST /execute`` and the CLI's ``run`` on a query that needs several AQL statements.

The service runs it as a multi-statement plan in one transaction for a
workbench (tenant-unbound) session, and refuses it for a tenant-bound one,
whose AQL every statement must pass the tenant-scope layers that do not yet
read a plan's steps. The CLI goes through ``arango_cypher.execute``.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import pytest
from arango import ArangoClient
from fastapi.testclient import TestClient

from arango_cypher import execute
from tests.helpers.mapping_fixtures import mapping_bundle_for
from tests.helpers.service_reload import fresh_service

pytestmark = pytest.mark.integration

_DB = "arango_cypher_execute_plan_it"
LPG = mapping_bundle_for("lpg")
MAPPING = {
    "conceptualSchema": LPG.conceptual_schema,
    "physicalMapping": LPG.physical_mapping,
    "metadata": LPG.metadata,
}
CYPHER = "MATCH (n:User {name: 'a'}) DELETE n MERGE (m:User {name: 'a'}) RETURN m.name AS name"


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
        database.create_collection("vertices").insert({"_key": "a", "type": "User", "name": "a"})
        database.create_collection("edges", edge=True)
        yield database
    finally:
        if sys_db.has_database(_DB):
            sys_db.delete_database(_DB)


def _client(db: Any, *, tenant_id: str | None = None) -> tuple[TestClient, str]:
    svc = fresh_service()
    token = "execute-plan-it-token"
    session = svc._Session(token=token, db=db, client=None)
    session.tenant_id = tenant_id
    svc._sessions[token] = session
    return TestClient(svc.app), token


def test_execute_runs_a_multi_statement_query_as_a_plan(db: Any) -> None:
    client, token = _client(db)
    resp = client.post(
        "/execute", json={"cypher": CYPHER, "mapping": MAPPING}, headers={"X-Arango-Session": token}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["results"] in (["a"], [{"name": "a"}])
    assert len(body["plan_steps"]) == 2
    assert body["plan_steps"][1]["input_param"] == "_step1_rows"
    assert body["aql"].startswith("// step 1 of 2")
    assert "multi-statement plan" in body["warnings"][0]["message"]
    assert db.collection("vertices").get("a") is None and db.collection("vertices").count() == 1


def test_execute_refuses_a_plan_for_a_tenant_bound_session(db: Any) -> None:
    client, token = _client(db, tenant_id="t1")
    resp = client.post(
        "/execute", json={"cypher": CYPHER, "mapping": MAPPING}, headers={"X-Arango-Session": token}
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["code"] == "MULTI_STATEMENT_TENANT_BOUND"
    assert db.collection("vertices").get("a") is not None  # nothing ran


def test_a_single_statement_query_keeps_the_one_statement_path(db: Any) -> None:
    client, token = _client(db)
    resp = client.post(
        "/execute",
        json={"cypher": "MATCH (n:User) RETURN n.name AS name", "mapping": MAPPING},
        headers={"X-Arango-Session": token},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["plan_steps"] is None


def test_the_python_execute_used_by_the_cli_runs_a_plan(db: Any) -> None:
    rows = list(execute(CYPHER, db=db, mapping=LPG))
    assert rows in (["a"], [{"name": "a"}])
    assert db.collection("vertices").get("a") is None
