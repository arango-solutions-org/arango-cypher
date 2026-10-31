"""Layer 1 / Wave 7 — session-bound ``tenantId`` regression tests.

Pins the multi-tenant Phase 1 invariants from
``docs/multitenant_prd.md`` §4:

* ``/connect`` accepts ``tenantId`` / ``tenantKey`` / ``isAdmin`` and
  validates the tenant exists in the ``Tenant`` collection when one is
  present in the connected database. Unknown tenants are refused with
  HTTP 403 (``error="unknown_tenant"``).
* The created session carries ``tenant_id`` / ``tenant_key`` /
  ``is_admin`` so Layer 5 can verify the bind-var ``@tenantId`` came
  from the authenticated session rather than the request body.
* In *workbench* mode (``ARANGO_CYPHER_WORKBENCH=1``) the
  body-supplied ``tenant_context`` on ``/nl2cypher`` / ``/nl2aql`` is
  honored verbatim.
* In *tenant-user* mode (env unset) a body-supplied ``tenant_context``
  whose value differs from the session-bound tenant is silently
  overridden, and a WARN log records the override for audit.

The fakes here mirror ``test_service_connect_diagnostics.py``'s shape
contract for ``arango_cypher.service.ArangoClient`` and only model the
methods exercised by the routes under test. They never touch a real
ArangoDB server.
"""

from __future__ import annotations

import importlib
import logging
import sys
from contextlib import contextmanager
from typing import Any
from unittest import mock

import pytest
from fastapi.testclient import TestClient

from tests.helpers.service_reload import fresh_service as _fresh_service


def _one_step_plan() -> Any:
    """What ``translate_plan`` returns for a query that fits one statement."""
    from arango_cypher import TranspiledQuery
    from arango_cypher.plans import PlanStep, TranslationPlan

    query = TranspiledQuery(aql="FOR n IN @@coll RETURN n", bind_vars={}, warnings=[])
    return TranslationPlan(steps=(PlanStep(query),), result_step=0)


from tests.helpers.service_reload import patched_arango_client as _patched_arango_client

# ---------------------------------------------------------------------------
# Fakes — minimal python-arango shape ``connect.py`` consumes
# ---------------------------------------------------------------------------


class _FakeCollection:
    def __init__(self, docs: dict[str, dict[str, Any]] | None = None):
        self._docs = docs or {}

    def get(self, key: str) -> dict[str, Any] | None:
        return self._docs.get(key)


class _FakeDb:
    """In-memory shape-match of ``StandardDatabase`` for connect tests."""

    def __init__(
        self,
        *,
        has_tenant_collection: bool,
        tenants: dict[str, dict[str, Any]] | None = None,
        databases: list[str] | None = None,
    ):
        self._has_tenant = has_tenant_collection
        self._tenant_coll = _FakeCollection(tenants or {})
        self._databases = databases or ["_system"]

    def version(self) -> str:
        return "3.12.0"

    def has_collection(self, name: str) -> bool:
        return name == "Tenant" and self._has_tenant

    def collection(self, name: str) -> _FakeCollection:
        if name == "Tenant":
            return self._tenant_coll
        raise KeyError(name)

    def databases(self) -> list[str]:
        return list(self._databases)


def _make_fake_client(db: _FakeDb):
    """Build a python-arango ``ArangoClient`` test double bound to *db*.

    Calls to ``client.db("_system", ...)`` return a system-DB stub whose
    only role is to enumerate databases for the connect response.
    """

    class _FakeClient:
        def __init__(self, hosts):
            self.hosts = hosts
            self.closed = False

        def db(self, name: str, username: str = "", password: str = ""):
            if name == "_system":
                return _FakeDb(has_tenant_collection=False, databases=db._databases)
            return db

        def close(self):
            self.closed = True

    return _FakeClient


def _app():
    """Resolve the FastAPI app from the *current* service module."""
    return _fresh_service().app


@pytest.fixture(autouse=True)
def _reset_nl_rate_limit():
    """Reset the process-global NL rate-limit bucket before each test.

    ``_check_nl_rate_limit`` uses a module-level ``_TokenBucket``
    (``NL_RATE_LIMIT_PER_MINUTE`` = 10/min) keyed by client identity. Every
    ``TestClient`` here shares the ``testclient`` key, so NL calls made by
    *earlier* test files in a full-suite run can drain the bucket and make the
    ``/nl2cypher`` / ``/nl2aql`` routes here return a spurious 429 — masking
    what these tests actually assert (tenant precedence + Layer-3 wiring).

    ``test_service_hardening`` reloads ``arango_cypher.service`` via
    ``importlib``, so several ``security`` module copies (each with its own
    bucket instance and its own ``_TokenBucket`` class) can be live at once.
    We therefore scan *every* live ``arango_cypher.service*`` module and clear
    the per-key state of any bucket-shaped attribute — duck-typed, since an
    ``isinstance`` check against one class would miss buckets built by another
    reloaded copy. This restores full capacity regardless of which module
    instance the route under test actually resolved.
    """
    for name, mod in list(sys.modules.items()):
        if mod is None or not name.startswith("arango_cypher.service"):
            continue
        for attr in ("_nl_bucket", "_compute_bucket"):
            bucket = getattr(mod, attr, None)
            tokens = getattr(bucket, "_tokens", None)
            last_refill = getattr(bucket, "_last_refill", None)
            if isinstance(tokens, dict) and isinstance(last_refill, dict):
                tokens.clear()
                last_refill.clear()
    yield


# ---------------------------------------------------------------------------
# /connect — Layer 1 tenant validation
# ---------------------------------------------------------------------------


class TestConnectTenantValidation:
    """``/connect`` must validate tenantId against the Tenant collection."""

    def test_connect_with_known_tenant_returns_session_bound_fields(self):
        """Happy path: ``tenantId`` exists → session carries it."""
        fake_db = _FakeDb(
            has_tenant_collection=True,
            tenants={
                "tenant-A-uuid": {"_key": "tenant-A-uuid", "NAME": "Acme"},
            },
        )
        with _patched_arango_client(_make_fake_client(fake_db)):
            client = TestClient(_app())
            resp = client.post(
                "/connect",
                json={
                    "url": "http://example.invalid",
                    "database": "test",
                    "username": "root",
                    "password": "",
                    "tenantId": "tenant-A-uuid",
                    "tenantKey": "tenant-A-uuid",
                    "isAdmin": False,
                },
            )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["tenant_id"] == "tenant-A-uuid"
        assert body["tenant_key"] == "tenant-A-uuid"
        assert body["is_admin"] is False
        assert "token" in body and body["token"]

        sess = _fresh_service()._sessions[body["token"]]
        assert sess.tenant_id == "tenant-A-uuid"
        assert sess.tenant_key == "tenant-A-uuid"
        assert sess.is_admin is False

    def test_connect_with_unknown_tenant_refused_403(self):
        """Tenant collection exists but ``tenantKey`` is missing → 403."""
        fake_db = _FakeDb(
            has_tenant_collection=True,
            tenants={"tenant-A-uuid": {"_key": "tenant-A-uuid"}},
        )
        with _patched_arango_client(_make_fake_client(fake_db)):
            client = TestClient(_app())
            resp = client.post(
                "/connect",
                json={
                    "url": "http://example.invalid",
                    "database": "test",
                    "username": "root",
                    "password": "",
                    "tenantId": "tenant-B-rogue",
                    "tenantKey": "tenant-B-rogue",
                },
            )

        assert resp.status_code == 403, resp.text
        detail = resp.json()["detail"]
        assert detail["error"] == "unknown_tenant"
        assert detail["tenantId"] == "tenant-B-rogue"
        assert detail["tenantKey"] == "tenant-B-rogue"

    def test_connect_without_tenant_collection_accepts_id_verbatim(self):
        """Single-tenant DB (no ``Tenant`` collection): ``/connect`` accepts
        any ``tenantId`` verbatim. Layer 5 will refuse tenant-scoped reads
        downstream — see ``test_tenant_plan_validator.py``.
        """
        fake_db = _FakeDb(has_tenant_collection=False)
        with _patched_arango_client(_make_fake_client(fake_db)):
            client = TestClient(_app())
            resp = client.post(
                "/connect",
                json={
                    "url": "http://example.invalid",
                    "database": "test",
                    "username": "root",
                    "password": "",
                    "tenantId": "tenant-anything",
                },
            )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["tenant_id"] == "tenant-anything"
        assert body["tenant_key"] == "tenant-anything"

    def test_connect_without_tenant_id_yields_no_binding(self):
        """No ``tenantId`` supplied → session.tenant_id is ``None``.

        This is the pre-Wave-7 baseline behaviour, preserved for
        single-tenant / workbench deployments. Layer 5 sees a
        ``None`` tenant_id and refuses any tenant-scoped read; queries
        over purely satellite / global collections still execute.
        """
        fake_db = _FakeDb(has_tenant_collection=True, tenants={})
        with _patched_arango_client(_make_fake_client(fake_db)):
            client = TestClient(_app())
            resp = client.post(
                "/connect",
                json={
                    "url": "http://example.invalid",
                    "database": "test",
                    "username": "root",
                    "password": "",
                },
            )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["tenant_id"] is None
        assert body["tenant_key"] is None
        assert body["is_admin"] is False

        sess = _fresh_service()._sessions[body["token"]]
        assert sess.tenant_id is None
        assert sess.tenant_key is None
        assert sess.is_admin is False

    def test_connect_admin_flag_persists_on_session(self):
        """``isAdmin=true`` is mirrored on the created session.

        Admin sessions still go through Layer 5 unchanged in this WP;
        the actual cross-tenant bypass is Wave 9 (MT-7) territory.
        """
        fake_db = _FakeDb(has_tenant_collection=False)
        with _patched_arango_client(_make_fake_client(fake_db)):
            client = TestClient(_app())
            resp = client.post(
                "/connect",
                json={
                    "url": "http://example.invalid",
                    "database": "test",
                    "username": "root",
                    "password": "",
                    "tenantId": "tenant-A-uuid",
                    "isAdmin": True,
                },
            )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["is_admin"] is True
        sess = _fresh_service()._sessions[body["token"]]
        assert sess.is_admin is True

    def test_connect_uses_tenant_id_as_default_tenant_key(self):
        """``tenantKey`` defaults to ``tenantId`` when omitted."""
        fake_db = _FakeDb(
            has_tenant_collection=True,
            tenants={"tenant-A-uuid": {"_key": "tenant-A-uuid"}},
        )
        with _patched_arango_client(_make_fake_client(fake_db)):
            client = TestClient(_app())
            resp = client.post(
                "/connect",
                json={
                    "url": "http://example.invalid",
                    "database": "test",
                    "username": "root",
                    "password": "",
                    "tenantId": "tenant-A-uuid",
                },
            )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["tenant_id"] == "tenant-A-uuid"
        assert body["tenant_key"] == "tenant-A-uuid"


# ---------------------------------------------------------------------------
# /nl2cypher and /nl2aql — workbench vs tenant-user mode
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_nl_session(monkeypatch: pytest.MonkeyPatch):
    """Inject a fake session with a known ``tenant_id`` and stub the NL
    pipeline so the routes execute without an LLM call.

    Yields ``(session_token, captured_tenant_context_ref)`` — the second
    element is a dict that ``nl_to_cypher`` / ``nl_to_aql`` write the
    received ``tenant_context`` into so the test can assert what the
    pipeline saw.
    """
    from arango_cypher import nl2cypher as nl_pkg
    from arango_cypher.nl2cypher import _core as nl_core

    fake_db = _FakeDb(has_tenant_collection=False)
    with _patched_arango_client(_make_fake_client(fake_db)):
        client = TestClient(_app())
        resp = client.post(
            "/connect",
            json={
                "url": "http://example.invalid",
                "database": "test",
                "username": "root",
                "password": "",
                "tenantId": "tenant-A-uuid",
            },
        )
        assert resp.status_code == 200, resp.text
        token = resp.json()["token"]

    captured: dict[str, Any] = {}

    class _Result:
        cypher = "MATCH (n) RETURN n LIMIT 0"
        aql = "FOR n IN @@coll RETURN n"
        bind_vars: dict[str, Any] = {}
        explanation = ""
        confidence = 1.0
        method = "stub"
        prompt_tokens = 0
        completion_tokens = 0
        total_tokens = 0
        cached_tokens = 0
        retries = 0

    def _fake_nl_to_cypher(*_args, tenant_context=None, **_kwargs):
        captured["tenant_context"] = tenant_context
        return _Result()

    def _fake_nl_to_aql(*_args, tenant_context=None, **_kwargs):
        captured["tenant_context"] = tenant_context
        return _Result()

    monkeypatch.setattr(nl_pkg, "nl_to_cypher", _fake_nl_to_cypher, raising=True)
    monkeypatch.setattr(nl_pkg, "nl_to_aql", _fake_nl_to_aql, raising=True)
    monkeypatch.setattr(nl_core, "nl_to_cypher", _fake_nl_to_cypher, raising=False)
    monkeypatch.setattr(nl_core, "nl_to_aql", _fake_nl_to_aql, raising=False)

    yield token, captured

    _fresh_service()._sessions.pop(token, None)


class TestWorkbenchVsTenantUserMode:
    """``ARANGO_CYPHER_WORKBENCH`` flips body-vs-session-bound tenant precedence."""

    def test_workbench_mode_honors_body_tenant_context(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fake_nl_session,
    ):
        """``ARANGO_CYPHER_WORKBENCH=1`` → body's ``tenant_context`` wins."""
        token, captured = fake_nl_session
        monkeypatch.setenv("ARANGO_CYPHER_WORKBENCH", "1")

        client = TestClient(_app())
        resp = client.post(
            "/nl2cypher",
            headers={"X-Arango-Session": token},
            json={
                "question": "list employees",
                "use_llm": False,
                "tenant_context": {
                    "property": "TENANT_HEX_ID",
                    "value": "tenant-WORKBENCH",
                },
            },
        )

        assert resp.status_code == 200, resp.text
        seen = captured["tenant_context"]
        assert seen is not None
        assert seen.value == "tenant-WORKBENCH"
        assert seen.property == "TENANT_HEX_ID"

    def test_tenant_user_mode_overrides_body_with_session(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fake_nl_session,
        caplog: pytest.LogCaptureFixture,
    ):
        """No ``ARANGO_CYPHER_WORKBENCH`` → session-bound tenant wins; WARN logged."""
        token, captured = fake_nl_session
        monkeypatch.delenv("ARANGO_CYPHER_WORKBENCH", raising=False)

        client = TestClient(_app())
        with caplog.at_level(logging.WARNING, logger="arango_cypher.service.routes.nl"):
            resp = client.post(
                "/nl2cypher",
                headers={"X-Arango-Session": token},
                json={
                    "question": "list employees",
                    "use_llm": False,
                    "tenant_context": {
                        "property": "TENANT_HEX_ID",
                        "value": "tenant-B-rogue",
                    },
                },
            )

        assert resp.status_code == 200, resp.text
        seen = captured["tenant_context"]
        assert seen is not None
        assert seen.value == "tenant-A-uuid"
        warning_messages = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("tenant-B-rogue" in m and "tenant-A-uuid" in m for m in warning_messages), (
            f"expected override-warning in logs; got {warning_messages!r}"
        )

    def test_tenant_user_mode_injects_when_body_omits_tenant(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fake_nl_session,
    ):
        """Body has no ``tenant_context`` but session is bound → inject session."""
        token, captured = fake_nl_session
        monkeypatch.delenv("ARANGO_CYPHER_WORKBENCH", raising=False)

        client = TestClient(_app())
        resp = client.post(
            "/nl2cypher",
            headers={"X-Arango-Session": token},
            json={"question": "list employees", "use_llm": False},
        )

        assert resp.status_code == 200, resp.text
        seen = captured["tenant_context"]
        assert seen is not None
        assert seen.value == "tenant-A-uuid"

    def test_tenant_user_mode_passes_through_when_body_matches_session(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fake_nl_session,
        caplog: pytest.LogCaptureFixture,
    ):
        """Body's tenant matches session → session is used, no WARN emitted."""
        token, captured = fake_nl_session
        monkeypatch.delenv("ARANGO_CYPHER_WORKBENCH", raising=False)

        client = TestClient(_app())
        with caplog.at_level(logging.WARNING, logger="arango_cypher.service.routes.nl"):
            resp = client.post(
                "/nl2aql",
                headers={"X-Arango-Session": token},
                json={
                    "question": "list employees",
                    "tenant_context": {
                        "property": "TENANT_HEX_ID",
                        "value": "tenant-A-uuid",
                    },
                },
            )

        assert resp.status_code == 200, resp.text
        seen = captured["tenant_context"]
        assert seen is not None
        assert seen.value == "tenant-A-uuid"
        warning_messages = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
        assert not any("ignored" in m and "tenant-A-uuid" in m for m in warning_messages), (
            f"unexpected override warning when body == session: {warning_messages!r}"
        )


# ---------------------------------------------------------------------------
# /nl2cypher — Layer 3 (Cypher AST tenant injection) route wiring (MT-3b)
# ---------------------------------------------------------------------------


class TestNl2CypherLayer3Injection:
    """MT-3b: ``/nl2cypher`` runs Layer 3 on the generated Cypher.

    Verifies the route wiring (not the core rewriter, which is covered by
    ``test_tenant_ast_cypher.py``): a tenant-bound session with a mapping
    gets the bind-var predicate injected and surfaced via ``tenantRewrites``;
    a literal tenant predicate is refused with a structured HTTP 403.
    """

    def _mapping(self) -> dict[str, Any]:
        return {
            "conceptual_schema": {
                "entities": [
                    {"name": "Tenant", "properties": []},
                    {"name": "Employee", "properties": [{"name": "name"}]},
                ],
                "relationships": [],
            },
            "physical_mapping": {
                "entities": {
                    "Tenant": {"style": "COLLECTION", "collectionName": "Tenant"},
                    "Employee": {
                        "style": "COLLECTION",
                        "collectionName": "Employee",
                        "tenantScope": {"role": "tenant_scoped", "tenantField": "TENANT_HEX_ID"},
                    },
                },
                "relationships": {},
            },
            "metadata": {},
        }

    def _connect_bound(self) -> str:
        fake_db = _FakeDb(has_tenant_collection=False)
        with _patched_arango_client(_make_fake_client(fake_db)):
            client = TestClient(_app())
            resp = client.post(
                "/connect",
                json={
                    "url": "http://example.invalid",
                    "database": "test",
                    "username": "root",
                    "password": "",
                    "tenantId": "tenant-A-uuid",
                },
            )
        assert resp.status_code == 200, resp.text
        return resp.json()["token"]

    def _stub_nl_to_cypher(self, monkeypatch: pytest.MonkeyPatch, cypher_out: str) -> None:
        from arango_cypher import nl2cypher as nl_pkg
        from arango_cypher.nl2cypher import _core as nl_core

        class _Result:
            cypher = cypher_out
            explanation = ""
            confidence = 1.0
            method = "stub"
            prompt_tokens = 0
            completion_tokens = 0
            total_tokens = 0
            cached_tokens = 0
            retries = 0
            advisories: list[Any] = []

        def _fake(*_args, **_kwargs):
            return _Result()

        monkeypatch.setattr(nl_pkg, "nl_to_cypher", _fake, raising=True)
        monkeypatch.setattr(nl_core, "nl_to_cypher", _fake, raising=False)

    def test_scoped_pattern_rewritten_and_surfaced(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("ARANGO_CYPHER_WORKBENCH", raising=False)
        token = self._connect_bound()
        self._stub_nl_to_cypher(monkeypatch, "MATCH (e:Employee) RETURN e")

        client = TestClient(_app())
        resp = client.post(
            "/nl2cypher",
            headers={"X-Arango-Session": token},
            json={"question": "list employees", "use_llm": False, "mapping": self._mapping()},
        )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "TENANT_HEX_ID: $tenantId" in body["cypher"]
        assert body["tenantRewrites"], "expected a non-empty tenantRewrites list"
        _fresh_service()._sessions.pop(token, None)

    def test_no_mapping_skips_layer3(self, monkeypatch: pytest.MonkeyPatch):
        """Without a mapping, Layer 3 is skipped — the Cypher passes through
        unchanged (Layer 5 remains the fail-closed boundary at execute)."""
        monkeypatch.delenv("ARANGO_CYPHER_WORKBENCH", raising=False)
        token = self._connect_bound()
        self._stub_nl_to_cypher(monkeypatch, "MATCH (e:Employee) RETURN e")

        client = TestClient(_app())
        resp = client.post(
            "/nl2cypher",
            headers={"X-Arango-Session": token},
            json={"question": "list employees", "use_llm": False},
        )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["cypher"] == "MATCH (e:Employee) RETURN e"
        assert body["tenantRewrites"] == []
        _fresh_service()._sessions.pop(token, None)

    def test_literal_tenant_predicate_refused_403(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("ARANGO_CYPHER_WORKBENCH", raising=False)
        token = self._connect_bound()
        self._stub_nl_to_cypher(
            monkeypatch,
            "MATCH (e:Employee {TENANT_HEX_ID: 'tenant-B-uuid'}) RETURN e",
        )

        client = TestClient(_app())
        resp = client.post(
            "/nl2cypher",
            headers={"X-Arango-Session": token},
            json={"question": "list rival employees", "use_llm": False, "mapping": self._mapping()},
        )

        assert resp.status_code == 403, resp.text
        detail = resp.json()["detail"]
        assert detail["error"] == "tenant_scope_violation"
        assert detail["code"] == "LITERAL_TENANT_PREDICATE"
        _fresh_service()._sessions.pop(token, None)


# ---------------------------------------------------------------------------
# /execute-aql — MT-7 admin cross-tenant bypass gating
# ---------------------------------------------------------------------------


class TestAdminCrossTenantBypassGating:
    """The route-level gate for ``cross_tenant`` on ``/execute-aql`` (MT-7).

    These pin the trust-boundary refusals that fire *before* any DB
    access: a non-admin cannot request a bypass, and an admin must give
    a reason for the audit stream. The successful bypass execution path
    is covered at the adapter/validator level in
    ``tests/test_tenant_admin_bypass.py``.
    """

    def _connect(self, *, is_admin: bool) -> str:
        fake_db = _FakeDb(has_tenant_collection=False)
        with _patched_arango_client(_make_fake_client(fake_db)):
            client = TestClient(_app())
            resp = client.post(
                "/connect",
                json={
                    "url": "http://example.invalid",
                    "database": "test",
                    "username": "root",
                    "password": "",
                    "tenantId": "tenant-A-uuid",
                    "isAdmin": is_admin,
                },
            )
        assert resp.status_code == 200, resp.text
        return resp.json()["token"]

    def test_non_admin_cross_tenant_refused_403(self):
        token = self._connect(is_admin=False)
        client = TestClient(_app())
        resp = client.post(
            "/execute-aql",
            headers={"X-Arango-Session": token},
            json={"aql": "FOR e IN Employee RETURN e", "cross_tenant": True, "bypass_reason": "x"},
        )
        assert resp.status_code == 403, resp.text
        assert resp.json()["detail"]["code"] == "ADMIN_REQUIRED"
        _fresh_service()._sessions.pop(token, None)

    def test_admin_cross_tenant_without_reason_refused_422(self):
        token = self._connect(is_admin=True)
        client = TestClient(_app())
        resp = client.post(
            "/execute-aql",
            headers={"X-Arango-Session": token},
            json={"aql": "FOR e IN Employee RETURN e", "cross_tenant": True},
        )
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"]["code"] == "BYPASS_REASON_REQUIRED"
        _fresh_service()._sessions.pop(token, None)


# ---------------------------------------------------------------------------
# /execute — tenant refusals must surface as 403, never masked as 500
# ---------------------------------------------------------------------------


class TestExecuteTenantViolationStatusCode:
    """Regression: a ``TenantScopeViolation`` raised by Layer 5/6 inside the
    ``_translate_errors`` block must reach the client as a structured HTTP
    403, not a generic 500.

    The bug: ``_translate_errors`` (a context manager wrapping
    ``safe_execute_aql``) caught the violation as a plain ``Exception`` and
    converted it to 500 before the route's own ``except TenantScopeViolation``
    handler could run — hiding an actionable "connect with a tenant" refusal
    behind "Internal Server Error".
    """

    def _connect_unbound(self) -> str:
        fake_db = _FakeDb(has_tenant_collection=False)
        with _patched_arango_client(_make_fake_client(fake_db)):
            client = TestClient(_app())
            resp = client.post(
                "/connect",
                json={
                    "url": "http://example.invalid",
                    "database": "test",
                    "username": "root",
                    "password": "",
                },
            )
        assert resp.status_code == 200, resp.text
        return resp.json()["token"]

    def test_tenant_violation_surfaces_as_403(self, monkeypatch: pytest.MonkeyPatch):
        from arango_cypher.service.routes import cypher as cypher_routes
        from arango_cypher.tenant_plan_validator import TenantScopeViolation

        token = self._connect_unbound()

        # Reach safe_execute_aql without needing a real schema/translation:
        # /execute translates through translate_plan (one step here).
        monkeypatch.setattr(cypher_routes, "_mapping_from_dict", lambda _m: object())
        monkeypatch.setattr(cypher_routes, "translate_plan", lambda *_a, **_k: _one_step_plan())

        def _raise_violation(**_kwargs):
            raise TenantScopeViolation(
                code="NO_SESSION_TENANT",
                message="session has no tenant_id; cannot validate tenant-scoped query",
                aql_digest="a" * 64,
                plan_digest="b" * 64,
            )

        monkeypatch.setattr(cypher_routes, "safe_execute_aql", _raise_violation)

        client = TestClient(_app())
        resp = client.post(
            "/execute",
            headers={"X-Arango-Session": token},
            json={"cypher": "MATCH (n:User) RETURN n", "mapping": {"any": "thing"}},
        )

        assert resp.status_code == 403, resp.text
        detail = resp.json()["detail"]
        assert detail["error"] == "tenant_scope_violation"
        assert detail["code"] == "NO_SESSION_TENANT"
        assert detail["aql_digest"] == "a" * 16
        _fresh_service()._sessions.pop(token, None)

    def test_generic_execute_error_still_500(self, monkeypatch: pytest.MonkeyPatch):
        """A non-tenant failure (e.g. a driver error) must still map to 500
        via ``_translate_errors`` — the 403 path is scoped to tenant refusals.
        """
        from arango_cypher.service.routes import cypher as cypher_routes

        token = self._connect_unbound()

        monkeypatch.setattr(cypher_routes, "_mapping_from_dict", lambda _m: object())
        monkeypatch.setattr(cypher_routes, "translate_plan", lambda *_a, **_k: _one_step_plan())

        def _raise_runtime(**_kwargs):
            raise RuntimeError("arango driver exploded")

        monkeypatch.setattr(cypher_routes, "safe_execute_aql", _raise_runtime)

        client = TestClient(_app())
        resp = client.post(
            "/execute",
            headers={"X-Arango-Session": token},
            json={"cypher": "MATCH (n:User) RETURN n", "mapping": {"any": "thing"}},
        )

        assert resp.status_code == 500, resp.text
        _fresh_service()._sessions.pop(token, None)
