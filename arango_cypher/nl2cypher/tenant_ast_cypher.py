"""Wave 8a MT-3a — Cypher AST tenant-injection (core).

This module is the **first phase** of the Layer 3 rewriter described in
``docs/multitenant_prd.md`` §6. MT-3 was sliced into three phases so each
phase fits in a single sub-agent run; the boundaries are:

* **MT-3a (this module)** — visitor skeleton + property-map injection
  for the *easy* cases. Handles TENANT_ROOT (``_key: $tenantKey``),
  TENANT_SCOPED with a denorm field, GLOBAL pass-through, literal
  rejection in property maps / WHERE / UNWIND, and the unknown-label
  REFUSE path. Traversal-only TENANT_SCOPED entities raise
  :class:`TenantScopeRewriteIncomplete`.
* **MT-3b** — traversal-path promotion for TENANT_SCOPED entities that
  have no denorm field (the
  ``(:Tenant {_key: $tenantKey})-[:R]->...->(e:Employee)`` rewrite from
  PRD §6.3, last example). MT-3b consumes the
  :class:`TenantScopeRewriteIncomplete` signal that MT-3a raises and
  catches the case here.
* **MT-3c** — route + UI wiring. Calls :func:`inject_tenant_scope` from
  ``arango_cypher/service/routes/cypher.py`` and surfaces the changes
  list as ``tenantRewrites`` on the translate-response, rendered in the
  Cypher editor's annotation strip.

Why a phased rollout
--------------------
The full Layer 3 spec (visitor + path-promotion + dedup + route + UI)
is ~400 LOC plus 20 tests. A single sub-agent run that touches a new
AST module, a service route, a response model, a UI panel, and
introduces 20+ goldens routinely blows the run's budget — Wave 8a's
prior attempts under ``feat/wave8a-mt3-cypher-ast-tenant-injection``
and ``-retry`` both stranded mid-implementation. Splitting at the
*algorithmic seam* (denorm-field path vs. traversal-path) keeps each
slice deployable in isolation and lets Layer 5 catch any
TENANT_SCOPED collection that MT-3a refuses to rewrite (it raises
``TenantScopeRewriteIncomplete``; the caller falls back to the
pre-rewrite Cypher and Layer 5's plan validator then enforces the
guard at execute time).

Design notes
------------
* **No external visitor base class.** The codebase had a recent cleanup
  that removed the generated ``CypherVisitor`` (only ``CypherLexer`` and
  ``CypherParser`` remain under ``arango_cypher/_antlr/``). Subclassing
  ``ParseTreeVisitor`` would require us to dispatch ``visitOC_*``
  manually anyway because the parser contexts call
  ``visitor.visitOC_NodePattern(self)`` only when the visitor defines
  that method — there is no auto-generated walker. We instead do a
  small recursive ``getRuleIndex`` switch (see :func:`_walk_rule`)
  that's easier to read for the three rules we care about and avoids
  pulling in code we'd otherwise need to regenerate from grammar
  changes.

* **Text edits keyed by char offset.** ANTLR4's
  ``ParserRuleContext.getText()`` is byte-faithful for unmodified
  subtrees because the Cypher grammar puts ``SP`` (whitespace) on the
  default channel — every whitespace token is consumed by some rule
  and therefore appears in ``getText()``. For *modified* node
  patterns we re-render the property map manually (alphabetical
  existing keys, tenant key last) and splice the new text in via
  reverse-offset string edits on the original Cypher source. This
  keeps the unchanged spans byte-identical without needing the
  ``TokenStreamRewriter`` (which would require the
  ``BufferedTokenStream`` we don't currently expose from
  :func:`arango_cypher.parser.parse_cypher`).

* **Rejection codes mirror Layer 5.** :class:`TenantScopeRewriteRejection`
  carries a short ``code`` string that matches the codes Layer 5 emits
  (see ``arango_cypher/tenant_plan_validator.py``:
  ``LITERAL_TENANT_PREDICATE``, ``UNKNOWN_ENTITY``). MT-3c will surface
  the same codes via the response model so the UI's error path
  doesn't have to know whether a refusal came from Layer 3, Layer 4,
  or Layer 5.

PRD source-of-truth
-------------------
* ``docs/multitenant_prd.md`` §6.1 / §6.2 / §6.3 / §6.5.
* ``docs/agent_prompts_multitenant.md`` MT-3 section (Wave 8a).
* :func:`arango_cypher.nl2cypher.tenant_ast_common.predicate_for_entity`
  is the single source of truth for "what predicate shape is needed
  for this label" — we never reinvent the role/denorm-field decision
  tree here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from antlr4 import ParserRuleContext

from arango_cypher._antlr.CypherParser import CypherParser

from .tenant_ast_common import (
    TENANT_ID_BIND,
    TENANT_KEY_BIND,
    UnknownEntityScope,
    is_literal_tenant_value,
    predicate_for_entity,
)
from .tenant_scope import TenantScopeManifest

__all__ = [
    "TenantScopeRewriteIncomplete",
    "TenantScopeRewriteRejection",
    "inject_tenant_scope",
]


# ---------------------------------------------------------------------------
# Public exceptions
# ---------------------------------------------------------------------------


class TenantScopeRewriteRejection(Exception):
    """The rewriter refuses to scope the query.

    Carries a short ``code`` string that matches the codes emitted by
    Layer 5 (``arango_cypher/tenant_plan_validator.py``) so the route
    layer (MT-3c) can render a single error UX regardless of which
    layer raised. The ``where`` field is a free-form locator (e.g.
    ``"(e:Employee)"`` or ``"WHERE e.TENANT_HEX_ID = 'tenant-B-uuid'"``)
    surfaced into the user-facing error message.

    Codes used by MT-3a
    -------------------
    * ``LITERAL_TENANT_PREDICATE`` — a property-map / WHERE / UNWIND
      literal carries a tenant identifier. The session bind variable
      is the only legitimate channel for tenant identity (PRD §6.2,
      §5.2).
    * ``UNKNOWN_ENTITY`` — a node pattern references a label that the
      manifest does not classify. Fail-closed: we cannot tell whether
      the label is tenant-scoped without an entry in the manifest.

    Codes reserved for MT-3b / MT-3c
    --------------------------------
    Future phases may add codes for traversal-path collisions and
    multi-tenant join refusal. They must keep the same dataclass
    shape so the response model in MT-3c does not have to special-case
    them.
    """

    def __init__(self, code: str, message: str, *, where: str = "") -> None:
        super().__init__(f"{code}: {message}" + (f" ({where})" if where else ""))
        self.code = code
        self.message = message
        self.where = where


class TenantScopeRewriteIncomplete(Exception):
    """MT-3a cannot fully rewrite the query; MT-3b is required.

    Raised when a node pattern is over a TENANT_SCOPED entity that has
    no denormalised tenant column (only a scoping-path through the
    relationship graph). The full rewrite would replace the bare
    pattern with a path from the ``Tenant`` root — that logic is
    deferred to MT-3b.

    For now, the caller (route layer in MT-3c) is expected to:

    1. Catch this exception.
    2. Fall back to the *original* (un-rewritten) Cypher.
    3. Let Layer 5 (``tenant_plan_validator.validate_plan``) refuse
       the query at execute-time if it actually leaks across tenants.

    This is safe because Layer 5 is the security boundary; MT-3a's
    job is best-effort rewrite, not gatekeeping. The
    ``IncompleteRewrite`` signal exists so MT-3c can log "tenant
    rewrite skipped — incomplete" rather than masquerading as
    "tenant rewrite applied".
    """

    def __init__(self, label: str, where: str = "") -> None:
        super().__init__(
            f"tenant scope rewrite incomplete for {label!r}"
            + (f" ({where})" if where else "")
            + "; MT-3b will handle traversal-path promotion"
        )
        self.label = label
        self.where = where


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def inject_tenant_scope(
    *,
    cypher: str,
    parse_tree: Any,
    manifest: TenantScopeManifest,
    tenant_id: str,
    tenant_key: str,
) -> tuple[str, list[str]]:
    """Rewrite ``cypher`` so every tenant-scoped node pattern carries a
    bind-variable tenant predicate.

    Parameters
    ----------
    cypher:
        The original Cypher source text. Used as the source of truth
        for byte offsets when splicing in the new property maps.
    parse_tree:
        The ANTLR4 parse tree produced by
        :func:`arango_cypher.parser.parse_cypher`. Must correspond to
        ``cypher`` (same input string); MT-3a does not re-parse.
    manifest:
        Tenant-scope manifest built by
        :func:`arango_cypher.nl2cypher.tenant_scope.analyze_tenant_scope`.
        Drives the per-label scope decision via
        :func:`predicate_for_entity`.
    tenant_id:
        Bound to ``@tenantId``; only used when MT-3c renders the
        response (MT-3a never embeds the value literally — every
        rewrite uses the bind variable ``$tenantId``).
    tenant_key:
        Bound to ``@tenantKey``; same caveat as ``tenant_id``.

    Returns
    -------
    rewritten_cypher, changes:
        ``rewritten_cypher`` is the modified text; identical to
        ``cypher`` when no rewrites were necessary (all labels GLOBAL).
        ``changes`` is a list of human-readable descriptions, one per
        injection, intended for MT-3c's UI annotation strip.

    Raises
    ------
    TenantScopeRewriteRejection
        On a literal tenant predicate (T2 defence), an unknown label,
        or a literal-tenant key inside an UNWIND list.
    TenantScopeRewriteIncomplete
        On a TENANT_SCOPED node pattern that requires the MT-3b
        traversal-path promotion. The caller must catch this and fall
        back to the original Cypher (Layer 5 still enforces).
    """
    # Touch the supplied tenant identifiers so the call signature is
    # stable for MT-3b/MT-3c. MT-3a does not embed the values into the
    # rewritten Cypher — only bind-variable *references* (``$tenantId``
    # / ``$tenantKey``) are emitted — but the parameters exist on the
    # public API so future phases (path-promotion, route wiring) can
    # consume them without breaking callers.
    _ = (tenant_id, tenant_key)

    if not isinstance(cypher, str):
        raise TypeError("cypher must be a string")
    if parse_tree is None:
        raise ValueError("parse_tree must not be None")

    # First pass: walk all node patterns. This collects every
    # (variable, label) binding we see (so the WHERE pass can resolve
    # `e.TENANT_HEX_ID` to the right manifest entry) AND records the
    # property-map edits we need to perform.
    var_to_labels: dict[str, list[str]] = {}
    edits: list[_Edit] = []
    changes: list[str] = []

    for node_pat in _find_all(parse_tree, CypherParser.RULE_oC_NodePattern):
        _visit_node_pattern(
            node_pat=node_pat,
            cypher=cypher,
            manifest=manifest,
            var_to_labels=var_to_labels,
            edits=edits,
            changes=changes,
        )

    # Second pass: WHERE clauses. We refuse any literal-tenant equality
    # on a known tenant field; we do NOT rewrite bind-var predicates
    # (they're already correct) and we do NOT add new WHERE clauses
    # (the property-map injection above is the canonical site for
    # MT-3a; MT-3b may introduce WHERE-form predicates for traversal
    # cases).
    for where_ctx in _find_all(parse_tree, CypherParser.RULE_oC_Where):
        _visit_where(
            where_ctx=where_ctx,
            cypher=cypher,
            manifest=manifest,
            var_to_labels=var_to_labels,
        )

    # Third pass: UNWIND with a literal list — if any element matches a
    # known tenant key, refuse. This is the T2 defence against
    # ``UNWIND ['tenant-A-uuid'] AS t MATCH (...) WHERE x.foo = t ...``.
    for unwind_ctx in _find_all(parse_tree, CypherParser.RULE_oC_Unwind):
        _visit_unwind(unwind_ctx=unwind_ctx, manifest=manifest)

    # Apply the edits we accumulated in pass 1. We sort by descending
    # start offset so each splice doesn't shift the offsets of later
    # splices.
    rewritten = _apply_edits(cypher, edits)
    return rewritten, changes


# ---------------------------------------------------------------------------
# Internal data types
# ---------------------------------------------------------------------------


@dataclass
class _Edit:
    """A single text edit to apply to the Cypher source.

    Offsets are 0-based; ``end`` is exclusive. ``start == end`` means
    "insert ``text`` at this position without deleting anything".
    """

    start: int
    end: int
    text: str


# ---------------------------------------------------------------------------
# Pass 1 — node patterns
# ---------------------------------------------------------------------------


def _visit_node_pattern(
    *,
    node_pat: Any,
    cypher: str,
    manifest: TenantScopeManifest,
    var_to_labels: dict[str, list[str]],
    edits: list[_Edit],
    changes: list[str],
) -> None:
    """Inspect one ``(var:Label {props})`` site; record binding + edit."""
    var_name = _node_pattern_variable(node_pat)
    labels = _node_pattern_labels(node_pat)

    # Record the variable→label binding for the WHERE pass even when
    # no rewrite is needed (e.g. GLOBAL labels still let us reason
    # about tenant-field predicates on them — though there shouldn't
    # be any, the binding is harmless and keeps the resolver simple).
    if var_name is not None:
        for label in labels:
            var_to_labels.setdefault(var_name, []).append(label)

    # Patterns with no label (e.g. ``(n)`` or ``()``) cannot be
    # classified. They're allowed: the LLM almost never emits them
    # outside of variable-length path heads, and forcing a rewrite
    # would refuse legitimate intra-tenant joins like
    # ``MATCH (n)-[:R]->(e:Employee)`` where ``n`` is constrained by
    # a prior pattern.
    if not labels:
        return

    # MT-3a only handles single-label node patterns (the common case).
    # Multi-label patterns are extremely rare in NL→Cypher output and
    # introduce ambiguous rewrites ("which label's denorm field do we
    # inject?"). We surface the ambiguity rather than guess.
    # AMBIGUITY: Multi-label patterns (e.g. ``(e:Employee:Person)``)
    # are deferred to MT-3b along with the traversal-path case; for
    # now we raise Incomplete so the caller falls back to the
    # un-rewritten Cypher and Layer 5 enforces.
    if len(labels) > 1:
        raise TenantScopeRewriteIncomplete(
            ":".join(labels),
            where=_describe_node_pattern(var_name, labels),
        )

    label = labels[0]

    try:
        shape = predicate_for_entity(label, manifest)
    except UnknownEntityScope as exc:
        raise TenantScopeRewriteRejection(
            "UNKNOWN_ENTITY",
            f"label {label!r} is not in the tenant-scope manifest",
            where=_describe_node_pattern(var_name, labels),
        ) from exc

    if shape is None:
        # GLOBAL → no injection. Leave the pattern byte-identical.
        return

    if shape.style == "traversal_path":
        # Traversal-only TENANT_SCOPED → MT-3b territory. Bail out
        # cleanly so the caller can fall through to Layer 5.
        raise TenantScopeRewriteIncomplete(
            label,
            where=_describe_node_pattern(var_name, labels),
        )

    if shape.style != "property_map":
        # Defensive: the only styles MT-3a is meant to handle are
        # ``property_map`` (this branch) and ``traversal_path``
        # (handled above). ``where_eq`` is currently unused by
        # :func:`predicate_for_entity` and ``prune`` is Layer 4 only,
        # but we surface the ambiguity rather than silently skipping
        # a predicate we don't know how to render here.
        raise TenantScopeRewriteIncomplete(
            label,
            where=_describe_node_pattern(var_name, labels),
        )

    assert shape.field is not None  # invariant for property_map style
    tenant_field = shape.field
    bind_name = shape.bind_name

    # Inspect the existing property map (if any) for a pre-existing
    # entry on the tenant field. Three outcomes:
    #   * No map present → splice a new ``{<tenant_field>: $<bind>}`` map.
    #   * Map present, no entry for tenant_field → merge.
    #   * Map present, entry references the bind variable → idempotent,
    #     no rewrite needed.
    #   * Map present, entry is a literal → REFUSE.
    existing_props = node_pat.oC_Properties()
    existing_entries = _read_property_map(existing_props) if existing_props is not None else []

    found_tenant_entry = False
    for key, value_ctx in existing_entries:
        if key != tenant_field:
            continue
        found_tenant_entry = True
        value_text = value_ctx.getText().strip()
        if value_text == f"${bind_name}":
            # Idempotent — Cypher already references the bind variable.
            # Skip the rewrite so a re-run of the pass returns
            # byte-identical output.
            return
        # Anything that is not the exact bind-variable reference is
        # treated as a literal — including comparisons to other bind
        # variables (the LLM should not be picking the tenant bind
        # variable's name). The narrow definition lets MT-3c keep
        # the response model unambiguous.
        if is_literal_tenant_value(_string_literal_value(value_ctx), manifest):
            raise TenantScopeRewriteRejection(
                "LITERAL_TENANT_PREDICATE",
                (
                    f"literal tenant value for {tenant_field!r} on "
                    f"({var_name or '?'}:{label}); the session bind "
                    f"variable ${bind_name} is the only legitimate channel"
                ),
                where=_describe_node_pattern(var_name, labels),
            )
        # Non-tenant literal at this key is also a refusal — the field
        # *is* the tenant field, so any non-bindvar RHS is unsafe.
        raise TenantScopeRewriteRejection(
            "LITERAL_TENANT_PREDICATE",
            (
                f"non-bind-variable value {value_text!r} for tenant "
                f"field {tenant_field!r} on ({var_name or '?'}:{label})"
            ),
            where=_describe_node_pattern(var_name, labels),
        )

    if found_tenant_entry:
        # Defensive: the early return inside the loop on a bind-var
        # match should have already exited. This is unreachable in
        # practice.
        return

    # Build the merged property map text and record the edit.
    merged_text = _render_merged_property_map(
        existing_entries=existing_entries,
        tenant_field=tenant_field,
        tenant_bind_name=bind_name,
    )

    if existing_props is None:
        insertion_offset = _insertion_offset_after_labels(node_pat)
        edits.append(_Edit(start=insertion_offset, end=insertion_offset, text=f" {merged_text}"))
    else:
        # Replace the existing properties span with the merged map.
        start = existing_props.start.start
        end = existing_props.stop.stop + 1
        edits.append(_Edit(start=start, end=end, text=merged_text))

    changes.append(f"Added {tenant_field} = ${bind_name} to {_describe_node_pattern(var_name, labels)}")


def _node_pattern_variable(node_pat: Any) -> str | None:
    """Return the variable name on a node pattern, or ``None``."""
    var_ctx = node_pat.oC_Variable()
    if var_ctx is None:
        return None
    return var_ctx.getText().strip()


def _node_pattern_labels(node_pat: Any) -> list[str]:
    """Return the list of label names on a node pattern (possibly empty)."""
    labels_ctx = node_pat.oC_NodeLabels()
    if labels_ctx is None:
        return []
    node_labels = labels_ctx.oC_NodeLabel() or []
    names: list[str] = []
    for nl in node_labels:
        ln = nl.oC_LabelName()
        if ln is None:
            continue
        text = ln.getText().strip()
        if text:
            names.append(text)
    return names


def _describe_node_pattern(var: str | None, labels: list[str]) -> str:
    """Render a node pattern as ``(var:Label)`` for error / change copy."""
    var_part = var if var else ""
    label_part = ":" + ":".join(labels) if labels else ""
    return f"({var_part}{label_part})"


def _insertion_offset_after_labels(node_pat: Any) -> int:
    """Return the char offset to insert a new property map at.

    Insertion point is *immediately after* the last "interesting"
    subrule of the node pattern — labels if present, else the variable,
    else the opening ``(``. This avoids the awkward double-space that
    would happen if we inserted at the closing ``)`` of a pattern that
    already has trailing whitespace like ``(e:Employee )``.
    """
    labels_ctx = node_pat.oC_NodeLabels()
    if labels_ctx is not None:
        return labels_ctx.stop.stop + 1
    var_ctx = node_pat.oC_Variable()
    if var_ctx is not None:
        return var_ctx.stop.stop + 1
    # Pattern is ``()`` — insert right after the opening paren.
    return node_pat.start.stop + 1


def _read_property_map(props_ctx: Any) -> list[tuple[str, Any]]:
    """Return ``[(key_text, value_ctx), ...]`` for a properties subtree.

    ``props_ctx`` is an ``OC_PropertiesContext``. The grammar lets the
    properties be either a literal map ``{...}`` or a bind-variable
    parameter ``$param``; only the literal-map case carries
    key/value pairs we can introspect.
    """
    map_lit = props_ctx.oC_MapLiteral() if hasattr(props_ctx, "oC_MapLiteral") else None
    if map_lit is None:
        return []
    keys = map_lit.oC_PropertyKeyName() or []
    vals = map_lit.oC_Expression() or []
    out: list[tuple[str, Any]] = []
    for key_ctx, val_ctx in zip(keys, vals, strict=False):
        out.append((key_ctx.getText().strip(), val_ctx))
    return out


def _render_merged_property_map(
    *,
    existing_entries: list[tuple[str, Any]],
    tenant_field: str,
    tenant_bind_name: str,
) -> str:
    """Render the merged property map text.

    Ordering contract (per the MT-3a task spec):

    * Existing (non-tenant) keys are emitted in **alphabetical**
      order — deterministic across runs.
    * The tenant field is always emitted **last** so it stands out
      visually in the rewritten Cypher.

    Existing values are echoed via ``ctx.getText()``, which preserves
    the user's original literal byte-for-byte (string quoting,
    function calls, parameter references, etc.).
    """
    non_tenant = sorted(
        (k for k, _ in existing_entries if k != tenant_field),
        key=str,
    )
    value_by_key: dict[str, str] = {k: v.getText() for k, v in existing_entries}
    pairs = [f"{k}: {value_by_key[k]}" for k in non_tenant]
    pairs.append(f"{tenant_field}: ${tenant_bind_name}")
    return "{" + ", ".join(pairs) + "}"


def _string_literal_value(value_ctx: Any) -> str | None:
    """If ``value_ctx`` is a bare string literal expression, return the
    unquoted value; otherwise return ``None``.

    Walks the single-child expression chain (``oC_Expression`` →
    ``oC_OrExpression`` → … → ``oC_Atom`` → ``oC_Literal``) and only
    returns a value when every intermediate node has exactly one
    rule-context child. Anything that branches (binary operator,
    function call, etc.) is by definition not a bare literal.
    """
    node: Any = value_ctx
    while node is not None and isinstance(node, ParserRuleContext):
        if node.getRuleIndex() == CypherParser.RULE_oC_Atom:
            literal = node.oC_Literal() if hasattr(node, "oC_Literal") else None
            if literal is None:
                return None
            sl = literal.StringLiteral() if hasattr(literal, "StringLiteral") else None
            if sl is None:
                return None
            return _strip_quotes(sl.getText())
        rule_children = [
            node.getChild(i)
            for i in range(node.getChildCount())
            if isinstance(node.getChild(i), ParserRuleContext)
        ]
        if len(rule_children) != 1:
            return None
        node = rule_children[0]
    return None


def _strip_quotes(text: str) -> str:
    """Drop the surrounding quote characters from a Cypher string literal."""
    if len(text) >= 2 and text[0] in ("'", '"') and text[-1] == text[0]:
        return text[1:-1]
    return text


# ---------------------------------------------------------------------------
# Pass 2 — WHERE clauses
# ---------------------------------------------------------------------------


def _visit_where(
    *,
    where_ctx: Any,
    cypher: str,
    manifest: TenantScopeManifest,
    var_to_labels: dict[str, list[str]],
) -> None:
    """Refuse any equality predicate ``var.<tenant_field> = <literal>``.

    Bind-variable predicates (``= $tenantId``) and non-tenant-field
    predicates are left alone. We deliberately don't rewrite anything
    here — the property-map injection in pass 1 is canonical; pass 2
    only enforces the T2 defence (no literal tenant value may appear
    as the RHS of a tenant-field equality).
    """
    _ = cypher  # reserved for future error-message context
    for cmp_ctx in _find_all(where_ctx, CypherParser.RULE_oC_ComparisonExpression):
        lhs_var, lhs_field = _decode_property_path_lhs(cmp_ctx)
        if lhs_var is None or lhs_field is None:
            continue
        # Resolve the variable to one or more labels. A single variable
        # may have multiple bindings across patterns; if *any* of them
        # is a tenant-scoped label whose denorm field matches
        # ``lhs_field``, we treat the predicate as tenant-relevant.
        labels = var_to_labels.get(lhs_var, [])
        if not _is_tenant_field_for_any_label(
            field=lhs_field,
            labels=labels,
            manifest=manifest,
        ):
            continue

        # The LHS is on a tenant field — examine the RHS of every
        # equality operator on this comparison.
        for op_text, rhs_ctx in _partial_comparison_rhs_pairs(cmp_ctx):
            if op_text != "=":
                # We deliberately scope MT-3a to the ``=`` case. ``IN``
                # and other operators on tenant fields would also be
                # refusable, but the spec scopes the first phase to
                # equality.
                continue
            rhs_text = rhs_ctx.getText().strip()
            if rhs_text.startswith("$"):
                # Bind-variable reference — already in the canonical
                # shape. No action.
                continue
            literal_value = _string_literal_value(rhs_ctx)
            if literal_value is not None and is_literal_tenant_value(literal_value, manifest):
                raise TenantScopeRewriteRejection(
                    "LITERAL_TENANT_PREDICATE",
                    (
                        f"literal tenant value {literal_value!r} on "
                        f"{lhs_var}.{lhs_field}; bind to ${TENANT_ID_BIND} "
                        f"or ${TENANT_KEY_BIND} instead"
                    ),
                    where=f"WHERE {lhs_var}.{lhs_field} = {rhs_text}",
                )


def _decode_property_path_lhs(cmp_ctx: Any) -> tuple[str | None, str | None]:
    """If the LHS of ``cmp_ctx`` is ``var.field``, return ``(var, field)``.

    Returns ``(None, None)`` for any LHS that is not a single
    property lookup on a variable atom — function calls, nested
    expressions, plain variables, plain literals, etc.
    """
    # The comparison's operand is an ``IN`` level (a single operand when it
    # holds no ``IN``) around the additive expression.
    in_ctx = cmp_ctx.oC_InExpression() if hasattr(cmp_ctx, "oC_InExpression") else None
    add_subs = (
        in_ctx.oC_AddOrSubtractExpression()
        if in_ctx is not None and hasattr(in_ctx, "oC_AddOrSubtractExpression")
        else None
    )
    if add_subs is None:
        return (None, None)
    # ``oC_AddOrSubtractExpression()`` returns a list when there are
    # repeated terms; we want the very first one (the LHS).
    if isinstance(add_subs, list):
        if not add_subs:
            return (None, None)
        lhs = add_subs[0]
    else:
        lhs = add_subs
    # Walk single-rule-context children until we land on an
    # ``OC_PropertyOrLabelsExpressionContext``.
    plo = _find_single_descendant(lhs, CypherParser.RULE_oC_PropertyOrLabelsExpression)
    if plo is None:
        return (None, None)
    atom = plo.oC_Atom() if hasattr(plo, "oC_Atom") else None
    lookups = plo.oC_PropertyLookup() or []
    if atom is None or not lookups or len(lookups) != 1:
        return (None, None)
    var_ctx = atom.oC_Variable() if hasattr(atom, "oC_Variable") else None
    if var_ctx is None:
        return (None, None)
    var_name = var_ctx.getText().strip()
    pkn = lookups[0].oC_PropertyKeyName() if hasattr(lookups[0], "oC_PropertyKeyName") else None
    if pkn is None:
        return (None, None)
    field_name = pkn.getText().strip()
    if not var_name or not field_name:
        return (None, None)
    return (var_name, field_name)


def _partial_comparison_rhs_pairs(cmp_ctx: Any) -> list[tuple[str, Any]]:
    """Return ``[(op_text, rhs_addsub_ctx), ...]`` for each partial comparison.

    For ``a = b = c``, ANTLR produces a single
    ``OC_ComparisonExpressionContext`` with two
    ``OC_PartialComparisonExpression`` children: ``= b`` and ``= c``.
    For our T2 check we only need the operator + the RHS expression;
    we don't bother reconstructing the implicit "previous comparand".
    """
    if not hasattr(cmp_ctx, "oC_PartialComparisonExpression"):
        return []
    partials = cmp_ctx.oC_PartialComparisonExpression() or []
    out: list[tuple[str, Any]] = []
    for partial in partials:
        # The first child is the operator token (``=``, ``<>``, etc.).
        if partial.getChildCount() == 0:
            continue
        op_text = partial.getChild(0).getText()
        rhs_ctx = partial.oC_InExpression() if hasattr(partial, "oC_InExpression") else None
        if rhs_ctx is None:
            continue
        out.append((op_text, rhs_ctx))
    return out


def _is_tenant_field_for_any_label(
    *,
    field: str,
    labels: list[str],
    manifest: TenantScopeManifest,
) -> bool:
    """Return ``True`` if ``field`` is the tenant predicate field for any
    of ``labels`` in ``manifest``.

    We swallow :class:`UnknownEntityScope` here — pass 2 should not
    re-refuse on an unknown label after pass 1 has either accepted or
    refused it. The point of this helper is the narrow "is this field
    the tenant denorm field on this variable's label?" question.
    """
    for label in labels:
        try:
            shape = predicate_for_entity(label, manifest)
        except UnknownEntityScope:
            continue
        if shape is None:
            continue
        if shape.field is None:
            continue
        if shape.field == field:
            return True
    return False


# ---------------------------------------------------------------------------
# Pass 3 — UNWIND
# ---------------------------------------------------------------------------


def _visit_unwind(*, unwind_ctx: Any, manifest: TenantScopeManifest) -> None:
    """Refuse ``UNWIND [<tenant literal>, ...]``.

    Only literal list expressions are checked. ``UNWIND $tenants AS t``
    is fine — the bind variable comes from the trusted session.
    """
    expr_ctx = unwind_ctx.oC_Expression() if hasattr(unwind_ctx, "oC_Expression") else None
    if expr_ctx is None:
        return
    list_lit = _find_single_descendant(expr_ctx, CypherParser.RULE_oC_ListLiteral)
    if list_lit is None:
        return
    items = list_lit.oC_Expression() if hasattr(list_lit, "oC_Expression") else []
    items = items or []
    for item_ctx in items:
        literal_value = _string_literal_value(item_ctx)
        if literal_value is None:
            continue
        if is_literal_tenant_value(literal_value, manifest):
            raise TenantScopeRewriteRejection(
                "LITERAL_TENANT_PREDICATE",
                (f"UNWIND list literal contains known tenant key {literal_value!r}"),
                where=f"UNWIND {expr_ctx.getText()}",
            )


# ---------------------------------------------------------------------------
# Tree walking helpers
# ---------------------------------------------------------------------------


def _walk_rule(root: Any, visitor: Any) -> None:
    """Pre-order DFS over every rule-context descendant of ``root``."""
    visitor(root)
    if not hasattr(root, "getChildCount"):
        return
    for i in range(root.getChildCount()):
        child = root.getChild(i)
        if isinstance(child, ParserRuleContext):
            _walk_rule(child, visitor)


def _find_all(root: Any, rule_index: int) -> list[Any]:
    """Return every descendant ``ParserRuleContext`` matching ``rule_index``."""
    out: list[Any] = []

    def _collect(node: Any) -> None:
        if hasattr(node, "getRuleIndex") and node.getRuleIndex() == rule_index:
            out.append(node)

    _walk_rule(root, _collect)
    return out


def _find_single_descendant(root: Any, rule_index: int) -> Any | None:
    """Return the first descendant ``ParserRuleContext`` matching
    ``rule_index``, walking *only* single-rule-context chains.

    Used by callers that want to know "is this expression syntactically
    a bare ``<X>``?". Returning ``None`` when the walk branches keeps
    the helper honest — if an expression has, e.g., a binary operator
    or a function call along the way, it isn't structurally a bare
    list literal / property lookup / etc.
    """
    node: Any = root
    while node is not None and isinstance(node, ParserRuleContext):
        if node.getRuleIndex() == rule_index:
            return node
        rule_children = [
            node.getChild(i)
            for i in range(node.getChildCount())
            if isinstance(node.getChild(i), ParserRuleContext)
        ]
        if len(rule_children) != 1:
            return None
        node = rule_children[0]
    return None


# ---------------------------------------------------------------------------
# Edit application
# ---------------------------------------------------------------------------


def _apply_edits(cypher: str, edits: list[_Edit]) -> str:
    """Apply ``edits`` to ``cypher`` in reverse offset order.

    Reverse order means each splice leaves the offsets of the
    not-yet-applied edits unchanged. We assert there is no overlap
    between edits — pass 1 only produces one edit per node pattern,
    and node patterns are non-overlapping by grammar.
    """
    if not edits:
        return cypher
    ordered = sorted(edits, key=lambda e: (e.start, e.end))
    # Defensive overlap check.
    for i in range(1, len(ordered)):
        prev = ordered[i - 1]
        curr = ordered[i]
        if curr.start < prev.end:
            raise AssertionError(
                f"tenant_ast_cypher: overlapping edits at {prev.start}..{prev.end} "
                f"and {curr.start}..{curr.end}; this is a bug in the rewriter."
            )
    out = cypher
    for edit in reversed(ordered):
        out = out[: edit.start] + edit.text + out[edit.end :]
    return out
