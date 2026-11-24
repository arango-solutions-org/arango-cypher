"""Tests for the tenant-scoping postcondition.

Covers:

* :func:`check_tenant_scope` — the core postcondition.
* :class:`PromptBuilder` — tenant-scope block is injected into the
  system prompt when a context is active, and omitted otherwise
  (byte-identity with zero-shot rendering).
* :func:`nl_to_cypher` end-to-end — fail-closed behaviour when the LLM
  stubbornly emits unscoped Cypher.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from arango_cypher.nl2cypher import (
    PromptBuilder,
    TenantContext,
    TenantScopeViolation,
    check_tenant_scope,
    has_tenant_entity,
    nl_to_cypher,
)
from arango_cypher.nl2cypher.tenant_guardrail import (
    CODE_LITERAL_TENANT_PREDICATE,
    CODE_NO_TENANT_BINDING,
    cypher_binds_tenant,
    prompt_section,
)
from arango_cypher.nl2cypher.tenant_scope import (
    EntityScope,
    EntityTenantRole,
    TenantScopeManifest,
    analyze_tenant_scope,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _bundle(entities: list[str]) -> SimpleNamespace:
    """Minimal stand-in for a MappingBundle that only exposes what the
    guardrail inspects. Using SimpleNamespace keeps the tests immune
    to MappingBundle signature churn."""
    return SimpleNamespace(
        conceptual_schema={"entities": [{"name": n} for n in entities]},
        physical_mapping={},
        metadata={},
    )


TENANT_CTX = TenantContext(
    property="TENANT_HEX_ID",
    value="abc123",
    display="Dagster Labs",
)

# Canonical context shape (Wave 4r.1+): scope by Tenant._key. Mirrors
# what the UI's TenantSelector now emits — see TenantSelector.toContext.
TENANT_CTX_KEY = TenantContext(
    property="_key",
    value="001c463d-500d-47c7-bc32-c824eb42f064",
    display="Pinecone",
)


# ---------------------------------------------------------------------------
# check_tenant_scope
# ---------------------------------------------------------------------------


class TestCheckTenantScope:
    def test_fires_when_cypher_drops_tenant(self) -> None:
        cypher = "MATCH (u:GSuiteUser) WHERE u.DEPARTMENT='Marketing' RETURN u.NAME"
        v = check_tenant_scope(cypher, tenant_context=TENANT_CTX)
        assert isinstance(v, TenantScopeViolation)
        assert v.tenant_property == "TENANT_HEX_ID"
        assert v.tenant_value == "abc123"
        assert "abc123" not in v.reason  # display used instead
        assert "Dagster Labs" in v.reason
        assert "TENANT_HEX_ID" in v.suggested_hint

    def test_passes_when_cypher_binds_tenant(self) -> None:
        cypher = (
            "MATCH (t:Tenant {TENANT_HEX_ID:'abc123'})<-[:TENANTUSERTENANT]-"
            "(:TenantUser)<-[:GSUITEUSERTENANTUSER]-(u:GSuiteUser) "
            "WHERE u.DEPARTMENT='Marketing' RETURN u.NAME"
        )
        assert check_tenant_scope(cypher, tenant_context=TENANT_CTX) is None

    def test_passes_when_no_tenant_context(self) -> None:
        cypher = "MATCH (u:GSuiteUser) RETURN u.NAME"
        assert check_tenant_scope(cypher, tenant_context=None) is None

    def test_tenant_user_does_not_count_as_tenant(self) -> None:
        # `:TenantUser` must not satisfy the `:Tenant` constraint — the
        # whole point of the guardrail is that prefix-collision labels
        # are the common failure mode.
        cypher = "MATCH (u:GSuiteUser)-[:GSUITEUSERTENANTUSER]->(:TenantUser) RETURN u.NAME"
        v = check_tenant_scope(cypher, tenant_context=TENANT_CTX)
        assert v is not None

    def test_tenant_cve_does_not_count_as_tenant(self) -> None:
        cypher = "MATCH (c:TenantCVE) RETURN c"
        v = check_tenant_scope(cypher, tenant_context=TENANT_CTX)
        assert v is not None

    def test_tenant_label_with_spaces_accepted(self) -> None:
        # `: Tenant` (with whitespace) is legal Cypher.
        cypher = "MATCH (t: Tenant) RETURN t"
        assert cypher_binds_tenant(cypher) is True
        assert check_tenant_scope(cypher, tenant_context=TENANT_CTX) is None

    def test_empty_cypher_is_violation(self) -> None:
        v = check_tenant_scope("", tenant_context=TENANT_CTX)
        assert v is not None

    def test_display_falls_back_to_value(self) -> None:
        ctx = TenantContext(property="NAME", value="Acme")
        v = check_tenant_scope("MATCH (x:User) RETURN x", tenant_context=ctx)
        assert v is not None
        assert "'Acme'" in v.reason

    def test_key_context_renders_key_match_pattern_in_hint(self) -> None:
        # When scoped by `_key`, the suggested-hint MATCH pattern must
        # use the Cypher `{_key: '<uuid>'}` shorthand. This is what the
        # retry loop feeds back to the LLM, so a regression here would
        # silently push the model back to TENANT_HEX_ID style queries.
        cypher = "MATCH (u:GSuiteUser) RETURN u.NAME"
        v = check_tenant_scope(cypher, tenant_context=TENANT_CTX_KEY)
        assert v is not None
        assert "_key" in v.suggested_hint
        assert TENANT_CTX_KEY.value in v.suggested_hint
        # And it must NOT regress to TENANT_HEX_ID phrasing.
        assert "TENANT_HEX_ID" not in v.suggested_hint

    def test_key_context_passes_when_cypher_binds_tenant_by_key(self) -> None:
        cypher = (
            "MATCH (t:Tenant {_key: '001c463d-500d-47c7-bc32-c824eb42f064'})"
            "-[:TENANTDEVICE]->(d:Device) RETURN d"
        )
        assert check_tenant_scope(cypher, tenant_context=TENANT_CTX_KEY) is None

    def test_key_context_passes_when_filter_uses_denorm_tenant_id(
        self,
    ) -> None:
        # The denormalised filter form is the preferred shape when the
        # target collection carries a TENANT_ID column. We deliberately
        # only require the presence of a `:Tenant` binding; this test
        # confirms that the standard recommended pattern (bind + filter)
        # passes through the guardrail.
        cypher = (
            "MATCH (t:Tenant {_key: '001c463d-500d-47c7-bc32-c824eb42f064'}), "
            "(d:Device) WHERE d.TENANT_ID = '001c463d-500d-47c7-bc32-c824eb42f064' "
            "RETURN d"
        )
        assert check_tenant_scope(cypher, tenant_context=TENANT_CTX_KEY) is None

    # -------------------------------------------------------------------
    # Manifest-aware behaviour
    # -------------------------------------------------------------------

    def _device_manifest(self) -> TenantScopeManifest:
        # Tenant + Device(TENANT_ID denorm) + Cve(GLOBAL).
        return TenantScopeManifest(
            tenant_entity="Tenant",
            entities={
                "Tenant": EntityScope(
                    role=EntityTenantRole.TENANT_ROOT,
                    reachable_from_tenant=True,
                ),
                "Device": EntityScope(
                    role=EntityTenantRole.TENANT_SCOPED,
                    denorm_field="TENANT_ID",
                    reachable_from_tenant=True,
                ),
                "Cve": EntityScope(role=EntityTenantRole.GLOBAL),
                "AppVersion": EntityScope(role=EntityTenantRole.GLOBAL),
            },
        )

    def test_manifest_global_only_query_is_not_a_violation(self) -> None:
        # The whole reason the user pushed back: a query that touches
        # only GLOBAL entities (e.g. metadata tables) MUST pass
        # through, otherwise the guardrail refuses legitimate
        # cross-tenant questions like "list all CVEs".
        cypher = "MATCH (c:Cve) WHERE c.SEVERITY > 7 RETURN c"
        v = check_tenant_scope(
            cypher,
            tenant_context=TENANT_CTX_KEY,
            manifest=self._device_manifest(),
        )
        assert v is None

    def test_manifest_mixed_global_and_scoped_still_requires_scope(self) -> None:
        # If even ONE referenced label is tenant-scoped, the whole
        # query must be scoped. Mixing a GLOBAL Cve lookup with a
        # tenant-scoped Device lookup without a tenant filter would
        # leak Devices across tenants — guardrail must fire.
        cypher = "MATCH (c:Cve), (d:Device) RETURN c, d"
        v = check_tenant_scope(
            cypher,
            tenant_context=TENANT_CTX_KEY,
            manifest=self._device_manifest(),
        )
        assert v is not None

    def test_manifest_denorm_field_filter_satisfies_scope(self) -> None:
        # No :Tenant binding at all — but a direct filter on
        # Device.TENANT_ID = $tenantId satisfies the scope because
        # the planner can hit the indexed equality and the bind-var
        # is the only acceptable proof of scope after Wave 8a / MT-2
        # (PRD §5.2 enhancement #1). The pre-MT-2 literal form is
        # rejected — see ``test_literal_tenant_predicate_*`` below.
        cypher = "MATCH (d:Device) WHERE d.TENANT_ID = $tenantId RETURN d"
        v = check_tenant_scope(
            cypher,
            tenant_context=TENANT_CTX_KEY,
            manifest=self._device_manifest(),
        )
        assert v is None

    def test_manifest_denorm_field_filter_with_inline_property_form(
        self,
    ) -> None:
        # Inline node-property form of the denorm filter:
        # `MATCH (d:Device {TENANT_ID: $tenantId})` is semantically
        # identical to the WHERE form and must also satisfy the
        # guardrail.
        cypher = "MATCH (d:Device {TENANT_ID: $tenantId}) RETURN d"
        v = check_tenant_scope(
            cypher,
            tenant_context=TENANT_CTX_KEY,
            manifest=self._device_manifest(),
        )
        assert v is None

    def test_manifest_denorm_field_filter_with_wrong_value_is_violation(
        self,
    ) -> None:
        # Filtering by a literal — any literal — must NOT satisfy
        # the scope. After Wave 8a / MT-2 the violation code shifts
        # from NO_TENANT_BINDING (pre-MT-2) to
        # LITERAL_TENANT_PREDICATE (post-MT-2): even a value that
        # doesn't match the active tenant is rejected on the
        # literal-vs-bindvar contract, not just on value mismatch.
        cypher = "MATCH (d:Device) WHERE d.TENANT_ID = 'some-other-tenant-key' RETURN d"
        v = check_tenant_scope(
            cypher,
            tenant_context=TENANT_CTX_KEY,
            manifest=self._device_manifest(),
        )
        assert v is not None
        assert v.code == "LITERAL_TENANT_PREDICATE"

    def test_manifest_violation_hint_mentions_denorm_field_when_available(
        self,
    ) -> None:
        # When the schema offers a denorm field, the retry hint must
        # surface it as the preferred fix — telling the model to
        # traverse from :Tenant for an entity that has a TENANT_ID
        # column would push it toward an unnecessarily expensive
        # query.
        cypher = "MATCH (d:Device) RETURN d"
        v = check_tenant_scope(
            cypher,
            tenant_context=TENANT_CTX_KEY,
            manifest=self._device_manifest(),
        )
        assert v is not None
        assert "TENANT_ID" in v.suggested_hint

    def test_manifest_aware_violation_when_only_traversal_available(
        self,
    ) -> None:
        # Library has no denorm field — the only way to scope it is
        # via traversal from :Tenant. An unscoped library query must
        # be a violation, and the hint must NOT recommend a denorm
        # filter (which would tell the LLM to invent a TENANT_ID
        # column that doesn't exist).
        manifest = TenantScopeManifest(
            tenant_entity="Tenant",
            entities={
                "Tenant": EntityScope(
                    role=EntityTenantRole.TENANT_ROOT,
                    reachable_from_tenant=True,
                ),
                "Library": EntityScope(
                    role=EntityTenantRole.TENANT_SCOPED,
                    denorm_field=None,
                    reachable_from_tenant=True,
                ),
            },
        )
        v = check_tenant_scope(
            "MATCH (l:Library) RETURN l",
            tenant_context=TENANT_CTX_KEY,
            manifest=manifest,
        )
        assert v is not None
        assert "TENANT_ID" not in v.suggested_hint
        assert "traverse" in v.suggested_hint.lower()


# ---------------------------------------------------------------------------
# Wave 8a / MT-2 — literal tenant predicates are always rejected.
# ---------------------------------------------------------------------------


class TestMT2LiteralTenantPredicate:
    """PRD §5.2 enhancement #1: the LLM-output guardrail rejects every
    literal tenant predicate so Layer 3 (Wave 8a / MT-3) only ever
    has to rewrite the bind-var form, and a hostile prompt cannot
    smuggle in a tenant identifier as user-controlled data.

    See ``docs/multitenant_prd.md`` §5.2 and
    ``docs/agent_prompts_multitenant.md`` MT-2 for the contract.
    """

    @staticmethod
    def _employee_manifest(
        *,
        known_tenant_keys: frozenset[str] | None,
    ) -> TenantScopeManifest:
        # The fixture mirrors a tenant-scoped Employee table with a
        # denormalised ``TENANT_HEX_ID`` column — the canonical shape
        # MT-2 protects. ``known_tenant_keys`` is parameterised so a
        # single helper covers both the sampled and the fail-closed
        # branches of ``is_literal_tenant_value``.
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
            },
            known_tenant_keys=known_tenant_keys,
        )

    def test_literal_tenant_predicate_rejected_even_when_value_matches(
        self,
    ) -> None:
        # The pre-MT-2 contract accepted this Cypher because the literal
        # *matched* the active tenant. The new contract rejects it
        # outright: literals are forbidden on tenant predicates
        # regardless of value, because Layer 3 (MT-3) rewrites every
        # legitimate tenant predicate to the bind-var form, so a
        # surviving literal — even a "harmless" one — is evidence the
        # LLM is treating the tenant identifier as user-controlled
        # data rather than a session-bound bind variable.
        manifest = self._employee_manifest(
            known_tenant_keys=frozenset({"tenant-A-uuid"}),
        )
        ctx = TenantContext(property="_key", value="tenant-A-uuid")
        cypher = "MATCH (e:Employee) WHERE e.TENANT_HEX_ID = 'tenant-A-uuid' RETURN e"
        v = check_tenant_scope(cypher, tenant_context=ctx, manifest=manifest)
        assert isinstance(v, TenantScopeViolation)
        assert v.code == CODE_LITERAL_TENANT_PREDICATE
        # Reason / hint must include the offending entity label and
        # field name so the LLM-retry loop and the user both know
        # which predicate was refused.
        assert "Employee" in v.reason
        assert "TENANT_HEX_ID" in v.reason
        assert "TENANT_HEX_ID" in v.suggested_hint
        # And the hint must point at the bind-var spelling — that's
        # the rewrite target the model has to adopt on the next
        # attempt.
        assert "$tenantId" in v.suggested_hint

    @pytest.mark.parametrize(
        "literal",
        ["'tenant-\\u0041-uuid'", '"tenant-\\U0041-uuid"', "'tenant-\\U00000041-uuid'"],
    )
    def test_a_literal_is_read_with_its_escapes_decoded(self, literal: str) -> None:
        # The predicate compares the literal's value, escapes decoded
        # as the translator decodes them: each of these is
        # ``tenant-A-uuid``.
        manifest = self._employee_manifest(
            known_tenant_keys=frozenset({"tenant-A-uuid"}),
        )
        ctx = TenantContext(property="_key", value="tenant-A-uuid")
        for cypher in (
            f"MATCH (e:Employee) WHERE e.TENANT_HEX_ID = {literal} RETURN e",
            f"MATCH (e:Employee {{TENANT_HEX_ID: {literal}}}) RETURN e",
        ):
            v = check_tenant_scope(cypher, tenant_context=ctx, manifest=manifest)
            assert isinstance(v, TenantScopeViolation), cypher
            assert v.code == CODE_LITERAL_TENANT_PREDICATE

    def test_literal_tenant_predicate_rejected_when_value_unknown(
        self,
    ) -> None:
        # The fail-closed branch of ``is_literal_tenant_value``:
        # when the manifest does not carry ``known_tenant_keys``
        # (the common case in tests, heuristic-mode mappings, and
        # older bundles where the Tenant collection was not sampled
        # at acquire time), every string literal on a tenant predicate
        # is treated as suspect. Refusing here is strictly safer than
        # the alternative ("can't tell, allow") which is the original
        # T2 bug.
        manifest = self._employee_manifest(known_tenant_keys=None)
        ctx = TenantContext(property="_key", value="tenant-A-uuid")
        cypher = "MATCH (e:Employee) WHERE e.TENANT_HEX_ID = 'whatever' RETURN e"
        v = check_tenant_scope(cypher, tenant_context=ctx, manifest=manifest)
        assert isinstance(v, TenantScopeViolation)
        assert v.code == CODE_LITERAL_TENANT_PREDICATE
        assert "Employee" in v.reason
        assert "TENANT_HEX_ID" in v.reason

    def test_bindvar_tenant_predicate_accepted(self) -> None:
        # The bind-var form is the only acceptable proof of scope
        # after MT-2. ``$tenantId`` is the canonical Cypher spelling
        # of :data:`tenant_ast_common.TENANT_ID_BIND` — the rewriter
        # in MT-3 will emit exactly this form, and Layer 1 will
        # supply the actual value via the session-bound bind vars.
        manifest = self._employee_manifest(
            known_tenant_keys=frozenset({"tenant-A-uuid"}),
        )
        ctx = TenantContext(property="_key", value="tenant-A-uuid")
        cypher = "MATCH (e:Employee) WHERE e.TENANT_HEX_ID = $tenantId RETURN e"
        assert check_tenant_scope(cypher, tenant_context=ctx, manifest=manifest) is None

    def test_bindvar_inline_property_form_accepted(self) -> None:
        # Same contract for the inline property-map shape, which is
        # what the MT-3 rewriter prefers when an existing property
        # map is already on the node pattern. Pinned here so the
        # two acceptable bind-var shapes (WHERE-eq and inline-map)
        # don't drift apart in a future regex tweak.
        manifest = self._employee_manifest(
            known_tenant_keys=frozenset({"tenant-A-uuid"}),
        )
        ctx = TenantContext(property="_key", value="tenant-A-uuid")
        cypher = "MATCH (e:Employee {TENANT_HEX_ID: $tenantId}) RETURN e"
        assert check_tenant_scope(cypher, tenant_context=ctx, manifest=manifest) is None

    def test_no_tenant_predicate_rejected_with_existing_code(self) -> None:
        # The pre-MT-2 NO_TENANT_BINDING path is preserved verbatim
        # for the case where the LLM emits no tenant predicate at
        # all. The new ``LITERAL_TENANT_PREDICATE`` code is purely
        # additive — it must not displace the existing rejection.
        manifest = self._employee_manifest(
            known_tenant_keys=frozenset({"tenant-A-uuid"}),
        )
        ctx = TenantContext(property="_key", value="tenant-A-uuid")
        cypher = "MATCH (e:Employee) RETURN e"
        v = check_tenant_scope(cypher, tenant_context=ctx, manifest=manifest)
        assert isinstance(v, TenantScopeViolation)
        assert v.code == CODE_NO_TENANT_BINDING

    def test_prompt_section_contains_literal_rule(self) -> None:
        # The new rule line must be present in the rendered prompt
        # so the LLM is told exactly what shape to emit. The
        # two-line wrap (header + clarifier) matches the existing
        # rule-block style — pinned here so a future formatter pass
        # doesn't silently flatten it. The existing rule lines stay
        # in place; this test asserts both the new substrings AND
        # the survival of an existing rule line ("Do NOT mix scope
        # styles").
        manifest = self._employee_manifest(
            known_tenant_keys=frozenset({"tenant-A-uuid"}),
        )
        ctx = TenantContext(property="_key", value="tenant-A-uuid")
        rendered = prompt_section(ctx, manifest)
        assert "Tenant identifiers are bind variables" in rendered
        assert "@tenantId" in rendered
        assert "@tenantKey" in rendered
        assert "Never compare a tenant column to a literal string" in rendered
        # The pre-MT-2 rule line is preserved verbatim — MT-2 is
        # additive, not a rewrite of the prompt block.
        assert "Do NOT mix scope styles in a single MATCH" in rendered


# ---------------------------------------------------------------------------
# has_tenant_entity
# ---------------------------------------------------------------------------


class TestHasTenantEntity:
    def test_detects_tenant_in_bundle(self) -> None:
        assert has_tenant_entity(_bundle(["Tenant", "GSuiteUser"])) is True

    def test_returns_false_when_absent(self) -> None:
        assert has_tenant_entity(_bundle(["User", "Movie"])) is False

    def test_accepts_plain_dict(self) -> None:
        d = {"conceptual_schema": {"entities": [{"name": "Tenant"}]}}
        assert has_tenant_entity(d) is True

    def test_accepts_camel_case_dict(self) -> None:
        d = {"conceptualSchema": {"entities": [{"name": "Tenant"}]}}
        assert has_tenant_entity(d) is True

    def test_none_safe(self) -> None:
        assert has_tenant_entity(None) is False
        assert has_tenant_entity({}) is False
        assert has_tenant_entity({"conceptual_schema": None}) is False


# ---------------------------------------------------------------------------
# PromptBuilder integration
# ---------------------------------------------------------------------------


class TestPromptBuilderTenantBlock:
    def test_no_tenant_context_leaves_prompt_byte_identical(self) -> None:
        # Byte-identity is the contract that keeps provider prefix
        # caching working; single-tenant users must pay zero prompt
        # tokens for this feature.
        baseline = PromptBuilder(schema_summary="S").render_system()
        with_none = PromptBuilder(
            schema_summary="S",
            tenant_context=None,
        ).render_system()
        assert baseline == with_none

    def test_tenant_context_adds_scope_block(self) -> None:
        system = PromptBuilder(
            schema_summary="S",
            tenant_context=TENANT_CTX,
        ).render_system()
        assert "Current tenant scope" in system
        assert "Dagster Labs" in system
        assert "TENANT_HEX_ID" in system
        assert "abc123" in system

    def test_tenant_block_precedes_few_shot_section(self) -> None:
        system = PromptBuilder(
            schema_summary="S",
            tenant_context=TENANT_CTX,
            few_shot_examples=[("nl", "MATCH (x) RETURN x")],
        ).render_system()
        tenant_idx = system.index("Current tenant scope")
        fewshot_idx = system.index("## Examples")
        assert tenant_idx < fewshot_idx

    def test_key_context_emits_key_shorthand_and_denorm_substitution(
        self,
    ) -> None:
        # The canonical _key context, given a manifest that exposes a
        # denormalised TENANT_ID field on a target entity, must:
        #   * show the LLM the `{_key: '<uuid>'}` Cypher shorthand,
        #   * emit the per-entity denorm-filter example with the
        #     active _key as the literal (since Device.TENANT_ID
        #     stores Tenant._key in this schema family).
        manifest = analyze_tenant_scope(
            {
                "conceptual_schema": {
                    "entities": [
                        {"name": "Tenant", "properties": []},
                        {
                            "name": "GSuiteUser",
                            "properties": [
                                {"name": "TENANT_ID"},
                                {"name": "NAME"},
                            ],
                        },
                    ],
                    "relationships": [],
                },
                "physical_mapping": {"entities": {}, "relationships": {}},
            }
        )
        system = PromptBuilder(
            schema_summary="S",
            tenant_context=TENANT_CTX_KEY,
            tenant_manifest=manifest,
        ).render_system()
        assert "Pinecone" in system
        assert "_key" in system
        assert TENANT_CTX_KEY.value in system
        assert "TENANT_ID" in system
        # Per-entity example: literal _key spliced into the filter.
        assert f"g.TENANT_ID = '{TENANT_CTX_KEY.value}'" in system

    def test_non_key_context_omits_concrete_denorm_substitution(self) -> None:
        # When the operator overrides the scope with a NAME /
        # SUBDOMAIN context, we don't know the corresponding _key, so
        # the per-entity denorm example MUST NOT bake the
        # non-_key value (e.g. "abc123") into the filter — it would
        # produce a query that returns zero rows because the denorm
        # column stores the _key, not the hex_id. The block falls
        # back to the `<Tenant._key>` placeholder so the LLM is told
        # to resolve the key first (typically by also binding :Tenant).
        manifest = analyze_tenant_scope(
            {
                "conceptual_schema": {
                    "entities": [
                        {"name": "Tenant", "properties": []},
                        {
                            "name": "GSuiteUser",
                            "properties": [
                                {"name": "TENANT_ID"},
                                {"name": "NAME"},
                            ],
                        },
                    ],
                    "relationships": [],
                },
                "physical_mapping": {"entities": {}, "relationships": {}},
            }
        )
        system = PromptBuilder(
            schema_summary="S",
            tenant_context=TENANT_CTX,
            tenant_manifest=manifest,
        ).render_system()
        assert "<Tenant._key>" in system
        assert f"g.TENANT_ID = '{TENANT_CTX.value}'" not in system

    def test_manifest_global_entities_listed_as_do_not_scope(self) -> None:
        # Cve / AppVersion are intentionally cross-tenant. The prompt
        # must surface them in a "do NOT scope" list so the LLM
        # doesn't invent a tenant filter for them — that was the bug
        # reported on the demo schema.
        manifest = analyze_tenant_scope(
            {
                "conceptual_schema": {
                    "entities": [
                        {"name": "Tenant", "properties": []},
                        {"name": "Cve", "properties": [{"name": "ID"}]},
                        {"name": "AppVersion", "properties": [{"name": "VERSION"}]},
                    ],
                    "relationships": [],
                },
                "physical_mapping": {"entities": {}, "relationships": {}},
            }
        )
        system = PromptBuilder(
            schema_summary="S",
            tenant_context=TENANT_CTX_KEY,
            tenant_manifest=manifest,
        ).render_system()
        assert "Global / metadata" in system
        assert "Cve" in system
        assert "AppVersion" in system

    def test_manifest_traversal_only_entities_listed_separately(self) -> None:
        # Library has no TENANT_ID column but is reachable from
        # Tenant via an edge — the prompt must put it in the
        # traversal-only group so the LLM doesn't try to filter on a
        # nonexistent denorm field.
        manifest = analyze_tenant_scope(
            {
                "conceptual_schema": {
                    "entities": [
                        {"name": "Tenant", "properties": []},
                        {"name": "Library", "properties": [{"name": "NAME"}]},
                    ],
                    "relationships": [
                        {"type": "TENANTLIBRARY", "from": "Tenant", "to": "Library"},
                    ],
                },
                "physical_mapping": {"entities": {}, "relationships": {}},
            }
        )
        system = PromptBuilder(
            schema_summary="S",
            tenant_context=TENANT_CTX_KEY,
            tenant_manifest=manifest,
        ).render_system()
        assert "via traversal only" in system
        assert "Library" in system


# ---------------------------------------------------------------------------
# nl_to_cypher end-to-end fail-closed behaviour
# ---------------------------------------------------------------------------


class _StubProvider:
    """Minimal :class:`LLMProvider` stub that returns a pre-canned
    Cypher string on every call."""

    def __init__(self, cypher: str) -> None:
        self._cypher = cypher
        self.calls = 0

    def generate(self, system: str, user: str) -> tuple[str, dict]:  # noqa: ARG002
        self.calls += 1
        return f"```cypher\n{self._cypher}\n```", {
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_tokens": 15,
            "cached_tokens": 0,
        }


@pytest.fixture
def multi_tenant_mapping() -> dict:
    # GSuiteUser carries a denormalised TENANT_ID column AND is reachable
    # from Tenant via a GSUITEUSERTENANTUSER edge. Mirrors the real
    # demo schema — important because the manifest-aware guardrail
    # only fires on entities it has classified as tenant-scoped, so a
    # fixture with no TENANT_ID and no edge would (correctly) be
    # treated as a global metadata table and the test would not
    # exercise the guardrail at all.
    return {
        "conceptual_schema": {
            "entities": [
                {"name": "Tenant", "properties": [{"name": "TENANT_HEX_ID"}]},
                {"name": "TenantUser", "properties": [{"name": "TENANT_ID"}]},
                {
                    "name": "GSuiteUser",
                    "properties": [
                        {"name": "TENANT_ID"},
                        {"name": "NAME"},
                    ],
                },
            ],
            "relationships": [
                {
                    "type": "TENANTUSERTENANT",
                    "from": "TenantUser",
                    "to": "Tenant",
                },
                {
                    "type": "GSUITEUSERTENANTUSER",
                    "from": "GSuiteUser",
                    "to": "TenantUser",
                },
            ],
        },
        "physical_mapping": {
            "entities": {
                "Tenant": {"style": "COLLECTION", "collectionName": "Tenant"},
                "TenantUser": {"style": "COLLECTION", "collectionName": "TenantUser"},
                "GSuiteUser": {"style": "COLLECTION", "collectionName": "GSuiteUser"},
            },
            "relationships": {},
        },
    }


class TestNlToCypherFailClosed:
    def test_blocks_unscoped_cypher(self, multi_tenant_mapping) -> None:
        # Stub LLM always emits the offending unscoped pattern.
        provider = _StubProvider("MATCH (u:GSuiteUser) RETURN u.NAME")
        result = nl_to_cypher(
            "At Dagster Labs list all GSuiteUsers",
            mapping=multi_tenant_mapping,
            llm_provider=provider,
            use_fewshot=False,
            use_entity_resolution=False,
            tenant_context=TENANT_CTX,
            max_retries=1,
        )
        assert result.cypher == ""
        assert result.method == "tenant_guardrail_blocked"
        assert result.confidence == 0.0
        assert "Dagster Labs" in result.explanation
        # The offending attempt is surfaced so users understand why we
        # refused, rather than silently hiding the LLM's output.
        assert "GSuiteUser" in result.explanation
        # Budget is respected: exactly 1 + max_retries calls.
        assert provider.calls == 2

    def test_passes_tenant_scoped_cypher(self, multi_tenant_mapping) -> None:
        provider = _StubProvider(
            "MATCH (t:Tenant {TENANT_HEX_ID:'abc123'})<-[:TENANTUSERTENANT]-"
            "(:TenantUser)<-[:GSUITEUSERTENANTUSER]-(u:GSuiteUser) "
            "RETURN u.NAME"
        )
        result = nl_to_cypher(
            "At Dagster Labs list all GSuiteUsers",
            mapping=multi_tenant_mapping,
            llm_provider=provider,
            use_fewshot=False,
            use_entity_resolution=False,
            tenant_context=TENANT_CTX,
            max_retries=1,
        )
        assert result.method == "llm"
        assert (
            ":Tenant " in result.cypher
            or ":Tenant{" in result.cypher
            or "(:Tenant" in result.cypher
            or "(t:Tenant" in result.cypher
        )
        assert provider.calls == 1

    def test_passes_key_scoped_cypher_with_denorm_filter(
        self,
        multi_tenant_mapping,
    ) -> None:
        # End-to-end: when the LLM emits the canonical _key form with
        # the denormalised TENANT_ID filter on the target collection
        # *as a bind-variable predicate*, the guardrail must let it
        # through unmodified. After Wave 8a / MT-2 the literal form
        # ``WHERE u.TENANT_ID = '<uuid>'`` is rejected with
        # ``LITERAL_TENANT_PREDICATE``; only the bind-var form
        # ``WHERE u.TENANT_ID = $tenantId`` is acceptable proof of
        # scope. This is the shape the new prompt teaches the model
        # to produce and the shape Layer 3 (MT-3) preserves.
        provider = _StubProvider(
            "MATCH (t:Tenant {_key:'001c463d-500d-47c7-bc32-c824eb42f064'}), "
            "(u:GSuiteUser) "
            "WHERE u.TENANT_ID = $tenantId "
            "RETURN u.NAME"
        )
        result = nl_to_cypher(
            "At Pinecone list all GSuiteUsers",
            mapping=multi_tenant_mapping,
            llm_provider=provider,
            use_fewshot=False,
            use_entity_resolution=False,
            tenant_context=TENANT_CTX_KEY,
            max_retries=1,
        )
        assert result.method == "llm"
        assert "_key" in result.cypher
        assert "TENANT_ID" in result.cypher
        assert "$tenantId" in result.cypher
        assert provider.calls == 1

    def test_no_context_no_guardrail(self, multi_tenant_mapping) -> None:
        # Without a tenant context, guardrail is a no-op — users who
        # haven't opted in still get whatever Cypher the LLM emits.
        provider = _StubProvider("MATCH (u:GSuiteUser) RETURN u.NAME")
        result = nl_to_cypher(
            "list all GSuiteUsers",
            mapping=multi_tenant_mapping,
            llm_provider=provider,
            use_fewshot=False,
            use_entity_resolution=False,
            tenant_context=None,
            max_retries=1,
        )
        assert result.cypher == "MATCH (u:GSuiteUser) RETURN u.NAME"
        assert result.method == "llm"
        assert provider.calls == 1

    def test_blocks_rule_based_fallback_in_multitenant(
        self,
        multi_tenant_mapping,
    ) -> None:
        # With no LLM provider, the rule-based fallback would normally
        # run — but it cannot enforce tenant scoping, so we must fail
        # closed instead.
        result = nl_to_cypher(
            "At Dagster Labs list all GSuiteUsers",
            mapping=multi_tenant_mapping,
            use_llm=False,
            use_fewshot=False,
            use_entity_resolution=False,
            tenant_context=TENANT_CTX,
        )
        assert result.cypher == ""
        assert result.method == "tenant_guardrail_blocked"
