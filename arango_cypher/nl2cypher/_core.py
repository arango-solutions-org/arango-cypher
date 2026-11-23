"""NL→Cypher core pipeline: schema summarization, prompt building, rule-based fallback."""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

from arango_query_core.mapping import MappingBundle
from arango_query_core.nl.providers import (
    LLMProvider,
    get_llm_provider,
)

from .postconditions import (
    Postcondition,
    postcondition_prompt_sections,
)
from .tenant_guardrail import TenantContext
from .tenant_guardrail import prompt_section as _tenant_prompt_section
from .tenant_scope import (
    TenantScopeManifest,
    analyze_tenant_scope,
)

if TYPE_CHECKING:
    from arango_query_core.nl.fewshot import FewShotIndex

    from .entity_resolution import EntityResolver

logger = logging.getLogger(__name__)


@dataclass
class NL2CypherResult:
    """Result of a natural language to Cypher translation."""

    cypher: str
    explanation: str = ""
    confidence: float = 0.0
    method: str = "rule_based"
    schema_context: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    retries: int = 0
    cached_tokens: int = 0
    """Prompt tokens served from the provider's prefix cache (WP-25.4).

    Populated when the LLM provider surfaces a ``cached_tokens`` field
    on its usage payload (OpenAI's ``prompt_tokens_details.cached_tokens``,
    Anthropic's ``cache_read_input_tokens``, …).  ``0`` for rule-based
    results and for providers that don't expose cache telemetry.
    """
    advisories: list[dict[str, Any]] = field(default_factory=list)
    """ArangoSearch/inverted-index advisories (WP-S3c).

    Each entry is an :meth:`IndexAdvisory.as_dict` payload
    (``{collection, field, reason, suggestedIndex}``) recorded when the
    entity resolver ran a fuzzy name probe against a field with no
    inverted/ArangoSearch coverage — i.e. a full collection scan. The
    service forwards these so the UI can offer one-click index creation.
    Empty for rule-based / literal / no-mapping results.
    """


def _property_quality_hint(prop_meta: dict[str, Any] | None) -> str:
    """Render a compact data-quality hint suffix for a property.

    Returns a string like ``" [sentinels: 'NULL', 'N/A'; numeric-like]"`` when
    the physical mapping carries sentinel/numeric-like metadata, otherwise
    the empty string.
    """
    if not isinstance(prop_meta, dict):
        return ""
    parts: list[str] = []
    sentinels = prop_meta.get("sentinelValues") or prop_meta.get("sentinel_values")
    if isinstance(sentinels, list | tuple) and sentinels:
        quoted = ", ".join(f"'{s}'" for s in list(sentinels)[:3])
        parts.append(f"sentinels: {quoted}")
    if prop_meta.get("numericLike") or prop_meta.get("numeric_like"):
        parts.append("numeric-like string")
    # WP-S2a: value-shape signal. When the analyzer reports the *shape* of a
    # property's values (e.g. a short ticker/code rather than a legal name) and/
    # or example values, surface them so the LLM filters against the real value
    # form instead of inventing a full legal name for a token-shaped field.
    shape = prop_meta.get("valueShape") or prop_meta.get("value_shape")
    if isinstance(shape, str) and shape.strip():
        parts.append(f"shape: {shape.strip()}")
    examples = (
        prop_meta.get("examples")
        or prop_meta.get("exampleValues")
        or prop_meta.get("sampleValues")
        or prop_meta.get("sample_values")
    )
    if isinstance(examples, list | tuple) and examples:
        quoted = ", ".join(f'"{e}"' for e in list(examples)[:3])
        parts.append(f"e.g. {quoted}")
    if not parts:
        return ""
    return f" [{'; '.join(parts)}]"


def _pm_entity_props(label: str, pm: dict[str, Any]) -> dict[str, dict[str, Any]]:
    if isinstance(pm.get("entities"), dict):
        ent = pm["entities"].get(label, {})
        if isinstance(ent, dict):
            props = ent.get("properties", {})
            if isinstance(props, dict):
                return {k: v for k, v in props.items() if isinstance(v, dict)}
    return {}


def _pm_relationship_props(rtype: str, pm: dict[str, Any]) -> dict[str, dict[str, Any]]:
    if isinstance(pm.get("relationships"), dict):
        rel = pm["relationships"].get(rtype, {})
        if isinstance(rel, dict):
            props = rel.get("properties", {})
            if isinstance(props, dict):
                return {k: v for k, v in props.items() if isinstance(v, dict)}
    return {}


def _flagged_properties(
    labeled_props: dict[str, dict[str, dict[str, Any]]],
) -> list[tuple[str, str, dict[str, Any]]]:
    """Flatten (label, propName, meta) for properties carrying quality hints."""
    flagged: list[tuple[str, str, dict[str, Any]]] = []
    for label, props in labeled_props.items():
        for pname, meta in props.items():
            if not isinstance(meta, dict):
                continue
            has_sentinel = bool(meta.get("sentinelValues") or meta.get("sentinel_values"))
            has_numeric = bool(meta.get("numericLike") or meta.get("numeric_like"))
            if has_sentinel or has_numeric:
                flagged.append((label, pname, meta))
    return flagged


_DATA_QUALITY_BLOCK_CYPHER = (
    "\nData-quality hints:\n"
    "  - When a property is marked 'sentinels: ...', the column uses string "
    "placeholder(s) for missing values (e.g. the literal text 'NULL'). These "
    "are NOT real nulls — exclude them in WHERE clauses, for example "
    "`WHERE t.COMPANY_SIZE <> 'NULL' AND t.COMPANY_SIZE IS NOT NULL`.\n"
    "  - When a property is marked 'numeric-like string', cast to number "
    "before ordering or comparing numerically, e.g. "
    "`toInteger(t.COMPANY_SIZE)` or `toFloat(t.AMOUNT)`.\n"
    "  - For 'top-N by numeric field X', combine both: filter out the "
    "sentinels, then ORDER BY the cast numeric value."
)


_VALUE_SHAPE_BLOCK_CYPHER = (
    "\nValue-shape hints:\n"
    "  - When a property shows 'shape: ...' or 'e.g. \"...\"', match against "
    "that value form. Do NOT invent a full legal/long name for a token- or "
    'code-shaped field (e.g. filter `WHERE o.name = "cinf"`, not '
    '`"Cincinnati Financial Corporation"`).\n'
    "  - If unsure of the exact stored value, prefer a case-insensitive or "
    "CONTAINS match over a brittle exact equality."
)


def _has_value_shape_hints(
    labeled_props: dict[str, dict[str, dict[str, Any]]],
) -> bool:
    """True when any property carries a valueShape / example-values signal."""
    for props in labeled_props.values():
        for meta in props.values():
            if not isinstance(meta, dict):
                continue
            if meta.get("valueShape") or meta.get("value_shape"):
                return True
            if (
                meta.get("examples")
                or meta.get("exampleValues")
                or meta.get("sampleValues")
                or meta.get("sample_values")
            ):
                return True
    return False


# Pre-WP-29 the schema card emitted raw labels like ``Node :Compliance.rst``
# and the LLM faithfully copied them back — producing Cypher that fails
# ANTLR parse at ``:Compliance.`` because ``oC_SymbolicName`` requires
# backtick-escaping for any character outside ``[A-Za-z_][A-Za-z0-9_]*``.
# ``_escape_label`` wraps a label in backticks only when its name actually
# contains non-identifier characters. Bare identifiers (``Person``,
# ``Movie``, ``BELONGS_TO``) are returned unchanged, which preserves the
# byte-stable zero-shot prompt that provider-side prefix caching depends
# on.
_SYMBOLIC_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _escape_label(name: str) -> str:
    """Backtick-escape a Cypher label / relationship type iff it is not a bare identifier."""
    if not name:
        return name
    return name if _SYMBOLIC_NAME_RE.match(name) else f"`{name}`"


# Human-readable, conceptual descriptions of each sharding style emitted by
# arangodb-schema-analyzer>=0.5 as `metadata.shardingProfile.style` (see the
# upstream PRD §6.2 bullet 3, also tracked in
# docs/schema_analyzer_issues/08-emit-physical-layout-and-shard-topology.md).
# We deliberately keep these descriptions at the *semantic* level — no
# physical collection names, shard attributes, or graph names leak into the
# LLM prompt (§1.2 no-physical-mapping contract).
_DEPLOYMENT_STYLE_HINTS: dict[str, str] = {
    "OneShard": (
        "Deployment: single-shard database. "
        "All data lives together; no cross-shard fanout concerns for query plans."
    ),
    "SmartGraph": (
        "Deployment: sharded graph with a shared partitioning attribute. "
        "Traversals that hold that attribute constant stay shard-local."
    ),
    "DisjointSmartGraph": (
        "Deployment: multi-tenant graph with hard isolation between tenants. "
        "Queries must be scoped to a single tenant — a traversal cannot cross "
        "tenant boundaries."
    ),
    "SatelliteGraph": (
        "Deployment: satellite graph, fully replicated on every DB-Server. "
        "Traversals run locally without shard fanout."
    ),
    "Sharded": ("Deployment: sharded database. Queries that filter by the shard key early are preferred."),
}


def _deployment_style_hint(bundle: MappingBundle) -> str | None:
    """Render a one-line conceptual hint describing the deployment style.

    Consumes ``metadata.shardingProfile.style`` emitted by
    ``arangodb-schema-analyzer>=0.5`` (upstream PRD §6.2 bullet 3). Returns
    ``None`` when:

    * the analyzer version predates ``shardingProfile`` emission, or
    * the profile is degraded (``status == "degraded"``) — we prefer to
      say nothing over giving the LLM a hint based on incomplete evidence, or
    * the style is unknown to this module (forward-compat: a future
      analyzer release may add a new style we don't yet recognise).

    The returned string is intentionally **semantic**, not physical: it
    describes the *consequence* of the deployment for query planning
    (shard-local traversal, tenant isolation, etc.), not the underlying
    shard keys or collection names.
    """
    meta = bundle.metadata if isinstance(bundle.metadata, dict) else {}
    profile = meta.get("shardingProfile")
    if not isinstance(profile, dict):
        return None
    status = profile.get("status") or meta.get("shardingProfileStatus")
    if status == "degraded":
        return None
    style = profile.get("style")
    if not isinstance(style, str):
        return None
    hint = _DEPLOYMENT_STYLE_HINTS.get(style)
    if hint is None:
        return None
    return f"  {hint}"


def _shard_families_block(bundle: MappingBundle) -> str | None:
    """Render a shard-family summary for the LLM prompt.

    Consumes ``physicalMapping.shardFamilies`` emitted by
    ``arangodb-schema-analyzer>=0.6`` (upstream PRD §6.2 bullet 5 /
    downstream issue #08 §2). A *shard family* is a set of conceptual
    entities that share an identical property set and a common
    CamelCase / snake_case suffix — typically the per-source /
    per-repo / per-stream collection-duplication pattern (e.g.
    ``IBEXDocument`` / ``MAROCCHINODocument`` / ``MOR1KXDocument`` /
    ``OR1200Document`` ⇒ family ``Document``).

    Returns ``None`` when:

    * the analyzer version predates ``shardFamilies`` emission, or
    * the mapping has no families, or
    * every family resolves to fewer than two valid members.

    The returned string lists each family by its *conceptual* member
    labels and tells the LLM that a discriminator-agnostic question
    must UNION across all members instead of silently picking one
    member alphabetically (the IBEX/MAROCCHINO/MOR1KX/OR1200
    first-in-summary bias, defect D7 in
    ``docs/schema_inference_bugfix_prd.md``). Per §1.2, only
    conceptual entity names — never physical collection names — leak
    into the prompt; the underlying ``collectionName`` field on each
    member is intentionally ignored here.
    """
    pm = bundle.physical_mapping if isinstance(bundle.physical_mapping, dict) else {}
    families = pm.get("shardFamilies")
    if not isinstance(families, list) or not families:
        return None

    rendered: list[str] = []
    for fam in families:
        if not isinstance(fam, dict):
            continue
        name = fam.get("name")
        members = fam.get("members") or []
        if not isinstance(name, str) or not name:
            continue
        if not isinstance(members, list):
            continue
        member_labels: list[str] = []
        for m in members:
            if not isinstance(m, dict):
                continue
            ent = m.get("entity")
            if isinstance(ent, str) and ent:
                member_labels.append(ent)
        if len(member_labels) < 2:
            continue

        disc = fam.get("discriminator") if isinstance(fam.get("discriminator"), dict) else {}
        disc_field = disc.get("field") if isinstance(disc, dict) else None
        if isinstance(disc_field, str) and disc_field:
            scope_hint = f"Filter on `{disc_field}` to target one member; otherwise UNION across all members."
        else:
            scope_hint = (
                "Pick one member if the question targets a single source; otherwise UNION across all members."
            )
        rendered.append(
            f"  Family `{_escape_label(name)}` — members: "
            + ", ".join(_escape_label(label) for label in member_labels)
        )
        rendered.append(f"    {scope_hint}")

    if not rendered:
        return None

    header = (
        "Shard families (parallel labels sharing one logical schema; "
        "questions that don't pin a specific member must consider all of them):"
    )
    return "\n".join([header, *rendered])


def _build_schema_summary(bundle: MappingBundle) -> str:
    """Build a conceptual-only schema description for LLM context.

    Per §1.2, the LLM prompt contains only conceptual labels, relationship
    types, properties (by conceptual name), and domain/range — never
    physical collection names, mapping styles, typeField/typeValue, physical
    field names, or other physical mapping details.  The LLM generates
    pure Cypher against the ontology; the transpiler handles the physical
    mapping.

    Data-quality hints (string sentinels, numeric-like string columns) are
    included in the property listing so the LLM can emit the right filters
    and casts.

    Labels and relationship types containing characters outside
    ``[A-Za-z_][A-Za-z0-9_]*`` are pre-escaped with backticks here (via
    :func:`_escape_label`) so the LLM sees syntactically-valid Cypher in
    the schema card and can copy it verbatim. Bare identifiers are
    unchanged, preserving byte-identity with the pre-WP-29 prompt.
    """
    cs = bundle.conceptual_schema
    pm = bundle.physical_mapping
    lines: list[str] = ["Graph schema (Cypher labels and relationship types):"]

    deployment_hint = _deployment_style_hint(bundle)
    if deployment_hint:
        lines.append(deployment_hint)

    families_block = _shard_families_block(bundle)
    if families_block:
        lines.append(families_block)

    entities_emitted: list[str] = []

    def _format_entity(label: str, prop_names: list[str]) -> str:
        pm_props = _pm_entity_props(label, pm)
        parts: list[str] = []
        for name in prop_names[:8]:
            hint = _property_quality_hint(pm_props.get(name))
            parts.append(f"{name}{hint}")
        prop_str = ", ".join(parts) if parts else "no properties"
        return f"  Node :{_escape_label(label)} ({prop_str})"

    cs_entities = cs.get("entities", [])
    cs_entity_types = cs.get("entityTypes", [])
    if isinstance(cs_entities, list) and cs_entities and isinstance(cs_entities[0], dict):
        for e in cs_entities:
            name = e.get("name", "")
            props = [p.get("name", "") for p in e.get("properties", []) if isinstance(p, dict)]
            if not props:
                props = _conceptual_props_for(name, cs, pm)
            lines.append(_format_entity(name, props))
            entities_emitted.append(name)
    elif isinstance(cs_entity_types, list) and cs_entity_types:
        for name in cs_entity_types:
            props = _conceptual_props_for(name, cs, pm)
            lines.append(_format_entity(name, props))
            entities_emitted.append(name)

    if not entities_emitted and isinstance(pm.get("entities"), dict):
        for name in pm["entities"]:
            props = _conceptual_props_for(name, cs, pm)
            lines.append(_format_entity(name, props))
            entities_emitted.append(name)

    cs_rels = cs.get("relationships", [])
    cs_rel_types = cs.get("relationshipTypes", [])
    if isinstance(cs_rels, list) and cs_rels and isinstance(cs_rels[0], dict):
        for r in cs_rels:
            rtype = r.get("type", "")
            from_e = r.get("fromEntity", "?")
            to_e = r.get("toEntity", "?")
            rprops = [
                p.get("name", "") for p in r.get("properties", []) if isinstance(p, dict) and p.get("name")
            ]
            pm_rprops = _pm_relationship_props(rtype, pm)
            formatted = [f"{n}{_property_quality_hint(pm_rprops.get(n))}" for n in rprops]
            prop_str = f" [{', '.join(formatted)}]" if formatted else ""
            lines.append(
                f"  (:{_escape_label(from_e)})-[:{_escape_label(rtype)}{prop_str}]->(:{_escape_label(to_e)})"
            )
    elif isinstance(cs_rel_types, list) and cs_rel_types:
        for rtype in cs_rel_types:
            from_e, to_e = _conceptual_domain_range(rtype, cs, pm)
            lines.append(f"  (:{_escape_label(from_e)})-[:{_escape_label(rtype)}]->(:{_escape_label(to_e)})")
    elif isinstance(pm.get("relationships"), dict):
        for rtype in pm["relationships"]:
            from_e, to_e = _conceptual_domain_range(rtype, cs, pm)
            lines.append(f"  (:{_escape_label(from_e)})-[:{_escape_label(rtype)}]->(:{_escape_label(to_e)})")

    labeled_ent_props = {label: _pm_entity_props(label, pm) for label in entities_emitted}
    if _flagged_properties(labeled_ent_props):
        lines.append(_DATA_QUALITY_BLOCK_CYPHER)
    if _has_value_shape_hints(labeled_ent_props):
        lines.append(_VALUE_SHAPE_BLOCK_CYPHER)

    return "\n".join(lines)


def _conceptual_props_for(
    label: str,
    cs: dict[str, Any],
    pm: dict[str, Any],
) -> list[str]:
    """Return property names for a conceptual label (max 8).

    Prefers conceptual schema properties; falls back to physical mapping
    property *names* (which are conceptual property names, not field names).
    """
    for e in cs.get("entities", []):
        if isinstance(e, dict) and e.get("name") == label:
            props = [p.get("name", "") for p in e.get("properties", []) if isinstance(p, dict)]
            if props:
                return props[:8]

    if isinstance(pm.get("entities"), dict):
        pme = pm["entities"].get(label, {})
        if isinstance(pme, dict):
            return list(pme.get("properties", {}).keys())[:8]
    return []


def _conceptual_domain_range(
    rtype: str,
    cs: dict[str, Any],
    pm: dict[str, Any],
) -> tuple[str, str]:
    """Return (domain, range) for a relationship using conceptual metadata."""
    for r in cs.get("relationships", []):
        if isinstance(r, dict) and r.get("type") == rtype:
            return r.get("fromEntity", "?"), r.get("toEntity", "?")
    if isinstance(pm.get("relationships"), dict):
        pmr = pm["relationships"].get(rtype, {})
        if isinstance(pmr, dict):
            return pmr.get("domain", "?"), pmr.get("range", "?")
    return "?", "?"


_SYSTEM_PROMPT = """You are a Cypher query expert. Given a natural language question and a graph schema, generate a valid Cypher query.

Rules:
- Use only node labels and relationship types from the schema
- Use property names from the schema
- Return a single Cypher query (no explanation)
- Use standard Cypher syntax (MATCH, WHERE, RETURN, ORDER BY, LIMIT, etc.)
- For counts, use count()
- For aggregations, use collect(), sum(), avg(), min(), max()
- Labels and relationship types containing characters other than ASCII letters, digits, and underscore must be wrapped in backticks, e.g. MATCH (d:`Compliance.rst`) RETURN d.doc_version. The schema below has already pre-escaped such names; copy them verbatim.
- Wrap the query in ```cypher``` code block

{schema}"""


_RETRY_USER_SUFFIX = "\n\nYour previous Cypher was invalid: {error}. Please fix it."

# ArangoDB EXPLAIN reports a reused variable as "variable 'x' is assigned
# multiple times". The most common LLM cause is reusing a path variable
# (``MATCH p = … -> (p)``) as a node/relationship variable, which the model
# tends to repeat across plain retries. Detect it and append a precise repair.
_MULTI_ASSIGN_RE = re.compile(r"variable '([^']+)' is assigned multiple times")


def _augment_explain_hint(explain_err: str) -> str:
    """Return a targeted repair hint for known EXPLAIN errors, else ``""``.

    Deterministic so the same actionable instruction is fed on every retry,
    rather than relying on the model to infer the fix from the raw error.
    """
    m = _MULTI_ASSIGN_RE.search(explain_err or "")
    if not m:
        return ""
    var = m.group(1)
    return (
        f" Hint: the variable `{var}` is bound more than once. This usually means "
        f"a path variable (`MATCH {var} = …`) was reused as a node or "
        f"relationship variable in the same pattern. Use a UNIQUE path variable "
        f"named `path` and keep every node/relationship variable distinct — e.g. "
        f"`MATCH path = (a)-[:REL]->(b) RETURN path`, never "
        f"`MATCH {var} = (a)-[:REL]->({var})`."
    )


@dataclass
class PromptBuilder:
    """Composable prompt builder for the NL→Cypher pipeline.

    Renders the ``system`` and ``user`` messages of an NL→Cypher prompt.
    :func:`nl_to_cypher` uses :meth:`render_system` (tenant scope and graph
    intent, no examples or entities) as the engine's grammar section; the
    engine adds the examples, resolved entities and postcondition rules, and
    the caller's hint in :data:`_RETRY_USER_SUFFIX`'s wording.
    Per PRD §1.2, only *conceptual* schema text ever reaches this builder —
    collection names, type discriminators, and AQL stay out of the prompt.

    Invariants
    ----------
    * Zero-shot case (empty ``few_shot_examples`` and ``resolved_entities``):
      ``render_system()`` is byte-identical to the pre-refactor rendering
      of the :data:`_SYSTEM_PROMPT` template against ``schema_summary``.
      This is pinned by :mod:`tests.test_nl2cypher_prompt_builder`.
    * ``retry_context`` never alters the system message; it is appended to
      the user message only, matching the historical retry wording.
    """

    schema_summary: str
    few_shot_examples: list[tuple[str, str]] = field(default_factory=list)
    resolved_entities: list[str] = field(default_factory=list)
    tenant_context: TenantContext | None = None
    tenant_manifest: TenantScopeManifest | None = None
    retry_context: str = ""
    graph_intent: bool = False
    postconditions: tuple[Postcondition, ...] = ()

    def render_system(self) -> str:
        base = _SYSTEM_PROMPT.replace("{schema}", self.schema_summary)

        extensions: list[str] = []
        tenant_block = _tenant_prompt_section(
            self.tenant_context,
            self.tenant_manifest,
        )
        if tenant_block:
            # Order: tenant scope first so it's visually prominent and
            # the LLM reads it before few-shot examples (which, absent
            # a tenant-scoped example in the corpus, may otherwise
            # dominate the output shape).
            extensions.append(tenant_block)
        # Caller invariants sit with the tenant block: both are rules the model
        # must satisfy, as opposed to examples or resolved entities. Rendered
        # once per call — PromptBuilder is constructed outside the retry loop,
        # so the prefix stays byte-stable across attempts.
        extensions.extend(postcondition_prompt_sections(self.postconditions))
        if self.graph_intent:
            # Appended (not baked into the cacheable prefix) so it only costs
            # tokens when the question actually asks for a graph.
            extensions.append(self._render_graph_intent_section())
        if self.few_shot_examples:
            extensions.append(self._render_few_shot_section())
        if self.resolved_entities:
            # TODO(WP-25.2): entity-resolution plug-in point — ER wave
            # will pre-resolve mentions in the question to canonical
            # conceptual entities before prompting.
            extensions.append(self._render_resolved_entities_section())

        if not extensions:
            return base
        return base + "\n\n" + "\n\n".join(extensions)

    def render_user(self, question: str) -> str:
        if not self.retry_context:
            return question
        return question + _RETRY_USER_SUFFIX.format(error=self.retry_context)

    def _render_few_shot_section(self) -> str:
        lines = ["## Examples"]
        for nl, cypher in self.few_shot_examples:
            lines.append(f"Q: {nl}")
            lines.append("```cypher")
            lines.append(cypher.strip())
            lines.append("```")
        return "\n".join(lines)

    def _render_graph_intent_section(self) -> str:
        return (
            "## Output shape: return a graph\n"
            "The user asked to see the result AS A GRAPH (a visualization of "
            "nodes and relationships). Bind the matched pattern to a dedicated "
            "PATH variable and return that path, so the client can render nodes "
            "and edges. Do NOT return only scalar properties.\n"
            "Rules:\n"
            "- Name the path variable `path`. It MUST be distinct from every "
            "node and relationship variable — NEVER reuse it as a node name. "
            "Write `MATCH path = (a)-[:REL]->(b)`, never "
            "`MATCH p = (a)-[:REL]->(p)` (reusing the variable is a syntax "
            "error: a variable is assigned twice).\n"
            "- Return the path variable (or the bound nodes and relationships), "
            "not a scalar like `b.name`.\n"
            "- Match named entities TOLERANTLY (fuzzy), not with a brittle exact "
            "`{name: '...'}`: use a case-insensitive substring match such as "
            "`WHERE toLower(a.name) CONTAINS toLower('cinf')`, or match a "
            "ticker/symbol/id field exactly when the user gave one (e.g. "
            "`(a {id: 'CINF'})`). Do NOT invent a full legal name.\n"
            "Example:\n"
            "```cypher\n"
            "MATCH path = (a)-[:REL]->(b)\n"
            "WHERE toLower(a.name) CONTAINS toLower('acme')\n"
            "RETURN path\n"
            "LIMIT 50\n"
            "```\n"
            "If multiple hops are implied, return the full path "
            "(`MATCH path = (a)-[*1..3]->(b) RETURN path`)."
        )

    def _render_resolved_entities_section(self) -> str:
        lines = [
            "## Resolved entities (GROUND TRUTH from the live database)",
            "Each mention below was fuzzy-matched to a REAL value that exists in "
            "the database. You MUST anchor on these — they override your own "
            "guesses:",
            "- Use the EXACT label shown as the node label (e.g. `(a:ORG)`), not "
            "a different or more specific label.",
            "- Filter on the EXACT property and value shown "
            '(e.g. `WHERE a.name == "cinf"` or `(a {name: "cinf"})`).',
            "- Do NOT invent, expand, or 'correct' the value (never turn "
            '"cinf" into "cincinnati financial corporation").',
            "- When several rows resolve the same mention, prefer the highest similarity.",
            "",
            "Resolutions:",
        ]
        for entry in self.resolved_entities:
            lines.append(f"- {entry}")
        return "\n".join(lines)


def _extract_cypher_from_response(text: str) -> str:
    """Extract Cypher query from LLM response (handles code blocks)."""
    m = re.search(r"```(?:cypher)?\s*\n(.*?)```", text, re.DOTALL)
    if m:
        return m.group(1).strip()
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    cypher_lines = []
    for line in lines:
        upper = line.upper()
        if any(
            kw in upper
            for kw in (
                "MATCH",
                "RETURN",
                "WHERE",
                "WITH",
                "OPTIONAL",
                "ORDER",
                "LIMIT",
                "UNWIND",
                "CREATE",
                "SET",
                "DELETE",
            )
        ):
            cypher_lines.append(line)
        elif cypher_lines:
            cypher_lines.append(line)
    return "\n".join(cypher_lines) if cypher_lines else text.strip()


def _validate_cypher(cypher: str) -> tuple[bool, str]:
    """Syntactic check using the ANTLR parser.

    Returns ``(ok, error_message)``.  On success *error_message* is empty.
    Falls back to a keyword heuristic when the parser is unavailable.
    """
    if not cypher or not cypher.strip():
        return False, "empty Cypher string"
    try:
        from arango_cypher.parser import parse_cypher

        parse_cypher(cypher)
        return True, ""
    except Exception as exc:
        return False, str(exc)


def _validate_via_explain(
    cypher: str,
    *,
    mapping: MappingBundle | None,
    db: Any | None,
) -> tuple[bool, str]:
    """Execution-grounded validation via :func:`explain_aql` (WP-25.3).

    Returns ``(True, "")`` in two cases: the validation step is skipped
    (no *db* or *mapping*), or EXPLAIN succeeded.  When either the
    Cypher→AQL transpile step or EXPLAIN itself fails, returns
    ``(False, short_error_message)`` suitable for LLM feedback.

    Translation errors are surfaced separately from EXPLAIN errors so
    the LLM can tell whether it broke the Cypher shape (transpile) or
    merely hallucinated a collection / property name (EXPLAIN).
    """
    if db is None or mapping is None:
        return True, ""
    try:
        from arango_query_core.exec import explain_aql

        from arango_cypher.api import translate
    except Exception as exc:
        logger.info("execution-grounded validation unavailable: %s", exc)
        return True, ""
    try:
        tq = translate(cypher, mapping=mapping)
    except Exception as exc:
        msg = str(exc) or exc.__class__.__name__
        msg = msg.splitlines()[0] if "\n" in msg else msg
        return False, f"Cypher did not transpile to AQL: {msg[:300]}"
    try:
        ok, err = explain_aql(db, tq.aql, tq.bind_vars or {})
    except Exception as exc:
        logger.info("EXPLAIN step raised unexpectedly: %s", exc)
        return True, ""
    return ok, err


def _fix_labels(cypher: str, ctx: _SchemaCtx) -> str:
    """Rewrite Cypher labels that don't exist in the mapping to the closest match.

    Handles common LLM hallucinations like ``Actor`` → ``Person`` by using the
    same fuzzy matching the rule-based engine uses, plus role-synonym lookup.
    """

    def _replace_label(m: re.Match) -> str:
        prefix = m.group(1)
        label = m.group(2)
        if label.lower() in ctx.entities:
            return prefix + ctx.entities[label.lower()]["name"]
        if label.lower() in {r["type"].lower() for r in ctx.relationships.values()}:
            return prefix + label
        role = label.lower().rstrip("s")
        rel = ctx.role_to_rel.get(role)
        if rel:
            from_e = rel.get("fromEntity", "")
            if from_e and from_e != "Any":
                return prefix + from_e
        ent = _match_entity(label, ctx.entities)
        if ent:
            return prefix + ent["name"]
        return prefix + label

    return re.sub(r"((?:\(|\[)[a-zA-Z0-9_]*:)([A-Z]\w*)", _replace_label, cypher)


def _build_schema_context(bundle: MappingBundle) -> _SchemaCtx:
    """Extract entities, relationships, and derived lookup tables from the mapping."""
    cs = bundle.conceptual_schema
    pm = bundle.physical_mapping

    entities: dict[str, dict] = {}
    cs_entities = cs.get("entities", [])
    cs_entity_types = cs.get("entityTypes", [])
    if isinstance(cs_entities, list) and cs_entities and isinstance(cs_entities[0], dict):
        entities = {e["name"].lower(): e for e in cs_entities if "name" in e}
    elif isinstance(cs_entity_types, list):
        for name in cs_entity_types:
            entities[name.lower()] = {"name": name, "properties": []}
    if not entities and isinstance(pm.get("entities"), dict):
        for name in pm["entities"]:
            entities[name.lower()] = {"name": name, "properties": []}

    relationships: dict[str, dict] = {}
    cs_rels = cs.get("relationships", [])
    cs_rel_types = cs.get("relationshipTypes", [])
    if isinstance(cs_rels, list) and cs_rels and isinstance(cs_rels[0], dict):
        relationships = {r["type"].lower(): r for r in cs_rels if "type" in r}
    elif isinstance(cs_rel_types, list):
        for rtype in cs_rel_types:
            pm_rel = (
                pm.get("relationships", {}).get(rtype, {})
                if isinstance(pm.get("relationships"), dict)
                else {}
            )
            relationships[rtype.lower()] = {
                "type": rtype,
                "fromEntity": pm_rel.get("domain", "Any"),
                "toEntity": pm_rel.get("range", "Any"),
                "properties": [],
            }
    if not relationships and isinstance(pm.get("relationships"), dict):
        for rtype, pm_rel in pm["relationships"].items():
            relationships[rtype.lower()] = {
                "type": rtype,
                "fromEntity": pm_rel.get("domain", "Any"),
                "toEntity": pm_rel.get("range", "Any"),
                "properties": [],
            }

    _ROLE_SYNONYMS: dict[str, list[str]] = {
        "acted_in": ["actor", "actress", "cast", "star", "performer"],
        "directed": ["director"],
        "produced": ["producer"],
        "wrote": ["writer", "author", "screenwriter"],
        "reviewed": ["reviewer", "critic"],
        "follows": ["follower"],
        "knows": ["friend", "acquaintance", "contact"],
    }
    role_to_rel: dict[str, dict] = {}
    for rkey, rdef in relationships.items():
        for _synonyms in _ROLE_SYNONYMS.values():
            normalized_rkey = rkey.replace("_", "")
            for syn_key, syn_list in _ROLE_SYNONYMS.items():
                if syn_key.replace("_", "") == normalized_rkey or syn_key == rkey:
                    for s in syn_list:
                        role_to_rel[s] = rdef
                    break

    return _SchemaCtx(entities=entities, relationships=relationships, role_to_rel=role_to_rel, pm=pm)


@dataclass
class _SchemaCtx:
    entities: dict[str, dict]
    relationships: dict[str, dict]
    role_to_rel: dict[str, dict]
    pm: dict[str, Any]


def _extract_filter_value(text: str) -> str:
    """Extract a meaningful filter value from text, stripping articles and entity nouns."""
    text = re.sub(r"^(?:the|a|an|some|all|any)\s+", "", text.strip())
    text = re.sub(r"\s+(?:movie|movies|film|films|person|persons|people)s?$", "", text, flags=re.I)
    return text.strip()


def _find_rel_for_verb(verb: str, relationships: dict[str, dict]) -> dict | None:
    """Map a verb from the question to a relationship using verb stems and synonyms."""
    verb = verb.lower().rstrip("s").rstrip("ed")
    _VERB_TO_REL: dict[str, str] = {
        "act": "acted_in",
        "star": "acted_in",
        "appear": "acted_in",
        "direct": "directed",
        "helm": "directed",
        "produc": "produced",
        "made": "produced",
        "writ": "wrote",
        "wrot": "wrote",
        "pen": "wrote",
        "review": "reviewed",
        "rat": "reviewed",
        "critiqu": "reviewed",
        "follow": "follows",
        "know": "knows",
    }
    for stem, rel_key in _VERB_TO_REL.items():
        if verb.startswith(stem) or stem.startswith(verb):
            if rel_key in relationships:
                return relationships[rel_key]
    rel = _match_relationship(verb, relationships)
    if rel:
        return rel
    return None


def _rule_based_translate(question: str, bundle: MappingBundle) -> NL2CypherResult:
    """Rule-based fallback: pattern-match common natural language queries."""
    ctx = _build_schema_context(bundle)
    entities = ctx.entities
    relationships = ctx.relationships
    role_to_rel = ctx.role_to_rel

    q = question.lower().strip().rstrip("?").rstrip(".")
    wants_single = bool(re.search(r"\b(?:a\s+(?:random|single|sample)|one)\b", q))

    _VERB_PHRASES = (
        r"(?:were|was|are|is)\s+in"
        r"|act(?:ed)?\s+in|star(?:red)?\s+in|appear(?:ed)?\s+in"
        r"|direct(?:ed)?|produc(?:ed)?|writ(?:ten|e|ten)?|wrote"
        r"|review(?:ed)?|follow(?:ed|s)?|know[sn]?"
    )
    m = re.match(
        r"(?:which|what|find|list|show)\s+(\w+)\s+"
        r"(?:(?:that|who|which)\s+)?"
        rf"(?:{_VERB_PHRASES})"
        r"\s*(.*)",
        q,
    )
    if m:
        role_word = m.group(1).rstrip("s")
        filter_text = _extract_filter_value(m.group(2)) if m.group(2).strip() else ""
        verb_match = re.search(
            r"((?:were|was|are|is)\s+in|acted?\s*in|starred?\s*in|appeared?\s*in"
            r"|directed?|produced?|wrote?|written|reviewed?|followed?|known?)",
            q,
        )
        rel = role_to_rel.get(role_word)
        if not rel and verb_match:
            verb_text = verb_match.group(1).split()[0]
            rel = _find_rel_for_verb(verb_text, relationships)
        if not rel:
            rel = _find_rel_for_verb(role_word, relationships)
        if rel:
            from_e = rel.get("fromEntity", "Any")
            to_e = rel.get("toEntity", "Any")
            if from_e != "Any" and to_e != "Any":
                cypher = f"MATCH (a:{from_e})-[:{rel['type']}]->(b:{to_e})"
                if filter_text:
                    cypher += f"\nWHERE toLower(b.title) CONTAINS '{filter_text}' OR toLower(b.name) CONTAINS '{filter_text}'"
                cypher += "\nRETURN a"
                return NL2CypherResult(
                    cypher=cypher,
                    explanation=f"Find {from_e} via {rel['type']}",
                    confidence=0.6,
                    method="rule_based",
                )

    m = re.match(
        r"(?:which|what)\s+(\w+)\s+"
        r"(?:did|has|have|had|does|do)\s+"
        r"(.+?)\s+"
        rf"(?:{_VERB_PHRASES})"
        r"\s*(.*)",
        q,
    )
    if m:
        entity_hint = m.group(1).rstrip("s")
        person_name = m.group(2).strip()
        entity = _match_entity(entity_hint, entities)
        verb_match = re.search(
            r"((?:were|was)\s+in|act(?:ed)?\s*in|star(?:red)?\s*in"
            r"|direct(?:ed)?|produc(?:ed)?|writ(?:ten|e)?|review(?:ed)?|follow(?:ed)?)",
            q,
        )
        rel = _find_rel_for_verb(verb_match.group(1).split()[0], relationships) if verb_match else None
        if entity and rel:
            from_e = rel.get("fromEntity", "Any")
            to_e = rel.get("toEntity", "Any")
            if from_e != "Any" and to_e != "Any":
                target_label = entity["name"]
                source_label = from_e if target_label != from_e else to_e
                cypher = (
                    f"MATCH (a:{source_label})-[:{rel['type']}]->(b:{target_label})\n"
                    f"WHERE toLower(a.name) CONTAINS '{person_name}'\n"
                    f"RETURN b"
                )
                return NL2CypherResult(
                    cypher=cypher,
                    explanation=f"Find {target_label} via {rel['type']}",
                    confidence=0.6,
                    method="rule_based",
                )

    m = re.match(r"who\s+(\w+)\s+(?:in\s+)?(.+)", q)
    if m:
        verb = m.group(1)
        filter_text = _extract_filter_value(m.group(2))
        rel = _find_rel_for_verb(verb, relationships)
        if rel:
            from_e = rel.get("fromEntity", "Any")
            to_e = rel.get("toEntity", "Any")
            if from_e != "Any" and to_e != "Any":
                cypher = (
                    f"MATCH (a:{from_e})-[:{rel['type']}]->(b:{to_e})\n"
                    f"WHERE toLower(b.title) CONTAINS '{filter_text}' OR toLower(b.name) CONTAINS '{filter_text}'\n"
                    f"RETURN a"
                )
                return NL2CypherResult(
                    cypher=cypher,
                    explanation=f"Find who {verb} {filter_text}",
                    confidence=0.5,
                    method="rule_based",
                )

    q = re.sub(
        r"^(?:(?:can\s+you\s+)?(?:please\s+)?(?:give\s+me|show\s+me|get\s+me|tell\s+me|i\s+(?:want|need))\s+)",
        "get ",
        q,
    )

    explicit_limit: int | None = None
    limit_m = re.match(r"^(\w+\s+)(\d+)\s+", q)
    if limit_m:
        explicit_limit = int(limit_m.group(2))
        q = limit_m.group(1) + q[limit_m.end() :]

    q = re.sub(r"^(\w+\s+)(?:(?:a|an)\s+(?:random|single|sample)\s+|the\s+|an?\s+)", r"\1", q)
    q = re.sub(r"^(\w+\s+)(?:some|any)\s+", r"\1all ", q)
    q = re.sub(r"^(\w+\s+)(?:random|sample)\s+", r"\1", q)

    m = re.match(
        r"(?:find|list|show|get|return|fetch|display|retrieve|select|which|what)\s+(?:all\s+)?(\w+)s?\b(.*)",
        q,
    )
    if m:
        entity_hint = m.group(1)
        rest = m.group(2).strip()

        role_word = entity_hint.rstrip("s")
        rel = role_to_rel.get(role_word)
        if rel:
            from_e = rel.get("fromEntity", "Any")
            to_e = rel.get("toEntity", "Any")
            if from_e != "Any" and to_e != "Any":
                filter_text = _extract_filter_value(rest) if rest else ""
                cypher = f"MATCH (a:{from_e})-[:{rel['type']}]->(b:{to_e})"
                if filter_text:
                    cypher += f"\nWHERE toLower(b.title) CONTAINS '{filter_text}' OR toLower(b.name) CONTAINS '{filter_text}'"
                cypher += "\nRETURN a"
                return NL2CypherResult(
                    cypher=cypher,
                    explanation=f"Find {from_e} via {rel['type']}",
                    confidence=0.5,
                    method="rule_based",
                )

        entity = _match_entity(entity_hint, entities)
        if entity:
            name = entity["name"]
            props = [p["name"] for p in entity.get("properties", [])[:5]]
            ret = ", ".join(f"n.{p}" for p in props) if props else "n"
            if explicit_limit:
                limit = f"\nLIMIT {explicit_limit}"
            elif wants_single:
                limit = "\nLIMIT 1"
            else:
                limit = ""

            if rest:
                where = _parse_simple_filter(rest, "n")
                if where:
                    return NL2CypherResult(
                        cypher=f"MATCH (n:{name})\nWHERE {where}\nRETURN {ret}{limit}",
                        explanation=f"Find {name} nodes with filter",
                        confidence=0.5,
                        method="rule_based",
                    )

            return NL2CypherResult(
                cypher=f"MATCH (n:{name})\nRETURN {ret}{limit}",
                explanation=f"{'Get one' if wants_single else 'List all'} {name} node{'s' if not wants_single else ''}",
                confidence=0.6,
                method="rule_based",
            )

    m = re.match(r"(?:how many|count)\s+(\w+)s?\b", q)
    if m:
        entity = _match_entity(m.group(1), entities)
        if entity:
            return NL2CypherResult(
                cypher=f"MATCH (n:{entity['name']})\nRETURN count(n)",
                explanation=f"Count {entity['name']} nodes",
                confidence=0.7,
                method="rule_based",
            )

    for rtype, rdef in relationships.items():
        if rtype.replace("_", " ") in q or rtype in q:
            from_e = rdef.get("fromEntity", "Any")
            to_e = rdef.get("toEntity", "Any")
            if from_e != "Any" and to_e != "Any":
                return NL2CypherResult(
                    cypher=f"MATCH (a:{from_e})-[:{rdef['type']}]->(b:{to_e})\nRETURN a, b",
                    explanation=f"Pattern matched relationship type {rdef['type']}",
                    confidence=0.3,
                    method="rule_based",
                )

    return NL2CypherResult(
        cypher="",
        explanation="Could not generate a query from the input. Try rephrasing or use an LLM backend.",
        confidence=0.0,
        method="rule_based",
    )


_IRREGULAR_PLURALS: dict[str, str] = {
    "people": "person",
    "persons": "person",
    "men": "man",
    "women": "woman",
    "children": "child",
    "mice": "mouse",
    "data": "datum",
    "indices": "index",
}


_T = TypeVar("_T")


def _entity_stem_match(hint: str, entities: dict[str, _T]) -> _T | None:
    """The entity whose lowercase name *hint* is, up to a plural ending
    ("customers" → customer, "categories" → category, "people" → person), else
    ``None``. Strict enough to test every word of a question, unlike the
    substring and prefix steps :func:`_match_entity` adds after it."""
    hint = hint.lower()
    hint_singular = _IRREGULAR_PLURALS.get(hint, hint)
    stems = {hint, hint_singular, hint.rstrip("s"), hint.rstrip("es"), re.sub(r"ies$", "y", hint)}
    for key, val in entities.items():
        key_stems = {key, key.rstrip("s"), key.rstrip("es"), re.sub(r"ies$", "y", key)}
        if stems & key_stems:
            return val
    return None


def _match_entity(hint: str, entities: dict[str, dict]) -> dict | None:
    """Fuzzy-match an entity name from user input."""
    exact = _entity_stem_match(hint, entities)
    if exact is not None:
        return exact
    hint = hint.lower()
    hint_singular = _IRREGULAR_PLURALS.get(hint, hint)
    stems = {hint, hint_singular, hint.rstrip("s"), hint.rstrip("es"), re.sub(r"ies$", "y", hint)}
    for key, val in entities.items():
        if hint in key or key in hint:
            return val
    for key, val in entities.items():
        key_base = re.sub(r"[aeiouy]+$", "", key)
        for s in stems:
            s_base = re.sub(r"[aeiouy]+$", "", s)
            if len(s_base) >= 3 and (s_base.startswith(key_base) or key_base.startswith(s_base)):
                return val
    return None


def _match_relationship(hint: str, relationships: dict[str, dict]) -> dict | None:
    """Fuzzy-match a relationship type."""
    hint = hint.lower()
    for key, val in relationships.items():
        normalized = key.replace("_", "").lower()
        if hint in normalized or normalized in hint:
            return val
    verb_map = {
        "acted": "acted_in",
        "directed": "directed",
        "produced": "produced",
        "wrote": "wrote",
        "reviewed": "reviewed",
        "follows": "follows",
        "knows": "knows",
        "purchased": "purchased",
        "bought": "purchased",
        "ordered": "purchased",
        "sold": "sold_by",
        "reports": "reports_to",
        "supplies": "supplied_by",
    }
    mapped = verb_map.get(hint)
    if mapped:
        for key, val in relationships.items():
            if key == mapped:
                return val
    return None


def _parse_simple_filter(text: str, var: str) -> str | None:
    """Parse simple 'where/with/in X' filters."""
    m = re.match(r"(?:where|with|in|from)\s+(?:the\s+)?(?:name|title)\s+(?:is\s+)?['\"]?(.+?)['\"]?$", text)
    if m:
        val = m.group(1).strip("'\"")
        return f"{var}.name = '{val}'"

    m = re.match(r"(?:where|in|from)\s+(?:country\s+)?(?:is\s+)?['\"]?(\w+)['\"]?$", text)
    if m:
        return f"{var}.country = '{m.group(1)}'"

    return None


# ---------------------------------------------------------------------------
# Default few-shot index (lazy, process-wide)
# ---------------------------------------------------------------------------

_DEFAULT_FEWSHOT_INDEX: FewShotIndex | None = None
_DEFAULT_FEWSHOT_RESOLVED = False
_DEFAULT_FEWSHOT_INVALIDATION_REGISTERED = False


def _invalidate_default_fewshot_index() -> None:
    """Drop the cached process-wide FewShotIndex.

    Wired into :mod:`arango_cypher.nl_corrections` via
    :func:`register_invalidation_listener` so every saved / deleted NL
    correction forces a rebuild on the next ``nl_to_cypher`` call. Also
    exposed for tests and external callers that want to force a refresh
    after mutating the corpus files directly.
    """
    global _DEFAULT_FEWSHOT_INDEX, _DEFAULT_FEWSHOT_RESOLVED
    _DEFAULT_FEWSHOT_INDEX = None
    _DEFAULT_FEWSHOT_RESOLVED = False


def _ensure_nl_corrections_listener() -> None:
    """Register the FewShotIndex invalidation hook exactly once per process.

    Deferred to first-call time (rather than module import) so importing
    ``arango_cypher.nl2cypher`` does not transitively import SQLite or
    touch the nl_corrections database file. Safe to call repeatedly.
    """
    global _DEFAULT_FEWSHOT_INVALIDATION_REGISTERED
    if _DEFAULT_FEWSHOT_INVALIDATION_REGISTERED:
        return
    try:
        from arango_cypher import nl_corrections as _nlc

        _nlc.register_invalidation_listener(_invalidate_default_fewshot_index)
        _DEFAULT_FEWSHOT_INVALIDATION_REGISTERED = True
    except Exception as exc:
        logger.info("nl_corrections listener registration failed: %s", exc)


#: Generated few-shot banks to load after the shipped seed corpora — an
#: ``os.pathsep``-separated list of files written by ``arango-cypher-py
#: synthbank``. Opt-in: adding examples changes every NL prompt, which an eval
#: run (``RUN_NL2CYPHER_EVAL=1``) must justify, so nothing is loaded by default.
FEWSHOT_BANKS_ENV = "NL2CYPHER_FEWSHOT_BANKS"


def _generated_bank_paths() -> list[Path]:
    """Existing bank files named by :data:`FEWSHOT_BANKS_ENV`.

    A configured path that does not exist is logged, not raised: the NL path
    must keep serving, but a typo must not pass silently as "no examples".
    """
    raw = os.environ.get(FEWSHOT_BANKS_ENV, "")
    found: list[Path] = []
    for entry in (e.strip() for e in raw.split(os.pathsep)):
        if not entry:
            continue
        path = Path(entry).expanduser()
        if path.is_file():
            found.append(path)
        else:
            logger.warning("%s names %s, which is not a file; skipped", FEWSHOT_BANKS_ENV, path)
    return found


class _NoExamplesRetriever:
    """A few-shot ``Retriever`` (the public protocol in
    ``arango_query_core.nl.fewshot``) that finds no example: the default index
    when ``rank_bm25`` is not installed. Kept here rather than importing the
    library's private no-op class, whose removal would silently turn the whole
    index off (the import sits inside the ``try`` below)."""

    def retrieve(self, question: str, k: int = 3) -> list[tuple[str, str]]:
        return []


def _get_default_fewshot_index() -> FewShotIndex | None:
    """Lazily build the default FewShotIndex from shipped corpora + user
    corrections.

    Seed examples from ``arango_cypher/nl2cypher/corpora/*.yml`` are
    loaded first; approved ``(question, cypher)`` pairs from the
    :mod:`arango_cypher.nl_corrections` store are appended afterward,
    so a user's correction wins ties against a seed example with the
    same BM25 score.

    Returns ``None`` if ``rank_bm25`` is unavailable or both the corpora
    and the corrections store are empty. Caller falls back to a
    zero-shot prompt in that case.
    """
    global _DEFAULT_FEWSHOT_INDEX, _DEFAULT_FEWSHOT_RESOLVED

    _ensure_nl_corrections_listener()

    if _DEFAULT_FEWSHOT_RESOLVED:
        return _DEFAULT_FEWSHOT_INDEX
    _DEFAULT_FEWSHOT_RESOLVED = True
    try:
        from pathlib import Path

        from arango_query_core.nl.fewshot import BM25Retriever, FewShotIndex, Retriever

        corpora_dir = Path(__file__).parent / "corpora"
        paths = sorted(corpora_dir.glob("*.yml")) + _generated_bank_paths()

        seed_index = FewShotIndex.from_corpus_files(paths) if paths else None
        seed_examples: list[tuple[str, str]] = list(seed_index.examples) if seed_index is not None else []

        correction_examples: list[tuple[str, str]] = []
        try:
            from arango_cypher import nl_corrections as _nlc

            correction_examples = _nlc.all_examples()
        except Exception as exc:
            logger.info("nl_corrections load failed (ignored): %s", exc)

        combined = seed_examples + correction_examples
        if not combined:
            _DEFAULT_FEWSHOT_INDEX = None
            return None

        try:
            retriever: Retriever = BM25Retriever(combined)
        except ImportError as exc:
            logger.info("rank_bm25 not installed; FewShotIndex degrades to no-op: %s", exc)
            retriever = _NoExamplesRetriever()
        _DEFAULT_FEWSHOT_INDEX = FewShotIndex(retriever, examples=combined)
    except Exception as exc:
        logger.info("FewShotIndex initialization failed: %s", exc)
        _DEFAULT_FEWSHOT_INDEX = None
    return _DEFAULT_FEWSHOT_INDEX


# Literal/constant questions ("return hello", 'return "hello"', "return 42")
# must not become database queries. Such questions reference no schema element,
# so we short-circuit them to a bare ``RETURN <literal>`` which the transpiler's
# standalone-RETURN path renders verbatim to AQL. This mirrors the transpiler
# guardrail (arango_cypher/_translate_v0/returns.py) on the NL side.
_LITERAL_RETURN_RE = re.compile(
    r"""^\s*
        (?:please\s+|just\s+|simply\s+|can\s+you\s+|could\s+you\s+)*
        return\s+
        (?P<value>
            "(?:[^"\\]|\\.)*"        # double-quoted string
          | '(?:[^'\\]|\\.)*'        # single-quoted string
          | -?\d+(?:\.\d+)?          # integer or decimal
          | true | false | null      # keyword literals
        )
        (?:\s+as\s+(?P<alias>[A-Za-z_]\w*))?
        \s*[.!?]?\s*$
    """,
    re.IGNORECASE | re.VERBOSE,
)


def _detect_literal_return(question: str) -> str | None:
    """Return a bare ``RETURN <literal>`` Cypher for constant-only questions.

    Recognizes questions whose entire intent is to echo a literal value
    (string, number, boolean, or null) — optionally aliased — and which
    therefore must *not* be turned into a graph query. Returns ``None`` for
    anything that references the schema or is otherwise non-trivial.
    """
    if not question or not question.strip():
        return None
    m = _LITERAL_RETURN_RE.match(question)
    if m is None:
        return None
    value = m.group("value")
    # Normalize keyword literals to lowercase; leave string/number literals
    # exactly as the user wrote them (single or double quotes are both valid
    # Cypher, and the transpiler accepts either).
    if value.lower() in {"true", "false", "null"}:
        value = value.lower()
    alias = m.group("alias")
    if alias:
        return f"RETURN {value} AS {alias}"
    return f"RETURN {value}"


# Explicit visualization intent: the user wants to *see the graph* (nodes +
# relationships), not a scalar projection. Kept deliberately conservative — it
# must fire on "show me, as a graph, …" / "visualize …" / "graph of …" but not
# on incidental mentions like "how many graphs are there".
_GRAPH_INTENT_RE = re.compile(
    r"""
        \bas\s+a\s+(?:graph|network|subgraph)\b
      | \bas\s+graph\b
      | \bvisuali[sz]e\b
      | \bvisuali[sz]ation\b
      | \b(?:show|draw|render|display|plot)\b[^.?!]*\b(?:graph|network|subgraph|relationships?|connections?)\b
      | \b(?:graph|network|subgraph)\s+of\b
      | \bgraph\s+view\b
    """,
    re.IGNORECASE | re.VERBOSE,
)


def _detect_graph_intent(question: str) -> bool:
    """True when the question explicitly asks for a graph/visualization result.

    Drives WP-S1: such questions should produce a path-returning Cypher
    (``MATCH p = (…)-[…]->(…) RETURN p``) so the UI can render nodes and edges,
    rather than a scalar like ``RETURN x.name``.
    """
    if not question or not question.strip():
        return False
    return _GRAPH_INTENT_RE.search(question) is not None


def nl_to_cypher(
    question: str,
    *,
    mapping: MappingBundle | dict[str, Any] | None = None,
    use_llm: bool = True,
    llm_provider: LLMProvider | None = None,
    max_retries: int = 2,
    use_fewshot: bool = True,
    fewshot_index: FewShotIndex | None = None,
    use_entity_resolution: bool = True,
    entity_resolver: EntityResolver | None = None,
    db: Any | None = None,
    tenant_context: TenantContext | None = None,
    retry_context: str | None = None,
    postconditions: Sequence[Postcondition] | None = None,
) -> NL2CypherResult:
    """Translate a natural language question to Cypher.

    Args:
        question: Plain English question about the graph.
        mapping: Schema mapping (MappingBundle or export dict).
        use_llm: If True, attempt LLM translation first.
        llm_provider: A custom LLM provider (implements ``LLMProvider``).
            If None and ``use_llm`` is True, falls back to an OpenAI-compatible
            provider configured via ``OPENAI_API_KEY`` / ``OPENAI_BASE_URL`` /
            ``OPENAI_MODEL`` environment variables.
        max_retries: Number of retry attempts if LLM output fails validation.
        use_fewshot: If True (default), retrieve top-K similar examples
            from the shipped corpora and inject them into the prompt.
        fewshot_index: Optional caller-supplied FewShotIndex to override
            the default one built from the shipped corpora.
        use_entity_resolution: If True (default) and a ``db`` handle or
            ``entity_resolver`` is provided, pre-resolve mentions in the
            question against the database's string properties so the LLM
            sees database-correct literals (e.g. ``"Forrest Gump"``
            instead of the user's ``"Forest Gump"``).  When no DB and no
            resolver are supplied the flag is a no-op and the prompt is
            byte-identical to the pre-WP-25.2 baseline.
        entity_resolver: Optional caller-supplied :class:`EntityResolver`.
            Overrides ``db``-based auto-construction when provided.
        db: Optional python-arango ``StandardDatabase``.  When supplied
            (and ``use_entity_resolution=True``) an :class:`EntityResolver`
            is constructed lazily against this connection.
    """
    # NL-side guardrail: questions that merely ask to echo a literal/constant
    # value must never become a database query, regardless of (or absence of)
    # the schema. Short-circuit to a bare RETURN so the transpiler renders it
    # verbatim (e.g. RETURN "hello") instead of inventing a graph lookup.
    literal_cypher = _detect_literal_return(question)
    if literal_cypher is not None:
        return NL2CypherResult(
            cypher=literal_cypher,
            explanation=("Constant expression — returned directly without querying the graph."),
            confidence=1.0,
            method="literal_return",
        )

    if mapping is None:
        return NL2CypherResult(
            cypher="",
            explanation="No schema mapping provided. Cannot generate Cypher without knowing the graph structure.",
            confidence=0.0,
        )

    if isinstance(mapping, dict):
        bundle = MappingBundle(
            conceptual_schema=mapping.get("conceptualSchema") or mapping.get("conceptual_schema") or {},
            physical_mapping=mapping.get("physicalMapping") or mapping.get("physical_mapping") or {},
            metadata=mapping.get("metadata", {}),
        )
    else:
        bundle = mapping

    schema_summary = _build_schema_summary(bundle)
    ctx = _build_schema_context(bundle)

    # Per-entity tenant scope manifest derived from the mapping. Cheap
    # (sub-millisecond on schemas with hundreds of entities) and
    # idempotent, so we re-derive on every call rather than caching
    # against a mutable bundle reference. When ``tenant_context`` is
    # None the manifest is built but never consulted — the cost is
    # noise on the cold path.
    tenant_manifest = analyze_tenant_scope(bundle) if tenant_context else None

    # WP-S1: detect "show me … as a graph" intent once and let it steer the
    # prompt toward path-returning Cypher (used by the LLM path below).
    graph_intent = _detect_graph_intent(question)

    if use_llm:
        provider = llm_provider or get_llm_provider()
        if provider is not None:
            resolved_lines: list[str] = []
            index_advisories: list[dict[str, Any]] = []
            if use_entity_resolution:
                resolver = entity_resolver
                if resolver is None and db is not None:
                    try:
                        from .entity_resolution import EntityResolver

                        resolver = EntityResolver(db=db, mapping=bundle)
                    except Exception as exc:
                        logger.info("EntityResolver init failed: %s", exc)
                        resolver = None
                if resolver is not None:
                    try:
                        hits = resolver.resolve(question)
                        if hits:
                            resolved_lines = resolver.format_prompt_section(hits)
                    except Exception as exc:
                        logger.info("EntityResolver.resolve failed: %s", exc)
                        resolved_lines = []
                    # WP-S3c: forward any inverted/ArangoSearch advisories the
                    # resolver recorded (fuzzy probes that fell back to a full
                    # scan) so the UI can offer one-click index creation. Read
                    # even when resolve() raised — partial probes may have
                    # already flagged a missing index.
                    try:
                        index_advisories = [a.as_dict() for a in resolver.advisories]
                    except Exception as exc:
                        logger.info("Reading resolver advisories failed: %s", exc)
                        index_advisories = []

            # Generate → validate → repair on arango-query-core's NLQueryEngine
            # (issue #24); imported here because engine_path imports this module.
            from .engine_path import translate_with_engine

            result = translate_with_engine(
                question,
                provider=provider,
                schema_summary=schema_summary,
                ctx=ctx,
                mapping=bundle,
                db=db,
                max_retries=max_retries,
                use_fewshot=use_fewshot,
                fewshot_index=fewshot_index,
                resolved_entities=resolved_lines,
                tenant_context=tenant_context,
                tenant_manifest=tenant_manifest,
                retry_context=retry_context,
                graph_intent=graph_intent,
                postconditions=postconditions,
            )
            # Fail-closed methods (`tenant_guardrail_blocked` for tenant
            # scope violations, `validation_failed` for retry-budget
            # exhaustion) have empty `cypher` by design; return them
            # instead of falling through to the rule-based fallback so
            # the UI surfaces the failure with a structured banner and
            # never silently writes invalid Cypher into the editor.
            if result is not None and result.method in (
                "tenant_guardrail_blocked",
                "validation_failed",
            ):
                if index_advisories:
                    result.advisories = index_advisories
                return result
            if result and result.cypher:
                if index_advisories:
                    result.advisories = index_advisories
                return result

    # Rule-based fallback cannot enforce tenant scoping — if a tenant
    # context is active, fail closed rather than silently emitting an
    # unscoped pattern from the rule-based translator.
    if tenant_context is not None:
        return NL2CypherResult(
            cypher="",
            explanation=(
                "Tenant-scoping guardrail: no LLM provider is available "
                "to produce a tenant-scoped Cypher query, and the "
                "rule-based fallback cannot enforce tenant isolation. "
                "Configure an LLM provider (e.g. set OPENAI_API_KEY) "
                "to use this multi-tenant workspace."
            ),
            confidence=0.0,
            method="tenant_guardrail_blocked",
        )

    return _rule_based_translate(question, bundle)


# ---------------------------------------------------------------------------
# Representative NL query suggestions (used to seed the UI "Ask" history)
# ---------------------------------------------------------------------------


_SUGGEST_PROMPT_TEMPLATE = """You generate short, natural-language example questions that a user might ask about a property graph.

The user has just connected to a database with the following schema:

{schema}

Rules:
- Produce between 6 and 10 distinct questions.
- Each question must be answerable against this schema (use only labels, relationship types, and properties shown above).
- Mix question shapes: simple lookups, one-hop traversals, two-hop traversals, filters on properties, aggregations (counts, averages), ordering / top-k, and at least one question that uses a property filter.
- Keep each question under ~120 characters, phrased the way a human would type it (no SQL, no Cypher, no code fences).
- Do NOT prefix questions with numbers, bullets, or labels.
- Output ONLY the questions, one per line, nothing else.
"""


def _llm_suggest_nl_queries(
    bundle: MappingBundle,
    provider: LLMProvider,
    count: int = 8,
) -> list[str]:
    """Ask the LLM to propose representative NL queries for the schema.

    Uses the public ``LLMProvider.generate`` protocol so any provider
    works — including :class:`~arango_cypher.nl2cypher.AnthropicProvider`,
    which does not subclass ``_BaseChatProvider``.
    """
    schema_summary = _build_schema_summary(bundle)
    system = _SUGGEST_PROMPT_TEMPLATE.format(schema=schema_summary)
    user = (
        f"Generate {count} example natural-language questions for this graph. "
        "Return only the questions, one per line."
    )
    try:
        result = provider.generate(system, user)
        content = result[0] if isinstance(result, tuple) else result
    except Exception as exc:
        logger.info("LLM suggest_nl_queries failed: %s", exc)
        return []

    lines: list[str] = []
    for raw in content.splitlines():
        s = raw.strip()
        if not s:
            continue
        s = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", s)
        s = s.strip().strip('"').strip("'")
        if len(s) < 4:
            continue
        if s.lower().startswith(("match ", "with ", "return ", "for ", "//", "#")):
            continue
        lines.append(s)
    seen: set[str] = set()
    out: list[str] = []
    for q in lines:
        key = q.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(q)
    return out[:count]


def _rule_based_suggest_nl_queries(
    bundle: MappingBundle,
    count: int = 8,
) -> list[str]:
    """Generate representative NL questions from the schema without an LLM."""
    cs = bundle.conceptual_schema
    pm = bundle.physical_mapping

    entities: list[dict[str, Any]] = []
    cs_ents = cs.get("entities", [])
    if isinstance(cs_ents, list) and cs_ents and isinstance(cs_ents[0], dict):
        for e in cs_ents:
            name = e.get("name") or ""
            props = [
                p.get("name", "") for p in e.get("properties", []) if isinstance(p, dict) and p.get("name")
            ]
            if name:
                entities.append({"name": name, "properties": props[:8]})
    if not entities and isinstance(pm.get("entities"), dict):
        for name, spec in pm["entities"].items():
            props = list((spec or {}).get("properties", {}).keys())[:8]
            entities.append({"name": name, "properties": props})

    rels: list[dict[str, Any]] = []
    cs_rels = cs.get("relationships", [])
    if isinstance(cs_rels, list) and cs_rels and isinstance(cs_rels[0], dict):
        for r in cs_rels:
            rels.append(
                {
                    "type": r.get("type", ""),
                    "from": r.get("fromEntity", ""),
                    "to": r.get("toEntity", ""),
                }
            )
    if not rels and isinstance(pm.get("relationships"), dict):
        for rtype, spec in pm["relationships"].items():
            spec = spec or {}
            rels.append(
                {
                    "type": rtype,
                    "from": spec.get("domain") or "",
                    "to": spec.get("range") or "",
                }
            )

    def _humanize(token: str) -> str:
        if not token:
            return token
        stripped = token.replace("_", "").replace("-", "")
        if stripped.isupper():
            s = token.replace("_", " ").replace("-", " ")
        else:
            s = re.sub(r"(?<!^)(?=[A-Z])", " ", token)
            s = s.replace("_", " ").replace("-", " ")
        return re.sub(r"\s+", " ", s).strip().lower()

    def _plural(word: str) -> str:
        if not word:
            return word
        if word.endswith("s"):
            return word
        if word.endswith("y") and len(word) > 1 and word[-2] not in "aeiou":
            return word[:-1] + "ies"
        return word + "s"

    def _verbalize_rel(rtype: str) -> str:
        return _humanize(rtype).replace(" ", " ").lower()

    suggestions: list[str] = []

    for e in entities[:4]:
        label = _plural(_humanize(e["name"]))
        suggestions.append(f"Show 10 {label}")
        suggestions.append(f"How many {label} are there?")
        if e["properties"]:
            prop = _humanize(e["properties"][0])
            suggestions.append(f"List {label} ordered by {prop}")

    for r in rels[:4]:
        if not (r["type"] and r["from"] and r["to"]):
            continue
        verb = _verbalize_rel(r["type"])
        src = _humanize(r["from"])
        dst_plural = _plural(_humanize(r["to"]))
        suggestions.append(f"For each {src}, show the {dst_plural} they {verb}")
        suggestions.append(f"Count {dst_plural} per {src}")

    if len(rels) >= 2:
        r1, r2 = rels[0], rels[1]
        if r1.get("to") and r2.get("from") and r1["to"] == r2["from"]:
            suggestions.append(
                f"Find {_plural(_humanize(r2['to']))} connected to {_plural(_humanize(r1['from']))} "
                f"through {_humanize(r1['to'])}"
            )

    seen: set[str] = set()
    out: list[str] = []
    for q in suggestions:
        key = q.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(q)
        if len(out) >= count:
            break
    return out


def suggest_nl_queries(
    mapping: MappingBundle | dict[str, Any] | None,
    *,
    count: int = 8,
    use_llm: bool = True,
    llm_provider: LLMProvider | None = None,
) -> list[str]:
    """Return a representative set of natural-language questions for the schema.

    Used to seed the UI "Ask" history after the user connects to a database
    and schema introspection completes. Falls back to rule-based generation
    when no LLM provider is configured or the LLM call fails.
    """
    if mapping is None:
        return []

    if isinstance(mapping, dict):
        bundle = MappingBundle(
            conceptual_schema=mapping.get("conceptualSchema") or mapping.get("conceptual_schema") or {},
            physical_mapping=mapping.get("physicalMapping") or mapping.get("physical_mapping") or {},
            metadata=mapping.get("metadata", {}),
        )
    else:
        bundle = mapping

    if use_llm:
        provider = llm_provider or get_llm_provider()
        if provider is not None:
            llm_out = _llm_suggest_nl_queries(bundle, provider, count=count)
            if llm_out:
                return llm_out

    return _rule_based_suggest_nl_queries(bundle, count=count)
