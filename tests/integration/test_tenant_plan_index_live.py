"""Layer 5 on real EXPLAIN plans: an IndexNode's condition, branch by branch.

ArangoDB gives an index condition as an OR of AND branches. The plan is
accepted only when every branch equality-matches the tenant field against
``@tenantId``. Each case also runs the query, and checks the verdict against
whether rows of another tenant come back. The unit counterpart is
``TestIndexNode`` in ``tests/test_tenant_plan_validator.py``.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import pytest
from arango import ArangoClient

from arango_cypher.tenant_plan_validator import TenantScopeViolation, validate_plan
from tests.test_tenant_plan_validator import _FakeSession, _manifest, _sharding_profile

pytestmark = pytest.mark.integration

_DB = "arango_cypher_tenant_plan_index_it"
_TENANT = "d.TENANT_HEX_ID == @tenantId"


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
        employees = database.create_collection("Employee")
        employees.add_index({"type": "persistent", "fields": ["TENANT_HEX_ID"]})
        employees.add_index({"type": "persistent", "fields": ["NAME"]})
        employees.import_bulk(
            [{"TENANT_HEX_ID": t, "NAME": n} for t in ("tenant-A-uuid", "tenant-B-uuid") for n in ("x", "y")]
        )
        yield database
    finally:
        if sys_db.has_database(_DB):
            sys_db.delete_database(_DB)


@pytest.mark.parametrize(
    ("flt", "accepted"),
    [
        (f"FILTER {_TENANT}", True),
        (f"FILTER {_TENANT} AND d.NAME == 'x'", True),
        (f"FILTER ({_TENANT} AND d.NAME == 'x') OR ({_TENANT} AND d.NAME == 'y')", True),
        (f"FILTER {_TENANT} OR d.NAME == 'x'", False),
    ],
)
def test_an_index_condition_is_accepted_only_when_every_branch_is_scoped(
    db: Any, flt: str, accepted: bool
) -> None:
    aql = f"FOR d IN Employee {flt} RETURN d"
    bind_vars = {"tenantId": "tenant-A-uuid"}
    plan = db.aql.explain(aql, bind_vars=bind_vars)
    assert any(n["type"] == "IndexNode" for n in plan["nodes"]), plan
    rows = list(db.aql.execute(aql, bind_vars=bind_vars))
    # The verdict matches what the query returns.
    assert accepted == all(r["TENANT_HEX_ID"] == "tenant-A-uuid" for r in rows)

    def validate() -> None:
        validate_plan(
            db=db,
            aql=aql,
            bind_vars=bind_vars,
            manifest=_manifest(),
            sharding_profile=_sharding_profile(),
            session=_FakeSession(),
        )

    if accepted:
        validate()
    else:
        with pytest.raises(TenantScopeViolation) as exc_info:
            validate()
        assert exc_info.value.code == "INDEX_MISSING_TENANT_PREDICATE"
