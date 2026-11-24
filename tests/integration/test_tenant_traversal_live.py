"""Tenant traversals on ArangoDB, through Layer 4 (rewrite) and Layer 5 (plan check).

A traversal over a graph that is not a disjoint SmartGraph keeps to the
session tenant with ``PRUNE v.<field> != @tenantId`` (it stops at a vertex of
another tenant) and ``FILTER v.<field> == @tenantId`` (it does not yield that
vertex). Layer 4 adds both, and Layer 5 accepts a real plan only with both: a
PRUNE alone yields the vertex it stops at, and a FILTER alone walks through
other tenants' vertices, so a path carries them. The unit counterparts are
``TestTraversal`` in ``tests/test_tenant_ast_aql.py`` and
``TestTraversalNode`` in ``tests/test_tenant_plan_validator.py``.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import pytest
from arango import ArangoClient

from arango_cypher.tenant_ast_aql import inject_tenant_scope
from arango_cypher.tenant_plan_validator import TenantScopeViolation, validate_plan
from tests.test_tenant_plan_validator import _FakeSession, _manifest, _sharding_profile

pytestmark = pytest.mark.integration

_DB = "arango_cypher_tenant_traversal_it"
_A = "tenant-A-uuid"
#: Tenant A: a1..a4; tenant B: b1, b2. a3 is reached from a1 only through b1.
_ASSETS = {"a1": _A, "a2": _A, "a3": _A, "a4": _A, "b1": "tenant-B-uuid", "b2": "tenant-B-uuid"}
_LINKS = [("a1", "a2"), ("a2", "a4"), ("a1", "b1"), ("b1", "a3"), ("b1", "b2")]
#: The start, scoped as Layer 3 leaves it (a scan, so the tenant filter is fused into it).
_START = "FOR s IN Asset FILTER s.name == 'a1' AND s.TENANT_HEX_ID == @tenantId "


@pytest.fixture(scope="module")
def db() -> Iterator[Any]:
    if os.environ.get("RUN_INTEGRATION") != "1":
        pytest.skip("RUN_INTEGRATION=1 required")
    user = os.environ.get("ARANGO_USER", "root")
    password = os.environ.get("ARANGO_PASS", "openSesame")
    client = ArangoClient(hosts=os.environ.get("ARANGO_URL", "http://localhost:8529"))
    sys_db = client.db("_system", username=user, password=password)
    if sys_db.has_database(_DB):
        sys_db.delete_database(_DB)
    sys_db.create_database(_DB)
    try:
        database = client.db(_DB, username=user, password=password)
        database.create_collection("Asset").import_bulk(
            [{"_key": k, "TENANT_HEX_ID": t, "name": k} for k, t in _ASSETS.items()]
        )
        database.create_collection("LINK", edge=True).import_bulk(
            [{"_from": f"Asset/{f}", "_to": f"Asset/{t}"} for f, t in _LINKS]
        )
        yield database
    finally:
        if sys_db.has_database(_DB):
            sys_db.delete_database(_DB)


def _rewrite(db: Any, aql: str) -> str:
    out, _, _ = inject_tenant_scope(
        db=db,
        aql=aql,
        bind_vars={"tenantId": _A},
        manifest=_manifest(),
        sharding_profile=_sharding_profile(),
        tenant_id=_A,
        tenant_key=_A,
    )
    return out


def _validate(db: Any, aql: str) -> None:
    validate_plan(
        db=db,
        aql=aql,
        bind_vars={"tenantId": _A},
        manifest=_manifest(),
        sharding_profile=_sharding_profile(),
        session=_FakeSession(),
    )


def _run(db: Any, aql: str) -> list[Any]:
    return list(db.aql.execute(aql, bind_vars={"tenantId": _A}))


@pytest.mark.parametrize(
    "traversal",
    [
        "FOR v, e, p IN 1..3 OUTBOUND s LINK",
        # An existing PRUNE and OPTIONS are kept.
        "FOR v, e, p IN 1..3 OUTBOUND s LINK PRUNE v.name == 'a4' OPTIONS {order: 'bfs'}",
    ],
)
def test_a_rewritten_traversal_keeps_to_the_tenant(db: Any, traversal: str) -> None:
    aql = _rewrite(db, f"{_START}{traversal} RETURN {{v: v._key, path: p.vertices[*]._key}}")
    _validate(db, aql)
    rows = _run(db, aql)
    assert sorted(r["v"] for r in rows) == ["a2", "a4"]
    assert all(_ASSETS[key] == _A for r in rows for key in r["path"])
    # A second pass adds nothing to the traversal.
    again = _rewrite(db, aql)
    assert again.count("PRUNE") == aql.count("PRUNE") == 1
    assert again.count("v.TENANT_HEX_ID == @tenantId") == aql.count("v.TENANT_HEX_ID == @tenantId") == 1


@pytest.mark.parametrize(
    ("clauses", "accepted", "leaks"),
    [
        ("PRUNE v.TENANT_HEX_ID != @tenantId FILTER v.TENANT_HEX_ID == @tenantId", True, False),
        # Yields b1, the vertex it stops at.
        ("PRUNE v.TENANT_HEX_ID != @tenantId", False, True),
        # Yields a3 through b1.
        ("FILTER v.TENANT_HEX_ID == @tenantId", False, True),
        # Proves nothing (it stops at the start here, so returns nothing).
        ("PRUNE v.TENANT_HEX_ID == @tenantId FILTER v.TENANT_HEX_ID == @tenantId", False, False),
    ],
)
def test_layer_5_accepts_a_traversal_only_when_it_prunes_and_filters(
    db: Any, clauses: str, accepted: bool, leaks: bool
) -> None:
    aql = (
        f"{_START}FILTER s.TENANT_HEX_ID == @tenantId FOR v, e, p IN 1..3 OUTBOUND s LINK {clauses} RETURN p"
    )
    paths = _run(db, aql)
    assert leaks == any(_ASSETS[vertex["_key"]] != _A for path in paths for vertex in path["vertices"])
    if accepted:
        _validate(db, aql)
    else:
        with pytest.raises(TenantScopeViolation) as exc_info:
            _validate(db, aql)
        assert exc_info.value.code == "UNCONSTRAINED_TRAVERSAL"
