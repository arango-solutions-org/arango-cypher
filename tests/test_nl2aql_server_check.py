"""NL→AQL: the optional server check and the traversal-direction check.

Regression: the regex check passed queries the server refuses (SQL functions such
as DIVIDE or YEAR) and rejected valid ones (``FOR x IN <variable>``), and nothing
caught a traversal walked against its edge's direction.
"""

from __future__ import annotations

from typing import Any

import pytest
from arango.exceptions import AQLQueryExplainError
from arango_query_core.mapping import MappingBundle
from fastapi.testclient import TestClient

from arango_cypher.nl2cypher._aql import _edge_endpoints, _traversal_direction_problems, nl_to_aql
from arango_cypher.service.routes.nl import _server_explain
from tests.helpers.fake_arango import FakeAQL, FakeDb, server_error
from tests.helpers.service_reload import fresh_service

PM = {
    "entities": {
        "Order": {"collectionName": "orders"},
        "Customer": {"collectionName": "customers"},
        "Person": {"collectionName": "people"},
    },
    "relationships": {
        "PLACED_BY": {"edgeCollectionName": "placed_by"},
        "KNOWS": {"edgeCollectionName": "knows"},
    },
}
CS = {
    "relationships": [
        {"type": "PLACED_BY", "fromEntity": "Order", "toEntity": "Customer"},
        {"type": "KNOWS", "fromEntity": "Person", "toEntity": "Person"},
    ]
}
BUNDLE = MappingBundle(conceptual_schema=CS, physical_mapping=PM, metadata={})
ENDPOINTS = _edge_endpoints(BUNDLE)
WRONG = "FOR c IN customers FOR o IN OUTBOUND c placed_by RETURN o"
RIGHT = "FOR c IN customers FOR o IN INBOUND c placed_by RETURN o"


class _Scripted:
    def __init__(self, *replies: str) -> None:
        self.replies, self.users = list(replies), []

    def generate(self, system: str, user: str) -> tuple[str, dict]:  # noqa: ARG002
        self.users.append(user)
        return f"```aql\n{self.replies.pop(0)}\n```", {}


def test_endpoints_resolve_collections_from_the_conceptual_schema():
    assert ENDPOINTS == {"placed_by": ("orders", "customers"), "knows": ("people", "people")}


def test_direction_problems():
    assert "use INBOUND" in _traversal_direction_problems(WRONG, ENDPOINTS)[0]
    assert _traversal_direction_problems('FOR o IN 1..1 OUTBOUND "customers/1" placed_by RETURN o', ENDPOINTS)
    assert _traversal_direction_problems(RIGHT, ENDPOINTS) == []
    assert _traversal_direction_problems("FOR p IN people FOR q IN INBOUND p knows RETURN q", ENDPOINTS) == []
    assert (
        _traversal_direction_problems(
            "LET c = DOCUMENT('customers/1') FOR o IN OUTBOUND c placed_by RETURN o", ENDPOINTS
        )
        == []
    )


def test_wrong_direction_is_sent_back_for_repair():
    provider = _Scripted(WRONG, RIGHT)
    assert nl_to_aql("orders per customer", mapping=BUNDLE, llm_provider=provider).aql == RIGHT
    assert "use INBOUND" in provider.users[1]


def test_server_error_drives_repair_and_replaces_the_regex():
    bad = "FOR o IN orders RETURN DIVIDE(o.total, 2)"
    good = "LET xs = (FOR o IN orders RETURN o.total) FOR x IN xs RETURN x / 2"  # the regex rejects this
    provider = _Scripted(bad, good)
    errors = {bad: "[1540] usage of unknown function 'DIVIDE()'"}
    assert nl_to_aql("half", mapping=BUNDLE, llm_provider=provider, explain=errors.get).aql == good
    assert "unknown function 'DIVIDE()'" in provider.users[1] and "DATE_YEAR()" in provider.users[1]


def test_list_valued_end_is_left_out_of_the_direction_check():
    # Polymorphic relationship (LDBC HAS_CREATOR): one start collection cannot be
    # judged against a set of from-collections, so the edge is skipped, not hashed.
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
    assert _edge_endpoints(bundle) == {}
    provider = _Scripted("FOR p IN person RETURN p")
    assert nl_to_aql("people", mapping=bundle, llm_provider=provider).aql == "FOR p IN person RETURN p"


def _shared(cs_rels: list[dict[str, str]]) -> MappingBundle:
    pm = {
        "entities": {"Order": {"collectionName": "orders"}, "Customer": {"collectionName": "customers"}},
        "relationships": {
            r["type"]: {
                "edgeCollectionName": "edges",
                "style": "GENERIC_WITH_TYPE",
                "typeField": "type",
                "typeValue": r["type"],
            }
            for r in cs_rels
        },
    }
    return MappingBundle(conceptual_schema={"relationships": cs_rels}, physical_mapping=pm, metadata={})


def test_shared_edge_collection_with_different_ends_is_left_out():
    bundle = _shared(
        [
            {"type": "OWNS", "fromEntity": "Customer", "toEntity": "Order"},
            {"type": "PLACED_BY", "fromEntity": "Order", "toEntity": "Customer"},
        ]
    )
    aql = "FOR c IN customers FOR o IN OUTBOUND c edges FILTER o.type == 'OWNS' RETURN o"
    assert _edge_endpoints(bundle) == {}
    assert _traversal_direction_problems(aql, _edge_endpoints(bundle)) == []


def test_shared_edge_collection_with_the_same_ends_is_checked():
    bundle = _shared(
        [
            {"type": "PLACED_BY", "fromEntity": "Order", "toEntity": "Customer"},
            {"type": "BILLED_TO", "fromEntity": "Order", "toEntity": "Customer"},
        ]
    )
    assert _edge_endpoints(bundle) == {"edges": ("orders", "customers")}


def test_a_failing_explain_is_reported_as_itself_and_spends_no_retries():
    provider = _Scripted("FOR o IN orders RETURN o", "FOR o IN orders RETURN o", "FOR o IN orders RETURN o")

    def down(_aql: str) -> str | None:
        raise ConnectionError("server unreachable")

    out = nl_to_aql("orders", mapping=BUNDLE, llm_provider=provider, explain=down)
    assert out.aql == "" and out.method == "server_check_failed"
    assert "ConnectionError: server unreachable" in out.explanation
    assert len(provider.users) == 1


class _ExplainAQL(FakeAQL):
    def __init__(self, error: Exception | None) -> None:
        super().__init__(lambda q, b: [], lambda q: ["SingletonNode", "ReturnNode"])
        self.error, self.explained = error, []

    def explain(
        self,
        query: str,
        all_plans: bool = False,
        max_plans: int | None = None,
        opt_rules: list[str] | None = None,
        bind_vars: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.explained.append(query)
        if self.error is not None:
            raise self.error
        return super().explain(query, all_plans, max_plans, opt_rules, bind_vars)


def _db(error: Exception | None = None) -> Any:
    db = FakeDb()
    db.aql = _ExplainAQL(error)
    return db


def test_server_explain_returns_query_refusals_and_raises_the_rest():
    assert _server_explain(_db())("FOR o IN orders RETURN o") is None
    unknown = server_error(AQLQueryExplainError, 400, 1540, "usage of unknown function 'DIVIDE()'")
    assert "unknown function 'DIVIDE()'" in (_server_explain(_db(unknown))("RETURN DIVIDE(1, 2)") or "")
    missing = server_error(AQLQueryExplainError, 404, 1203, "collection or view not found: nope")
    assert "not found" in (_server_explain(_db(missing))("FOR x IN nope RETURN x") or "")
    unavailable = server_error(AQLQueryExplainError, 503, 503, "service unavailable")
    with pytest.raises(AQLQueryExplainError):
        _server_explain(_db(unavailable))("FOR o IN orders RETURN o")


class _Client:
    def close(self) -> None:
        pass


@pytest.mark.parametrize("connected", [True, False])
def test_nl2aql_endpoint_checks_against_the_connected_database(monkeypatch, connected):
    svc = fresh_service()
    db = _db()
    headers = {}
    if connected:
        svc._sessions["nl2aql-explain"] = svc._Session(token="nl2aql-explain", db=db, client=_Client())
        headers = {"X-Arango-Session": "nl2aql-explain"}
    captured: dict[str, Any] = {}

    def _fake_nl_to_aql(question, *, mapping=None, tenant_context=None, cypher=None, explain=None, **_kw):
        captured["explain"] = explain
        return type(
            "R",
            (),
            {
                "aql": "",
                "bind_vars": {},
                "explanation": "",
                "confidence": 0.0,
                "method": "x",
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "cached_tokens": 0,
            },
        )()

    monkeypatch.setattr("arango_cypher.nl2cypher.nl_to_aql", _fake_nl_to_aql)
    try:
        resp = TestClient(svc.app).post("/nl2aql", json={"question": "q", "mapping": {}}, headers=headers)
    finally:
        svc._sessions.pop("nl2aql-explain", None)
    assert resp.status_code == 200, resp.text
    if connected:
        assert captured["explain"]("FOR o IN orders RETURN o") is None
        assert db.aql.explained == ["FOR o IN orders RETURN o"]
    else:
        assert captured["explain"] is None
