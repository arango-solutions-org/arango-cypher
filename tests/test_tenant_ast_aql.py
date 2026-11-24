"""Layer 4 / Wave 8a MT-4 — AQL AST tenant-injection rewriter.

The rewriter (``arango_cypher.tenant_ast_aql.inject_tenant_scope``) is
the only defence against tenant-data leaks on the NL→AQL direct path
and on raw ``/execute-aql`` submissions. Every test here pins the
contract Layer 5 (``tenant_plan_validator``) depends on:

* every tenant-scoped ``EnumerateCollectionNode`` / ``IndexNode`` ends
  up with ``FILTER <var>.<tenant_field> == @tenantId`` (idempotent),
* every ``TraversalNode`` over a non-disjoint graph ends up with
  ``OPTIONS { prune: ... }`` referencing ``@tenantId``,
* satellite-only / GLOBAL reads are *unchanged* — Layer 4 must not
  refuse legitimate reference-data queries,
* the ``bind_vars`` returned by the rewriter never carry a literal
  tenant value,
* ``inject(inject(aql)) == inject(aql)`` byte-for-byte,
* the `changes` list reads like an audit log entry the UI surfaces.

Every test mocks ``db.aql.explain`` via the ``plan_override`` kwarg so
the suite runs offline. The plan-shape fixtures mirror the ones in
``tests/test_tenant_plan_validator.py`` so a contract drift between
Layers 4 and 5 surfaces as a test failure in both files at once.
"""

from __future__ import annotations

from typing import Any

import pytest

from arango_cypher.nl2cypher.tenant_scope import (
    EntityScope,
    EntityTenantRole,
    TenantScopeManifest,
)
from arango_cypher.tenant_ast_aql import (
    AqlRewriteError,
    inject_tenant_scope,
)
from arango_cypher.tenant_plan_validator import (
    TenantScopeViolation,
    validate_plan,
)

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


class _FakeSession:
    """Stand-in for ``arango_cypher.service.security._Session``.

    Layer 4 itself doesn't read the session, but the Layer 5
    round-trip test ``test_round_trip_through_layer_5_validator``
    calls the validator, which does.
    """

    def __init__(
        self,
        *,
        token: str = "session-A-TOKEN",
        tenant_id: str | None = "tenant-A-uuid",
        tenant_key: str | None = "tenant-A-uuid",
        is_admin: bool = False,
    ) -> None:
        self.token = token
        self.tenant_id = tenant_id
        self.tenant_key = tenant_key
        self.is_admin = is_admin


def _manifest() -> TenantScopeManifest:
    """Standard four-entity manifest:

    * ``Tenant`` (TENANT_ROOT)
    * ``Employee`` (TENANT_SCOPED with denorm field TENANT_HEX_ID)
    * ``Asset`` (TENANT_SCOPED, traversal-only — has scoping_path)
    * ``Country`` (GLOBAL — the satellite reference data).
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
    """Disjoint-SmartGraph profile used by every test that needs one.

    Mirrors the fixture in ``test_tenant_plan_validator.py`` so a
    Layer 4 + Layer 5 round-trip can be exercised by re-using the
    same sharding profile on both ends.
    """
    return {
        "style": "DisjointSmartGraph",
        "members": {
            "Tenant": {"kind": "tenant-root"},
            "Employee": {"kind": "smartgraph"},
            "Asset": {"kind": "smartgraph"},
            "Product": {"kind": "satellite"},
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
                "name": "AssetRefGraph",
                "smartGraphAttribute": "_key",
                "isDisjoint": False,
                "vertexCollections": ["Asset", "Product"],
                "edgeCollections": ["MENTIONS"],
            },
            {
                "name": "CountryGraph",
                "smartGraphAttribute": "_key",
                "isDisjoint": False,
                "vertexCollections": ["Country"],
            },
        ],
    }


# ---------- plan-node builders (mirror Layer 5's test fixtures) -------------


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


def _index_node(
    *,
    nid: int,
    collection: str,
    outvar: str = "doc",
    condition: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "type": "IndexNode",
        "id": nid,
        "collection": collection,
        "outVariable": {"name": outvar, "id": 100},
        "condition": condition,
    }


def _calc_eq_attr_bindvar(
    *,
    nid: int,
    var_name: str,
    attr: str,
    bindvar: str = "tenantId",
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


def _traversal_node(
    *,
    nid: int,
    vertex_outvar: str = "v",
    edge_outvar: str | None = "e",
    path_outvar: str | None = None,
    graph_name: str | None = None,
    vertex_collections: list[str] | None = None,
    prune: Any = None,
) -> dict[str, Any]:
    node: dict[str, Any] = {
        "type": "TraversalNode",
        "id": nid,
        "vertexOutVariable": {"name": vertex_outvar, "id": 110},
        "options": {"prune": prune} if prune is not None else {},
    }
    if edge_outvar is not None:
        node["edgeOutVariable"] = {"name": edge_outvar, "id": 111}
    if path_outvar is not None:
        node["pathOutVariable"] = {"name": path_outvar, "id": 112}
    if graph_name is not None:
        node["graphName"] = graph_name
    if vertex_collections is not None:
        node["graph"] = {"vertexCollections": vertex_collections}
    return node


def _subquery_node(
    *,
    nid: int,
    inner_nodes: list[dict[str, Any]],
    outvar: str = "sub",
) -> dict[str, Any]:
    return {
        "type": "SubqueryNode",
        "id": nid,
        "outVariable": {"name": outvar, "id": 300},
        "subquery": {"nodes": inner_nodes},
    }


def _wrap_plan(nodes: list[dict[str, Any]]) -> dict[str, Any]:
    return {"nodes": nodes}


def _inject(
    *,
    aql: str,
    plan: dict[str, Any],
    bind_vars: dict[str, Any] | None = None,
    manifest: TenantScopeManifest | None = None,
    sharding_profile: dict[str, Any] | None = None,
    collection_to_entity: dict[str, str] | None = None,
    tenant_id: str = "tenant-A-uuid",
    tenant_key: str = "tenant-A-uuid",
) -> tuple[str, dict[str, Any], list[str]]:
    return inject_tenant_scope(
        db=None,
        aql=aql,
        bind_vars=bind_vars if bind_vars is not None else {"tenantId": tenant_id},
        manifest=manifest if manifest is not None else _manifest(),
        sharding_profile=sharding_profile if sharding_profile is not None else _sharding_profile(),
        tenant_id=tenant_id,
        tenant_key=tenant_key,
        plan_override=plan,
        collection_to_entity=collection_to_entity,
    )


# ---------------------------------------------------------------------------
# 1. Satellite / GLOBAL passes through unchanged
# ---------------------------------------------------------------------------


class TestSatelliteAndGlobal:
    def test_satellite_only_no_change(self) -> None:
        aql = "FOR c IN Country RETURN c"
        plan = _wrap_plan(
            [_singleton_node(), _enum_node(nid=2, collection="Country", outvar="c"), _return_node(nid=3)]
        )
        out, bind_vars, changes = _inject(
            aql=aql,
            plan=plan,
            bind_vars={"tenantId": "tenant-A-uuid"},
        )
        assert out == aql, "satellite read should not be rewritten"
        assert changes == []
        # Bind vars unchanged (defensive copy is OK).
        assert bind_vars == {"tenantId": "tenant-A-uuid"}

    def test_global_entity_no_change(self) -> None:
        """The manifest classifies Country as GLOBAL even though the
        sharding profile says ``satellite``. Verify both signals
        independently lead to no rewrite.
        """
        aql = "FOR c IN Country RETURN c"
        plan = _wrap_plan(
            [_singleton_node(), _enum_node(nid=2, collection="Country", outvar="c"), _return_node(nid=3)]
        )
        # Pass an empty sharding profile so the GLOBAL signal must
        # come from the manifest alone.
        out, _, changes = _inject(
            aql=aql,
            plan=plan,
            sharding_profile={},
        )
        assert out == aql
        assert changes == []


# ---------------------------------------------------------------------------
# 2. Tenant-scoped collection: FILTER injection
# ---------------------------------------------------------------------------


class TestTenantScopedFilterInjection:
    def test_tenant_scoped_collection_filter_injected(self) -> None:
        """Canonical case: ``FOR e IN Employee RETURN e`` →
        ``FOR e IN Employee\n  FILTER e.TENANT_HEX_ID == @tenantId RETURN e``.
        """
        aql = "FOR e IN Employee RETURN e"
        plan = _wrap_plan(
            [_singleton_node(), _enum_node(nid=2, collection="Employee", outvar="e"), _return_node(nid=3)]
        )
        out, bind_vars, changes = _inject(aql=aql, plan=plan)
        assert "FILTER e.TENANT_HEX_ID == @tenantId" in out
        # Original FOR survives unchanged in shape.
        assert "FOR e IN Employee" in out
        assert len(changes) == 1
        assert "FILTER e.TENANT_HEX_ID == @tenantId" in changes[0]
        # Bind vars never gain a literal tenant value.
        assert bind_vars == {"tenantId": "tenant-A-uuid"}

    def test_filter_inserted_immediately_after_for_clause(self) -> None:
        """The FILTER should sit between the FOR header and the RETURN
        so the predicate runs before the body — required for prune
        equivalence in nested patterns."""
        aql = "FOR e IN Employee RETURN e"
        plan = _wrap_plan(
            [_singleton_node(), _enum_node(nid=2, collection="Employee", outvar="e"), _return_node(nid=3)]
        )
        out, _, _ = _inject(aql=aql, plan=plan)
        # Order: FOR ... IN Employee, FILTER, RETURN
        for_idx = out.index("FOR e IN Employee")
        filter_idx = out.index("FILTER e.TENANT_HEX_ID == @tenantId")
        return_idx = out.index("RETURN e")
        assert for_idx < filter_idx < return_idx

    def test_existing_filter_kept_no_dup(self) -> None:
        """Idempotency at the single-plan level: when the plan already
        carries a CalculationNode binding ``e.TENANT_HEX_ID`` to
        ``@tenantId``, the rewriter must NOT add a second FILTER.
        """
        aql = "FOR e IN Employee FILTER e.TENANT_HEX_ID == @tenantId RETURN e"
        plan = _wrap_plan(
            [
                _singleton_node(),
                _enum_node(nid=2, collection="Employee", outvar="e"),
                _calc_eq_attr_bindvar(nid=3, var_name="e", attr="TENANT_HEX_ID"),
                _filter_node(nid=4, calc_id=3),
                _return_node(nid=5),
            ]
        )
        out, _, changes = _inject(aql=aql, plan=plan)
        assert out == aql
        assert changes == []

    def test_backtick_quoted_collection_name(self) -> None:
        """ArangoDB allows backtick-quoting reserved-word collection
        names. The rewriter must still locate the FOR site."""
        aql = "FOR e IN `Employee` RETURN e"
        plan = _wrap_plan(
            [_singleton_node(), _enum_node(nid=2, collection="Employee", outvar="e"), _return_node(nid=3)]
        )
        out, _, changes = _inject(aql=aql, plan=plan)
        assert "FILTER e.TENANT_HEX_ID == @tenantId" in out
        assert "FOR e IN `Employee`" in out
        assert changes


class TestUnconstrainable:
    def test_collection_lacking_both_attrs_rejected(self) -> None:
        """An entity classified as TENANT_SCOPED but with neither a
        denorm_field nor a scoping_path (and no smart-graph attribute
        on its collection) is unscopable — refuse.
        """
        manifest = TenantScopeManifest(
            tenant_entity="Tenant",
            entities={
                "Tenant": EntityScope(role=EntityTenantRole.TENANT_ROOT),
                "Phantom": EntityScope(
                    role=EntityTenantRole.TENANT_SCOPED,
                    denorm_field=None,
                    reachable_from_tenant=False,
                    scoping_path=None,
                ),
            },
        )
        # Sharding profile that doesn't carry a smart-graph attribute
        # for the Phantom collection either.
        profile = {
            "style": "Sharded",
            "members": {"Phantom": {"kind": "regular"}},
            "graphs": [],
        }
        aql = "FOR p IN Phantom RETURN p"
        plan = _wrap_plan(
            [_singleton_node(), _enum_node(nid=2, collection="Phantom", outvar="p"), _return_node(nid=3)]
        )
        with pytest.raises(AqlRewriteError) as exc:
            _inject(
                aql=aql,
                plan=plan,
                manifest=manifest,
                sharding_profile=profile,
            )
        assert exc.value.code == "UNCONSTRAINED_COLLECTION_ACCESS"

    def test_unmapped_collection_rejected(self) -> None:
        """A collection that isn't in the manifest at all triggers
        ``UNCONSTRAINED_COLLECTION_ACCESS`` (via the shared
        ``predicate_for_collection`` → ``UnknownEntityScope`` path)."""
        aql = "FOR u IN UnknownColl RETURN u"
        plan = _wrap_plan(
            [_singleton_node(), _enum_node(nid=2, collection="UnknownColl", outvar="u"), _return_node(nid=3)]
        )
        # No sharding-profile entry either — the unknown collection
        # gets layout_kind=None, so we fall through to the manifest
        # lookup which raises UnknownEntityScope.
        with pytest.raises(AqlRewriteError) as exc:
            _inject(aql=aql, plan=plan)
        assert exc.value.code == "UNCONSTRAINED_COLLECTION_ACCESS"


# ---------------------------------------------------------------------------
# 3. Traversal rewrites
# ---------------------------------------------------------------------------


class TestTraversal:
    def test_traversal_outbound_disjoint_no_prune_no_rewrite_needed(self) -> None:
        """Storage-enforced safety on a disjoint smartgraph means
        Layer 4 should leave the traversal unchanged — Layer 5 already
        accepts it without a prune."""
        aql = "FOR t IN Tenant FILTER t._key == @tenantKey FOR v, e IN 1..3 OUTBOUND t TenantGraph RETURN v"
        plan = _wrap_plan(
            [
                _singleton_node(),
                _index_node(
                    nid=2,
                    collection="Tenant",
                    outvar="t",
                    condition={
                        "type": "compare ==",
                        "subNodes": [
                            {
                                "type": "attribute access",
                                "name": "_key",
                                "subNodes": [{"type": "reference", "name": "t", "id": 100}],
                            },
                            {"type": "parameter", "name": "tenantKey"},
                        ],
                    },
                ),
                _traversal_node(
                    nid=3,
                    vertex_outvar="v",
                    edge_outvar="e",
                    graph_name="TenantGraph",
                ),
                _return_node(nid=4),
            ]
        )
        out, _, changes = _inject(
            aql=aql,
            plan=plan,
            bind_vars={"tenantId": "tenant-A-uuid", "tenantKey": "tenant-A-uuid"},
        )
        assert out == aql
        assert changes == []

    def _asset_traversal(self, aql: str, **node: Any) -> str:
        plan = _wrap_plan(
            [
                _singleton_node(),
                {
                    **_traversal_node(nid=3, vertex_outvar="v", edge_outvar=None, graph_name="AssetRefGraph"),
                    **node,
                },
                _return_node(nid=4),
            ]
        )
        out, _, changes = _inject(aql=aql, plan=plan)
        if out != aql:
            assert any("PRUNE" in c and "FILTER" in c for c in changes)
        return out

    def test_traversal_any_with_satellite_prune_added(self) -> None:
        """Traversal over a non-disjoint graph whose vertex collections
        include a TENANT_SCOPED entity (Asset): it stops at, and does not
        yield, a vertex of another tenant."""
        aql = "FOR p IN Product FILTER p._key == @prodKey FOR v, e IN 1..2 ANY p AssetRefGraph RETURN v"
        plan = _wrap_plan(
            [
                _singleton_node(),
                _index_node(nid=2, collection="Product", outvar="p", condition=None),
                _traversal_node(nid=3, vertex_outvar="v", edge_outvar="e", graph_name="AssetRefGraph"),
                _return_node(nid=4),
            ]
        )
        out, _, changes = _inject(
            aql=aql, plan=plan, bind_vars={"tenantId": "tenant-A-uuid", "prodKey": "p1"}
        )
        # AssetRefGraph contains the satellite Product collection too; the
        # field must be the TENANT_SCOPED member's (Asset → TENANT_HEX_ID).
        assert out == (
            "FOR p IN Product FILTER p._key == @prodKey FOR v, e IN 1..2 ANY p AssetRefGraph \n"
            "  PRUNE v.TENANT_HEX_ID != @tenantId\n"
            "  FILTER v.TENANT_HEX_ID == @tenantId RETURN v"
        )
        assert changes == [
            "Added PRUNE v.TENANT_HEX_ID != @tenantId and FILTER v.TENANT_HEX_ID == @tenantId to traversal over v"
        ]

    def test_an_existing_prune_is_kept_beside_the_tenant_boundary(self) -> None:
        out = self._asset_traversal(
            'FOR p IN Product FOR v IN 1..2 ANY p AssetRefGraph PRUNE v.name == "x" RETURN v'
        )
        assert out == (
            "FOR p IN Product FOR v IN 1..2 ANY p AssetRefGraph "
            'PRUNE v.TENANT_HEX_ID != @tenantId OR ( v.name == "x") \n'
            "  FILTER v.TENANT_HEX_ID == @tenantId RETURN v"
        )

    def test_the_prune_goes_before_options_and_the_filter_after(self) -> None:
        out = self._asset_traversal(
            'FOR p IN Product FOR v IN 1..2 ANY p AssetRefGraph PRUNE v.n == ")" OPTIONS {order: "bfs"} RETURN v'
        )
        assert out == (
            "FOR p IN Product FOR v IN 1..2 ANY p AssetRefGraph "
            'PRUNE v.TENANT_HEX_ID != @tenantId OR ( v.n == ")") OPTIONS {order: "bfs"}\n'
            "  FILTER v.TENANT_HEX_ID == @tenantId RETURN v"
        )
        out = self._asset_traversal(
            'FOR p IN Product FOR v IN 1..2 ANY p AssetRefGraph OPTIONS {order: "bfs"} RETURN v'
        )
        assert out == (
            "FOR p IN Product FOR v IN 1..2 ANY p AssetRefGraph \n"
            '  PRUNE v.TENANT_HEX_ID != @tenantId OPTIONS {order: "bfs"}\n'
            "  FILTER v.TENANT_HEX_ID == @tenantId RETURN v"
        )

    def test_traversal_already_bounded_by_the_tenant_no_dup(self) -> None:
        """A second pass sees the PRUNE and the fused FILTER, with the
        tenant resolved as its own EXPLAIN resolves it, and changes nothing."""

        def field(op: str) -> dict[str, Any]:
            return {
                "type": op,
                "subNodes": [
                    {
                        "type": "attribute access",
                        "name": "TENANT_HEX_ID",
                        "subNodes": [{"type": "reference", "name": "v", "id": 110}],
                    },
                    {"type": "value", "value": "tenant-A-uuid"},
                ],
            }

        aql = (
            "FOR p IN Product FOR v IN 1..2 ANY p AssetRefGraph \n  PRUNE v.TENANT_HEX_ID != @tenantId"
            "\n  FILTER v.TENANT_HEX_ID == @tenantId RETURN v"
        )
        out = self._asset_traversal(
            aql,
            expression=field("compare !="),
            condition={
                "type": "n-ary or",
                "subNodes": [{"type": "n-ary and", "subNodes": [field("compare ==")]}],
            },
        )
        assert out == aql

    def test_a_prune_that_assigns_a_variable_is_refused(self) -> None:
        with pytest.raises(AqlRewriteError) as exc:
            self._asset_traversal(
                "FOR p IN Product FOR v IN 1..2 ANY p AssetRefGraph PRUNE c = v.n == 1 RETURN v"
            )
        assert exc.value.code == "UNCONSTRAINED_TRAVERSAL"

    def test_traversal_all_satellite_no_rewrite(self) -> None:
        """Pure satellite traversal needs no prune — Layer 5 accepts
        satellite-only traversals."""
        aql = "FOR p IN Product FILTER p._key == @prodKey FOR v IN 1..1 ANY p CountryGraph RETURN v"
        plan = _wrap_plan(
            [
                _singleton_node(),
                _index_node(nid=2, collection="Product", outvar="p", condition=None),
                _traversal_node(
                    nid=3,
                    vertex_outvar="v",
                    edge_outvar=None,
                    graph_name="CountryGraph",
                    vertex_collections=["Country"],
                ),
                _return_node(nid=4),
            ]
        )
        out, _, changes = _inject(
            aql=aql,
            plan=plan,
            bind_vars={"tenantId": "tenant-A-uuid", "prodKey": "p1"},
        )
        # Satellite traversal: untouched. Product is satellite too, so
        # the upstream IndexNode also pass-through.
        assert "OPTIONS { prune:" not in out
        assert changes == []


# ---------------------------------------------------------------------------
# 4. Subquery recursion
# ---------------------------------------------------------------------------


class TestSubquery:
    def test_subquery_recursion(self) -> None:
        """A LET-subquery scanning Employee → inner FOR is rewritten."""
        aql = "LET emps = (FOR e IN Employee RETURN e) RETURN LENGTH(emps)"
        inner = [
            _enum_node(nid=10, collection="Employee", outvar="e"),
            _return_node(nid=11),
        ]
        plan = _wrap_plan(
            [
                _singleton_node(),
                _subquery_node(nid=2, inner_nodes=inner, outvar="emps"),
                _return_node(nid=3),
            ]
        )
        out, _, changes = _inject(aql=aql, plan=plan)
        assert "FILTER e.TENANT_HEX_ID == @tenantId" in out
        # The inner FOR was inside the subquery body; the outer
        # `LET emps = (...)` wrapping is preserved.
        assert "LET emps = (FOR e IN Employee" in out
        assert any("FOR e IN Employee" in c for c in changes)

    def test_count_subquery_rewritten(self) -> None:
        """``RETURN COUNT(FOR e IN Employee RETURN 1)``-style — the
        inner FOR over Employee still gets a FILTER even though the
        outer context is a function call, because the plan represents
        it as a SubqueryNode + inner EnumerateCollectionNode."""
        aql = "RETURN COUNT(FOR e IN Employee RETURN 1)"
        inner = [
            _enum_node(nid=10, collection="Employee", outvar="e"),
            _return_node(nid=11),
        ]
        plan = _wrap_plan(
            [
                _singleton_node(),
                _subquery_node(nid=2, inner_nodes=inner, outvar="cnt"),
                _return_node(nid=3),
            ]
        )
        out, _, changes = _inject(aql=aql, plan=plan)
        assert "FILTER e.TENANT_HEX_ID == @tenantId" in out
        assert changes


# ---------------------------------------------------------------------------
# 5. Function calls referencing a collection
# ---------------------------------------------------------------------------


class TestFunctionCallCollectionArg:
    def test_function_call_collection_arg(self) -> None:
        """``LENGTH(Employee)`` parses to a subquery in ArangoDB
        EXPLAIN; the inner FOR e IN Employee receives the FILTER."""
        # ArangoDB rewrites the textual `LENGTH(Employee)` into
        # something like `LENGTH(FOR e IN Employee RETURN 1)` at parse
        # time, which the plan exposes as a SubqueryNode. We mirror
        # that desugaring in the fixture: the AQL source we ship to
        # the rewriter is the already-desugared form (which is what
        # the transpiler emits and what raw `/execute-aql` callers
        # see after EXPLAIN), and the plan shows the same.
        aql = "RETURN LENGTH(FOR e IN Employee RETURN 1)"
        inner = [
            _enum_node(nid=10, collection="Employee", outvar="e"),
            _return_node(nid=11),
        ]
        plan = _wrap_plan(
            [
                _singleton_node(),
                _subquery_node(nid=2, inner_nodes=inner, outvar="n"),
                _return_node(nid=3),
            ]
        )
        out, _, changes = _inject(aql=aql, plan=plan)
        assert "FILTER e.TENANT_HEX_ID == @tenantId" in out
        assert changes


# ---------------------------------------------------------------------------
# 6. Idempotency / bind-var hygiene
# ---------------------------------------------------------------------------


class TestIdempotenceAndBindVars:
    def test_idempotent_double_pass(self) -> None:
        """``inject(inject(aql)) == inject(aql)`` byte-for-byte.

        First pass plan: bare ``FOR e IN Employee``. Second pass plan:
        same FOR but with a CalculationNode now binding e.TENANT_HEX_ID
        to @tenantId (representing the post-rewrite plan).
        """
        aql_v0 = "FOR e IN Employee RETURN e"
        plan_v0 = _wrap_plan(
            [_singleton_node(), _enum_node(nid=2, collection="Employee", outvar="e"), _return_node(nid=3)]
        )
        aql_v1, _, _ = _inject(aql=aql_v0, plan=plan_v0)
        plan_v1 = _wrap_plan(
            [
                _singleton_node(),
                _enum_node(nid=2, collection="Employee", outvar="e"),
                _calc_eq_attr_bindvar(nid=3, var_name="e", attr="TENANT_HEX_ID"),
                _filter_node(nid=4, calc_id=3),
                _return_node(nid=5),
            ]
        )
        aql_v2, _, changes_v2 = _inject(aql=aql_v1, plan=plan_v1)
        assert aql_v2 == aql_v1
        assert changes_v2 == []

    def test_no_literal_tenant_in_bind_vars(self) -> None:
        """The augmented bind_vars must not gain a literal tenant
        value — the only legitimate carrier for a tenant identity in
        the request is the session-bound ``@tenantId`` / ``@tenantKey``
        bind that the Layer 6 spread already wrote.
        """
        aql = "FOR e IN Employee RETURN e"
        plan = _wrap_plan(
            [_singleton_node(), _enum_node(nid=2, collection="Employee", outvar="e"), _return_node(nid=3)]
        )
        manifest = _manifest()
        # Pin the known-tenant-keys snapshot for the assertion below.
        manifest = TenantScopeManifest(
            tenant_entity=manifest.tenant_entity,
            entities=manifest.entities,
            known_tenant_keys=frozenset({"tenant-A-uuid", "tenant-B-uuid"}),
        )
        _, bind_vars, _ = _inject(
            aql=aql,
            plan=plan,
            manifest=manifest,
            bind_vars={"tenantId": "tenant-A-uuid"},
        )
        leaked = {
            k: v
            for k, v in bind_vars.items()
            if isinstance(v, str)
            and v in manifest.known_tenant_keys
            and k not in {"tenantId", "tenantKey"}  # legitimate carriers
        }
        assert leaked == {}, f"literal tenant value leaked into bind_vars: {leaked!r}"

    def test_changes_list_human_readable(self) -> None:
        """The changes list is the UI's annotation-strip source. Each
        entry must be a single human-readable sentence that names the
        site + the predicate."""
        aql = "FOR e IN Employee RETURN e"
        plan = _wrap_plan(
            [_singleton_node(), _enum_node(nid=2, collection="Employee", outvar="e"), _return_node(nid=3)]
        )
        _, _, changes = _inject(aql=aql, plan=plan)
        assert len(changes) == 1
        msg = changes[0]
        # Names the predicate text.
        assert "FILTER e.TENANT_HEX_ID == @tenantId" in msg
        # Names the site.
        assert "FOR e IN Employee" in msg
        # Reads as a sentence — starts with a verb-phrase.
        assert msg.startswith("Added "), msg


# ---------------------------------------------------------------------------
# 7. §7.3 canonical PRD example + Layer-5 contract round-trip
# ---------------------------------------------------------------------------


class TestPrdSectionSevenExample:
    """PRD §7.3 canonical example: list assets mentioning a product.

    Source AQL (transpiler output) reads Product (satellite), follows
    the MENTIONS edge, and scans Asset (TENANT_SCOPED). Layer 4 must
    inject ``FILTER a.TENANT_HEX_ID == @tenantId`` on the Asset scan
    and leave Product / MENTIONS untouched.
    """

    def _aql(self) -> str:
        return (
            "FOR p IN Product\n"
            "  FILTER p._key == @productKey\n"
            "  FOR e IN MENTIONS\n"
            "    FILTER e._to == p._id\n"
            "    FOR a IN Asset\n"
            "      FILTER a._id == e._from\n"
            "      RETURN a"
        )

    def _manifest(self) -> TenantScopeManifest:
        return TenantScopeManifest(
            tenant_entity="Tenant",
            entities={
                "Tenant": EntityScope(role=EntityTenantRole.TENANT_ROOT),
                "Asset": EntityScope(
                    role=EntityTenantRole.TENANT_SCOPED,
                    denorm_field="TENANT_HEX_ID",
                    reachable_from_tenant=True,
                ),
                "Product": EntityScope(role=EntityTenantRole.GLOBAL),
                "MENTIONS": EntityScope(role=EntityTenantRole.GLOBAL),
            },
        )

    def _profile(self) -> dict[str, Any]:
        return {
            "style": "DisjointSmartGraph",
            "members": {
                "Asset": {"kind": "smartgraph"},
                "Product": {"kind": "satellite"},
                "MENTIONS": {"kind": "satellite"},
            },
            "graphs": [
                {
                    "name": "TenantGraph",
                    "smartGraphAttribute": "TENANT_HEX_ID",
                    "isDisjoint": True,
                    "vertexCollections": ["Asset"],
                    "edgeCollections": [],
                }
            ],
        }

    def _plan(self) -> dict[str, Any]:
        return _wrap_plan(
            [
                _singleton_node(),
                _enum_node(nid=2, collection="Product", outvar="p"),
                _enum_node(nid=3, collection="MENTIONS", outvar="e"),
                _enum_node(nid=4, collection="Asset", outvar="a"),
                _return_node(nid=5),
            ]
        )

    def test_prd_example_asset_filter_injected(self) -> None:
        out, _, changes = _inject(
            aql=self._aql(),
            plan=self._plan(),
            manifest=self._manifest(),
            sharding_profile=self._profile(),
            bind_vars={"tenantId": "tenant-A-uuid", "productKey": "p-1"},
        )
        # Asset gets a FILTER, Product / MENTIONS don't.
        assert "FILTER a.TENANT_HEX_ID == @tenantId" in out
        assert "FILTER p.TENANT_HEX_ID == @tenantId" not in out
        assert "FILTER e.TENANT_HEX_ID == @tenantId" not in out
        assert len(changes) == 1
        assert "Asset" in changes[0]


# ---------------------------------------------------------------------------
# 8. Round-trip with Layer 5
#
# This is the *contract* test: after Layer 4 has rewritten the AQL, a
# Layer 5 validate_plan call on the rewritten plan must accept it.
# Cross-layer drift surfaces here.
# ---------------------------------------------------------------------------


class TestRoundTripLayer5:
    def test_round_trip_through_layer_5_validator(self) -> None:
        """Layer 4 rewrites → Layer 5 accepts."""
        aql = "FOR e IN Employee RETURN e"
        plan_pre = _wrap_plan(
            [_singleton_node(), _enum_node(nid=2, collection="Employee", outvar="e"), _return_node(nid=3)]
        )
        # Layer 4 wouldn't accept this plan as already-constrained;
        # invoke the rewriter to observe the change list and confirm
        # the textual rewrite happens.
        rewritten, bind_vars, changes = _inject(
            aql=aql,
            plan=plan_pre,
            bind_vars={"tenantId": "tenant-A-uuid"},
        )
        assert changes, "Layer 4 must rewrite the canonical case"

        # Now feed Layer 5 the *post-rewrite* plan shape. We mock the
        # post-rewrite plan: the same EnumerateCollectionNode plus a
        # CalculationNode binding e.TENANT_HEX_ID to @tenantId and a
        # FilterNode. This is what ArangoDB EXPLAIN would emit for
        # the rewritten AQL.
        plan_post = _wrap_plan(
            [
                _singleton_node(),
                _enum_node(nid=2, collection="Employee", outvar="e"),
                _calc_eq_attr_bindvar(nid=3, var_name="e", attr="TENANT_HEX_ID"),
                _filter_node(nid=4, calc_id=3),
                _return_node(nid=5),
            ]
        )
        # Layer 5 should accept this plan with no exception.
        validate_plan(
            db=None,
            aql=rewritten,
            bind_vars=bind_vars,
            manifest=_manifest(),
            sharding_profile=_sharding_profile(),
            collection_to_entity={"Employee": "Employee"},
            session=_FakeSession(),
            plan_override=plan_post,
        )

    def test_pre_rewrite_aql_refused_by_layer_5(self) -> None:
        """The plan Layer 5 sees *before* Layer 4 rewrites must be
        refused. This is the corollary contract — without Layer 4,
        the query would leak."""
        aql_pre = "FOR e IN Employee RETURN e"
        plan_pre = _wrap_plan(
            [_singleton_node(), _enum_node(nid=2, collection="Employee", outvar="e"), _return_node(nid=3)]
        )
        with pytest.raises(TenantScopeViolation) as exc_info:
            validate_plan(
                db=None,
                aql=aql_pre,
                bind_vars={"tenantId": "tenant-A-uuid"},
                manifest=_manifest(),
                sharding_profile=_sharding_profile(),
                collection_to_entity={"Employee": "Employee"},
                session=_FakeSession(),
                plan_override=plan_pre,
            )
        assert exc_info.value.code == "UNCONSTRAINED_COLLECTION_SCAN"


# ---------------------------------------------------------------------------
# 9. Admin / workbench / single-tenant fall-through
# ---------------------------------------------------------------------------


class TestAdminWorkbenchPassThrough:
    def test_admin_in_workbench_mode_no_op(self) -> None:
        """Workbench-mode + admin path: the route would call the
        rewriter with an empty manifest (no tenant entity discovered).
        The rewriter must short-circuit to a no-op without raising.

        This mirrors the integration-level behaviour the route layer
        enforces: when the session is admin AND workbench mode is
        enabled, the manifest acquisition step yields a single-tenant
        / no-tenant manifest, so Layer 4 has no work to do.
        """
        manifest = TenantScopeManifest(tenant_entity=None, entities={})
        aql = "FOR e IN Employee RETURN e"
        plan = _wrap_plan(
            [_singleton_node(), _enum_node(nid=2, collection="Employee", outvar="e"), _return_node(nid=3)]
        )
        out, bind_vars, changes = _inject(
            aql=aql,
            plan=plan,
            manifest=manifest,
            bind_vars={},  # no tenant bind — workbench
        )
        assert out == aql
        assert changes == []
        assert bind_vars == {}

    def test_single_tenant_deployment_no_op(self) -> None:
        """A deployment with no Tenant entity at all (single-tenant
        schema) is structurally indistinguishable from admin/workbench
        from the rewriter's perspective — pass through unchanged.
        """
        manifest = TenantScopeManifest(
            tenant_entity=None,
            entities={
                "Employee": EntityScope(role=EntityTenantRole.GLOBAL),
            },
        )
        aql = "FOR e IN Employee RETURN e"
        plan = _wrap_plan(
            [_singleton_node(), _enum_node(nid=2, collection="Employee", outvar="e"), _return_node(nid=3)]
        )
        out, _, changes = _inject(aql=aql, plan=plan, manifest=manifest)
        assert out == aql
        assert changes == []


# ---------------------------------------------------------------------------
# 10. IndexNode + condition-already-covers branch
# ---------------------------------------------------------------------------


class TestIndexNode:
    def test_indexnode_already_covering_tenant_no_dup(self) -> None:
        """An IndexNode whose ``condition`` already binds
        ``e.TENANT_HEX_ID`` to ``@tenantId`` is the post-optimisation
        form of the canonical case; no rewrite needed."""
        aql = "FOR e IN Employee FILTER e.TENANT_HEX_ID == @tenantId RETURN e"
        plan = _wrap_plan(
            [
                _singleton_node(),
                _index_node(
                    nid=2,
                    collection="Employee",
                    outvar="e",
                    condition={
                        "type": "compare ==",
                        "subNodes": [
                            {
                                "type": "attribute access",
                                "name": "TENANT_HEX_ID",
                                "subNodes": [{"type": "reference", "name": "e", "id": 100}],
                            },
                            {"type": "parameter", "name": "tenantId"},
                        ],
                    },
                ),
                _return_node(nid=3),
            ]
        )
        out, _, changes = _inject(aql=aql, plan=plan)
        assert out == aql
        assert changes == []

    def test_indexnode_without_tenant_predicate_gets_filter_injected(self) -> None:
        """An IndexNode keyed by an unrelated field (e.g. ``_key``)
        on a tenant-scoped collection is unsafe — Layer 4 still
        injects a downstream FILTER even though the plan shape is
        IndexNode rather than EnumerateCollectionNode."""
        aql = "FOR e IN Employee FILTER e._key == 'abc' RETURN e"
        plan = _wrap_plan(
            [
                _singleton_node(),
                _index_node(
                    nid=2,
                    collection="Employee",
                    outvar="e",
                    condition={
                        "type": "compare ==",
                        "subNodes": [
                            {
                                "type": "attribute access",
                                "name": "_key",
                                "subNodes": [{"type": "reference", "name": "e", "id": 100}],
                            },
                            {"type": "value", "value": "abc"},
                        ],
                    },
                ),
                _return_node(nid=3),
            ]
        )
        out, _, changes = _inject(aql=aql, plan=plan)
        assert "FILTER e.TENANT_HEX_ID == @tenantId" in out
        assert changes


# ---------------------------------------------------------------------------
# 11. Edge cases / failure modes
# ---------------------------------------------------------------------------


class TestFailureModes:
    def test_explain_returns_non_dict_raises(self) -> None:
        """Layer 4 must fail-closed on a malformed EXPLAIN response —
        no rewrite is safer than a silently-empty rewrite."""
        with pytest.raises(AqlRewriteError) as exc:
            inject_tenant_scope(
                db=None,
                aql="FOR e IN Employee RETURN e",
                bind_vars={"tenantId": "tenant-A-uuid"},
                manifest=_manifest(),
                sharding_profile=_sharding_profile(),
                tenant_id="tenant-A-uuid",
                tenant_key="tenant-A-uuid",
                plan_override=[],  # not a dict; should trip the malformed branch
            )
        assert exc.value.code == "EXPLAIN_MALFORMED"

    def test_explain_db_exception_propagates_as_rewrite_error(self) -> None:
        """When ``db.aql.explain`` raises, Layer 4 wraps the exception
        in ``AqlRewriteError`` with code ``EXPLAIN_FAILED`` so route
        handlers can map it to HTTP 422 without leaking the raw
        client driver exception."""

        class _BadDb:
            class _Aql:
                def explain(self, aql: str, bind_vars: dict[str, Any]) -> dict[str, Any]:
                    raise RuntimeError("simulated explain failure")

            aql = _Aql()

        with pytest.raises(AqlRewriteError) as exc:
            inject_tenant_scope(
                db=_BadDb(),
                aql="FOR e IN Employee RETURN e",
                bind_vars={"tenantId": "tenant-A-uuid"},
                manifest=_manifest(),
                sharding_profile=_sharding_profile(),
                tenant_id="tenant-A-uuid",
                tenant_key="tenant-A-uuid",
            )
        assert exc.value.code == "EXPLAIN_FAILED"
        assert "simulated explain failure" in exc.value.message

    def test_plan_says_site_exists_but_source_does_not_raises(self) -> None:
        """Defensive: when the plan's outvar/collection pair does not
        appear in the source text, the rewriter refuses rather than
        silently dropping the rewrite.
        """
        plan = _wrap_plan(
            [
                _singleton_node(),
                _enum_node(nid=2, collection="Employee", outvar="empoyee_typo"),
                _return_node(nid=3),
            ]
        )
        with pytest.raises(AqlRewriteError) as exc:
            _inject(
                aql="FOR e IN Employee RETURN e",  # source has 'e' not 'empoyee_typo'
                plan=plan,
            )
        assert exc.value.code == "REWRITE_SITE_NOT_FOUND"

    def test_multiple_tenant_scoped_collections_all_filtered(self) -> None:
        """A query that touches two TENANT_SCOPED collections must get
        a FILTER injected for each — covers the multi-FOR composition
        that the transpiler emits for multi-pattern Cypher MATCH."""
        aql = "FOR e IN Employee FOR a IN Asset FILTER e._id == a._id RETURN [e, a]"
        # Asset's manifest entry has no denorm_field but the smart-
        # graph attribute (TENANT_HEX_ID) is reachable via the
        # AssetRefGraph in _sharding_profile().
        manifest = TenantScopeManifest(
            tenant_entity="Tenant",
            entities={
                "Tenant": EntityScope(role=EntityTenantRole.TENANT_ROOT),
                "Employee": EntityScope(
                    role=EntityTenantRole.TENANT_SCOPED,
                    denorm_field="TENANT_HEX_ID",
                    reachable_from_tenant=True,
                ),
                "Asset": EntityScope(
                    role=EntityTenantRole.TENANT_SCOPED,
                    denorm_field="TENANT_HEX_ID",
                    reachable_from_tenant=True,
                ),
            },
        )
        plan = _wrap_plan(
            [
                _singleton_node(),
                _enum_node(nid=2, collection="Employee", outvar="e"),
                _enum_node(nid=3, collection="Asset", outvar="a"),
                _return_node(nid=4),
            ]
        )
        out, _, changes = _inject(aql=aql, plan=plan, manifest=manifest)
        assert "FILTER e.TENANT_HEX_ID == @tenantId" in out
        assert "FILTER a.TENANT_HEX_ID == @tenantId" in out
        assert len(changes) == 2

    def test_collection_to_entity_overrides_default_naming(self) -> None:
        """When the physical collection name differs from the entity
        label (e.g. ``COLLECTION-style`` mapping with a renamed
        physical), the ``collection_to_entity`` map must be consulted.
        """
        manifest = TenantScopeManifest(
            tenant_entity="Tenant",
            entities={
                "Tenant": EntityScope(role=EntityTenantRole.TENANT_ROOT),
                "EmployeeEntity": EntityScope(
                    role=EntityTenantRole.TENANT_SCOPED,
                    denorm_field="TENANT_HEX_ID",
                    reachable_from_tenant=True,
                ),
            },
        )
        # Physical collection name is "EMPLOYEES_V2"; the entity in
        # the manifest is "EmployeeEntity".
        coll_to_entity = {"EMPLOYEES_V2": "EmployeeEntity"}
        aql = "FOR e IN EMPLOYEES_V2 RETURN e"
        plan = _wrap_plan(
            [
                _singleton_node(),
                _enum_node(nid=2, collection="EMPLOYEES_V2", outvar="e"),
                _return_node(nid=3),
            ]
        )
        out, _, changes = _inject(
            aql=aql,
            plan=plan,
            manifest=manifest,
            sharding_profile={
                "members": {"EMPLOYEES_V2": {"kind": "smartgraph"}},
                "graphs": [],
                "collectionToEntity": coll_to_entity,
            },
            collection_to_entity=coll_to_entity,
        )
        assert "FILTER e.TENANT_HEX_ID == @tenantId" in out
        assert changes


# ---------------------------------------------------------------------------
# 12. Tenant-root: Tenant collection access pass-through
#
# Layer 4 should NOT inject anything on the Tenant collection itself;
# tenant-root constraint (``_key == @tenantKey``) is Layer 5's
# concern, not Layer 4's textual-rewrite scope.
# ---------------------------------------------------------------------------


class TestTenantRoot:
    def test_tenant_root_pass_through(self) -> None:
        aql = "FOR t IN Tenant FILTER t._key == @tenantKey RETURN t"
        plan = _wrap_plan(
            [
                _singleton_node(),
                _enum_node(nid=2, collection="Tenant", outvar="t"),
                _return_node(nid=3),
            ]
        )
        out, _, changes = _inject(
            aql=aql,
            plan=plan,
            bind_vars={"tenantId": "tenant-A-uuid", "tenantKey": "tenant-A-uuid"},
        )
        # TENANT_ROOT is Layer 5's job; Layer 4 leaves it alone.
        assert out == aql
        assert all("Tenant" not in c or "FILTER" not in c for c in changes)
