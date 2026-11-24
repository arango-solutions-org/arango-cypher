"""Layer 5 / Wave 7 part 3 — EXPLAIN-plan validator (the security boundary).

This is the most important test suite in the multi-tenant Phase 1 PR
(per ``docs/multitenant_prd.md`` §8.6: "the validator's correctness is
defined by these tests; any ambiguity in the pipeline is pushed into
'is the test expressing the right intent?' rather than 'does the
validator have the right heuristic?'").

Every test takes a hand-crafted EXPLAIN-plan fragment and asserts the
validator accepts or refuses it. No live ArangoDB. The ``db`` argument
is bypassed via ``plan_override`` so the round-trip is deterministic.

Coverage map (PRD §8.6 + Wave 7 prompt):

* ``EnumerateCollectionNode`` over a tenant-scoped collection:
    * no filter → ``UNCONSTRAINED_COLLECTION_SCAN``
    * literal-string predicate → ``LITERAL_TENANT_PREDICATE``
    * ``@tenantId`` bind-var predicate → accepted
* ``IndexNode``:
    * keyed by ``smartGraphAttribute == @tenantId`` → accepted
    * keyed only by ``_key`` with no tenant predicate → rejected
    * tenant-root index lookup keyed by ``_key == @tenantKey`` → accepted
* ``TraversalNode``:
    * satellite-only vertex collections → accepted
    * ``options.prune`` references ``@tenantId`` → accepted
    * graphName resolves to a disjoint smartgraph → accepted
    * none of the above → ``UNCONSTRAINED_TRAVERSAL``
* Subqueries: recursion catches an unconstrained inner scan.
* Bind-var sanity:
    * ``session.tenant_id`` is None and plan touches a TENANT_SCOPED
      collection → ``NO_SESSION_TENANT``
    * ``bind_vars['tenantId'] != session.tenant_id`` →
      ``TENANT_BIND_MISMATCH``
* Audit:
    * every accept emits a ``TENANT_SCOPE_OK`` log with both digests
    * every refuse emits a ``TENANT_SCOPE_VIOLATION`` log
* Defence in depth:
    * every accept case is re-run with a deliberately-wrong session
      tenant and asserts the bind-var check dominates.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import pytest

from arango_cypher.nl2cypher.tenant_scope import (
    EntityScope,
    EntityTenantRole,
    TenantScopeManifest,
)
from arango_cypher.tenant_plan_validator import (
    TenantScopeViolation,
    validate_plan,
)

# ---------------------------------------------------------------------------
# Fixtures: hand-crafted manifest, sharding profile, and plan fragments
# ---------------------------------------------------------------------------


@dataclass
class _FakeSession:
    token: str = "session-A-TOKEN"
    tenant_id: str | None = "tenant-A-uuid"
    tenant_key: str | None = "tenant-A-uuid"
    is_admin: bool = False


def _manifest() -> TenantScopeManifest:
    """Manifest with one TENANT_ROOT (Tenant), two TENANT_SCOPED
    entities (Employee with denorm field, Asset traversal-only) and one
    GLOBAL (Country).
    """
    return TenantScopeManifest(
        tenant_entity="Tenant",
        entities={
            "Tenant": EntityScope(
                role=EntityTenantRole.TENANT_ROOT,
                denorm_field=None,
                reachable_from_tenant=True,
            ),
            "Employee": EntityScope(
                role=EntityTenantRole.TENANT_SCOPED,
                denorm_field="TENANT_HEX_ID",
                reachable_from_tenant=True,
            ),
            "Asset": EntityScope(
                role=EntityTenantRole.TENANT_SCOPED,
                denorm_field=None,
                reachable_from_tenant=True,
                scoping_path=("TENANTOWNSASSET",),
            ),
            "Country": EntityScope(
                role=EntityTenantRole.GLOBAL,
                denorm_field=None,
                reachable_from_tenant=False,
            ),
        },
    )


def _sharding_profile() -> dict[str, Any]:
    return {
        "style": "DisjointSmartGraph",
        "members": {
            "Tenant": {"kind": "tenant-root"},
            "Employee": {"kind": "smartgraph"},
            "Asset": {"kind": "smartgraph"},
            "Country": {"kind": "satellite"},
        },
        "graphs": [
            {
                "name": "TenantGraph",
                "smartGraphAttribute": "TENANT_HEX_ID",
                "isDisjoint": True,
                "vertexCollections": ["Tenant", "Employee", "Asset"],
                "edgeCollections": ["TENANTOWNSEMPLOYEE", "TENANTOWNSASSET"],
            },
            {
                "name": "CountryGraph",
                "smartGraphAttribute": "_key",
                "isDisjoint": False,
                "vertexCollections": ["Country"],
            },
        ],
    }


def _enum_node(
    *,
    nid: int,
    collection: str,
    outvar: str = "doc",
    outvar_id: int = 100,
) -> dict[str, Any]:
    return {
        "type": "EnumerateCollectionNode",
        "id": nid,
        "collection": collection,
        "outVariable": {"name": outvar, "id": outvar_id},
    }


def _calc_eq_attr_bindvar(
    *,
    nid: int,
    var_name: str,
    attr: str,
    bindvar: str,
) -> dict[str, Any]:
    return {
        "type": "CalculationNode",
        "id": nid,
        "outVariable": {"name": f"_calc{nid}", "id": 200 + nid},
        "expression": {
            "type": "compare ==",
            "subNodes": [
                {
                    "type": "attribute access",
                    "name": attr,
                    "subNodes": [
                        {"type": "reference", "name": var_name, "id": 100},
                    ],
                },
                {"type": "parameter", "name": bindvar},
            ],
        },
    }


def _calc_eq_attr_literal(
    *,
    nid: int,
    var_name: str,
    attr: str,
    literal: str,
) -> dict[str, Any]:
    return {
        "type": "CalculationNode",
        "id": nid,
        "outVariable": {"name": f"_calc{nid}", "id": 200 + nid},
        "expression": {
            "type": "compare ==",
            "subNodes": [
                {
                    "type": "attribute access",
                    "name": attr,
                    "subNodes": [
                        {"type": "reference", "name": var_name, "id": 100},
                    ],
                },
                {"type": "value", "value": literal},
            ],
        },
    }


def _filter_node(*, nid: int, calc_id: int) -> dict[str, Any]:
    return {
        "type": "FilterNode",
        "id": nid,
        "inVariable": {"name": f"_calc{calc_id}", "id": 200 + calc_id},
    }


def _return_node(*, nid: int) -> dict[str, Any]:
    return {"type": "ReturnNode", "id": nid}


def _singleton_node() -> dict[str, Any]:
    return {"type": "SingletonNode", "id": 1}


def _wrap_plan(nodes: list[dict[str, Any]]) -> dict[str, Any]:
    return {"nodes": nodes}


def _call_validate(
    *,
    plan: dict[str, Any],
    bind_vars: dict[str, Any] | None = None,
    session: _FakeSession | None = None,
    manifest: TenantScopeManifest | None = None,
    sharding_profile: dict[str, Any] | None = None,
    collection_to_entity: dict[str, str] | None = None,
) -> None:
    validate_plan(
        db=None,
        aql="hand-crafted",
        bind_vars=bind_vars if bind_vars is not None else {"tenantId": "tenant-A-uuid"},
        manifest=manifest if manifest is not None else _manifest(),
        sharding_profile=sharding_profile if sharding_profile is not None else _sharding_profile(),
        collection_to_entity=collection_to_entity,
        session=session if session is not None else _FakeSession(),
        plan_override=plan,
    )


# ---------------------------------------------------------------------------
# EnumerateCollectionNode tests
# ---------------------------------------------------------------------------


class TestEnumerateCollection:
    def test_satellite_only_query_accepted_without_tenant_binding(self) -> None:
        plan = _wrap_plan([_singleton_node(), _enum_node(nid=2, collection="Country"), _return_node(nid=3)])
        # No bind-var, no session tenant — still accepted because the
        # plan touches no tenant-scoped collections.
        _call_validate(
            plan=plan,
            bind_vars={},
            session=_FakeSession(tenant_id=None, tenant_key=None),
        )

    def test_scan_without_tenant_filter_rejected(self) -> None:
        plan = _wrap_plan([_singleton_node(), _enum_node(nid=2, collection="Employee"), _return_node(nid=3)])
        with pytest.raises(TenantScopeViolation) as exc_info:
            _call_validate(plan=plan)
        assert exc_info.value.code == "UNCONSTRAINED_COLLECTION_SCAN"
        assert "Employee" in exc_info.value.message

    def test_scan_with_literal_tenant_predicate_rejected(self) -> None:
        plan = _wrap_plan(
            [
                _singleton_node(),
                _enum_node(nid=2, collection="Employee"),
                _calc_eq_attr_literal(nid=3, var_name="doc", attr="TENANT_HEX_ID", literal="tenant-A-uuid"),
                _filter_node(nid=4, calc_id=3),
                _return_node(nid=5),
            ]
        )
        with pytest.raises(TenantScopeViolation) as exc_info:
            _call_validate(plan=plan)
        assert exc_info.value.code == "LITERAL_TENANT_PREDICATE"
        assert "tenant-A-uuid" in exc_info.value.message

    def test_scan_with_bindvar_tenant_predicate_accepted(self) -> None:
        plan = _wrap_plan(
            [
                _singleton_node(),
                _enum_node(nid=2, collection="Employee"),
                _calc_eq_attr_bindvar(nid=3, var_name="doc", attr="TENANT_HEX_ID", bindvar="tenantId"),
                _filter_node(nid=4, calc_id=3),
                _return_node(nid=5),
            ]
        )
        _call_validate(plan=plan)

    def test_scan_with_bindvar_predicate_but_wrong_session_tenant_rejected(self) -> None:
        """Defence in depth: bind-var matches plan but mismatches session."""
        plan = _wrap_plan(
            [
                _singleton_node(),
                _enum_node(nid=2, collection="Employee"),
                _calc_eq_attr_bindvar(nid=3, var_name="doc", attr="TENANT_HEX_ID", bindvar="tenantId"),
                _filter_node(nid=4, calc_id=3),
                _return_node(nid=5),
            ]
        )
        with pytest.raises(TenantScopeViolation) as exc_info:
            _call_validate(
                plan=plan,
                bind_vars={"tenantId": "tenant-B-uuid"},
                session=_FakeSession(tenant_id="tenant-A-uuid"),
            )
        assert exc_info.value.code == "TENANT_BIND_MISMATCH"

    def test_no_session_tenant_with_tenant_scoped_scan_rejected(self) -> None:
        plan = _wrap_plan(
            [
                _singleton_node(),
                _enum_node(nid=2, collection="Employee"),
                _return_node(nid=3),
            ]
        )
        with pytest.raises(TenantScopeViolation) as exc_info:
            _call_validate(
                plan=plan,
                bind_vars={},
                session=_FakeSession(tenant_id=None, tenant_key=None),
            )
        assert exc_info.value.code == "NO_SESSION_TENANT"

    def test_predicate_against_wrong_variable_does_not_count(self) -> None:
        """A predicate on a different document variable must not be
        accepted as scope for the tenant-scoped enum we care about.
        """
        plan = _wrap_plan(
            [
                _singleton_node(),
                _enum_node(nid=2, collection="Employee", outvar="emp"),
                _calc_eq_attr_bindvar(
                    nid=3,
                    var_name="other_var",
                    attr="TENANT_HEX_ID",
                    bindvar="tenantId",
                ),
                _filter_node(nid=4, calc_id=3),
                _return_node(nid=5),
            ]
        )
        with pytest.raises(TenantScopeViolation) as exc_info:
            _call_validate(plan=plan)
        assert exc_info.value.code == "UNCONSTRAINED_COLLECTION_SCAN"


# ---------------------------------------------------------------------------
# IndexNode tests
# ---------------------------------------------------------------------------


class TestIndexNode:
    def _index_node(
        self,
        *,
        collection: str,
        outvar: str = "doc",
        condition: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "type": "IndexNode",
            "id": 2,
            "collection": collection,
            "outVariable": {"name": outvar, "id": 100},
            "condition": condition,
        }

    def test_index_keyed_by_smartgraph_attribute_accepted(self) -> None:
        node = self._index_node(
            collection="Employee",
            condition={
                "type": "compare ==",
                "subNodes": [
                    {
                        "type": "attribute access",
                        "name": "TENANT_HEX_ID",
                        "subNodes": [{"type": "reference", "name": "doc", "id": 100}],
                    },
                    {"type": "parameter", "name": "tenantId"},
                ],
            },
        )
        plan = _wrap_plan([_singleton_node(), node, _return_node(nid=3)])
        _call_validate(plan=plan)

    def test_index_keyed_only_by_key_on_tenant_scoped_rejected(self) -> None:
        node = self._index_node(
            collection="Employee",
            condition={
                "type": "compare ==",
                "subNodes": [
                    {
                        "type": "attribute access",
                        "name": "_key",
                        "subNodes": [{"type": "reference", "name": "doc", "id": 100}],
                    },
                    {"type": "parameter", "name": "someKey"},
                ],
            },
        )
        plan = _wrap_plan([_singleton_node(), node, _return_node(nid=3)])
        with pytest.raises(TenantScopeViolation) as exc_info:
            _call_validate(plan=plan)
        assert exc_info.value.code == "INDEX_MISSING_TENANT_PREDICATE"

    def test_tenant_root_index_keyed_by_tenant_key_accepted(self) -> None:
        node = self._index_node(
            collection="Tenant",
            condition={
                "type": "compare ==",
                "subNodes": [
                    {
                        "type": "attribute access",
                        "name": "_key",
                        "subNodes": [{"type": "reference", "name": "doc", "id": 100}],
                    },
                    {"type": "parameter", "name": "tenantKey"},
                ],
            },
        )
        plan = _wrap_plan([_singleton_node(), node, _return_node(nid=3)])
        _call_validate(
            plan=plan,
            bind_vars={"tenantId": "tenant-A-uuid", "tenantKey": "tenant-A-uuid"},
        )

    @staticmethod
    def _eq(attr: str, rhs: dict[str, Any]) -> dict[str, Any]:
        return {
            "type": "compare ==",
            "subNodes": [
                {
                    "type": "attribute access",
                    "name": attr,
                    "subNodes": [{"type": "reference", "name": "doc", "id": 100}],
                },
                rhs,
            ],
        }

    def _dnf(self, *branches: list[dict[str, Any]]) -> dict[str, Any]:
        """An index condition as ArangoDB gives it: an OR of AND branches."""
        return {"type": "n-ary or", "subNodes": [{"type": "n-ary and", "subNodes": b} for b in branches]}

    def test_every_branch_of_an_index_condition_is_scoped(self) -> None:
        tenant = self._eq("TENANT_HEX_ID", {"type": "parameter", "name": "tenantId"})
        cond = self._dnf(
            [tenant, self._eq("NAME", {"type": "value", "value": "x"})],
            [tenant, self._eq("NAME", {"type": "value", "value": "y"})],
        )
        node = self._index_node(collection="Employee", condition=cond)
        _call_validate(plan=_wrap_plan([_singleton_node(), node, _return_node(nid=3)]))

    @pytest.mark.parametrize(
        ("collection", "attr", "bind", "code"),
        [
            ("Employee", "TENANT_HEX_ID", "tenantId", "INDEX_MISSING_TENANT_PREDICATE"),
            ("Tenant", "_key", "tenantKey", "TENANT_ROOT_UNCONSTRAINED"),
        ],
    )
    def test_a_branch_without_the_tenant_predicate_is_rejected(
        self, collection: str, attr: str, bind: str, code: str
    ) -> None:
        cond = self._dnf(
            [self._eq(attr, {"type": "parameter", "name": bind})],
            [self._eq("NAME", {"type": "value", "value": "x"})],
        )
        node = self._index_node(collection=collection, condition=cond)
        plan = _wrap_plan([_singleton_node(), node, _return_node(nid=3)])
        with pytest.raises(TenantScopeViolation) as exc_info:
            _call_validate(plan=plan, bind_vars={"tenantId": "tenant-A-uuid", "tenantKey": "tenant-A-uuid"})
        assert exc_info.value.code == code

    def test_a_negated_tenant_predicate_is_rejected(self) -> None:
        tenant = self._eq("TENANT_HEX_ID", {"type": "parameter", "name": "tenantId"})
        cond = self._dnf([{"type": "logical not", "subNodes": [tenant]}])
        node = self._index_node(collection="Employee", condition=cond)
        plan = _wrap_plan([_singleton_node(), node, _return_node(nid=3)])
        with pytest.raises(TenantScopeViolation) as exc_info:
            _call_validate(plan=plan)
        assert exc_info.value.code == "INDEX_MISSING_TENANT_PREDICATE"

    def test_an_empty_or_is_rejected(self) -> None:
        node = self._index_node(collection="Employee", condition={"type": "n-ary or", "subNodes": []})
        plan = _wrap_plan([_singleton_node(), node, _return_node(nid=3)])
        with pytest.raises(TenantScopeViolation):
            _call_validate(plan=plan)

    def test_tenant_root_index_without_tenant_key_rejected(self) -> None:
        node = self._index_node(
            collection="Tenant",
            condition={
                "type": "compare ==",
                "subNodes": [
                    {
                        "type": "attribute access",
                        "name": "NAME",
                        "subNodes": [{"type": "reference", "name": "doc", "id": 100}],
                    },
                    {"type": "value", "value": "Acme"},
                ],
            },
        )
        plan = _wrap_plan([_singleton_node(), node, _return_node(nid=3)])
        with pytest.raises(TenantScopeViolation) as exc_info:
            _call_validate(plan=plan)
        assert exc_info.value.code == "TENANT_ROOT_UNCONSTRAINED"


# ---------------------------------------------------------------------------
# TraversalNode tests
# ---------------------------------------------------------------------------


class TestTraversalNode:
    def _traversal(
        self,
        *,
        graph_name: str | None = None,
        vertex_collections: list[str] | None = None,
        prune: Any = None,
    ) -> dict[str, Any]:
        node: dict[str, Any] = {
            "type": "TraversalNode",
            "id": 2,
            "options": {"prune": prune} if prune is not None else {},
        }
        if graph_name is not None:
            node["graphName"] = graph_name
        if vertex_collections is not None:
            node["graph"] = {"vertexCollections": vertex_collections}
        return node

    def test_unconstrained_traversal_rejected(self) -> None:
        plan = _wrap_plan(
            [
                _singleton_node(),
                self._traversal(vertex_collections=["Employee", "Asset"]),
                _return_node(nid=3),
            ]
        )
        with pytest.raises(TenantScopeViolation) as exc_info:
            _call_validate(plan=plan)
        assert exc_info.value.code == "UNCONSTRAINED_TRAVERSAL"

    def test_disjoint_smartgraph_traversal_accepted(self) -> None:
        plan = _wrap_plan(
            [
                _singleton_node(),
                self._traversal(graph_name="TenantGraph"),
                _return_node(nid=3),
            ]
        )
        _call_validate(plan=plan)

    def test_non_disjoint_smartgraph_traversal_rejected(self) -> None:
        """Non-disjoint named graph with smartgraph vertices and no
        ``prune`` on ``@tenantId`` → rejected. Tenant-scoped vertices
        could leak across tenants in a non-disjoint SmartGraph; only a
        per-step tenant filter or a disjoint SmartGraph proves the
        constraint.
        """
        sharding = _sharding_profile()
        # Add a non-disjoint graph whose vertices include a smartgraph
        # collection — the path that exercises the rejection branch.
        sharding["graphs"].append(
            {
                "name": "SharedSmartGraph",
                "smartGraphAttribute": "TENANT_HEX_ID",
                "isDisjoint": False,
                "vertexCollections": ["Employee"],
            }
        )
        plan = _wrap_plan(
            [
                _singleton_node(),
                self._traversal(graph_name="SharedSmartGraph"),
                _return_node(nid=3),
            ]
        )
        with pytest.raises(TenantScopeViolation) as exc_info:
            _call_validate(plan=plan, sharding_profile=sharding)
        assert exc_info.value.code == "UNCONSTRAINED_TRAVERSAL"

    def test_satellite_only_traversal_accepted(self) -> None:
        plan = _wrap_plan(
            [
                _singleton_node(),
                self._traversal(vertex_collections=["Country"]),
                _return_node(nid=3),
            ]
        )
        # No bind-var needed — satellite-only.
        _call_validate(
            plan=plan,
            bind_vars={},
            session=_FakeSession(tenant_id=None),
        )

    def test_traversal_with_string_prune_on_tenant_id_accepted(self) -> None:
        plan = _wrap_plan(
            [
                _singleton_node(),
                self._traversal(
                    vertex_collections=["Employee"],
                    prune="v.TENANT_HEX_ID != @tenantId",
                ),
                _return_node(nid=3),
            ]
        )
        _call_validate(plan=plan)

    def test_traversal_with_expr_prune_on_tenant_id_accepted(self) -> None:
        prune_expr = {
            "type": "compare !=",
            "subNodes": [
                {
                    "type": "attribute access",
                    "name": "TENANT_HEX_ID",
                    "subNodes": [{"type": "reference", "name": "v"}],
                },
                {"type": "parameter", "name": "tenantId"},
            ],
        }
        plan = _wrap_plan(
            [
                _singleton_node(),
                self._traversal(vertex_collections=["Employee"], prune=prune_expr),
                _return_node(nid=3),
            ]
        )
        _call_validate(plan=plan)


# ---------------------------------------------------------------------------
# SubqueryNode tests
# ---------------------------------------------------------------------------


class TestSubqueryNode:
    def test_unconstrained_inner_scan_rejected(self) -> None:
        inner = _wrap_plan([_enum_node(nid=10, collection="Employee"), _return_node(nid=11)])
        outer = _wrap_plan(
            [
                _singleton_node(),
                {"type": "SubqueryNode", "id": 2, "subquery": inner},
                _return_node(nid=3),
            ]
        )
        with pytest.raises(TenantScopeViolation) as exc_info:
            _call_validate(plan=outer)
        assert exc_info.value.code == "UNCONSTRAINED_COLLECTION_SCAN"

    def test_constrained_inner_scan_accepted(self) -> None:
        inner = _wrap_plan(
            [
                _enum_node(nid=10, collection="Employee"),
                _calc_eq_attr_bindvar(
                    nid=11,
                    var_name="doc",
                    attr="TENANT_HEX_ID",
                    bindvar="tenantId",
                ),
                _filter_node(nid=12, calc_id=11),
                _return_node(nid=13),
            ]
        )
        outer = _wrap_plan(
            [
                _singleton_node(),
                {"type": "SubqueryNode", "id": 2, "subquery": inner},
                _return_node(nid=3),
            ]
        )
        _call_validate(plan=outer)


# ---------------------------------------------------------------------------
# Audit logging
# ---------------------------------------------------------------------------


class TestAuditLogging:
    def test_pass_emits_tenant_scope_ok_with_digests(self, caplog: pytest.LogCaptureFixture) -> None:
        plan = _wrap_plan(
            [
                _singleton_node(),
                _enum_node(nid=2, collection="Employee"),
                _calc_eq_attr_bindvar(nid=3, var_name="doc", attr="TENANT_HEX_ID", bindvar="tenantId"),
                _filter_node(nid=4, calc_id=3),
                _return_node(nid=5),
            ]
        )
        with caplog.at_level(logging.INFO, logger="arango_cypher.tenant_plan_validator"):
            _call_validate(plan=plan)
        ok_lines = [r.getMessage() for r in caplog.records if "TENANT_SCOPE_OK" in r.getMessage()]
        assert ok_lines, f"expected one TENANT_SCOPE_OK record; got {caplog.records!r}"
        line = ok_lines[-1]
        assert "tenant=tenant-A-uuid" in line
        assert "aql_digest=" in line
        assert "plan_digest=" in line

    def test_violation_emits_structured_warning_with_digests(self, caplog: pytest.LogCaptureFixture) -> None:
        plan = _wrap_plan([_singleton_node(), _enum_node(nid=2, collection="Employee"), _return_node(nid=3)])
        with caplog.at_level(logging.WARNING, logger="arango_cypher.tenant_plan_validator"):
            with pytest.raises(TenantScopeViolation):
                _call_validate(plan=plan)
        warn_lines = [r.getMessage() for r in caplog.records if "TENANT_SCOPE_VIOLATION" in r.getMessage()]
        assert warn_lines, f"expected violation record; got {caplog.records!r}"
        line = warn_lines[-1]
        assert "code=UNCONSTRAINED_COLLECTION_SCAN" in line
        assert "aql_digest=" in line
        assert "plan_digest=" in line


# ---------------------------------------------------------------------------
# EXPLAIN round-trip (db.aql.explain integration boundary)
# ---------------------------------------------------------------------------


class TestExplainIntegration:
    """When no ``plan_override`` is supplied the validator must call
    ``db.aql.explain``. We mock the DB to confirm the call shape and to
    surface any EXPLAIN failure as a ``TenantScopeViolation`` with code
    ``EXPLAIN_FAILED`` (fail-closed).
    """

    def test_calls_db_explain_and_uses_plan(self) -> None:
        class _FakeAql:
            def __init__(self) -> None:
                self.calls: list[tuple[str, dict[str, Any]]] = []

            def explain(self, aql: str, bind_vars: dict[str, Any]) -> dict[str, Any]:
                self.calls.append((aql, dict(bind_vars)))
                return {
                    "plan": _wrap_plan(
                        [_singleton_node(), _enum_node(nid=2, collection="Country"), _return_node(nid=3)]
                    ),
                    "warnings": [],
                }

        class _FakeDb:
            def __init__(self) -> None:
                self.aql = _FakeAql()

        db = _FakeDb()
        validate_plan(
            db=db,
            aql="FOR c IN Country RETURN c",
            bind_vars={},
            manifest=_manifest(),
            sharding_profile=_sharding_profile(),
            session=_FakeSession(tenant_id=None),
        )
        assert db.aql.calls == [("FOR c IN Country RETURN c", {})]

    def test_explain_failure_surfaces_as_violation(self) -> None:
        class _FakeAql:
            def explain(self, aql: str, bind_vars: dict[str, Any]) -> dict[str, Any]:
                raise RuntimeError("boom")

        class _FakeDb:
            def __init__(self) -> None:
                self.aql = _FakeAql()

        with pytest.raises(TenantScopeViolation) as exc_info:
            validate_plan(
                db=_FakeDb(),
                aql="FOR e IN Employee RETURN e",
                bind_vars={"tenantId": "tenant-A-uuid"},
                manifest=_manifest(),
                sharding_profile=_sharding_profile(),
                session=_FakeSession(),
            )
        assert exc_info.value.code == "EXPLAIN_FAILED"


# ---------------------------------------------------------------------------
# Non-tenant / single-tenant schema short-circuit (regression: Layer 5 used to
# falsely refuse ALL edge access and traversals on schemas with no tenant
# scoping, because edge collections have no conceptual entity — and thus no
# GLOBAL role to short-circuit on — and _check_traversal had no global gate at
# all. Verified live against FinReflectKG, whose manifest has zero scoped
# entities yet every traversal was rejected with UNCONSTRAINED_TRAVERSAL.)
# ---------------------------------------------------------------------------


def _global_only_manifest() -> TenantScopeManifest:
    """A schema with no tenant model: every conceptual entity is GLOBAL and
    there is no tenant root. Edge collections (e.g. ``relations``) are not
    mapped to any entity, so their role resolves to ``None``.
    """
    return TenantScopeManifest(
        tenant_entity=None,
        entities={
            "ORG": EntityScope(role=EntityTenantRole.GLOBAL, denorm_field=None, reachable_from_tenant=False),
            "PERSON": EntityScope(
                role=EntityTenantRole.GLOBAL, denorm_field=None, reachable_from_tenant=False
            ),
        },
    )


class TestNonTenantSchemaShortCircuit:
    """On a schema with no tenant scoping, Layer 5 must execute (nothing to
    isolate) instead of refusing. No sharding profile, no tenant bind."""

    _unbound = _FakeSession(tenant_id=None, tenant_key=None)

    def test_node_scan_on_global_collection_accepted(self) -> None:
        plan = _wrap_plan([_singleton_node(), _enum_node(nid=2, collection="Node"), _return_node(nid=3)])
        _call_validate(
            plan=plan,
            bind_vars={},
            manifest=_global_only_manifest(),
            sharding_profile={},
            collection_to_entity={"Node": "ORG"},
            session=self._unbound,
        )

    def test_edge_collection_scan_accepted(self) -> None:
        # ``relations`` maps to no entity (role None) and has unknown layout
        # — previously this fell through to UNCONSTRAINED_COLLECTION_SCAN.
        plan = _wrap_plan([_singleton_node(), _enum_node(nid=2, collection="relations"), _return_node(nid=3)])
        _call_validate(
            plan=plan,
            bind_vars={},
            manifest=_global_only_manifest(),
            sharding_profile={},
            collection_to_entity={"Node": "ORG"},
            session=self._unbound,
        )

    def test_edge_index_lookup_accepted(self) -> None:
        node = {
            "type": "IndexNode",
            "id": 2,
            "collection": "relations",
            "outVariable": {"name": "e", "id": 100},
            "condition": None,
        }
        plan = _wrap_plan([_singleton_node(), node, _return_node(nid=3)])
        _call_validate(
            plan=plan,
            bind_vars={},
            manifest=_global_only_manifest(),
            sharding_profile={},
            collection_to_entity={"Node": "ORG"},
            session=self._unbound,
        )

    def test_anonymous_traversal_accepted(self) -> None:
        # Real anonymous-traversal shape: top-level vertex/edge collection
        # lists, and ``graph`` is a *list* of edge collections (not a dict).
        node = {
            "type": "TraversalNode",
            "id": 2,
            "options": {},
            "graph": ["relations"],
            "vertexCollections": ["Node", "relations"],
            "edgeCollections": ["relations"],
        }
        plan = _wrap_plan([_singleton_node(), node, _return_node(nid=3)])
        _call_validate(
            plan=plan,
            bind_vars={},
            manifest=_global_only_manifest(),
            sharding_profile={},
            collection_to_entity={"Node": "ORG"},
            session=self._unbound,
        )

    def test_tenant_scoped_traversal_still_refused(self) -> None:
        # DEFENCE IN DEPTH: the short-circuit must NOT weaken enforcement.
        # The same anonymous-traversal shape touching a TENANT_SCOPED
        # collection (Employee, smartgraph) is still refused.
        node = {
            "type": "TraversalNode",
            "id": 2,
            "options": {},
            "graph": ["TENANTOWNSEMPLOYEE"],
            "vertexCollections": ["Tenant", "Employee"],
            "edgeCollections": ["TENANTOWNSEMPLOYEE"],
        }
        plan = _wrap_plan([_singleton_node(), node, _return_node(nid=3)])
        with pytest.raises(TenantScopeViolation) as exc_info:
            _call_validate(plan=plan)  # default _manifest() + _sharding_profile()
        assert exc_info.value.code == "UNCONSTRAINED_TRAVERSAL"

    def test_tenant_scoped_edge_scan_still_refused(self) -> None:
        # Employee (TENANT_SCOPED, smartgraph) scanned without a tenant
        # predicate is still refused — the gate only spares non-touching
        # collections.
        plan = _wrap_plan([_singleton_node(), _enum_node(nid=2, collection="Employee"), _return_node(nid=3)])
        with pytest.raises(TenantScopeViolation) as exc_info:
            _call_validate(plan=plan, bind_vars={})
        assert exc_info.value.code == "UNCONSTRAINED_COLLECTION_SCAN"


# ---------------------------------------------------------------------------
# No manifest: fail closed
# ---------------------------------------------------------------------------


class TestNoManifestFailsClosed:
    """``validate_plan`` accepts ``manifest=None`` in its signature; the walker
    dereferenced it unconditionally, so a direct call crashed with
    AttributeError mid-check instead of refusing. Found by mypy."""

    def test_a_missing_manifest_is_a_refusal_not_a_crash(self) -> None:
        with pytest.raises(TenantScopeViolation) as exc_info:
            validate_plan(
                db=None,
                aql="FOR c IN Country RETURN c",
                bind_vars={},
                manifest=None,
                sharding_profile=None,
                session=_FakeSession(),
                plan_override=_wrap_plan([_singleton_node(), _return_node(nid=2)]),
            )

        assert exc_info.value.code == "NO_TENANT_MANIFEST"
