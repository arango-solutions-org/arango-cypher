"""NL→AQL direct translation pipeline.

Unlike NL→Cypher (which hides physical mapping from the LLM per §1.2),
the direct NL→AQL path deliberately exposes collection names, edge
collections, type discriminators, and cardinality statistics so the
model can emit efficient AQL without going through Cypher.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from arango_query_core.mapping import MappingBundle
from arango_query_core.nl.providers import LLMProvider, _get_default_provider

from ._core import _escape_label, _property_quality_hint
from .postconditions import Postcondition
from .tenant_guardrail import TenantContext
from .tenant_guardrail import prompt_section as _tenant_prompt_section
from .tenant_scope import (
    EntityTenantRole,
    TenantScopeManifest,
    analyze_tenant_scope,
)

# Match `Tenant` as a word-boundary token — any NL→AQL emission that
# targets the tenant collection will contain it (e.g. `FOR t IN
# Tenant`, `OUTBOUND "Tenant/..."`). Case-sensitive on purpose: the
# collection name is canonical.
_TENANT_COLLECTION_RE = re.compile(r"\bTenant\b")

logger = logging.getLogger(__name__)


@dataclass
class NL2AqlResult:
    """Result of a natural language to AQL direct translation."""

    aql: str
    bind_vars: dict[str, Any]
    explanation: str = ""
    confidence: float = 0.0
    method: str = "llm"
    schema_context: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cached_tokens: int = 0
    """Prompt tokens served from the provider's prefix cache (WP-25.4).

    Mirrors :attr:`NL2CypherResult.cached_tokens`; see that docstring.
    """


_AQL_PROMPT_TEMPLATE = """You are an ArangoDB AQL query expert. Given a natural language question and a database schema, generate a valid AQL query.

{schema}

## AQL Syntax Reference

### Basic query structure
```
FOR doc IN collection
  FILTER condition
  SORT doc.field ASC|DESC
  LIMIT [offset,] count
  RETURN doc | {{ field1: doc.field1, ... }}
```

### Graph traversal (1-hop)
```
FOR vertex, edge IN 1..1 OUTBOUND|INBOUND|ANY startVertex edgeCollection
  RETURN vertex
```
- OUTBOUND: follows _from -> _to direction (startVertex is _from)
- INBOUND: reverse direction (startVertex is _to)
- ANY: both directions
- The startVertex must be a document or document _id

### Graph traversal (multi-hop)
```
FOR vertex, edge, path IN min..max OUTBOUND|INBOUND|ANY startVertex edgeCollection
  RETURN vertex
```
- path.vertices is array of vertices along the path
- path.edges is array of edges along the path

### Chaining traversals (multi-step patterns)
```
FOR a IN Collection1
  FILTER a.prop == "value"
  FOR b IN OUTBOUND a edgeCollection1
    FOR c IN OUTBOUND b edgeCollection2
      RETURN {{ a: a, b: b, c: c }}
```

### Aggregation with COLLECT
```
// Count with grouping
FOR doc IN collection
  FOR related IN OUTBOUND doc edgeCol
    COLLECT key = doc.field WITH COUNT INTO count
    RETURN {{ key, count }}

// Aggregate functions
FOR doc IN collection
  COLLECT key = doc.field AGGREGATE total = SUM(doc.value), avg = AVG(doc.value), cnt = COUNT(doc)
  RETURN {{ key, total, avg, cnt }}

// Collect into array
FOR a IN col1
  FOR b IN OUTBOUND a edgeCol
    COLLECT groupKey = a.field INTO items = b
    RETURN {{ groupKey, items }}
```

### OPTIONAL MATCH equivalent (left join)
```
FOR d IN Collection
  LET related = FIRST(FOR r IN OUTBOUND d edgeCol RETURN r)
  RETURN {{ d, related }}  // related is null if no match
```

### Subqueries
```
FOR doc IN collection
  LET count = LENGTH(FOR x IN OUTBOUND doc edgeCol RETURN 1)
  RETURN {{ doc, connectionCount: count }}
```

### Type discriminator filtering
When entities share a collection with a type discriminator field:
```
FOR doc IN sharedCollection
  FILTER doc.typeField == "TypeValue"
  RETURN doc
```

### Counting / grouping by entity or relationship type
Many schemas store ALL nodes in one document collection and ALL edges in one
edge collection, with the kind distinguished by a type field shown in the
schema as ``[type discriminator: <field>=...]``. To answer "how many of each
type" / "most common entity or relationship types", COLLECT on that exact
discriminator field — NOT on _from/_to and NOT on invented fields like
_fromType/_toType (those do not exist; edges only have _from, _to, _id, _key):
```
// most common relationship types (edges share one collection, typed by <field>)
FOR e IN edgeCollection
  COLLECT relType = e.<typeField> WITH COUNT INTO count
  SORT count DESC
  LIMIT 30
  RETURN {{ relationshipType: relType, count }}
```

## Critical Rules
1. Use EXACT collection names from the schema (case-sensitive)
2. Use EXACT field names from the schema (case-sensitive)
3. Do NOT use bind parameters (@@col or @param) — use literal names and values
4. Use LOWER() for case-insensitive string comparisons: FILTER LOWER(doc.name) == LOWER("value")
5. For CONTAINS matching: FILTER CONTAINS(LOWER(doc.field), LOWER("value"))
6. System properties: _key, _id, _rev, _from, _to (edges also have _from, _to)
7. _id format is "collectionName/key" (e.g., "Device/12345")
8. When filtering by a related entity's property, traverse to that entity first
9. Use DISTINCT to deduplicate: RETURN DISTINCT doc
10. AQL uses == for equality (not =), != for inequality
11. String concatenation: use CONCAT(), not +
12. When counting, use COLLECT WITH COUNT INTO or COLLECT AGGREGATE cnt = COUNT(1)
13. For "top N" queries: SORT field DESC LIMIT N
14. Wrap the query in ```aql``` code block
15. Return a single valid AQL query
"""


#: Added to the repair message when the server names an unknown function: the model
#: is almost always reaching for a SQL function AQL does not have.
_UNKNOWN_FUNCTION_HINT = (
    " AQL has no SQL functions such as DIVIDE, YEAR, CAST, IIF or CURRENT_TIMESTAMP;"
    " use a / b, DATE_YEAR(), TO_NUMBER(), cond ? a : b and DATE_NOW()."
)


def _edge_ends(bundle: MappingBundle) -> dict[str, tuple[Any, Any]]:
    """``relationship type -> (domain label, range label)``, either may be None.

    The analyzer's export keeps an edge's ends only on the conceptual relationship;
    fall back to them, as schema_acquire's statistics do.
    """
    cs_ends = {
        r.get("type"): (r.get("fromEntity"), r.get("toEntity"))
        for r in bundle.conceptual_schema.get("relationships") or []
        if isinstance(r, dict)
    }
    rels = bundle.physical_mapping.get("relationships")
    if not isinstance(rels, dict):
        return {}
    ends = {}
    for rtype, rel in rels.items():
        if isinstance(rel, dict):
            cs_from, cs_to = cs_ends.get(rtype, (None, None))
            ends[rtype] = (rel.get("domain") or cs_from, rel.get("range") or cs_to)
    return ends


def _edge_endpoints(bundle: MappingBundle) -> dict[str, tuple[str, str]]:
    """``edge collection -> (from collection, to collection)`` where both ends are entities."""
    pm = bundle.physical_mapping
    ents = pm.get("entities") or {}
    return {
        pm["relationships"][rtype].get("edgeCollectionName", rtype): (
            ents[d].get("collectionName", d),
            ents[r].get("collectionName", r),
        )
        for rtype, (d, r) in _edge_ends(bundle).items()
        if d in ents and r in ents
    }


_FOR_IN_RE = re.compile(r"\bFOR\s+([A-Za-z_]\w*)\s+IN\s+([A-Za-z_][\w-]*)\b", re.IGNORECASE)
_TRAVERSAL_RE = re.compile(
    r"\bIN\s+(?:\d+(?:\s*\.\.\s*\d+)?\s+)?(OUTBOUND|INBOUND)\s+"
    r"(?:([A-Za-z_]\w*)(?:\._id)?|[\"']([A-Za-z_][\w-]*)/[^\"']*[\"'])\s+([A-Za-z_][\w-]*)",
    re.IGNORECASE,
)


def _traversal_direction_problems(aql: str, endpoints: dict[str, tuple[str, str]]) -> list[str]:
    """Traversals that start at the end of an edge collection their direction walks into.

    Only clear cases: the start is a ``FOR v IN <collection>`` variable or a
    ``"collection/key"`` literal, and the edge's two ends are different collections.
    """
    var_col = {m.group(1): m.group(2) for m in _FOR_IN_RE.finditer(aql)}
    problems = []
    for m in _TRAVERSAL_RE.finditer(aql):
        direction, var, literal_col, edge_col = m.group(1).upper(), m.group(2), m.group(3), m.group(4)
        from_col, to_col = endpoints.get(edge_col, (None, None))
        start = literal_col or var_col.get(var or "")
        if not start or from_col == to_col:
            continue
        if (direction, start) in (("OUTBOUND", to_col), ("INBOUND", from_col)):
            right = "INBOUND" if direction == "OUTBOUND" else "OUTBOUND"
            problems.append(
                f"the traversal over '{edge_col}' starts from a '{start}' document with "
                f"{direction}, but its edges go from '{from_col}' to '{to_col}': use {right}"
            )
    return problems


def _build_physical_schema_summary(bundle: MappingBundle) -> str:
    """Build a physical-schema description for direct NL→AQL generation.

    Unlike the conceptual-only summary used for NL→Cypher, this includes
    collection names, edge collection names, type fields, physical
    field names, and cardinality statistics so the LLM can generate
    efficient AQL directly.
    """
    pm = bundle.physical_mapping
    cs = bundle.conceptual_schema
    ends = _edge_ends(bundle)
    stats = bundle.metadata.get("statistics", {})
    entity_stats = stats.get("entities", {})
    rel_stats = stats.get("relationships", {})

    lines: list[str] = ["Database schema (ArangoDB collections and edges):"]

    flagged_entity_props: list[tuple[str, str, dict[str, Any]]] = []
    if isinstance(pm.get("entities"), dict):
        lines.append("\nDocument collections:")
        for label, ent in pm["entities"].items():
            if not isinstance(ent, dict):
                continue
            col = ent.get("collectionName", label)
            style = ent.get("style", "COLLECTION")
            props = ent.get("properties", {})
            if isinstance(props, dict):
                prop_entries = list(props.items())[:12]
                formatted: list[str] = []
                for pname, pmeta in prop_entries:
                    hint = _property_quality_hint(pmeta if isinstance(pmeta, dict) else None)
                    formatted.append(f"{pname}{hint}")
                    if isinstance(pmeta, dict) and (pmeta.get("sentinelValues") or pmeta.get("numericLike")):
                        flagged_entity_props.append((label, pname, pmeta))
                prop_str = ", ".join(formatted) if formatted else "no properties"
            else:
                prop_str = "no properties"

            type_info = ""
            if style in ("LABEL", "GENERIC_WITH_TYPE") and ent.get("typeField"):
                type_info = f" [type discriminator: {ent['typeField']}={ent.get('typeValue', label)}]"

            count_info = ""
            est = entity_stats.get(label, {})
            if isinstance(est, dict) and "estimated_count" in est:
                count_info = f" — ~{est['estimated_count']:,} documents"

            lines.append(f"  Collection '{col}' (entity: {_escape_label(label)}){type_info}{count_info}")
            lines.append(f"    Fields: {prop_str}")

    if isinstance(pm.get("relationships"), dict):
        lines.append("\nEdge collections:")
        for rtype, rel in pm["relationships"].items():
            if not isinstance(rel, dict):
                continue
            edge_col = rel.get("edgeCollectionName", rtype)
            style = rel.get("style", "DEDICATED_COLLECTION")
            domain, range_ = (end or "?" for end in ends.get(rtype, (None, None)))
            props = rel.get("properties", {})
            # ``_fromType`` / ``_toType`` are denormalised *node*-type
            # fields on the edge (the kind of the source / target vertex).
            # Their names contain "Type", which reliably lures the LLM into
            # grouping relationship-type questions by them. Annotate so the
            # model groups by the real relationship-type discriminator
            # (``rel['typeField']``, e.g. ``type``) instead.
            _edge_field_notes = {
                "_fromType": "_fromType (source NODE type — NOT the relationship type)",
                "_toType": "_toType (target NODE type — NOT the relationship type)",
            }
            prop_names = (
                [_edge_field_notes.get(p, p) for p in list(props.keys())[:8]]
                if isinstance(props, dict)
                else []
            )
            prop_str = ", ".join(prop_names) if prop_names else "no properties"

            type_info = ""
            if style == "GENERIC_WITH_TYPE" and rel.get("typeField"):
                type_info = (
                    f" [relationship-type field: {rel['typeField']} "
                    f"(=='{rel.get('typeValue', rtype)}' for this type; "
                    f"GROUP BY this field to count relationship types)]"
                )

            domain_col = _resolve_collection_name(domain, pm) or domain
            range_col = _resolve_collection_name(range_, pm) or range_

            rs = rel_stats.get(rtype, {})
            cardinality_info = ""
            if isinstance(rs, dict) and rs.get("edge_count"):
                parts = [f"~{rs['edge_count']:,} edges"]
                if rs.get("avg_out_degree"):
                    parts.append(f"avg fan-out: {rs['avg_out_degree']}/{domain}")
                if rs.get("avg_in_degree"):
                    parts.append(f"avg fan-in: {rs['avg_in_degree']}/{range_}")
                if rs.get("cardinality_pattern"):
                    parts.append(f"pattern: {rs['cardinality_pattern']}")
                cardinality_info = "\n    Cardinality: " + ", ".join(parts)

            lines.append(f"  Edge collection '{edge_col}' (relationship: {_escape_label(rtype)}){type_info}")
            lines.append(
                f"    Connects: {_escape_label(domain)}('{domain_col}') -> "
                f"{_escape_label(range_)}('{range_col}')"
            )
            if prop_str != "no properties":
                lines.append(f"    Fields: {prop_str}")
            if cardinality_info:
                lines.append(cardinality_info)

    cs_rels = cs.get("relationships", [])
    if isinstance(cs_rels, list) and cs_rels:
        lines.append("\nGraph topology (for traversal queries):")
        for r in cs_rels:
            if not isinstance(r, dict):
                continue
            rtype = r.get("type", "")
            from_e = r.get("fromEntity", "?")
            to_e = r.get("toEntity", "?")
            edge_col = ""
            if isinstance(pm.get("relationships"), dict):
                pm_rel = pm["relationships"].get(rtype, {})
                edge_col = pm_rel.get("edgeCollectionName", "") if isinstance(pm_rel, dict) else ""
            if edge_col:
                lines.append(f"  {from_e} --[{edge_col}]--> {to_e}")

    if entity_stats or rel_stats:
        lines.append("\nQuery optimization hints:")
        lines.append("  - Start traversals from the SMALLER collection when filtering by a property.")
        # Direction comes from _from/_to, never cardinality (a child -> parent edge is N:1).
        lines.append(
            "  - Direction follows each edge's Connects line A -> B: OUTBOUND from an A "
            "document, INBOUND from a B document, whatever the cardinality."
        )
        lines.append("  - Use LIMIT early when only a few results are needed from large collections.")

    if flagged_entity_props:
        lines.append("\nData-quality hints:")
        lines.append(
            "  - Fields marked 'sentinels: ...' store a string placeholder for "
            "missing values (e.g. literal 'NULL'). These are NOT real nulls; "
            "exclude them in FILTER, e.g. "
            "`FILTER t.COMPANY_SIZE != 'NULL' AND t.COMPANY_SIZE != null`."
        )
        lines.append(
            "  - Fields marked 'numeric-like string' hold numbers stored as "
            "strings. Cast before numeric comparison or ordering, e.g. "
            "`SORT TO_NUMBER(t.COMPANY_SIZE) DESC`."
        )
        lines.append(
            "  - For 'top-N by numeric X', combine both: filter sentinels, "
            "then SORT TO_NUMBER(...) DESC LIMIT N."
        )

    return "\n".join(lines)


def _resolve_collection_name(entity_label: str, pm: dict[str, Any]) -> str | None:
    """Resolve an entity label to its physical collection name."""
    if isinstance(pm.get("entities"), dict):
        ent = pm["entities"].get(entity_label, {})
        if isinstance(ent, dict):
            return ent.get("collectionName", entity_label)
    return None


def _extract_aql_from_response(text: str) -> tuple[str, dict[str, Any]]:
    """Extract AQL query from LLM response (handles code blocks).

    Returns (aql, bind_vars). Bind vars are currently empty since we
    ask the LLM to use literals.
    """
    m = re.search(r"```(?:aql)?\s*\n(.*?)```", text, re.DOTALL)
    if m:
        return m.group(1).strip(), {}
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    aql_lines = []
    for line in lines:
        upper = line.upper()
        if any(
            kw in upper
            for kw in (
                "FOR",
                "RETURN",
                "FILTER",
                "LET",
                "SORT",
                "LIMIT",
                "COLLECT",
                "INSERT",
                "UPDATE",
                "REMOVE",
                "WITH",
            )
        ):
            aql_lines.append(line)
        elif aql_lines:
            aql_lines.append(line)
    return "\n".join(aql_lines) if aql_lines else text.strip(), {}


def _validate_aql_syntax(
    aql: str,
    *,
    known_collections: set[str] | None = None,
) -> tuple[bool, str]:
    """Structural AQL syntax check.

    Returns ``(ok, error_message)``.  Checks performed:
    1. At least one top-level AQL clause keyword present.
    2. Balanced parentheses, brackets, and braces.
    3. Every ``FOR`` is followed by a matching ``RETURN``, ``INSERT``,
       ``UPDATE``, or ``REMOVE``.
    4. Collection names referenced via ``FOR … IN <collection>`` or
       ``INTO <collection>`` match *known_collections* when provided.
    """
    if not aql or not aql.strip():
        return False, "empty AQL string"

    upper = aql.upper()
    has_clause = any(kw in upper for kw in ("FOR", "RETURN", "INSERT", "UPDATE", "REMOVE", "LET"))
    if not has_clause:
        return False, "no recognizable AQL clause keyword found"

    for open_ch, close_ch, name in [("(", ")", "parentheses"), ("[", "]", "brackets"), ("{", "}", "braces")]:
        depth = 0
        for ch in aql:
            if ch == open_ch:
                depth += 1
            elif ch == close_ch:
                depth -= 1
            if depth < 0:
                return False, f"unbalanced {name}: unexpected closing '{close_ch}'"
        if depth != 0:
            return False, f"unbalanced {name}: {depth} unclosed '{open_ch}'"

    for_count = len(re.findall(r"\bFOR\b", upper))
    terminal_count = len(re.findall(r"\b(?:RETURN|INSERT|UPDATE|REMOVE)\b", upper))
    if for_count > 0 and terminal_count == 0:
        return False, "FOR clause without a corresponding RETURN/INSERT/UPDATE/REMOVE"

    if known_collections:
        mentioned = set()
        for m in re.finditer(r"\bFOR\s+\w+\s+IN\s+(\w+)", aql):
            token = m.group(1)
            # ``FOR v IN 1..3 OUTBOUND start edge`` is a graph traversal:
            # the token after IN is the depth range (a number), not a
            # collection. Collection identifiers never start with a digit,
            # so skip purely numeric captures rather than flag "1" as an
            # unknown collection. (Two-variable traversals ``FOR v, e IN``
            # don't match this regex at all.)
            if token.isdigit():
                continue
            mentioned.add(token)
        # ``INTO <name>`` names a collection ONLY in the data-modification
        # form ``INSERT <expr> INTO <collection>``. The grouping form
        # ``COLLECT … INTO <var>`` (and ``WITH COUNT INTO <var>``) binds an
        # aggregation *variable*, not a collection — treating it as a
        # collection falsely rejects every valid aggregation query (the
        # classic "unknown collection: group" bug). Scope the check to the
        # INSERT target only; the non-greedy gap stops at the first INTO
        # after INSERT, which is the collection name.
        for m in re.finditer(r"\bINSERT\b[\s\S]*?\bINTO\s+(\w+)", aql, re.IGNORECASE):
            mentioned.add(m.group(1))
        built_in = {"OUTBOUND", "INBOUND", "ANY", "GRAPH"}
        bad = mentioned - known_collections - built_in
        if bad:
            return False, f"unknown collection(s): {', '.join(sorted(bad))}"

    return True, ""


def _aql_tenant_scope_satisfied(
    aql: str,
    *,
    tenant_context: TenantContext,
    manifest: TenantScopeManifest | None,
) -> bool:
    """Return ``True`` if the emitted AQL is tenant-scoped.

    Manifest-aware acceptance, mirroring the Cypher guardrail:

    1. If ``manifest`` is supplied and the AQL references **only**
       collections classified as ``GLOBAL``, the query is
       intentionally cross-tenant — accept.
    2. If the AQL references the ``Tenant`` collection at all,
       assume it is scoped via traversal — accept.
    3. If ``manifest`` is supplied, look at every tenant-scoped
       collection it knows about; if any of them appears in the AQL
       *and* the AQL contains a filter equating that collection's
       denorm field to ``tenant_context.value``, accept.
    4. Otherwise, reject.

    The manifest-less path falls back to rule (2) only — preserves
    v1 behaviour for callers that haven't migrated.
    """
    referenced_names = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", aql or ""))

    if manifest is not None:
        # GLOBAL-only short-circuit: only consider entity names the
        # manifest knows about, otherwise we'd refuse to short-circuit
        # any query that mentions an AQL keyword (FOR, FILTER, …) we
        # haven't classified.
        known_entities = referenced_names & set(manifest.entities.keys())
        if known_entities:
            roles = {manifest.role_of(name) for name in known_entities}
            if (
                roles
                and EntityTenantRole.TENANT_SCOPED not in roles
                and EntityTenantRole.TENANT_ROOT not in roles
            ):
                return True

    if _TENANT_COLLECTION_RE.search(aql or ""):
        return True

    if manifest is not None:
        v = re.escape(tenant_context.value)
        for entity_name in manifest.scoped_entities():
            field_name = manifest.denorm_field_of(entity_name)
            if not field_name:
                continue
            f = re.escape(field_name)
            # Accept any of:
            #   FILTER x.<field> == "<value>"
            #   FILTER x.<field> == '<value>'
            #   x.<field> == @<bind>  (with the bind value matching)
            pattern = re.compile(
                rf"\.\s*{f}\s*={{1,2}}\s*['\"]{v}['\"]",
            )
            if pattern.search(aql):
                return True

    return False


def _call_llm_for_aql(
    question: str,
    schema_summary: str,
    provider: LLMProvider,
    max_retries: int = 2,
    known_collections: set[str] | None = None,
    tenant_context: TenantContext | None = None,
    tenant_manifest: TenantScopeManifest | None = None,
    cypher: str | None = None,
    explain: Callable[[str], str | None] | None = None,
    endpoints: dict[str, tuple[str, str]] | None = None,
) -> NL2AqlResult | None:
    """Call the LLM to generate AQL directly, with validation and retry.

    NL→AQL uses the full physical schema and a distinct system prompt
    (:data:`_AQL_PROMPT_TEMPLATE`), so it deliberately does not share
    :class:`PromptBuilder` with the NL→Cypher path — mixing the two
    would risk leaking physical details into the conceptual prompt
    (see PRD §1.2). The system string is rendered once here and reused
    across retry attempts; only the user message changes per attempt.
    """
    system = _AQL_PROMPT_TEMPLATE.format(schema=schema_summary)
    tenant_block = _tenant_prompt_section(tenant_context, tenant_manifest)
    if tenant_block:
        # Append the tenant-scope block after the schema+syntax
        # reference so it is the last guidance the model reads, giving
        # it strong anchor priority without interleaving with the AQL
        # syntax primer. The manifest-aware block already lists each
        # entity's correct scoping path, so we no longer pin the
        # output to a hardcoded `FOR t IN Tenant` shape — that was
        # incorrect for the GLOBAL-only and denorm-filter cases.
        system = system.rstrip() + "\n\n" + tenant_block.replace(":Tenant", "the Tenant collection")
    last_error = ""
    total_usage: dict[str, int] = {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "cached_tokens": 0,
    }
    base_user = _cypher_to_aql_user_prompt(cypher) if cypher else question
    for attempt in range(1 + max_retries):
        try:
            user = base_user
            if attempt > 0 and last_error:
                user = (
                    f"{base_user}\n\n"
                    f"(Previous attempt produced invalid AQL: {last_error}. "
                    f"Please fix and try again.)"
                )

            result = provider.generate(system, user)
            if isinstance(result, tuple):
                content, usage = result
                for k in total_usage:
                    total_usage[k] += int(usage.get(k, 0) or 0)
            else:
                content = result

            aql, bind_vars = _extract_aql_from_response(content)

            if explain is None:
                ok, err_msg = _validate_aql_syntax(aql, known_collections=known_collections)
            else:
                # The server's parser replaces the regex check, which passes unknown
                # functions and rejects valid queries such as ``FOR x IN <variable>``.
                server_error = explain(aql) if aql.strip() else "empty AQL string"
                ok, err_msg = server_error is None, f"ArangoDB rejected it: {server_error}"
                if server_error and "unknown function" in server_error.lower():
                    err_msg += _UNKNOWN_FUNCTION_HINT
            if ok and endpoints:
                problems = _traversal_direction_problems(aql, endpoints)
                ok, err_msg = not problems, "; ".join(problems)
            if ok:
                # AQL-level tenant postcondition. Manifest-aware:
                # accept GLOBAL-only queries and denorm-field filters
                # in addition to traversal-from-Tenant. See
                # ``_aql_tenant_scope_satisfied`` for the full
                # acceptance contract.
                if tenant_context is not None and not _aql_tenant_scope_satisfied(
                    aql,
                    tenant_context=tenant_context,
                    manifest=tenant_manifest,
                ):
                    last_error = (
                        "Query is not scoped to the active tenant. "
                        "Either filter a tenant-scoped collection on "
                        "its denormalised tenant field "
                        f"(e.g. `FILTER d.<TENANT_FIELD> == "
                        f'"{tenant_context.value}"`), OR bind the '
                        "Tenant collection by `_key` and traverse to "
                        "the target via the schema's tenant-scoping "
                        "edges. See the per-entity scoping rules above."
                    )
                    logger.warning(
                        "AQL tenant-scoping violation (attempt %d/%d)",
                        attempt + 1,
                        1 + max_retries,
                    )
                    continue

                return NL2AqlResult(
                    aql=aql,
                    bind_vars=bind_vars,
                    explanation=content,
                    confidence=0.8,
                    method="llm_direct",
                    schema_context=schema_summary,
                    prompt_tokens=total_usage["prompt_tokens"],
                    completion_tokens=total_usage["completion_tokens"],
                    total_tokens=total_usage["total_tokens"],
                    cached_tokens=total_usage["cached_tokens"],
                )

            last_error = err_msg or "generated text did not parse as AQL"
            logger.info(
                "LLM AQL attempt %d/%d: validation failed (%s) for: %s",
                attempt + 1,
                1 + max_retries,
                last_error,
                aql[:120],
            )
        except Exception as e:
            logger.warning("LLM AQL call failed (attempt %d): %s", attempt + 1, e)
            last_error = str(e)

    return None


def _collect_known_collections(bundle: MappingBundle) -> set[str]:
    """Extract all physical collection names from the mapping for AQL validation."""
    pm = bundle.physical_mapping
    cols: set[str] = set()
    if isinstance(pm.get("entities"), dict):
        for ent in pm["entities"].values():
            if isinstance(ent, dict) and ent.get("collectionName"):
                cols.add(ent["collectionName"])
    if isinstance(pm.get("relationships"), dict):
        for rel in pm["relationships"].values():
            if isinstance(rel, dict) and rel.get("edgeCollectionName"):
                cols.add(rel["edgeCollectionName"])
    return cols


def _cypher_to_aql_user_prompt(cypher: str) -> str:
    """Frame a failing Cypher query as a Cypher->AQL translation task.

    Used by the workbench's "Generate AQL with AI" recovery action when a
    Cypher query cannot be transpiled deterministically (e.g. it uses a
    feature outside the v0 subset). The system prompt is unchanged — the
    model is still the AQL expert with the full physical schema — only the
    user message reframes the task from "answer this question" to "translate
    this Cypher".
    """
    return (
        "Translate the following openCypher query into an equivalent ArangoDB "
        "AQL query, using ONLY the collections, edge collections, and fields "
        "described in the schema above. Preserve the original intent: keep the "
        "same filters, traversal directions, grouping/aggregation, ordering, "
        "and limits. Map Cypher labels and relationship types to the matching "
        "collections or type-discriminator fields from the schema.\n\n"
        f"Cypher query:\n{cypher.strip()}"
    )


def nl_to_aql(
    question: str,
    *,
    mapping: MappingBundle | dict[str, Any] | None = None,
    llm_provider: LLMProvider | None = None,
    max_retries: int = 2,
    tenant_context: TenantContext | None = None,
    cypher: str | None = None,
    postconditions: Sequence[Postcondition] | None = None,
    explain: Callable[[str], str | None] | None = None,
) -> NL2AqlResult:
    """Translate a natural language question directly to AQL.

    Unlike :func:`nl_to_cypher`, this bypasses the intermediate Cypher
    representation and generates AQL directly by providing the LLM with
    the full physical schema (collection names, edge collections, field
    names, type discriminators).

    Requires an LLM — there is no rule-based fallback for direct AQL
    generation.

    Args:
        question: Plain English question about the graph.
        mapping: Schema mapping (MappingBundle or export dict).
        llm_provider: A custom LLM provider. If None, uses OpenAI
            provider from environment variables.
        max_retries: Number of retry attempts if LLM output fails validation.
        tenant_context: Active tenant scope, if any.
        cypher: When set, the LLM is asked to translate this Cypher query to
            AQL instead of answering ``question``. This powers the workbench
            fallback for Cypher that the deterministic transpiler cannot
            handle. ``question`` may still be passed for logging/context.
        explain: Optional server check: returns the server's error for a candidate
            query, or ``None`` when it parses and plans. Replaces the regex check,
            and the model repairs against its message.
    """
    if postconditions:
        # This path generates AQL directly, so there is no Cypher for a
        # postcondition to inspect. Refusing is deliberate: silently accepting
        # a safety check and then not running it is strictly worse than not
        # offering the parameter at all. Callers who need postconditions want
        # nl_to_cypher(), which has the retry loop they enforce through.
        raise ValueError(
            "postconditions are not supported by nl_to_aql(): this path bypasses "
            "Cypher generation, so there is nothing for a Cypher postcondition to "
            "check. Use nl_to_cypher(..., postconditions=[...]) instead."
        )

    if mapping is None:
        return NL2AqlResult(
            aql="",
            bind_vars={},
            explanation="No schema mapping provided. Cannot generate AQL without knowing the database structure.",
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

    schema_summary = _build_physical_schema_summary(bundle)

    base_provider = llm_provider or _get_default_provider()
    if base_provider is None:
        return NL2AqlResult(
            aql="",
            bind_vars={},
            explanation="No LLM provider configured. Direct NL→AQL requires an LLM. Set OPENAI_API_KEY in .env.",
            confidence=0.0,
            method="none",
        )

    known_collections = _collect_known_collections(bundle)
    tenant_manifest = analyze_tenant_scope(bundle) if tenant_context else None
    result = _call_llm_for_aql(
        question,
        schema_summary,
        base_provider,
        max_retries=max_retries,
        known_collections=known_collections,
        tenant_context=tenant_context,
        tenant_manifest=tenant_manifest,
        cypher=cypher,
        explain=explain,
        endpoints=_edge_endpoints(bundle),
    )
    if result and result.aql:
        return result

    if tenant_context is not None:
        return NL2AqlResult(
            aql="",
            bind_vars={},
            explanation=(
                "Tenant-scoping guardrail: could not produce an AQL query "
                f"scoped to tenant {tenant_context.display_name!r} after "
                f"{1 + max_retries} attempts. Refusing to emit an unscoped "
                "query."
            ),
            confidence=0.0,
            method="tenant_guardrail_blocked",
        )

    return NL2AqlResult(
        aql="",
        bind_vars={},
        explanation=(
            "LLM could not translate this Cypher query to AQL. The query may use "
            "a feature with no AQL equivalent in this schema."
            if cypher
            else "LLM could not generate valid AQL. Try rephrasing the question."
        ),
        confidence=0.0,
        method="llm_direct",
    )
