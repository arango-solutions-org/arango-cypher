"""End to end: a real ArangoDB rejects generated AQL, and the error inbox files
it once and counts the repeat (GitHub is the fake in ``tests/helpers``).

The broken translation stands in for the BYOC bug fixed in #17 (unquoted
hyphenated collection names in the ``WITH`` prelude, ERR 1501).
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import pytest
from arango import ArangoClient
from fastapi.testclient import TestClient
from starlette.exceptions import HTTPException as StarletteHTTPException

from arango_cypher.api import TranspiledQuery
from arango_cypher.plans import PlanStep, TranslationPlan
from arango_cypher.service.error_inbox import ErrorInbox, parse_body
from arango_cypher.service.github_issues import GitHubIssues
from tests.helpers.fake_github import REPO, TOKEN, FakeGitHub
from tests.helpers.service_reload import fresh_service

pytestmark = pytest.mark.integration

_DB = "arango_cypher_error_inbox_it"
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
def database() -> Iterator[Any]:
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
        yield client, db
    finally:
        if sys_db.has_database(_DB):
            sys_db.delete_database(_DB)


@pytest.fixture
def github() -> Iterator[FakeGitHub]:
    fake = FakeGitHub()
    yield fake
    fake.close()


def test_a_rejected_translation_is_filed_once_and_counted(
    database: tuple[Any, Any], github: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, db = database
    svc = fresh_service()
    inbox = ErrorInbox(GitHubIssues(REPO, TOKEN, api=github.api))
    monkeypatch.setitem(
        svc.app.exception_handlers[StarletteHTTPException].__globals__, "get_inbox", lambda: inbox
    )
    execute = next(r.endpoint for r in svc.app.routes if getattr(r, "path", None) == "/execute")
    for n, collection in enumerate(("IAM-users", "IAM-AWS-DOCS-DEMO_Chunks")):
        broken = TranspiledQuery(aql=f"WITH {collection}\nFOR u IN users RETURN u", bind_vars={}, warnings=[])
        plan = TranslationPlan(steps=(PlanStep(broken),), result_step=0)
        monkeypatch.setitem(execute.__globals__, "translate_plan", lambda *a, _p=plan, **k: _p)
        token = f"inbox-live-{n}"
        svc._sessions[token] = svc._Session(token=token, db=db, client=client, platform_user=f"user{n}")
        resp = TestClient(svc.app).post(
            "/execute",
            json={"cypher": "MATCH (u:User) RETURN u", "mapping": _MAPPING},
            headers={"X-Arango-Session": token},
        )
        assert resp.status_code == 500, resp.text
        assert resp.json()["report"]["seen"] == n + 1
        inbox.flush()
    assert list(github.issues) == [1]  # same bug, different collections: one issue
    issue = github.issues[1]
    assert "ArangoDB error N: syntax error" in issue["title"]
    assert "WITH IAM-users" in issue["body"]  # the first example is kept
    counts = parse_body(issue["body"])[1]
    assert counts.seen == 2 and len(counts.users) == 2 and counts.endpoints == ["/execute"]
