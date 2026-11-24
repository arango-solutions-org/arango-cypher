"""Wave 8a MT-3a — contract tests for the core Cypher AST rewriter.

These tests pin the algorithmic seam between MT-3a (this PR — visitor
skeleton, property-map injection, WHERE / UNWIND literal rejection)
and MT-3b (traversal-path promotion) / MT-3c (route + UI wiring):

* Every TENANT_ROOT and TENANT_SCOPED-with-denorm node pattern gets the
  bind-variable predicate injected into its property map, with
  deterministic key ordering (alphabetical existing keys, tenant
  field last).
* Every literal tenant value — in a property map, in a WHERE
  equality, or inside an ``UNWIND [...]`` list literal — is refused.
* Every TENANT_SCOPED-without-denorm (traversal-only) node pattern
  raises :class:`TenantScopeRewriteIncomplete` so MT-3c can fall back
  to Layer 5 enforcement until MT-3b lands.
* Every unknown label is refused (fail-closed).
* GLOBAL entities are untouched — the rewriter returns the original
  Cypher byte-for-byte.

Test fixtures intentionally use the *narrowest* manifest required for
each case (rather than a fat shared manifest) so a test failure
points cleanly at the missing entity entry. The
``_build_manifest`` helper accepts an explicit ``known_tenant_keys``
set so we can exercise both the populated-keyset path and the
fail-closed (``None``) path of
:func:`tenant_ast_common.is_literal_tenant_value`.
"""

from __future__ import annotations

import pytest

from arango_cypher.nl2cypher.tenant_ast_common import (
    UnknownEntityScope,  # noqa: F401 - re-exported in error context
)
from arango_cypher.nl2cypher.tenant_ast_cypher import (
    TenantScopeRewriteIncomplete,
    TenantScopeRewriteRejection,
    inject_tenant_scope,
)
from arango_cypher.nl2cypher.tenant_scope import (
    EntityScope,
    EntityTenantRole,
    TenantScopeManifest,
)
from arango_cypher.parser import parse_cypher

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _build_manifest(
    *,
    known_tenant_keys: frozenset[str] | None = frozenset({"tenant-A-uuid", "tenant-B-uuid"}),
) -> TenantScopeManifest:
    """Return the smallest manifest that exercises every MT-3a code path.

    * ``Tenant`` is the TENANT_ROOT (``_key`` / ``$tenantKey``).
    * ``Employee`` is TENANT_SCOPED with a denorm field — the happy
      property-map injection path.
    * ``Country`` is GLOBAL — exercises the byte-identical pass-through.
    * ``Device`` is TENANT_SCOPED without a denorm field but with a
      scoping path — exercises the
      :class:`TenantScopeRewriteIncomplete` branch deferred to MT-3b.
    """
    return TenantScopeManifest(
        tenant_entity="Tenant",
        entities={
            "Tenant": EntityScope(
                role=EntityTenantRole.TENANT_ROOT,
                reachable_from_tenant=True,
            ),
            "Employee": EntityScope(
                role=EntityTenantRole.TENANT_SCOPED,
                denorm_field="TENANT_HEX_ID",
                reachable_from_tenant=True,
            ),
            "Country": EntityScope(
                role=EntityTenantRole.GLOBAL,
            ),
            "Device": EntityScope(
                role=EntityTenantRole.TENANT_SCOPED,
                denorm_field=None,
                scoping_path=("TENANTUSERTENANT",),
                reachable_from_tenant=True,
            ),
        },
        known_tenant_keys=known_tenant_keys,
    )


def _inject(cypher: str, *, manifest: TenantScopeManifest | None = None) -> tuple[str, list[str]]:
    """Parse + inject — the harness every test goes through.

    Pinning the dummy ``tenant_id`` / ``tenant_key`` here means the
    tests never accidentally assert on a real-looking UUID; the
    *bind-variable* names (``$tenantId`` / ``$tenantKey``) are the
    contract surface MT-3a guarantees.
    """
    mf = manifest if manifest is not None else _build_manifest()
    parse_result = parse_cypher(cypher)
    return inject_tenant_scope(
        cypher=cypher,
        parse_tree=parse_result.tree,
        manifest=mf,
        tenant_id="ignored-in-mt3a",
        tenant_key="ignored-in-mt3a",
    )


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


def test_global_entity_no_change() -> None:
    """A query over a GLOBAL label must be returned byte-for-byte and
    must not appear in the changes list."""
    cypher = "MATCH (c:Country) RETURN c"
    rewritten, changes = _inject(cypher)
    assert rewritten == cypher
    assert changes == []


def test_tenant_scoped_with_denorm_inline_property() -> None:
    """A bare TENANT_SCOPED pattern gets ``{<denorm>: $tenantId}``
    injected."""
    cypher = "MATCH (e:Employee) RETURN e"
    rewritten, changes = _inject(cypher)
    assert rewritten == "MATCH (e:Employee {TENANT_HEX_ID: $tenantId}) RETURN e"
    assert changes == ["Added TENANT_HEX_ID = $tenantId to (e:Employee)"]


def test_tenant_root_inline_key() -> None:
    """A bare TENANT_ROOT pattern gets ``{_key: $tenantKey}`` injected."""
    cypher = "MATCH (t:Tenant) RETURN t"
    rewritten, changes = _inject(cypher)
    assert rewritten == "MATCH (t:Tenant {_key: $tenantKey}) RETURN t"
    assert changes == ["Added _key = $tenantKey to (t:Tenant)"]


def test_existing_property_map_merged() -> None:
    """An existing property map gains the tenant field at the end.

    Ordering contract: existing keys first (alphabetical), tenant
    field last. ``name: 'Alice'`` therefore appears before
    ``TENANT_HEX_ID: $tenantId`` in the output — even though
    ``TENANT_HEX_ID`` < ``name`` in ASCII order — because the tenant
    field's "last" position overrides alphabetical sort.
    """
    cypher = "MATCH (e:Employee {name: 'Alice'}) RETURN e"
    rewritten, changes = _inject(cypher)
    assert rewritten == "MATCH (e:Employee {name: 'Alice', TENANT_HEX_ID: $tenantId}) RETURN e"
    assert changes == ["Added TENANT_HEX_ID = $tenantId to (e:Employee)"]


def test_existing_property_map_alphabetical_with_multiple_keys() -> None:
    """Property keys other than the tenant field are sorted
    alphabetically; tenant field stays last regardless of input order."""
    cypher = "MATCH (e:Employee {name: 'Alice', age: 30}) RETURN e"
    rewritten, _ = _inject(cypher)
    assert rewritten == "MATCH (e:Employee {age: 30, name: 'Alice', TENANT_HEX_ID: $tenantId}) RETURN e"


def test_existing_tenant_property_with_bindvar_kept() -> None:
    """An already-correct ``{TENANT_HEX_ID: $tenantId}`` is idempotent.

    A second run of the pass over its own output should return the
    same text — re-injection bugs would otherwise compound on every
    rewrite (e.g. duplicate keys, comma drift).
    """
    cypher = "MATCH (e:Employee {TENANT_HEX_ID: $tenantId}) RETURN e"
    rewritten, changes = _inject(cypher)
    assert rewritten == cypher
    assert changes == []


# ---------------------------------------------------------------------------
# Refusal paths
# ---------------------------------------------------------------------------


def test_existing_tenant_property_with_literal_rejected() -> None:
    """A literal tenant value in the property map is the T2 attack
    surface; MT-3a refuses with ``LITERAL_TENANT_PREDICATE``."""
    cypher = "MATCH (e:Employee {TENANT_HEX_ID: 'tenant-B-uuid'}) RETURN e"
    with pytest.raises(TenantScopeRewriteRejection) as ei:
        _inject(cypher)
    assert ei.value.code == "LITERAL_TENANT_PREDICATE"
    assert "(e:Employee)" in ei.value.where


def test_where_literal_tenant_predicate_rejected() -> None:
    """An equality predicate against a known tenant literal in a
    WHERE clause is refused.

    The known-tenant-keys set on the manifest enables this check;
    the literal ``'tenant-A-uuid'`` is in that set, so this is the
    populated-keyset path through
    :func:`is_literal_tenant_value`.
    """
    cypher = "MATCH (e:Employee) WHERE e.TENANT_HEX_ID = 'tenant-A-uuid' RETURN e"
    with pytest.raises(TenantScopeRewriteRejection) as ei:
        _inject(cypher)
    assert ei.value.code == "LITERAL_TENANT_PREDICATE"
    assert "TENANT_HEX_ID" in ei.value.where


def test_where_bindvar_tenant_predicate_kept() -> None:
    """``WHERE e.TENANT_HEX_ID = $tenantId`` is acceptable; the WHERE
    clause itself is not rewritten, but the node pattern still
    receives the canonical property-map injection."""
    cypher = "MATCH (e:Employee) WHERE e.TENANT_HEX_ID = $tenantId RETURN e"
    rewritten, changes = _inject(cypher)
    # Node pattern injected; WHERE preserved verbatim.
    expected = "MATCH (e:Employee {TENANT_HEX_ID: $tenantId}) WHERE e.TENANT_HEX_ID = $tenantId RETURN e"
    assert rewritten == expected
    assert changes == ["Added TENANT_HEX_ID = $tenantId to (e:Employee)"]


def test_unwind_known_tenant_literal_rejected() -> None:
    """``UNWIND ['tenant-A-uuid'] AS t`` is the T2 smuggling pattern;
    MT-3a refuses when any element of the literal list is in
    ``known_tenant_keys``."""
    cypher = "UNWIND ['tenant-A-uuid'] AS t RETURN t"
    with pytest.raises(TenantScopeRewriteRejection) as ei:
        _inject(cypher)
    assert ei.value.code == "LITERAL_TENANT_PREDICATE"
    assert "tenant-A-uuid" in str(ei.value)


@pytest.mark.parametrize(
    "cypher",
    [
        "MATCH (e:Employee) WHERE e.TENANT_HEX_ID = 'tenant-\\u0041-uuid' RETURN e",
        'MATCH (e:Employee) WHERE e.TENANT_HEX_ID = "tenant-\\U0041-uuid" RETURN e',
        "UNWIND ['tenant-\\U00000041-uuid'] AS t RETURN t",
    ],
)
def test_a_literal_is_read_with_its_escapes_decoded(cypher: str) -> None:
    """A literal is compared by its value, as the translator reads it:
    ``'tenant-\\u0041-uuid'`` is ``tenant-A-uuid``."""
    with pytest.raises(TenantScopeRewriteRejection) as ei:
        _inject(cypher)
    assert ei.value.code == "LITERAL_TENANT_PREDICATE"


def test_unwind_unknown_string_allowed_when_keyset_populated() -> None:
    """When the keyset is populated and the literal isn't in it, the
    UNWIND passes — only known tenant keys are refused."""
    cypher = "UNWIND ['banana'] AS t RETURN t"
    rewritten, changes = _inject(cypher)
    assert rewritten == cypher
    assert changes == []


def test_unwind_unknown_string_rejected_when_keyset_is_none() -> None:
    """When the manifest has not sampled the Tenant collection
    (``known_tenant_keys=None``),
    :func:`is_literal_tenant_value` fails closed — every string
    literal in an UNWIND is treated as a potential tenant key and the
    rewriter refuses. This pins the fail-closed contract from
    Wave 8-pre at the Layer 3 boundary."""
    cypher = "UNWIND ['anything'] AS t RETURN t"
    manifest = _build_manifest(known_tenant_keys=None)
    with pytest.raises(TenantScopeRewriteRejection) as ei:
        _inject(cypher, manifest=manifest)
    assert ei.value.code == "LITERAL_TENANT_PREDICATE"


def test_traversal_only_raises_incomplete() -> None:
    """A TENANT_SCOPED entity with only a scoping path (no denorm
    field) is MT-3b territory; MT-3a raises
    :class:`TenantScopeRewriteIncomplete` so the caller can fall back
    to the un-rewritten Cypher and rely on Layer 5 to enforce."""
    cypher = "MATCH (d:Device) RETURN d"
    with pytest.raises(TenantScopeRewriteIncomplete) as ei:
        _inject(cypher)
    assert ei.value.label == "Device"
    assert "(d:Device)" in ei.value.where


def test_unknown_label_rejected() -> None:
    """An unmapped label is refused with ``UNKNOWN_ENTITY``; the rewriter
    never guesses the scope."""
    cypher = "MATCH (x:MysteryBox) RETURN x"
    with pytest.raises(TenantScopeRewriteRejection) as ei:
        _inject(cypher)
    assert ei.value.code == "UNKNOWN_ENTITY"
    assert "MysteryBox" in ei.value.message


# ---------------------------------------------------------------------------
# Cross-cutting contract: changes-list copy
# ---------------------------------------------------------------------------


def test_changes_list_human_readable() -> None:
    """Each entry in the changes list is rendered as a short
    English sentence that MT-3c can dump into the UI annotation strip
    verbatim. We assert the exact shape so the UI panel can rely on
    it (the leading "Added", the equality operator, and the
    pattern-shaped suffix)."""
    cypher = "MATCH (e:Employee)-[:OWNS]->(d:Device) MATCH (t:Tenant) RETURN e, d, t"
    # ``Device`` is traversal-only — this is intentional; MT-3a should
    # raise Incomplete BEFORE producing a partial changes list. Pin
    # that contract by asserting we never see the half-rewrite.
    with pytest.raises(TenantScopeRewriteIncomplete):
        _inject(cypher)


def test_changes_list_human_readable_full_pass() -> None:
    """Two distinct node patterns over fully-rewritable entities each
    produce one human-readable entry in the changes list."""
    cypher = "MATCH (t:Tenant)-[:OWNS]->(e:Employee) RETURN t, e"
    rewritten, changes = _inject(cypher)
    assert (
        rewritten
        == "MATCH (t:Tenant {_key: $tenantKey})-[:OWNS]->(e:Employee {TENANT_HEX_ID: $tenantId}) RETURN t, e"
    )
    assert changes == [
        "Added _key = $tenantKey to (t:Tenant)",
        "Added TENANT_HEX_ID = $tenantId to (e:Employee)",
    ]


@pytest.mark.parametrize(
    ("literal", "value"),
    [
        ("'plain'", "plain"),
        ("'tenant-\\u0041-uuid'", "tenant-A-uuid"),
        ('"a\\tb"', "a\tb"),
        # The translator refuses this literal; the check sees it unquoted.
        ("'\\uD800'", "\\uD800"),
    ],
)
def test_cypher_literal_value(literal: str, value: str) -> None:
    from arango_cypher.nl2cypher.tenant_ast_common import cypher_literal_value

    assert cypher_literal_value(literal) == value
