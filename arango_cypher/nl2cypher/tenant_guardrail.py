"""Tenant-scoping postcondition for NL → Cypher / AQL translation.

When the mapping bundle declares a ``Tenant`` entity and the caller
supplies an active :class:`TenantContext`, the emitted Cypher MUST be
scoped to that tenant. Otherwise the query silently escapes tenant
isolation and returns cross-tenant rows — a data-leak-class bug in a
multi-tenant graph, not a translation-quality nit.

What "scoped" means is **manifest-driven**, not hardcoded:

* If the query touches a tenant-scoped entity that carries a
  denormalised tenant field (e.g. ``Device.TENANT_ID``), filtering on
  that field with the active tenant value is sufficient — no
  ``:Tenant`` binding required, because the planner can satisfy the
  scope with an indexed equality on the column.
* If the query touches a tenant-scoped entity that has no denorm
  field, the only way to scope it is via traversal from a
  ``:Tenant`` node, and the guardrail enforces that.
* If the query touches **only** GLOBAL entities (e.g. ``Cve``,
  ``AppVersion``), the guardrail does not fire — those collections are
  intentionally cross-tenant reference data and forcing a
  ``:Tenant`` binding would refuse legitimate questions like "list
  all CVEs".

The classification of which entity is which comes from
:func:`arango_cypher.nl2cypher.tenant_scope.analyze_tenant_scope` —
keeping that knowledge in a separate module means we can replace the
heuristic with the upstream schema analyzer's first-class
``tenantScope`` annotation without churning this file.

The guardrail is invoked inside
:func:`arango_cypher.nl2cypher.nl_to_cypher` after every LLM emission.
A violation is fed back into the retry loop so the model can correct
itself; if the retry budget is exhausted the caller receives an
empty-Cypher result with an explanation — the translator
**fails closed**, never silently returning a cross-tenant query.

Back-compat note
----------------
``prompt_section`` and ``check_tenant_scope`` both accept a manifest
optionally. Callers that don't pass one fall through to the v1
behaviour (force a ``:Tenant`` binding, hardcoded ``TENANT_ID`` hint
in the prompt). New code should always pass a manifest — the
fallback exists only to keep the public API stable for downstream
consumers who haven't migrated yet.
"""

from __future__ import annotations

import builtins
import re
from dataclasses import dataclass
from typing import Any

from .tenant_ast_common import (
    TENANT_ID_BIND,
    TENANT_KEY_BIND,
    cypher_literal_value,
    is_bindvar_reference,
    is_literal_tenant_value,
)
from .tenant_scope import (
    EntityTenantRole,
    TenantScopeManifest,
)

# ---------------------------------------------------------------------------
# Violation codes (single source of truth so callers can branch on the
# machine-readable shape rather than substring-grepping ``reason``).
# ---------------------------------------------------------------------------

#: Existing pre-MT-2 violation: the Cypher binds no ``:Tenant`` and
#: does not filter a tenant-scoped entity by its denormalised tenant
#: field at all. Default code on a :class:`TenantScopeViolation`.
CODE_NO_TENANT_BINDING = "NO_TENANT_BINDING"

#: Wave 8a / MT-2 violation: the Cypher contains a denormalised
#: tenant predicate whose right-hand side is a literal tenant value
#: (or, under the fail-closed branch when ``manifest.known_tenant_keys``
#: is unset, *any* string literal). Bind-variable references to
#: :data:`tenant_ast_common.TENANT_ID_BIND` /
#: :data:`tenant_ast_common.TENANT_KEY_BIND` are the only accepted
#: proof of scope after this code lands. Layer 3 (MT-3) mechanically
#: rewrites legitimate denorm filters into the bind-var form, so a
#: literal that survives Layer 3 is evidence of a T2-class injection
#: probe at the LLM layer (PRD §5.2 enhancement #1).
CODE_LITERAL_TENANT_PREDICATE = "LITERAL_TENANT_PREDICATE"

# Match `:Tenant` as a standalone label; explicitly reject
# `:TenantUser`, `:TenantCVE`, `:TenantAppVersion`, etc.
_TENANT_LABEL_RE = re.compile(r":\s*Tenant\b(?!\w)")
_TENANT_ENTITY_NAMES = ("Tenant",)

# Capture every node-label occurrence `:LabelName` in a Cypher string.
# We intentionally use a permissive scan rather than a real parser:
# the guardrail runs on every LLM emission and on the retry hot path,
# so a stdlib regex beats spinning up the ANTLR parser. False
# positives (e.g. labels inside string literals) are tolerable
# because the worst case is a rejected query, which the retry loop
# can correct — strictly preferable to letting a real cross-tenant
# query through.
_LABEL_RE = re.compile(r":\s*([A-Za-z_][A-Za-z0-9_]*)")


@dataclass(frozen=True)
class TenantContext:
    """Ambient tenant scope applied to every NL translation in a session.

    Attributes
    ----------
    property:
        Physical property name on the ``Tenant`` entity used to
        match the scope. The canonical value is ``"_key"`` — the
        ArangoDB document key, which is unique within the
        collection, automatically indexed, and transpiles to the
        cheapest possible filter (``t._key == '<uuid>'``). Older
        clients / hand-supplied contexts may use ``"NAME"``,
        ``"SUBDOMAIN"``, or ``"TENANT_HEX_ID"`` — those still work
        but are slower, schema-specific, and not guaranteed unique.
    value:
        The exact value to match. For ``property="_key"`` this is
        the bare key (e.g. ``"001c463d-500d-47c7-bc32-c824eb42f064"``,
        not the full ``"Tenant/001c463d-..."`` ``_id``).
    display:
        Optional human-readable label for prompts and UI (e.g.
        ``"Dagster Labs"``). Defaults to ``value`` when omitted.
    """

    property: str
    value: str
    display: str | None = None

    @builtins.property  # the dataclass field `property` shadows the builtin name here
    def display_name(self) -> str:
        return self.display or self.value


@dataclass(frozen=True)
class TenantScopeViolation:
    """Diagnostic for a translation that dropped the tenant constraint.

    ``code`` is the machine-actionable shape of the violation. Today
    two codes exist:

    * :data:`CODE_NO_TENANT_BINDING` — the original Wave-4r contract:
      no ``:Tenant`` binding and no denormalised tenant predicate.
      The LLM dropped scoping entirely.
    * :data:`CODE_LITERAL_TENANT_PREDICATE` — Wave 8a / MT-2 (PRD
      §5.2 enhancement #1): the LLM wrote a denormalised tenant
      predicate using a string literal RHS. Always rejected — the
      session-bound bind variable
      (:data:`tenant_ast_common.TENANT_ID_BIND` /
      :data:`tenant_ast_common.TENANT_KEY_BIND`) is the only
      accepted shape so Layer 3's mechanical rewrite remains
      lossless and a hostile prompt cannot inject a tenant
      identifier as user-controlled data.

    Defaults to :data:`CODE_NO_TENANT_BINDING` to preserve the
    pre-MT-2 caller contract for the existing rejection path.

    ``physical_enforcement`` carries forward
    ``metadata.multitenancy.physicalEnforcement`` from the analyzer
    (>=0.6, upstream PRD §6.2 bullet 4). When ``True`` the underlying
    storage layer (smartgraph disjoint attribute, shard key, …)
    physically prevents cross-tenant reads, so a guardrail violation
    is a translation-quality concern, not a data-leak. When ``False``
    (or ``None`` for older analyzers / hand-built mappings), the
    guardrail is the *only* line of defense and a violation must be
    treated as a hard refusal — the retry loop already does this; the
    field is surfaced here so callers logging / alerting on
    violations can label them correctly.
    """

    tenant_property: str
    tenant_value: str
    reason: str
    suggested_hint: str
    physical_enforcement: bool | None = None
    code: str = CODE_NO_TENANT_BINDING


# ---------------------------------------------------------------------------
# Mapping introspection helpers (kept for back-compat with v1 callers)
# ---------------------------------------------------------------------------


def has_tenant_entity(bundle_or_dict: Any) -> bool:
    """Return ``True`` if the mapping declares a ``Tenant`` entity.

    Accepts either a :class:`~arango_query_core.mapping.MappingBundle`
    or a plain ``dict`` (in which case both ``conceptual_schema`` and
    ``conceptualSchema`` keys are tried).
    """
    cs: dict[str, Any] | None
    if hasattr(bundle_or_dict, "conceptual_schema"):
        cs = bundle_or_dict.conceptual_schema or {}
    elif isinstance(bundle_or_dict, dict):
        cs = bundle_or_dict.get("conceptual_schema") or bundle_or_dict.get("conceptualSchema") or {}
    else:
        return False
    if not isinstance(cs, dict):
        return False
    entities = cs.get("entities") or []
    if not isinstance(entities, list):
        return False
    names = {e.get("name") for e in entities if isinstance(e, dict) and isinstance(e.get("name"), str)}
    return any(n in names for n in _TENANT_ENTITY_NAMES)


def cypher_binds_tenant(cypher: str) -> bool:
    """Return ``True`` if any clause binds a ``:Tenant`` node."""
    return bool(_TENANT_LABEL_RE.search(cypher or ""))


def multitenancy_physical_enforcement(bundle_or_dict: Any) -> bool | None:
    """Return ``metadata.multitenancy.physicalEnforcement`` from a bundle.

    Consumes the analyzer (>=0.6) classification (upstream PRD §6.2
    bullet 4). Returns:

    * ``True`` — the deployment style enforces tenancy in storage
      (e.g. disjoint smartgraph, tenant-keyed shard key); the
      database physically prevents cross-tenant reads.
    * ``False`` — tenancy is by application convention only
      (``discriminator_field`` style); the guardrail is the only
      enforcement layer.
    * ``None`` — older analyzer version (no ``multitenancy`` block)
      OR ``style == "none"``; treat the same way as a hand-built
      mapping (no signal either way).
    """
    if hasattr(bundle_or_dict, "metadata"):
        meta = bundle_or_dict.metadata or {}
    elif isinstance(bundle_or_dict, dict):
        meta = bundle_or_dict.get("metadata") or {}
    else:
        return None
    if not isinstance(meta, dict):
        return None
    block = meta.get("multitenancy")
    if not isinstance(block, dict):
        return None
    style = block.get("style")
    if not isinstance(style, str) or style == "none":
        return None
    enforcement = block.get("physicalEnforcement")
    if isinstance(enforcement, bool):
        return enforcement
    return None


def cypher_referenced_labels(cypher: str) -> set[str]:
    """Return the set of node labels referenced in ``cypher``.

    Used by :func:`check_tenant_scope` to decide whether the query
    touches any tenant-scoped entity at all. Never raises — returns
    an empty set on garbage input.
    """
    if not cypher:
        return set()
    return {m.group(1) for m in _LABEL_RE.finditer(cypher)}


# ---------------------------------------------------------------------------
# Guardrail
# ---------------------------------------------------------------------------


# Canonical tenant bind-variable names accepted as scope proof on the
# RHS of a denormalised tenant predicate. Imported from the shared
# Wave-8-pre vocabulary so the three rewriters (Layer 2 / 3 / 4)
# never disagree on what a tenant bind-var is named — see
# ``tenant_ast_common`` for the rationale.
_ACCEPTED_TENANT_BIND_NAMES: frozenset[str] = frozenset({TENANT_ID_BIND, TENANT_KEY_BIND})


@dataclass(frozen=True)
class _DenormPredicateScan:
    """Outcome of scanning a Cypher string for predicates against a
    single denormalised tenant field.

    Attributes
    ----------
    bind_var_satisfied:
        ``True`` when the Cypher contains at least one predicate of
        the form ``x.<field> = $<bind_name>`` (or the inline
        property-map equivalent ``{<field>: $<bind_name>}``) whose
        bind name is one of the canonical tenant binds. This is the
        only acceptable proof of scope after MT-2.
    literal_values:
        Every literal RHS observed for the same field, in source
        order. The caller hands each one to
        :func:`tenant_ast_common.is_literal_tenant_value` to decide
        whether it constitutes a T2-class injection probe.
    """

    bind_var_satisfied: bool
    literal_values: tuple[str, ...]


def _scan_denorm_predicates(
    cypher: str,
    *,
    field_name: str,
) -> _DenormPredicateScan:
    """Inspect ``cypher`` for predicates against ``field_name``.

    Recognises the same syntactic shapes the LLM emits today —
    ``WHERE x.<field> = <rhs>``, ``WHERE x.<field> == <rhs>``, and the
    inline node-property form ``MATCH (x:Label {<field>: <rhs>})`` —
    where ``<rhs>`` is either a single/double-quoted string literal
    OR a Cypher bind-variable reference (``$<name>``). Quote style
    and whitespace are tolerated. Backticked field names are NOT
    matched (consistent with the pre-MT-2 helper this replaces; the
    schema analyzer never emits backtickable tenant field names in
    the field set Layer 2 sees).

    The scanner is deliberately permissive on the LHS (any
    ``identifier.<field>``) and strict on the RHS — the literal-vs-
    bind-var dichotomy is exactly the contract MT-2 ratchets up.
    The result is a structural classification, not a satisfaction
    verdict; the caller (:func:`check_tenant_scope`) decides what to
    do with literal RHSes by delegating to
    :func:`tenant_ast_common.is_literal_tenant_value`.
    """
    if not cypher or not field_name:
        return _DenormPredicateScan(bind_var_satisfied=False, literal_values=())

    f = re.escape(field_name)
    # ``x.<field> = <rhs>`` / ``x.<field> == <rhs>`` — RHS is either a
    # quoted literal (capture group 1 OR 2 depending on the quote
    # style) or a bind-var reference ``$<name>`` (capture group 3).
    eq_pattern = re.compile(
        rf"\.\s*{f}\s*={{1,2}}\s*(?:'([^']*)'|\"([^\"]*)\"|\$([A-Za-z_][A-Za-z0-9_]*))",
    )
    # ``{<field>: <rhs>}`` — same RHS shape as above.
    inline_pattern = re.compile(
        rf"\{{[^{{}}]*\b{f}\s*:\s*(?:'([^']*)'|\"([^\"]*)\"|\$([A-Za-z_][A-Za-z0-9_]*))",
    )

    bind_var_satisfied = False
    literal_values: list[str] = []

    for pattern in (eq_pattern, inline_pattern):
        for m in pattern.finditer(cypher):
            single, double, bindvar = m.group(1), m.group(2), m.group(3)
            if bindvar is not None:
                # Route through the shared bind-var matcher so a
                # future wire-format change (e.g. ArangoDB
                # introduces a new ``parameter`` node shape in
                # EXPLAIN plans) is fixed in exactly one place.
                synthetic_node = {"type": "parameter", "name": bindvar}
                if any(is_bindvar_reference(synthetic_node, name=n) for n in _ACCEPTED_TENANT_BIND_NAMES):
                    bind_var_satisfied = True
                # A non-tenant bind-var (``$departmentId`` etc.) is
                # not scope proof and not a literal — silently
                # ignored, identical to the pre-MT-2 behaviour for
                # an unrecognised RHS.
                continue
            if single is not None:
                literal_values.append(cypher_literal_value(f"'{single}'"))
            elif double is not None:
                literal_values.append(cypher_literal_value(f'"{double}"'))

    return _DenormPredicateScan(
        bind_var_satisfied=bind_var_satisfied,
        literal_values=tuple(literal_values),
    )


def check_tenant_scope(
    cypher: str,
    *,
    tenant_context: TenantContext | None,
    manifest: TenantScopeManifest | None = None,
    physical_enforcement: bool | None = None,
) -> TenantScopeViolation | None:
    """Return a :class:`TenantScopeViolation` if isolation was breached.

    Returns ``None`` in any of these cases:

    * ``tenant_context`` is ``None`` — the caller has not opted into
      tenant scoping, so there is no constraint to enforce.
    * ``manifest`` is provided and the Cypher references **only**
      entities classified as ``GLOBAL`` (e.g. metadata tables) — the
      query is intentionally cross-tenant and forcing a ``:Tenant``
      binding would refuse legitimate metadata-only queries.
    * The emitted Cypher binds a ``:Tenant`` node — assumed to be
      scoped correctly by traversal.
    * ``manifest`` is provided, the Cypher touches at least one
      tenant-scoped entity that carries a denormalised tenant field,
      AND the Cypher contains a predicate of the form
      ``x.<denorm_field> = $<bind>`` (or the inline property-map
      equivalent) where ``<bind>`` is one of the canonical tenant
      bind variables (:data:`tenant_ast_common.TENANT_ID_BIND` /
      :data:`tenant_ast_common.TENANT_KEY_BIND`). The planner
      satisfies the scope with an indexed equality at execution
      time, no traversal needed.

    A violation is returned in two distinct shapes (machine-readable
    on :attr:`TenantScopeViolation.code`):

    * :data:`CODE_LITERAL_TENANT_PREDICATE` (Wave 8a / MT-2) — the
      Cypher contains a denormalised tenant predicate whose RHS is
      a literal tenant value. This check fires BEFORE the
      ``:Tenant`` binding short-circuit because a literal tenant
      value alongside a legitimate ``:Tenant`` binding is still a
      T2-class injection probe; the binding does not absolve the
      literal.
    * :data:`CODE_NO_TENANT_BINDING` — the original Wave-4r
      contract: no scope satisfaction path matched.
    """
    if tenant_context is None:
        return None

    # Manifest-aware path: skip GLOBAL-only and accept denorm-filter scoping.
    if manifest is not None:
        labels = cypher_referenced_labels(cypher)
        if labels:
            roles = {manifest.role_of(label) for label in labels}
            if (
                roles
                and EntityTenantRole.TENANT_SCOPED not in roles
                and EntityTenantRole.TENANT_ROOT not in roles
            ):
                # Only GLOBAL entities are referenced. The query is
                # tenant-independent by construction (e.g.
                # `MATCH (c:Cve) RETURN c`). Allow.
                return None

        # Wave 8a / MT-2: walk every tenant-scoped label that carries
        # a denormalised tenant column and inspect its predicates.
        # Literal RHSes are rejected outright (T2 defence) BEFORE the
        # ``:Tenant`` binding short-circuit, so a hostile prompt that
        # smuggles in a literal alongside a legitimate ``:Tenant``
        # binding still surfaces as ``LITERAL_TENANT_PREDICATE`` —
        # otherwise the binding would mask the injection probe.
        bind_var_satisfied = False
        for label in labels:
            field_name = manifest.denorm_field_of(label)
            if not field_name:
                continue
            scan = _scan_denorm_predicates(cypher, field_name=field_name)
            for literal in scan.literal_values:
                if is_literal_tenant_value(literal, manifest):
                    return _build_literal_tenant_violation(
                        tenant_context=tenant_context,
                        physical_enforcement=physical_enforcement,
                        entity_label=label,
                        field_name=field_name,
                        literal_value=literal,
                    )
            if scan.bind_var_satisfied:
                bind_var_satisfied = True

        if bind_var_satisfied:
            return None

        if cypher_binds_tenant(cypher):
            return None
    elif cypher_binds_tenant(cypher):
        # No manifest: fall back to the v1 contract — a `:Tenant`
        # binding is the only acceptance condition.
        return None

    return _build_violation(cypher, tenant_context, manifest, physical_enforcement)


def _build_literal_tenant_violation(
    *,
    tenant_context: TenantContext,
    physical_enforcement: bool | None,
    entity_label: str,
    field_name: str,
    literal_value: str,
) -> TenantScopeViolation:
    """Render a :data:`CODE_LITERAL_TENANT_PREDICATE` violation.

    The reason / hint pair is shaped so the LLM-retry loop in
    :func:`arango_cypher.nl2cypher._core.nl_to_cypher` can feed it
    straight back as ``builder.retry_context`` and the model can
    correct itself in the next attempt — the bind-var spelling
    (``$tenantId`` for Cypher) is the literal text the rewrite
    target should adopt.

    The offending entity label and field name are surfaced in both
    ``reason`` (for human readers / audit logs) and ``suggested_hint``
    (for the LLM) so the user understands *which* predicate was
    refused. The literal value itself is included verbatim — these
    strings come from the LLM's own output, never from the request
    body, so reflecting them back is not an injection vector.
    """
    var = entity_label[0].lower() if entity_label else "x"
    suggested_hint = (
        f"Replace the literal `{var}.{field_name} = '{literal_value}'` "
        f"with the session-bound bind variable `{var}.{field_name} = "
        f"${TENANT_ID_BIND}`. Tenant identifiers are bind variables "
        f"({TENANT_ID_BIND} / {TENANT_KEY_BIND}); never compare a "
        "tenant column to a literal string."
    )
    return TenantScopeViolation(
        tenant_property=tenant_context.property,
        tenant_value=tenant_context.value,
        reason=(
            f"Tenant predicate on entity {entity_label!r} field "
            f"{field_name!r} compares against the literal "
            f"{literal_value!r} instead of a session-bound bind "
            "variable. Literal tenant values in generated Cypher are "
            "always rejected so a hostile prompt cannot smuggle in a "
            "tenant identifier as user-controlled data and Layer 3's "
            "AST rewrite remains lossless."
        ),
        suggested_hint=suggested_hint,
        physical_enforcement=physical_enforcement,
        code=CODE_LITERAL_TENANT_PREDICATE,
    )


def _build_violation(
    cypher: str,
    tenant_context: TenantContext,
    manifest: TenantScopeManifest | None,
    physical_enforcement: bool | None = None,
) -> TenantScopeViolation:
    """Render a violation with a hint tailored to the schema's denorm fields."""
    if tenant_context.property == "_key":
        match_pattern = f"(t:Tenant {{_key: '{tenant_context.value}'}})"
    else:
        match_pattern = f"(t:Tenant {{{tenant_context.property}: {tenant_context.value!r}}})"

    # If the manifest tells us at least one referenced entity has a
    # denorm field, suggest the cheaper denorm-filter form first.
    denorm_hint: str | None = None
    if manifest is not None:
        for label in cypher_referenced_labels(cypher):
            field_name = manifest.denorm_field_of(label)
            if field_name:
                denorm_hint = (
                    f"`MATCH ({label[0].lower()}:{label}) "
                    f"WHERE {label[0].lower()}.{field_name} = "
                    f"'{tenant_context.value}' …`"
                )
                break

    if denorm_hint is not None:
        suggested_hint = (
            "Either filter the target entity directly on its tenant-id "
            f"field, e.g. {denorm_hint}, OR bind the tenant in MATCH "
            f"and traverse to the target via the schema's tenant edges, "
            f"e.g. {match_pattern}<-[:…]-(target)."
        )
    else:
        suggested_hint = (
            "Bind the tenant in MATCH and traverse to the target entity "
            f"through its tenant-scoping path, e.g. {match_pattern}"
            "<-[:…]-(target). The schema does not expose a denormalised "
            "tenant field on the target collection, so a graph traversal "
            "from :Tenant is required."
        )

    return TenantScopeViolation(
        tenant_property=tenant_context.property,
        tenant_value=tenant_context.value,
        reason=(
            f"Query must be scoped to tenant {tenant_context.display_name!r} "
            f"(Tenant.{tenant_context.property}) but the translated Cypher "
            "neither binds a :Tenant node nor filters a tenant-scoped "
            "entity by its denormalised tenant field — it would return "
            "cross-tenant results."
        ),
        suggested_hint=suggested_hint,
        physical_enforcement=physical_enforcement,
    )


# ---------------------------------------------------------------------------
# Prompt section (manifest-driven)
# ---------------------------------------------------------------------------


def prompt_section(
    tenant_context: TenantContext | None,
    manifest: TenantScopeManifest | None = None,
) -> str:
    """Render the "## Current tenant scope" block for the system prompt.

    Returns the empty string when no tenant context is active so the
    prompt remains byte-identical to the pre-guardrail shape for
    single-tenant graphs.

    When a ``manifest`` is supplied, the block is fully data-driven:
    it lists the schema's tenant-scoped entities in two groups (those
    that can be filtered directly via a denormalised field, and those
    that require traversal from ``:Tenant``), and a third group of
    GLOBAL entities the model is told *not* to scope. This avoids the
    v1 footgun of telling the LLM "every collection has a TENANT_ID" —
    which it doesn't, and which led the model to invent fields and
    refuse legitimate metadata queries.

    Without a manifest, falls back to the v1 wording (kept for
    out-of-tree consumers that haven't migrated yet).
    """
    if tenant_context is None:
        return ""

    match_hint, scope_clause = _format_match_hint(tenant_context)

    if manifest is None:
        return _legacy_prompt_body(tenant_context, match_hint, scope_clause)

    return _manifest_prompt_body(
        tenant_context,
        manifest,
        match_hint,
        scope_clause,
    )


def _format_match_hint(
    tenant_context: TenantContext,
) -> tuple[str, str]:
    """Return ``(match_hint, scope_clause)`` strings for the prompt header."""
    if tenant_context.property == "_key":
        match_hint = (
            f"`MATCH (t:Tenant {{_key: '{tenant_context.value}'}})` — "
            "the `_key` property is the ArangoDB document key for the "
            "Tenant document and is the canonical, indexed tenant "
            "identifier."
        )
        scope_clause = f"match Tenant._key == {tenant_context.value!r}"
    else:
        match_hint = f"`MATCH (t:Tenant {{{tenant_context.property}: {tenant_context.value!r}}})`"
        scope_clause = f"match Tenant.{tenant_context.property} == {tenant_context.value!r}"
    return match_hint, scope_clause


def _manifest_prompt_body(
    tenant_context: TenantContext,
    manifest: TenantScopeManifest,
    match_hint: str,
    scope_clause: str,
) -> str:
    # Group scoped entities by whether they have a denorm field.
    denorm_entries: list[tuple[str, str]] = []  # (entity, field)
    traversal_entries: list[str] = []
    for name in sorted(manifest.scoped_entities()):
        field_name = manifest.denorm_field_of(name)
        if field_name:
            denorm_entries.append((name, field_name))
        else:
            traversal_entries.append(name)
    global_entries = sorted(manifest.global_entities())

    lines: list[str] = [
        "## Current tenant scope",
        f"Active tenant: {tenant_context.display_name!r} ({scope_clause}).",
        "",
        (
            "Per-entity scoping rules (derived from the mapping; do NOT "
            "invent fields that are not listed below):"
        ),
        "",
    ]

    if denorm_entries:
        # The denorm field stores the Tenant `_key`, not whatever
        # property the operator chose to scope by. When the active
        # context is keyed on `_key` we can substitute the literal;
        # otherwise we use a `<Tenant._key>` placeholder so the LLM
        # knows it must first resolve the key (typically by also
        # binding :Tenant) rather than splicing in the wrong value
        # (e.g. a TENANT_HEX_ID).
        if tenant_context.property == "_key":
            denorm_value_repr = f"'{tenant_context.value}'"
        else:
            denorm_value_repr = "<Tenant._key>"
        lines.append(
            "**Tenant-scoped via denormalised field** — prefer a direct "
            "filter on the listed field (cheap, indexed, no traversal):"
        )
        for entity, field_name in denorm_entries:
            var = entity[0].lower()
            example = (
                f"  - `{entity}`: filter on `{field_name}` — "
                f"`MATCH ({var}:{entity}) WHERE "
                f"{var}.{field_name} = {denorm_value_repr}`"
            )
            lines.append(example)
        lines.append("")

    if traversal_entries:
        lines.append(
            "**Tenant-scoped via traversal only** — these entities have no "
            "denormalised tenant field; reach them by binding :Tenant and "
            "traversing the schema's edges:"
        )
        lines.append("  - " + ", ".join(traversal_entries))
        lines.append(f"  - Bind :Tenant first using {match_hint}")
        lines.append("")

    if global_entries:
        lines.append(
            "**Global / metadata (do NOT scope)** — these entities are "
            "intentionally cross-tenant. Querying them must NOT include "
            "any tenant filter or :Tenant binding:"
        )
        lines.append("  - " + ", ".join(global_entries))
        lines.append("")

    lines.append(
        "Do NOT mix scope styles in a single MATCH (don't bind :Tenant by "
        "`_key` and then re-filter the target by some other tenant-ish "
        "field — pick one consistent identifier)."
    )
    # Wave 8a / MT-2 (PRD §5.2 enhancement #1): the literal form is
    # mechanically rejected by the postcheck — Layer 3 will rewrite
    # the bind-var shape into the executable AQL, so the model must
    # never compare a tenant column to a string literal. The two-line
    # wrap matches the existing rule-line style; the test
    # ``test_prompt_section_contains_literal_rule`` pins both lines.
    lines.append("- Tenant identifiers are bind variables (@tenantId, @tenantKey).")
    lines.append("  Never compare a tenant column to a literal string.")
    return "\n".join(lines)


def _legacy_prompt_body(
    tenant_context: TenantContext,
    match_hint: str,
    scope_clause: str,
) -> str:
    """v1 prompt body (no manifest available).

    Kept verbatim so out-of-tree callers that don't yet pass a
    manifest get the same wording they had before — this preserves
    the byte-identical-prompt invariant pinned in
    ``test_no_tenant_context_leaves_prompt_byte_identical``.
    """
    body = (
        "## Current tenant scope\n"
        f"All queries MUST be scoped to tenant "
        f"{tenant_context.display_name!r} ({scope_clause}).\n"
        "Every MATCH clause must include a :Tenant node bound to this "
        f"value: {match_hint}\n\n"
        "Bind the tenant in MATCH and traverse to the target entity via "
        "the schema's tenant-scoping relationship (e.g. "
        "`(:Tenant)<-[:TENANTUSERTENANT]-(:TenantUser)<-[:GSUITEUSERTENANTUSER]-"
        "(target)`)."
    )
    return body


__all__ = [
    "CODE_LITERAL_TENANT_PREDICATE",
    "CODE_NO_TENANT_BINDING",
    "TenantContext",
    "TenantScopeViolation",
    "check_tenant_scope",
    "cypher_binds_tenant",
    "cypher_referenced_labels",
    "has_tenant_entity",
    "multitenancy_physical_enforcement",
    "prompt_section",
]
