"""AQL that a real ArangoDB rejects, through the service.

Before: ``403 tenant_scope_violation`` with the raw EXPLAIN text, which made
an Arango Cypher bug (generated AQL with unquoted hyphenated collection names)
look like a permissions problem. Now generated AQL that the database rejects
is a 500 that names it a bug in Arango Cypher, and AQL the caller supplied is
a 400. The unit counterpart is ``tests/test_aql_rejected_response.py``.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import pytest
from arango import ArangoClient
from fastapi.testclient import TestClient

from arango_cypher.api import TranspiledQuery
from arango_cypher.plans import PlanStep, TranslationPlan
from tests.helpers.service_reload import fresh_service

pytestmark = pytest.mark.integration

_DB = "arango_cypher_aql_rejected_it"
_MAPPING = {
    "conceptualSchema": {
        "entities": [{"labels": ["User"], "name": "User", "properties": []}],
        "relationships": [],
    },
    "physicalMapping": {
        "entities": {"User": {"collectionName": "users", "style": "COLLECTION"}},
        "relationships": {},
    },
    "metadata": {},
}


@pytest.fixture(scope="module")
def session() -> Iterator[tuple[Any, TestClient, str]]:
    user = os.environ.get("ARANGO_USER", "root")
    password = os.environ.get("ARANGO_PASS", "openSesame")
    client = ArangoClient(hosts=os.environ.get("ARANGO_URL", "http://localhost:8529"))
    sys_db = client.db("_system", username=user, password=password)
    if sys_db.has_database(_DB):
        sys_db.delete_database(_DB)
    sys_db.create_database(_DB)
    try:
        db = client.db(_DB, username=user, password=password)
        db.create_collection("users").insert({"n": 1})
        svc = fresh_service()
        token = "aql-rejected-it-token"
        svc._sessions[token] = svc._Session(token=token, db=db, client=client)
        yield svc, TestClient(svc.app), token
        svc._sessions.pop(token, None)
    finally:
        if sys_db.has_database(_DB):
            sys_db.delete_database(_DB)


def _endpoint(svc: Any, path: str) -> Any:
    return next(r.endpoint for r in svc.app.routes if getattr(r, "path", None) == path)


def test_generated_aql_the_database_rejects_is_reported_as_our_bug(
    session: tuple[Any, TestClient, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    svc, client, token = session
    # Stand in for a translator bug: the AQL the hyphenated-name bug produced.
    broken = TranspiledQuery(aql="WITH IAM-users\nFOR u IN users RETURN u", bind_vars={}, warnings=[])
    # /execute translates through translate_plan: a one-step plan of the broken query.
    plan = TranslationPlan(steps=(PlanStep(broken),), result_step=0)
    monkeypatch.setitem(_endpoint(svc, "/execute").__globals__, "translate_plan", lambda *a, **k: plan)
    resp = client.post(
        "/execute",
        json={"cypher": "MATCH (u:User) RETURN u", "mapping": _MAPPING},
        headers={"X-Arango-Session": token},
    )
    assert resp.status_code == 500, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == "GENERATED_AQL_REJECTED"
    assert "ArangoDB error 1501" in detail["message"]
    assert "bug in Arango Cypher, not in your query" in detail["message"]


def test_supplied_aql_the_database_rejects_is_a_400(session: tuple[Any, TestClient, str]) -> None:
    _, client, token = session
    resp = client.post(
        "/execute-aql", json={"aql": "FOR u IN users RETURN"}, headers={"X-Arango-Session": token}
    )
    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == "AQL_REJECTED"
    assert detail["message"].startswith("ArangoDB rejected this AQL:")


def test_valid_aql_still_runs(session: tuple[Any, TestClient, str]) -> None:
    _, client, token = session
    resp = client.post(
        "/execute-aql", json={"aql": "FOR u IN users RETURN u.n"}, headers={"X-Arango-Session": token}
    )
    assert resp.status_code == 200 and resp.json()["results"] == [1]


_STALE_MAPPING = {
    "conceptualSchema": {
        "entities": [{"labels": ["Gone"], "name": "Gone", "properties": []}],
        "relationships": [],
    },
    "physicalMapping": {
        "entities": {"Gone": {"collectionName": "dropped_since_analysis", "style": "COLLECTION"}},
        "relationships": {},
    },
    "metadata": {},
}


def test_a_mapping_naming_a_dropped_collection_says_refresh_the_schema(
    session: tuple[Any, TestClient, str],
) -> None:
    _, client, token = session
    resp = client.post(
        "/execute",
        json={"cypher": "MATCH (g:Gone) RETURN g", "mapping": _STALE_MAPPING},
        headers={"X-Arango-Session": token},
    )
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == "SCHEMA_OUT_OF_DATE" and detail["collection"] == "dropped_since_analysis"
    assert "report" not in resp.json()


def test_supplied_aql_naming_a_missing_collection_is_a_400(session: tuple[Any, TestClient, str]) -> None:
    _, client, token = session
    resp = client.post(
        "/execute-aql",
        json={"aql": "FOR x IN no_such_collection RETURN x"},
        headers={"X-Arango-Session": token},
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"]["code"] == "AQL_UNKNOWN_COLLECTION"
    assert resp.json()["detail"]["collection"] == "no_such_collection"
