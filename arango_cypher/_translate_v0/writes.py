"""Write-clause compilation for the v0 translator: shared helpers.

The write translators live beside it: FOREACH (``write_foreach``), MATCH …
SET / DELETE / REMOVE (``write_mutate``), CREATE (``write_create``) and MERGE
(``write_merge``). This module keeps what they share — property writes, the
collection a pipeline variable lives in, the reading clauses before a write,
SET/DELETE/REMOVE after a WITH — and lazy wrappers for helpers that still
live in ``core.py``, so the package splits without a core <-> writes import
cycle at module import time.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from arango_query_core import AqlQuery, CoreError, MappingResolver

from .._antlr.CypherParser import CypherParser
from .label_tests import label_write, shared_label_fields
from .literals import _aql_string_literal
from .naming import _strip_label_backticks
from .property_writes import PropertyOp, PropertyWrites, StoredWrite
from .state import _active_resolver


def _atom_variable(atom: Any) -> str | None:
    """The variable an atom names: ``n``, or ``(n)`` in ``SET (n).p = …``."""
    if atom is None:
        return None
    if atom.oC_Variable() is not None:
        return atom.oC_Variable().getText().strip()
    inner = atom.oC_ParenthesizedExpression()
    if inner is not None:
        text = inner.oC_Expression().getText().strip()
        if re.fullmatch(r"[A-Za-z_]\w*", text):
            return text
    return None


def _property_writes(
    set_clauses: list[CypherParser.OC_SetContext],
    remove_clauses: list,
    *,
    bind_vars: dict[str, Any],
    target_of: Callable[[str], str] = lambda name: name,
    rewrite_value: Callable[[str], str] | None = None,
) -> PropertyWrites:
    """A statement's SET and REMOVE items, per variable, in query order.

    Refuses a nested property, which used to write or remove the wrong one
    (``n.a.b`` touched ``n.b``), and ``SET n:Label``, which failed obscurely.
    ``REMOVE n:Label`` is skipped, as it always was.
    """
    writes = PropertyWrites()
    resolver = _active_resolver.get()
    label_fields = shared_label_fields(resolver.bundle.physical_mapping) if resolver is not None else None

    def value_of(expr: Any) -> str:
        value = _compile_expression(expr, bind_vars)
        return rewrite_value(value) if rewrite_value else value

    for clause in sorted([*set_clauses, *remove_clauses], key=lambda c: c.start.tokenIndex):
        is_set = isinstance(clause, CypherParser.OC_SetContext)
        verb = "SET" if is_set else "REMOVE"
        items = (clause.oC_SetItem() if is_set else clause.oC_RemoveItem()) or []
        for item in items:
            prop_expr = item.oC_PropertyExpression()
            if prop_expr is not None:
                atom_var = _atom_variable(prop_expr.oC_Atom())
                lookups = prop_expr.oC_PropertyLookup() or []
                if atom_var is None or not lookups:
                    raise CoreError(f"{verb} requires a property expression", code="UNSUPPORTED")
                if len(lookups) > 1:
                    raise CoreError(
                        f"{verb} of a nested property ({prop_expr.getText()}) is not supported; "
                        "set the whole top-level property instead",
                        code="UNSUPPORTED",
                    )
                var = target_of(atom_var)
                token = lookups[0].oC_PropertyKeyName().getText().strip()
                name = _strip_label_backticks(token)
                if is_set:
                    writes.add(
                        var, PropertyOp("set", name=name, key=token, value=value_of(item.oC_Expression()))
                    )
                else:
                    writes.add(var, PropertyOp("remove", name=name, key=_aql_string_literal(name)))
                continue
            if item.oC_NodeLabels() is not None and item.oC_Variable() is not None and label_fields:
                # SET n:L / REMOVE n:L where the mapping stores several labels
                # per node (label_tests): a patch of the labels and type fields.
                var = target_of(item.oC_Variable().getText().strip())
                labels = [
                    _strip_label_backticks(nl.oC_LabelName().getText().strip())
                    for nl in item.oC_NodeLabels().oC_NodeLabel()
                ]
                aql_var = rewrite_value(var) if rewrite_value else var
                patch = label_write(aql_var, labels, add=is_set, fields=label_fields)
                writes.add(var, PropertyOp("merge", value=patch))
                continue
            # SET/REMOVE n:Label without a labels field (labels are collections
            # or a single type field there) is not modelled. REMOVE used to be
            # skipped without a word; both are refused.
            if item.oC_NodeLabels() is not None or item.oC_Variable() is None:
                raise CoreError(f"{verb} of a label ({item.getText()}) is not supported", code="UNSUPPORTED")
            var = target_of(item.oC_Variable().getText().strip())
            merges = any(child.getText() == "+=" for child in item.getChildren())
            writes.add(
                var, PropertyOp("merge" if merges else "replace", value=value_of(item.oC_Expression()))
            )
    return writes


def _stored_write_line(var: str, collection_ref: str, write: StoredWrite, indent: str = "  ") -> str:
    line = f"{indent}{write.operation} {var} WITH {write.document} IN {collection_ref}"
    return f"{line} {write.options}" if write.options else line


def _refuse_repeated_collections(writes: list[tuple[str, str]]) -> None:
    """Fail closed when two writes in one query hit the same collection.

    *writes* pairs a description with the collection name. AQL refuses the
    second (ERR 1579), and a translation error says which writes collided.
    """
    seen: dict[str, str] = {}
    for what, collection in writes:
        if collection in seen:
            raise CoreError(
                f"{seen[collection]} and {what} both write collection {collection!r}; AQL allows one "
                "write per collection in a query, so run them as separate statements",
                code="UNSUPPORTED",
            )
        seen[collection] = what


# Pipeline scanners used by WITH … SET/DELETE/REMOVE tails.  ``FOR x IN @@coll``
# binds ``x`` to collection bind-key ``@coll``; traversal ``FOR v, r IN … @@edge``
# binds the relationship variable ``r``; plain ``LET alias = src`` copies that
# binding so renamed WITH projections stay writable.
_FOR_COLLECTION_RE = re.compile(r"^\s*FOR\s+(\w+)\s+IN\s+@@(\w+)\s*$")


_FOR_TRAVERSAL_RE = re.compile(
    r"^\s*FOR\s+(\w+),\s*(\w+)\s+IN\s+\d+\.\.\d+\s+(?:OUTBOUND|INBOUND|ANY)\s+\w+\s+@@(\w+)\s*$"
)


_LET_ALIAS_RE = re.compile(r"^\s*LET\s+(\w+)\s+=\s+(\w+)\s*$")


def _infer_var_collections_from_pipeline(lines: list[str]) -> dict[str, str]:
    """Map AQL document variables to their collection bind keys.

    Only identity aliases are tracked (``LET x = p``).  Computed projections
    such as ``LET born = p.born`` are intentionally omitted so SET/DELETE on a
    non-document value fails closed instead of mutating the wrong collection.
    """
    out: dict[str, str] = {}
    for line in lines:
        m = _FOR_COLLECTION_RE.match(line)
        if m is not None:
            out[m.group(1)] = f"@{m.group(2)}"
            continue
        m = _FOR_TRAVERSAL_RE.match(line)
        if m is not None:
            # Vertex collection is not always a @@ bind on this line (LPG often
            # filters with IS_SAME_COLLECTION).  The edge variable is.
            out[m.group(2)] = f"@{m.group(3)}"
            continue
        m = _LET_ALIAS_RE.match(line)
        if m is not None and m.group(2) in out:
            out[m.group(1)] = out[m.group(2)]
    return out


def _append_multipart_mutate_tail(
    *,
    set_clauses: list[CypherParser.OC_SetContext],
    delete_clauses: list[CypherParser.OC_DeleteContext],
    remove_clauses: list,
    tail: CypherParser.OC_SinglePartQueryContext,
    lines: list[str],
    var_env: dict[str, str],
    bind_vars: dict[str, Any],
    resolver: MappingResolver,
) -> AqlQuery:
    """Emit SET / DELETE / REMOVE after a WITH pipeline.

    Handles ``MATCH … WITH … SET/DELETE/REMOVE … [RETURN …]``.  Collection
    targets are recovered from the already-built MATCH/WITH AQL so aliased
    document projections (``WITH p AS x``) stay bound to the source collection.
    """
    from .pipeline_deletes import needs_pipeline_delete, translate_pipeline_delete
    from .pipeline_writes import emit_pipeline_writes

    if delete_clauses and not (set_clauses or remove_clauses) and needs_pipeline_delete(delete_clauses):
        # DELETE of a list element, a map value, …: read all rows, then remove.
        return translate_pipeline_delete(
            delete_clauses, tail=tail, lines=lines, var_env=var_env, bind_vars=bind_vars, resolver=resolver
        )
    emit_pipeline_writes(
        set_clauses=set_clauses,
        delete_clauses=delete_clauses,
        remove_clauses=remove_clauses,
        lines=lines,
        var_env=var_env,
        bind_vars=bind_vars,
        resolver=resolver,
    )

    ret = tail.oC_Return()
    if ret is not None:
        _append_return(
            ret.oC_ProjectionBody(),
            lines=lines,
            bind_vars=bind_vars,
            var_env=var_env,
        )

    return AqlQuery(text="\n".join(lines), bind_vars=bind_vars)


def _core_helper(name: str):
    from . import core

    return getattr(core, name)


def _compile_expression(*args, **kwargs):
    return _core_helper("_compile_expression")(*args, **kwargs)


def _compile_cypher_fragment(*args, **kwargs):
    return _core_helper("_compile_cypher_fragment")(*args, **kwargs)


def _compile_where(*args, **kwargs):
    return _core_helper("_compile_where")(*args, **kwargs)


def _compile_match_pipeline(*args, **kwargs):
    return _core_helper("_compile_match_pipeline")(*args, **kwargs)


def _compile_match_multi_parts_from_parts(*args, **kwargs):
    return _core_helper("_compile_match_multi_parts_from_parts")(*args, **kwargs)


def _append_return(*args, **kwargs):
    return _core_helper("_append_return")(*args, **kwargs)


def _compile_return_object(*args, **kwargs):
    return _core_helper("_compile_return_object")(*args, **kwargs)


def _pick_primary_entity_label(*args, **kwargs):
    return _core_helper("_pick_primary_entity_label")(*args, **kwargs)


def _extra_label_filters(*args, **kwargs):
    return _core_helper("_extra_label_filters")(*args, **kwargs)


def _extract_node_var_and_labels(*args, **kwargs):
    return _core_helper("_extract_node_var_and_labels")(*args, **kwargs)


def _relationship_direction(*args, **kwargs):
    return _core_helper("_relationship_direction")(*args, **kwargs)


def _extract_relationship_type_and_var(*args, **kwargs):
    return _core_helper("_extract_relationship_type_and_var")(*args, **kwargs)


def _resolve_relationship_for_pattern(*args, **kwargs):
    return _core_helper("_resolve_relationship_for_pattern")(*args, **kwargs)


def _infer_unlabeled_collection(*args, **kwargs):
    return _core_helper("_infer_unlabeled_collection")(*args, **kwargs)


def _compile_node_pattern_properties(*args, **kwargs):
    return _core_helper("_compile_node_pattern_properties")(*args, **kwargs)


def _compile_label_predicate(*args, **kwargs):
    return _core_helper("_compile_label_predicate")(*args, **kwargs)


def _multi_label_filter(*args, **kwargs):
    return _core_helper("_multi_label_filter")(*args, **kwargs)


def _supertype_label_fields(label: str, collection: str, resolver: MappingResolver) -> dict[str, list[str]]:
    """``{labelsField: [labels]}`` a new *label* document in *collection* must carry.

    A ``MULTI_LABEL`` supertype finds its members by their labels array, so a
    node created under a member label lists every supertype that covers it in
    that collection, then its own label (the loader's convention). Without it,
    ``MATCH (m:Message)`` would never see a newly created ``Post``.
    """
    fields: dict[str, list[str]] = {}
    for name in resolver.all_entity_labels():
        emap = resolver.resolve_entity(name)
        if emap.get("style") != "MULTI_LABEL" or emap.get("collectionName") != collection:
            continue
        if label in resolver.supertype_members(name):
            fields.setdefault(emap["labelsField"], []).append(emap.get("labelValue") or name)
    for values in fields.values():
        values.append(label)
    return fields


def _rewrite_vars(*args, **kwargs):
    return _core_helper("_rewrite_vars")(*args, **kwargs)


def _emit_unwind_for(
    uw: CypherParser.OC_UnwindContext,
    *,
    bind_vars: dict[str, Any],
    var_env: dict[str, str],
) -> str:
    """Compile ``UNWIND <expr> AS <var>`` to an AQL ``FOR <var> IN <expr>`` line.

    Binds ``var`` into ``var_env`` and rewrites already-bound variables in the
    list expression (so ``MATCH (a) UNWIND a.items AS x`` resolves ``a``).
    """
    expr = _compile_expression(uw.oC_Expression(), bind_vars)
    if var_env:
        expr = _rewrite_vars(expr, var_env)
    var = uw.oC_Variable().getText().strip()
    var_env[var] = var
    return f"FOR {var} IN {expr}"


def _compile_write_reading_clauses(
    spq: CypherParser.OC_SinglePartQueryContext,
    *,
    resolver: MappingResolver,
    bind_vars: dict[str, Any],
) -> tuple[list[str], dict[str, str]]:
    """Compile the MATCH/UNWIND reading clauses preceding a write clause.

    Returns ``(lines, var_env)`` where ``lines`` is the ``FOR …`` prefix that
    the write (CREATE/MERGE) nests inside (sequential ``FOR``s in AQL are
    nested loops). Supports MATCH-only, UNWIND-only, and MATCH-then-UNWIND
    (a following ``UNWIND`` may iterate a matched list). ``UNWIND`` before
    ``MATCH`` and non-MATCH/UNWIND reading clauses are refused rather than
    mis-compiled.
    """
    reading_clauses = spq.oC_ReadingClause() or []
    lines: list[str] = []
    var_env: dict[str, str] = {}
    if not reading_clauses:
        return lines, var_env

    match_clauses: list[CypherParser.OC_MatchContext] = []
    unwind_clauses: list[CypherParser.OC_UnwindContext] = []
    seen_unwind = False
    for rc in reading_clauses:
        m = rc.oC_Match()
        uw = rc.oC_Unwind()
        if m is not None:
            if seen_unwind:
                raise CoreError(
                    "MATCH after UNWIND is not supported with write clauses",
                    code="NOT_IMPLEMENTED",
                )
            if m.OPTIONAL() is not None:
                # It was compiled as a MATCH: a row it does not match (which
                # Cypher keeps, null-extended) lost its write without a word.
                raise CoreError(
                    "OPTIONAL MATCH before CREATE/MERGE is not supported: its unmatched rows would "
                    "not be written",
                    code="UNSUPPORTED",
                )
            match_clauses.append(m)
        elif uw is not None:
            seen_unwind = True
            unwind_clauses.append(uw)
        else:
            raise CoreError(
                "Only MATCH/UNWIND reading clauses are supported with write clauses",
                code="NOT_IMPLEMENTED",
            )

    if match_clauses:
        all_parts: list = []
        extra_wheres: list = []
        for mc in match_clauses:
            all_parts.extend(mc.oC_Pattern().oC_PatternPart() or [])
            wc = mc.oC_Where()
            if wc is not None:
                extra_wheres.append(wc)
        if len(all_parts) == 1 and len(match_clauses) == 1:
            match_lines, forbidden = _compile_match_pipeline(
                match_clauses[0], resolver=resolver, bind_vars=bind_vars
            )
        else:
            match_lines, forbidden = _compile_match_multi_parts_from_parts(
                all_parts, extra_wheres=extra_wheres, resolver=resolver, bind_vars=bind_vars
            )
        lines.extend(match_lines)
        var_env = {v: v for v in forbidden}

    for uw in unwind_clauses:
        lines.append(_emit_unwind_for(uw, bind_vars=bind_vars, var_env=var_env))

    return lines, var_env


def _compile_relationship_pattern_properties(*args, **kwargs):
    return _core_helper("_compile_relationship_pattern_properties")(*args, **kwargs)
