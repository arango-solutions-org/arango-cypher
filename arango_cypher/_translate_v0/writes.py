"""Write-clause compilation for the v0 translator.

This module owns CREATE, MERGE, SET, DELETE, REMOVE, and FOREACH. It keeps
lazy wrappers for helpers that still live in ``core.py`` so the package can be
split without a core <-> writes import cycle at module import time.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from arango_query_core import AqlQuery, CoreError, MappingResolver

from .._antlr.CypherParser import CypherParser
from .columns import written_column
from .label_tests import label_test, label_write, label_write_entries, shared_label_fields
from .label_tests import labels_field as entity_labels_field
from .literals import _aql_string_literal
from .merge_bindings import endpoint_action_lines, merge_path_line, refuse_stale_endpoint_return
from .merge_docs import MapAction, insert_document, update_clause, upsert_options
from .merge_patterns import (
    MergeEdge,
    MergeEndpoint,
    pattern_merge_lines,
    refuse_unbound_endpoints,
    unbound_endpoints,
)
from .naming import _aql_collection_ref, _pick_bind_key, _pick_fresh_var, _strip_label_backticks
from .property_writes import PropertyOp, PropertyWrites, StoredWrite, keep_fields_for
from .scopes import outer_bound_names
from .state import _active_resolver
from .supertypes import union_scan_source
from .write_guards import (
    refuse_untranslated_foreach_parts,
    trailing_set_fields,
)


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


def _translate_foreach_query(
    spq: CypherParser.OC_SinglePartQueryContext,
    *,
    foreach_clauses: list[CypherParser.OC_ForeachContext],
    resolver: MappingResolver,
    bind_vars: dict[str, Any],
) -> AqlQuery:
    """Translate ``MATCH ... FOREACH (x IN list | SET ...)`` to AQL.

    ``FOREACH (x IN list | SET x.prop = val)``
    becomes:
    ``FOR x IN list UPDATE x WITH {prop: val} IN @@collection``

    What this compiler does not compile is refused rather than left out of
    the AQL: a reading clause other than MATCH (an UNWIND, a CALL, OPTIONAL
    MATCH's null rows), a RETURN, a second FOREACH (it would nest inside the
    first and write once per element of both lists), and a SET of a whole
    map or a label inside the FOREACH.
    """
    reading_clauses = spq.oC_ReadingClause() or []
    refuse_untranslated_foreach_parts(spq, foreach_clauses)

    lines: list[str] = []

    if reading_clauses:
        match_ctxs: list[CypherParser.OC_MatchContext] = []
        for rc in reading_clauses:
            m = rc.oC_Match()
            if m is not None:
                match_ctxs.append(m)

        if match_ctxs:
            if len(match_ctxs) == 1:
                match_lines, _ = _compile_match_pipeline(
                    match_ctxs[0],
                    resolver=resolver,
                    bind_vars=bind_vars,
                )
            else:
                all_parts: list[Any] = []
                extra_wheres = []
                for mc_item in match_ctxs:
                    pattern = mc_item.oC_Pattern()
                    all_parts.extend(pattern.oC_PatternPart() or [])
                    w = mc_item.oC_Where()
                    if w is not None:
                        extra_wheres.append(w)
                match_lines, _ = _compile_match_multi_parts_from_parts(
                    all_parts,
                    extra_wheres=extra_wheres,
                    resolver=resolver,
                    bind_vars=bind_vars,
                )
            lines.extend(match_lines)

    for fe in foreach_clauses:
        var_name = fe.oC_Variable().getText().strip()
        list_expr = _compile_expression(fe.oC_Expression(), bind_vars)

        lines.append(f"FOR {var_name} IN {list_expr}")

        inner_updating = fe.oC_UpdatingClause() or []
        for uc in inner_updating:
            if uc.oC_Set() is not None:
                sc = uc.oC_Set()
                set_items = sc.oC_SetItem() or []
                update_fields: dict[str, dict[str, str]] = {}
                for si in set_items:
                    prop_expr = si.oC_PropertyExpression()
                    if prop_expr is not None:
                        atom = prop_expr.oC_Atom()
                        target_var = (
                            atom.oC_Variable().getText().strip()
                            if atom.oC_Variable() is not None
                            else var_name
                        )
                        lookups = prop_expr.oC_PropertyLookup() or []
                        if not lookups:
                            raise CoreError("SET requires a property expression", code="UNSUPPORTED")
                        prop_name = lookups[-1].oC_PropertyKeyName().getText().strip()
                        val = _compile_expression(si.oC_Expression(), bind_vars)
                        update_fields.setdefault(target_var, {})[prop_name] = val
                    else:
                        raise CoreError(
                            f"SET of a whole map or a label inside FOREACH ({si.getText()}) is not supported",
                            code="UNSUPPORTED",
                        )

                for target_var, fields in update_fields.items():
                    pairs = ", ".join(f"{k}: {v}" for k, v in fields.items())
                    if "@collection" in bind_vars:
                        coll_ref = "@@collection"
                    else:
                        coll_key = _pick_bind_key("@feCollection", bind_vars)
                        all_labels = resolver.all_entity_labels()
                        if all_labels:
                            e_map = resolver.resolve_entity(_strip_label_backticks(all_labels[0]))
                            bind_vars[coll_key] = e_map.get("collectionName")
                        else:
                            bind_vars[coll_key] = "unknown"
                        coll_ref = _aql_collection_ref(coll_key)
                    lines.append(f"  UPDATE {target_var} WITH {{{pairs}}} IN {coll_ref}")
            elif uc.oC_Create() is not None:
                # FOREACH (x IN list | CREATE (n {... x ...})) becomes
                # ``FOR x IN list  INSERT {...} IN coll``. Reuse the create
                # compiler with the loop variable in scope; no LET binding is
                # needed since nothing downstream references the new rows.
                _compile_create(
                    uc.oC_Create(),
                    resolver=resolver,
                    bind_vars=bind_vars,
                    var_env={var_name: var_name},
                    lines=lines,
                    indent="  ",
                    has_return=False,
                )
            elif uc.oC_Delete() is not None:
                _compile_foreach_delete(
                    uc.oC_Delete(),
                    var_name=var_name,
                    resolver=resolver,
                    bind_vars=bind_vars,
                    lines=lines,
                )
            else:
                raise CoreError("Unsupported clause inside FOREACH", code="UNSUPPORTED")

    return AqlQuery(text="\n".join(lines), bind_vars=bind_vars)


def _compile_foreach_delete(
    delete_ctx: CypherParser.OC_DeleteContext,
    *,
    var_name: str,
    resolver: MappingResolver,
    bind_vars: dict[str, Any],
    lines: list[str],
) -> None:
    """Compile ``FOREACH (x IN list | DELETE x)`` into ``REMOVE x IN coll``.

    The deleted documents come from the iterated list, so their collection is
    inferred (same single-domain inference the read/mutating paths use) and the
    statement fails closed on a multi-collection schema. ``DETACH DELETE``
    inside FOREACH is rejected: removing incident edges per element across all
    edge collections in a nested loop is both rare and prone to ERR-1579
    collisions; use a top-level ``MATCH … DETACH DELETE`` instead.
    """
    if delete_ctx.DETACH() is not None:
        raise CoreError(
            "DETACH DELETE inside FOREACH is not supported; use a top-level MATCH … DETACH DELETE",
            code="NOT_IMPLEMENTED",
        )

    coll_key = _find_or_create_collection_bind_key(
        "@collection", _infer_unlabeled_collection(resolver), bind_vars
    )
    coll_ref = _aql_collection_ref(coll_key)
    for de in delete_ctx.oC_Expression() or []:
        del_target = _compile_expression(de, bind_vars)
        lines.append(f"  REMOVE {del_target} IN {coll_ref}")


def _translate_mutating_query(
    spq: CypherParser.OC_SinglePartQueryContext,
    *,
    set_clauses: list[CypherParser.OC_SetContext],
    delete_clauses: list[CypherParser.OC_DeleteContext],
    remove_clauses: list,
    resolver: MappingResolver,
    bind_vars: dict[str, Any],
) -> AqlQuery:
    """Translate MATCH ... SET / DELETE / REMOVE queries to AQL.

    SET n.prop = val  →  UPDATE n WITH {prop: val} IN @@collection
    DELETE n          →  REMOVE n IN @@collection
    DETACH DELETE n   →  (remove edges first, then REMOVE node)
    REMOVE n.prop     →  UPDATE n WITH {"prop": null} IN @@collection OPTIONS {keepNull: false}
    """
    reading_clauses = spq.oC_ReadingClause() or []
    match_ctxs: list[CypherParser.OC_MatchContext] = []
    for rc in reading_clauses:
        m = rc.oC_Match()
        if m is not None:
            match_ctxs.append(m)

    if not match_ctxs:
        raise CoreError("MATCH is required before SET/DELETE", code="UNSUPPORTED")

    # Compile the MATCH body into FOR/FILTER lines. Only the first MATCH and its
    # first pattern part are compiled here, so anything else that would narrow
    # the rows written must be refused: ignoring it writes (or deletes) more
    # than the query asks for. Several reading clauses compile as a pipeline.
    if len(reading_clauses) > 1:
        from .pipeline_writes import translate_multi_reading_mutation

        return translate_multi_reading_mutation(
            spq,
            set_clauses=set_clauses,
            delete_clauses=delete_clauses,
            remove_clauses=remove_clauses,
            resolver=resolver,
            bind_vars=bind_vars,
        )
    mc = match_ctxs[0]
    pattern = mc.oC_Pattern()
    parts = pattern.oC_PatternPart() or []
    if not parts:
        raise CoreError("MATCH pattern is required", code="UNSUPPORTED")
    if len(parts) > 1:
        raise CoreError(
            "SET/DELETE/REMOVE after a MATCH with several pattern parts is not supported: only the "
            "first part would be applied, writing rows the others exclude",
            code="UNSUPPORTED",
        )

    anon = parts[0].oC_AnonymousPatternPart()
    elem = anon.oC_PatternElement()
    start_node = elem.oC_NodePattern()
    if start_node is None:
        raise CoreError("MATCH requires a node pattern", code="UNSUPPORTED")

    chains = elem.oC_PatternElementChain() or []
    var, labels = _extract_node_var_and_labels(start_node, default_var="n")
    prop_filters = _compile_node_pattern_properties(start_node, var=var, bind_vars=bind_vars)

    lines: list[str] = [f"FOR {var} IN @@collection"]
    # Bind key of the collection each pattern variable lives in, for writes.
    # ``None``: the variable cannot be written (a variable-length relationship
    # is a list of edges).
    write_keys: dict[str, str | None] = {var: "@collection"}
    # Variables matched by a UNION supertype, whose documents live in several
    # collections: an UPDATE/REMOVE names one, so they cannot be written.
    union_vars: dict[str, str] = {}

    if labels:
        primary = _pick_primary_entity_label(labels, resolver)
        entity_mapping = resolver.resolve_entity(_strip_label_backticks(primary))

        style = entity_mapping.get("style")
        if style == "UNION":
            lines = [
                f"FOR {var} IN {union_scan_source(var, primary, resolver, bind_vars, filters=prop_filters)}"
            ]
            prop_filters = []
            union_vars[var] = _strip_label_backticks(primary)
        else:
            bind_vars["@collection"] = entity_mapping["collectionName"]
        if style == "LABEL":
            bind_vars["typeField"] = entity_mapping["typeField"]
            bind_vars["typeValue"] = entity_mapping["typeValue"]
            lines.append("  FILTER " + label_test(var, "typeField", "typeValue", entity_mapping, bind_vars))
        elif style == "MULTI_LABEL":
            lines.append(f"  FILTER {_multi_label_filter(var, entity_mapping, bind_vars)}")
        elif style not in ("COLLECTION", "UNION"):
            raise CoreError(f"Unsupported entity mapping style: {style}", code="INVALID_MAPPING")
    else:
        # Unlabeled ``MATCH (n) SET/DELETE/REMOVE …``: resolve to the single
        # domain collection using the same inference the read path uses. Fails
        # closed (CoreError) when the schema has more than one candidate
        # collection, so we never silently mutate the wrong store.
        bind_vars["@collection"] = _infer_unlabeled_collection(resolver)

    for f in prop_filters:
        lines.append(f"  FILTER {f}")

    # Handle relationship chain if present
    current = var
    for chain in chains:
        rel_pat = chain.oC_RelationshipPattern()
        v_node = chain.oC_NodePattern()
        if rel_pat is None or v_node is None:
            raise CoreError("Invalid pattern in SET/DELETE", code="UNSUPPORTED")

        v_var, v_labels = _extract_node_var_and_labels(v_node, default_var="v")
        rel_type, rel_var, rel_range = _extract_relationship_type_and_var(rel_pat, default_var="r")
        direction = _relationship_direction(rel_pat)

        r_map = _resolve_relationship_for_pattern(resolver, rel_type)
        edge_key = _pick_bind_key("@edgeCollection", bind_vars)
        bind_vars[edge_key] = r_map.get("edgeCollectionName") or r_map.get("collectionName")

        rmin, rmax = rel_range
        edge_ref = _aql_collection_ref(edge_key)
        lines.append(f"  FOR {v_var}, {rel_var} IN {rmin}..{rmax} {direction} {current} {edge_ref}")
        write_keys[rel_var] = edge_key if (rmin, rmax) == (1, 1) else None

        if v_labels:
            v_primary = _pick_primary_entity_label(v_labels, resolver)
            v_map = resolver.resolve_entity(_strip_label_backticks(v_primary))
            v_style = v_map.get("style")
            if v_style == "UNION":
                union_vars[v_var] = _strip_label_backticks(v_primary)
                write_keys[v_var] = None
            else:
                write_keys[v_var] = _find_or_create_collection_bind_key(
                    "@collection", v_map["collectionName"], bind_vars
                )
            if v_style == "LABEL":
                vtf = _pick_bind_key("vTypeField", bind_vars)
                vtv = _pick_bind_key("vTypeValue", bind_vars)
                bind_vars[vtf] = v_map.get("typeField")
                bind_vars[vtv] = v_map.get("typeValue")
                lines.append("    FILTER " + label_test(v_var, vtf, vtv, v_map, bind_vars))
            elif v_style == "MULTI_LABEL":
                lines.append(f"    FILTER {_multi_label_filter(v_var, v_map, bind_vars)}")
            elif v_style not in ("COLLECTION", "UNION"):
                raise CoreError(f"Unsupported entity mapping style: {v_style}", code="INVALID_MAPPING")
            elif not (
                len(v_labels) == 1
                and rel_type is not None
                and resolver.edge_constrains_target(rel_type, _strip_label_backticks(v_primary), direction)
            ):
                # The edge may lead into another collection (a polymorphic
                # LIKES reaches posts and comments): keep only the labelled
                # one — or, for a UNION supertype, its members — as the read
                # path does, or the write touches the others.
                pred = _compile_label_predicate(v_var, v_node.oC_NodeLabels(), bind_vars)
                if pred is not None:
                    lines.append(f"    FILTER {pred}")

        r_style = r_map.get("style")
        if r_style == "GENERIC_WITH_TYPE":
            rtf = _pick_bind_key("relTypeField", bind_vars)
            rtv = _pick_bind_key("relTypeValue", bind_vars)
            bind_vars[rtf] = r_map.get("typeField")
            bind_vars[rtv] = r_map.get("typeValue")
            lines.append(f"    FILTER {rel_var}[@{rtf}] == @{rtv}")

        for f in _compile_relationship_pattern_properties(rel_pat, var=rel_var, bind_vars=bind_vars):
            lines.append(f"    FILTER {f}")
        for f in _compile_node_pattern_properties(v_node, var=v_var, bind_vars=bind_vars):
            lines.append(f"    FILTER {f}")

        if not v_labels:
            # An unlabeled end node lives wherever the edge leads; resolve it
            # the way the read path does, failing closed when that is ambiguous.
            write_keys.setdefault(v_var, "")
        current = v_var

    where_ctx = mc.oC_Where()
    if where_ctx is not None:
        wf = _compile_where(where_ctx.oC_Expression(), bind_vars)
        lines.append(f"  FILTER {wf}")

    # ``OPTIONAL MATCH … SET/REMOVE/DELETE``: with no match there is one row
    # whose variables are null, and a write to null does nothing.
    optional = mc.OPTIONAL() is not None
    if optional:
        row_vars = list(write_keys)
        lines = [
            "LET _opt_rows = (",
            *(f"  {ln}" for ln in lines),
            f"    RETURN {{{', '.join(f'{v}: {v}' for v in row_vars)}}}",
            ")",
            "FOR _opt_row IN (LENGTH(_opt_rows) > 0 ? _opt_rows : [{}])",
            *(f"  LET {v} = _opt_row.{v}" for v in row_vars),
        ]

    def guarded(var: str, statement: str) -> str:
        """*statement* (a write of ``_w``) applied to *var* unless it is null."""
        return f"FOR _w IN ({var} == null ? [] : [{var}]) {statement}"

    def write_key_for(target_var: str, verb: str) -> str:
        if target_var not in write_keys:
            raise CoreError(
                f"{verb} targets {target_var!r}, which is not bound by the first MATCH pattern",
                code="UNSUPPORTED",
            )
        key = write_keys[target_var]
        if target_var in union_vars:
            raise CoreError(
                f"{verb} on {target_var!r} is not supported: it is matched as the supertype "
                f"{union_vars[target_var]!r}, whose members live in several collections",
                code="UNSUPPORTED",
            )
        if key is None:
            raise CoreError(
                f"{verb} on {target_var!r}, a variable-length relationship (a list of edges), "
                "is not supported",
                code="UNSUPPORTED",
            )
        if key == "":
            key = _find_or_create_collection_bind_key(
                "@collection", _infer_unlabeled_collection(resolver), bind_vars
            )
            write_keys[target_var] = key
        return key

    # SET and REMOVE: one write per variable, in its own collection.
    writes = _property_writes(set_clauses, remove_clauses, bind_vars=bind_vars)
    physical = resolver.bundle.physical_mapping
    written: list[tuple[str, str]] = []
    ret = spq.oC_Return()
    # What RETURN reads a written variable as: the document after the write.
    return_env: dict[str, str] = {}
    for target_var in writes.ops:
        key = write_key_for(target_var, "SET/REMOVE")
        write = writes.stored_write(target_var, keep_fields_for(bind_vars[key], physical))
        written.append((f"SET/REMOVE on {target_var}", bind_vars[key]))
        if optional:
            statement = _stored_write_line("_w", _aql_collection_ref(key), write, indent="")
            updated = _pick_fresh_var(
                f"{target_var}_new", forbidden_vars=set(write_keys) | set(return_env.values())
            )
            lines.append(f"  LET {updated} = FIRST({guarded(target_var, statement)} RETURN NEW)")
            return_env[target_var] = updated
            continue
        lines.append(_stored_write_line(target_var, _aql_collection_ref(key), write))
        if ret is not None:
            updated = _pick_fresh_var(
                f"{target_var}_new", forbidden_vars=set(write_keys) | set(return_env.values())
            )
            lines.append(f"  LET {updated} = NEW")
            return_env[target_var] = updated

    # Compile DELETE
    for dc in delete_clauses:
        is_detach = dc.DETACH() is not None
        for de in dc.oC_Expression() or []:
            del_var = _compile_expression(de, bind_vars).strip()
            if not re.fullmatch(r"\w+", del_var):
                raise CoreError("DELETE only supports a MATCH-bound variable", code="UNSUPPORTED")
            key = write_key_for(del_var, "DELETE")
            if is_detach:
                for idx, ec in enumerate(resolver.all_edge_collections()):
                    ec_key = _pick_bind_key("@detachEdge", bind_vars)
                    bind_vars[ec_key] = ec
                    ec_ref = _aql_collection_ref(ec_key)
                    de_var = f"_de{idx}"
                    start = f"{del_var} == null ? [] : [{del_var}]" if optional else ""
                    edges = (
                        f"FOR _ds IN ({start}) FOR {de_var} IN 1..1 ANY _ds {ec_ref}"
                        if optional
                        else f"FOR {de_var} IN 1..1 ANY {del_var} {ec_ref}"
                    )
                    lines.append(f"  LET _edgeRm{idx} = ({edges} REMOVE {de_var} IN {ec_ref})")
                    written.append((f"DETACH DELETE {del_var} (its {ec} edges)", ec))
            if optional:
                lines.append(
                    f"  LET _rm_{del_var} = ({guarded(del_var, f'REMOVE _w IN {_aql_collection_ref(key)}')})"
                )
            else:
                lines.append(f"  REMOVE {del_var} IN {_aql_collection_ref(key)}")
            written.append((f"DELETE {del_var}", bind_vars[key]))

    _refuse_repeated_collections(written)

    # Optional RETURN
    if ret is not None:
        var_env = {v: return_env.get(v, v) for v in write_keys} if return_env else None
        _append_return(ret.oC_ProjectionBody(), lines=lines, bind_vars=bind_vars, var_env=var_env)

    return AqlQuery(text="\n".join(lines), bind_vars=bind_vars)


def _translate_create_query(
    spq: CypherParser.OC_SinglePartQueryContext,
    *,
    create_clauses: list[CypherParser.OC_CreateContext],
    set_clauses: list[CypherParser.OC_SetContext] | None = None,
    remove_clauses: list | None = None,
    resolver: MappingResolver,
    bind_vars: dict[str, Any],
) -> AqlQuery:
    """Translate a single-part query containing CREATE clause(s), optionally
    followed by SET/REMOVE on the created variables."""
    ret = spq.oC_Return()
    # SET/REMOVE on created variables are folded into the inserted documents:
    # a separate UPDATE would write the same collection twice (ERR 1579).
    writes = _property_writes(set_clauses or [], remove_clauses or [], bind_vars=bind_vars)

    # Maps each created variable to the bind key of the collection it was
    # inserted into, so a trailing SET/REMOVE can target the right collection.
    var_collections: dict[str, str] = {}

    # MATCH/UNWIND reading clauses become the FOR prefix the CREATE nests in
    # (``UNWIND [..] AS x CREATE (n {p: x})`` → ``FOR x IN [..] INSERT {p: x}``).
    lines, var_env = _compile_write_reading_clauses(spq, resolver=resolver, bind_vars=bind_vars)
    indent = "  " if lines else ""

    num_creates = len(create_clauses)
    for ci, cc in enumerate(create_clauses):
        force_let = ci < num_creates - 1
        _compile_create(
            cc,
            resolver=resolver,
            bind_vars=bind_vars,
            var_env=var_env,
            lines=lines,
            indent=indent,
            has_return=ret is not None or force_let,
            var_collections=var_collections,
            writes=writes,
        )

    not_created = [v for v in writes.ops if v not in var_collections]
    if not_created:
        raise CoreError(
            f"SET/REMOVE after CREATE targets variable {not_created[0]!r} that was not created in this query",
            code="NOT_IMPLEMENTED",
        )

    if ret is not None:
        _compile_return_for_create(
            ret.oC_ProjectionBody(),
            lines=lines,
            bind_vars=bind_vars,
            indent=indent,
        )

    return AqlQuery(text="\n".join(lines), bind_vars=bind_vars)


@dataclass
class CreateOp:
    """One node or relationship a CREATE pattern makes, in pattern order."""

    kind: str  # "node" or "rel"
    var: str
    labels: list[str] | None = None
    node_ctx: Any = None
    rel_pat: Any = None
    from_var: str = ""
    to_var: str = ""


def create_ops(create_ctx: CypherParser.OC_CreateContext, var_env: dict[str, str]) -> list[CreateOp]:
    """The nodes and relationships a CREATE makes. A node already in
    *var_env* is referenced, not created; each created node is added to it."""
    pattern = create_ctx.oC_Pattern()
    parts = pattern.oC_PatternPart() or []

    ops: list[CreateOp] = []
    create_counter = 0
    anon_counter = 0

    def _unique_anon(var: str) -> str:
        nonlocal anon_counter
        if var != "_anon" or var not in var_env:
            return var
        while f"_anon{anon_counter}" in var_env:
            anon_counter += 1
        result = f"_anon{anon_counter}"
        anon_counter += 1
        return result

    def _refuse_rebinding(var: str, node_ctx: Any) -> None:
        # openCypher VariableAlreadyBound: CREATE may reference a bound node
        # but not give it labels or properties.
        if var in var_env and (node_ctx.oC_NodeLabels() is not None or node_ctx.oC_Properties() is not None):
            raise CoreError(
                f"CREATE cannot add labels or properties to the already bound variable {var!r}",
                code="UNSUPPORTED",
            )

    for part in parts:
        anon = part.oC_AnonymousPatternPart()
        elem = anon.oC_PatternElement()
        node = elem.oC_NodePattern()
        chains = elem.oC_PatternElementChain() or []

        start_var, start_labels = _extract_node_var_and_labels(node, default_var="_anon")
        start_var = _unique_anon(start_var)
        _refuse_rebinding(start_var, node)

        if start_var not in var_env:
            ops.append(CreateOp(kind="node", var=start_var, labels=start_labels, node_ctx=node))
            var_env[start_var] = start_var

        current_var = start_var
        for chain in chains:
            rel_pat = chain.oC_RelationshipPattern()
            end_node = chain.oC_NodePattern()
            if rel_pat is None or end_node is None:
                raise CoreError("Invalid CREATE pattern", code="UNSUPPORTED")

            end_var, end_labels = _extract_node_var_and_labels(end_node, default_var="_anon")
            end_var = _unique_anon(end_var)
            _refuse_rebinding(end_var, end_node)

            if end_var not in var_env:
                ops.append(CreateOp(kind="node", var=end_var, labels=end_labels, node_ctx=end_node))
                var_env[end_var] = end_var

            detail = rel_pat.oC_RelationshipDetail()
            rel_var_name = ""
            if detail is not None and detail.oC_Variable() is not None:
                rel_var_name = detail.oC_Variable().getText().strip()
            if not rel_var_name:
                rel_var_name = f"_c{create_counter}"
                create_counter += 1

            direction = _relationship_direction(rel_pat)
            if direction == "ANY":
                # openCypher RequiresDirectedRelationship.
                raise CoreError("CREATE requires a directed relationship", code="UNSUPPORTED")
            if direction == "INBOUND":
                from_v, to_v = end_var, current_var
            else:
                from_v, to_v = current_var, end_var

            ops.append(
                CreateOp(
                    kind="rel",
                    var=rel_var_name,
                    rel_pat=rel_pat,
                    from_var=from_v,
                    to_var=to_v,
                )
            )

            current_var = end_var

    return ops


def _compile_create(
    create_ctx: CypherParser.OC_CreateContext,
    *,
    resolver: MappingResolver,
    bind_vars: dict[str, Any],
    var_env: dict[str, str],
    lines: list[str],
    indent: str,
    has_return: bool,
    var_collections: dict[str, str] | None = None,
    writes: PropertyWrites | None = None,
) -> None:
    """Compile a single CREATE clause into AQL INSERT lines.

    *writes* are SET/REMOVE items on the created variables, folded into the
    documents being inserted.
    """
    ops = create_ops(create_ctx, var_env)

    for i, op in enumerate(ops):
        is_last = i == len(ops) - 1
        needs_let = has_return or not is_last

        if op.kind == "node":
            _compile_create_node(
                op.var,
                op.labels or [],
                op.node_ctx,
                resolver=resolver,
                bind_vars=bind_vars,
                lines=lines,
                indent=indent,
                needs_let=needs_let,
                var_collections=var_collections,
                writes=writes,
            )
        elif op.kind == "rel":
            _compile_create_rel(
                op.var,
                op.rel_pat,
                op.from_var,
                op.to_var,
                resolver=resolver,
                bind_vars=bind_vars,
                lines=lines,
                indent=indent,
                needs_let=needs_let,
                var_collections=var_collections,
                writes=writes,
            )


def _find_or_create_collection_bind_key(
    base: str,
    collection_name: str,
    bind_vars: dict[str, Any],
) -> str:
    """Reuse an existing bind key if it already points to the same collection."""
    if base in bind_vars and bind_vars[base] == collection_name:
        return base
    i = 2
    while f"{base}{i}" in bind_vars:
        if bind_vars[f"{base}{i}"] == collection_name:
            return f"{base}{i}"
        i += 1
    key = _pick_bind_key(base, bind_vars)
    bind_vars[key] = collection_name
    return key


def _compile_create_node(
    var: str,
    labels: list[str],
    node_ctx: CypherParser.OC_NodePatternContext,
    *,
    resolver: MappingResolver,
    bind_vars: dict[str, Any],
    lines: list[str],
    indent: str,
    needs_let: bool,
    var_collections: dict[str, str] | None = None,
    writes: PropertyWrites | None = None,
) -> None:
    """Compile a single node INSERT."""
    doc, coll_key, extra_fields = create_node_doc(labels, node_ctx, resolver=resolver, bind_vars=bind_vars)
    if writes is not None:
        doc = writes.folded_into(var, doc, extra_fields)
    coll_ref = _aql_collection_ref(coll_key)
    if var_collections is not None:
        var_collections[var] = coll_key

    if needs_let:
        lines.append(f"{indent}LET {var} = FIRST(INSERT {doc} INTO {coll_ref} RETURN NEW)")
    else:
        lines.append(f"{indent}INSERT {doc} INTO {coll_ref}")


def create_node_doc(
    labels: list[str],
    node_ctx: CypherParser.OC_NodePatternContext,
    *,
    resolver: MappingResolver,
    bind_vars: dict[str, Any],
) -> tuple[str, str, list[str]]:
    """The document a created node is inserted as: ``(doc, collection bind
    key, mapping fields)``, the fields being the type discriminator and
    supertype labels the mapping requires."""
    param_ref = _create_props_param_ref(node_ctx.oC_Properties(), bind_vars)
    props = [] if param_ref else _compile_create_props(node_ctx.oC_Properties(), bind_vars)
    extra_fields: list[str] = []

    if not labels:
        coll_name = _infer_unlabeled_collection(resolver)
        coll_key = _find_or_create_collection_bind_key("@collection", coll_name, bind_vars)
    else:
        primary = _pick_primary_entity_label(labels, resolver)
        entity_mapping = resolver.resolve_entity(_strip_label_backticks(primary))
        style = entity_mapping.get("style")
        if style in ("MULTI_LABEL", "UNION"):
            raise CoreError(
                f"Cannot CREATE a node by its supertype {primary!r}: name a concrete label",
                code="UNSUPPORTED",
            )

        coll_key = _find_or_create_collection_bind_key(
            "@collection",
            entity_mapping["collectionName"],
            bind_vars,
        )
        if style == "LABEL":
            type_field = entity_mapping.get("typeField", "type")
            tv_key = _pick_bind_key("typeValue", bind_vars)
            bind_vars[tv_key] = entity_mapping.get("typeValue")
            extra_fields.append(f"{type_field}: @{tv_key}")
            lf = entity_labels_field(entity_mapping)
            all_labels = list(dict.fromkeys(_strip_label_backticks(lab) for lab in labels))
            if lf is not None and len(all_labels) > 1:
                # Every label of a multi-label node (label_tests docstring).
                lv_key = _pick_bind_key("nodeLabels", bind_vars)
                bind_vars[lv_key] = all_labels
                extra_fields.append(f"{lf}: @{lv_key}")
        elif style != "COLLECTION":
            raise CoreError(f"Unsupported entity mapping style: {style}", code="INVALID_MAPPING")
        concrete = _strip_label_backticks(primary)
        for labels_field, values in _supertype_label_fields(
            concrete, entity_mapping["collectionName"], resolver
        ).items():
            lv_key = _pick_bind_key("labelsValue", bind_vars)
            bind_vars[lv_key] = values
            extra_fields.append(f"{labels_field}: @{lv_key}")

    return _build_insert_doc(props, extra_fields, base=param_ref), coll_key, extra_fields


def _compile_create_rel(
    var: str,
    rel_pat: CypherParser.OC_RelationshipPatternContext,
    from_var: str,
    to_var: str,
    *,
    resolver: MappingResolver,
    bind_vars: dict[str, Any],
    lines: list[str],
    indent: str,
    needs_let: bool,
    var_collections: dict[str, str] | None = None,
    writes: PropertyWrites | None = None,
) -> None:
    """Compile a single relationship INSERT."""
    doc, edge_coll_key, extra_fields = create_rel_doc(
        rel_pat, from_var, to_var, resolver=resolver, bind_vars=bind_vars
    )
    if writes is not None:
        doc = writes.folded_into(var, doc, extra_fields)
    coll_ref = _aql_collection_ref(edge_coll_key)
    if var_collections is not None:
        var_collections[var] = edge_coll_key

    if needs_let:
        lines.append(f"{indent}LET {var} = FIRST(INSERT {doc} INTO {coll_ref} RETURN NEW)")
    else:
        lines.append(f"{indent}INSERT {doc} INTO {coll_ref}")


def create_rel_doc(
    rel_pat: CypherParser.OC_RelationshipPatternContext,
    from_var: str,
    to_var: str,
    *,
    resolver: MappingResolver,
    bind_vars: dict[str, Any],
) -> tuple[str, str, list[str]]:
    """The document a created relationship is inserted as: ``(doc, edge
    collection bind key, mapping fields)``, the fields being ``_from``,
    ``_to`` and the type discriminator."""
    detail = rel_pat.oC_RelationshipDetail()
    if detail is None:
        raise CoreError("Relationship type is required for CREATE", code="UNSUPPORTED")
    if detail.oC_RangeLiteral() is not None:
        # openCypher: CreatingVarLength, a compile-time SyntaxError.
        raise CoreError("CREATE cannot create a variable-length relationship", code="UNSUPPORTED")
    types_ctx = detail.oC_RelationshipTypes()
    if types_ctx is None:
        raise CoreError("Relationship type is required for CREATE", code="UNSUPPORTED")
    types = types_ctx.oC_RelTypeName()
    if not types or len(types) != 1:
        raise CoreError("Exactly one relationship type is required for CREATE", code="UNSUPPORTED")
    rel_type = types[0].getText().strip()

    r_map = resolver.resolve_relationship(_strip_label_backticks(rel_type))
    r_style = r_map.get("style")

    edge_coll_name = r_map.get("edgeCollectionName") or r_map.get("collectionName")
    if not isinstance(edge_coll_name, str) or not edge_coll_name:
        raise CoreError(
            f"Invalid relationship mapping collection for: {rel_type}",
            code="INVALID_MAPPING",
        )

    edge_coll_key = _find_or_create_collection_bind_key(
        "@edgeCollection",
        edge_coll_name,
        bind_vars,
    )

    extra_fields = [f"_from: {from_var}._id", f"_to: {to_var}._id"]

    if r_style == "GENERIC_WITH_TYPE":
        type_field = r_map.get("typeField", "type")
        rtv_key = _pick_bind_key("relTypeValue", bind_vars)
        bind_vars[rtv_key] = r_map.get("typeValue")
        extra_fields.append(f"{type_field}: @{rtv_key}")
    elif r_style == "EMBEDDED":
        raise CoreError(
            "EMBEDDED relationships are not supported for CREATE",
            code="UNSUPPORTED",
        )
    elif r_style != "DEDICATED_COLLECTION":
        raise CoreError(
            f"Unsupported relationship mapping style for CREATE: {r_style}",
            code="INVALID_MAPPING",
        )

    rel_props_ctx = detail.oC_Properties()
    param_ref = _create_props_param_ref(rel_props_ctx, bind_vars)
    props = [] if param_ref else _compile_create_rel_props(rel_pat, bind_vars)
    return _build_insert_doc(props, extra_fields, base=param_ref), edge_coll_key, extra_fields


def _compile_create_props(
    props_ctx: CypherParser.OC_PropertiesContext | None,
    bind_vars: dict[str, Any],
) -> list[tuple[str, str]]:
    """Extract property key-value pairs from a pattern's properties for INSERT."""
    if props_ctx is None:
        return []
    if props_ctx.oC_Parameter() is not None:
        raise CoreError(
            "Parameterized properties are not supported in CREATE",
            code="NOT_IMPLEMENTED",
        )
    m = props_ctx.oC_MapLiteral()
    if m is None:
        return []
    keys = m.oC_PropertyKeyName() or []
    vals = m.oC_Expression() or []
    if len(keys) != len(vals):
        raise CoreError("Invalid properties map in CREATE", code="UNSUPPORTED")
    out: list[tuple[str, str]] = []
    for k_ctx, v_ctx in zip(keys, vals, strict=False):
        key = k_ctx.getText().strip()
        if not key:
            raise CoreError("Invalid property key in CREATE", code="UNSUPPORTED")
        expr = _compile_expression(v_ctx, bind_vars)
        out.append((key, expr))
    return out


def _compile_create_rel_props(
    rel_pat: CypherParser.OC_RelationshipPatternContext,
    bind_vars: dict[str, Any],
) -> list[tuple[str, str]]:
    """Extract properties from a relationship pattern for CREATE."""
    detail = rel_pat.oC_RelationshipDetail()
    if detail is None:
        return []
    return _compile_create_props(detail.oC_Properties(), bind_vars)


def _create_props_param_ref(
    props_ctx: CypherParser.OC_PropertiesContext | None,
    bind_vars: dict[str, Any],
) -> str | None:
    """Return the AQL bind ref (``@name``) when a pattern's properties are a
    whole-map parameter (``CREATE (n $props)``), else ``None``."""
    if props_ctx is None:
        return None
    param = props_ctx.oC_Parameter()
    if param is None:
        return None
    return _compile_expression(param, bind_vars)


def _build_insert_doc(
    props: list[tuple[str, str]],
    extra_fields: list[str] | None = None,
    base: str | None = None,
) -> str:
    """Build an AQL object literal for INSERT.

    When ``base`` is set (a whole-map parameter like ``@props``), the explicit
    ``extra_fields``/``props`` are merged over it via ``MERGE`` so server-set
    fields (type discriminator, ``_from``/``_to``) still apply.
    """
    fields: list[str] = list(extra_fields or [])
    fields.extend(f"{k}: {v}" for k, v in props)
    inner = "{" + ", ".join(fields) + "}" if fields else "{}"
    if base:
        return base if not fields else f"MERGE({base}, {inner})"
    return inner


def projection_reshapes_rows(proj: CypherParser.OC_ProjectionBodyContext) -> bool:
    """Whether a projection aggregates, sorts, pages or de-duplicates its rows."""
    if (
        proj.DISTINCT() is not None
        or proj.oC_Order() is not None
        or proj.oC_Skip() is not None
        or proj.oC_Limit() is not None
    ):
        return True
    aggregate_call = _core_helper("_aggregate_call")
    parse_collect = _core_helper("_parse_collect")
    nested = _core_helper("nested_aggregates")
    return any(
        aggregate_call(it.oC_Expression().getText().strip()) is not None
        or parse_collect(it.oC_Expression().getText().strip()) is not None
        or bool(nested(it.oC_Expression()))
        for it in proj.oC_ProjectionItems().oC_ProjectionItem()
    )


def _compile_return_for_create(
    proj: CypherParser.OC_ProjectionBodyContext,
    *,
    lines: list[str],
    bind_vars: dict[str, Any],
    indent: str = "",
) -> None:
    """Compile a RETURN clause for a CREATE/MERGE query (respects indent level).

    A RETURN that aggregates, sorts, pages or de-duplicates is the general
    RETURN (``_append_return``); the plain one stays a bare ``RETURN``.
    """
    if projection_reshapes_rows(proj):
        _append_return(proj, lines=lines, bind_vars=bind_vars)
        return
    items_ctx = proj.oC_ProjectionItems()
    items = items_ctx.oC_ProjectionItem()
    if not items:
        raise CoreError("RETURN items required", code="UNSUPPORTED")

    compiled_items: list[tuple[str | None, str]] = []
    written_names: dict[int, str] = {}
    for it in items:
        expr = _compile_expression(it.oC_Expression(), bind_vars)
        alias = it.oC_Variable().getText().strip() if it.oC_Variable() is not None else None
        written = written_column(it)
        if written is not None:
            written_names[len(compiled_items)] = written
        compiled_items.append((alias, expr))

    if len(compiled_items) == 1 and compiled_items[0][0] is None:
        lines.append(f"{indent}RETURN {compiled_items[0][1]}")
    else:
        lines.append(f"{indent}RETURN " + _compile_return_object(compiled_items, written_names))


def _translate_merge_query(
    spq: CypherParser.OC_SinglePartQueryContext,
    *,
    merge_clauses: list[CypherParser.OC_MergeContext],
    resolver: MappingResolver,
    bind_vars: dict[str, Any],
    trailing_sets: list[CypherParser.OC_SetContext] | None = None,
) -> AqlQuery:
    """Translate MERGE clause(s) into AQL UPSERT.

    *trailing_sets* are the SET clauses after the MERGE (``MERGE (n:L {id:
    1}) SET n.p = $v``): they apply to the merged node whether it was created
    or matched, so they join both the INSERT and the UPDATE document. Only a
    single node MERGE takes them; anywhere else they would be dropped, and
    are refused.
    """
    _refuse_untranslated_merge_parts(spq, merge_clauses)
    trailing_sets = trailing_sets or []
    if trailing_sets and (
        len(merge_clauses) != 1
        or merge_clauses[0]
        .oC_PatternPart()
        .oC_AnonymousPatternPart()
        .oC_PatternElement()
        .oC_PatternElementChain()
    ):
        raise CoreError(
            "SET after a relationship MERGE or several MERGEs is not supported: it would not be "
            "written. Use ON CREATE SET / ON MATCH SET, or a separate statement.",
            code="UNSUPPORTED",
        )
    if len(merge_clauses) != 1:
        return _translate_multi_merge_query(
            spq,
            merge_clauses=merge_clauses,
            resolver=resolver,
            bind_vars=bind_vars,
        )
    merge_ctx = merge_clauses[0]
    pattern_part = merge_ctx.oC_PatternPart()
    anon = pattern_part.oC_AnonymousPatternPart()
    elem = anon.oC_PatternElement()
    node = elem.oC_NodePattern()
    chains = elem.oC_PatternElementChain() or []

    if chains:
        return _translate_merge_relationship(
            spq,
            merge_ctx=merge_ctx,
            node=node,
            chains=chains,
            resolver=resolver,
            bind_vars=bind_vars,
        )

    var, _coll_key, coll_ref, search_doc, insert_doc, update_doc = _build_merge_node_docs(
        node, merge_ctx, resolver=resolver, bind_vars=bind_vars, trailing_sets=trailing_sets
    )

    lines: list[str] = []
    _compile_merge_reading_clauses(spq, resolver=resolver, bind_vars=bind_vars, lines=lines)

    lines.append(f"UPSERT {search_doc}")
    lines.append(f"INSERT {insert_doc}")
    lines.append(f"UPDATE {update_doc}")
    lines.append(f"IN {coll_ref}")

    ret = spq.oC_Return()
    if ret is not None:
        ret_var = var
        lines.append(f"LET {ret_var} = NEW")
        lines.extend(merge_path_line(merge_ctx, [ret_var], []))
        _compile_return_for_create(
            ret.oC_ProjectionBody(),
            lines=lines,
            bind_vars=bind_vars,
        )

    return AqlQuery(text="\n".join(lines), bind_vars=bind_vars)


def _refuse_untranslated_merge_parts(
    spq: CypherParser.OC_SinglePartQueryContext, merge_clauses: list[CypherParser.OC_MergeContext]
) -> None:
    """Refuse what the UPSERT translation would drop instead of compiling.

    A path variable is bound for a single MERGE of a node or of one hop
    (:func:`merge_bindings.merge_path_line`); on several MERGEs or a longer pattern it would
    not be, and is refused. A CREATE beside the MERGE (which the AQL also left
    out) is refused by the router (:func:`write_guards.refuse_dropped_writes`)
    before this runs.
    """
    del spq  # the clauses beside the MERGE are the router's to check
    for merge in merge_clauses:
        chains = merge.oC_PatternPart().oC_AnonymousPatternPart().oC_PatternElement().oC_PatternElementChain()
        if merge.oC_PatternPart().oC_Variable() is not None and (
            len(merge_clauses) > 1 or len(chains or []) > 1
        ):
            raise CoreError(
                f"MERGE of a named path ({merge.oC_PatternPart().oC_Variable().getText()} = …) is not supported",
                code="UNSUPPORTED",
            )


def _compile_merge_reading_clauses(
    spq: CypherParser.OC_SinglePartQueryContext,
    *,
    resolver: MappingResolver,
    bind_vars: dict[str, Any],
    lines: list[str],
) -> None:
    """Compile the MATCH/UNWIND reading clauses preceding a MERGE statement.

    Emits the ``FOR …`` prefix (matched pipeline and/or ``UNWIND`` loops) that
    the UPSERT nests inside, so ``UNWIND [..] AS x MERGE (n {p: x})`` becomes
    ``FOR x IN [..] UPSERT {p: x} …`` — previously the ``UNWIND`` was silently
    dropped, leaving ``x`` unbound in the UPSERT.
    """
    read_lines, _ = _compile_write_reading_clauses(spq, resolver=resolver, bind_vars=bind_vars)
    lines.extend(read_lines)


def _declared_names(ctx: Any) -> set[str]:
    """Variables a pattern declares (nodes, relationships, the path)."""
    names: set[str] = set()

    def walk(node: Any) -> None:
        if (
            isinstance(
                node,
                (
                    CypherParser.OC_NodePatternContext,
                    CypherParser.OC_RelationshipDetailContext,
                    CypherParser.OC_PatternPartContext,
                ),
            )
            and node.oC_Variable() is not None
        ):
            names.add(node.oC_Variable().getText().strip())
        for i in range(node.getChildCount() if hasattr(node, "getChildCount") else 0):
            walk(node.getChild(i))

    walk(ctx)
    return names


def _extract_merge_actions(
    merge_ctx: CypherParser.OC_MergeContext,
    bind_vars: dict[str, Any],
    trailing_sets: list[CypherParser.OC_SetContext] | None = None,
    *,
    map_actions: bool = False,
    endpoint_actions: dict[tuple[str, bool], list[str]] | None = None,
) -> tuple[list[str], list[str]]:
    """Extract ON CREATE SET / ON MATCH SET fields from a MERGE clause.

    *trailing_sets* (``MERGE (n …) SET n.p = v``) add to both lists, after the
    actions, as they run after them (:func:`write_guards.trailing_set_fields`).
    """
    on_create_fields: list[str] = []
    on_match_fields: list[str] = []

    # The node a node-only MERGE binds, and its pattern labels.
    elem = merge_ctx.oC_PatternPart().oC_AnonymousPatternPart().oC_PatternElement()
    merged_var, merged_labels = (
        _extract_node_var_and_labels(elem.oC_NodePattern(), default_var="n")
        if not elem.oC_PatternElementChain()
        else ("", [])
    )
    resolver = _active_resolver.get()
    label_fields = shared_label_fields(resolver.bundle.physical_mapping) if resolver is not None else None
    # What an ON CREATE / ON MATCH item may write: the pattern's variables
    # and the query's.
    visible = outer_bound_names(merge_ctx) | _declared_names(merge_ctx.oC_PatternPart())
    # The variable whose document the UPSERT writes: the node of a node MERGE,
    # the relationship of a single-hop one (None: not checked, multi-hop).
    chains = elem.oC_PatternElementChain() or []
    writable: set[str] | None = None
    if not chains:
        writable = {merged_var}
    elif len(chains) == 1:
        rel_detail = chains[0].oC_RelationshipPattern().oC_RelationshipDetail()
        rel_name = rel_detail.oC_Variable() if rel_detail is not None else None
        writable = {_strip_label_backticks(rel_name.getText().strip())} if rel_name is not None else set()

    merge_actions = merge_ctx.oC_MergeAction() or []
    for action in merge_actions:
        is_on_create = action.CREATE() is not None
        set_ctx = action.oC_Set()
        if set_ctx is None:
            continue
        set_items = set_ctx.oC_SetItem() or []
        for si in set_items:
            if si.oC_NodeLabels() is not None:
                target = si.oC_Variable().getText().strip() if si.oC_Variable() is not None else ""
                if target != merged_var or label_fields is None:
                    raise CoreError(
                        f"MERGE ... SET of a label ({si.getText()}) is not supported here",
                        code="UNSUPPORTED",
                    )
                added = [
                    _strip_label_backticks(nl.oC_LabelName().getText().strip())
                    for nl in si.oC_NodeLabels().oC_NodeLabel()
                ]
                if is_on_create:
                    # The created node's labels are known: the pattern's and these.
                    known = list(
                        dict.fromkeys([*(_strip_label_backticks(lb) for lb in merged_labels), *added])
                    )
                    on_create_fields.append(f"{_aql_string_literal(label_fields[1])}: {json.dumps(known)}")
                    if not merged_labels:
                        on_create_fields.append(
                            f"{_aql_string_literal(label_fields[0])}: {json.dumps(known[0])}"
                        )
                else:
                    on_match_fields.extend(label_write_entries("OLD", added, add=True, fields=label_fields))
                continue
            prop_target = (
                _atom_variable(si.oC_PropertyExpression().oC_Atom())
                if si.oC_PropertyExpression() is not None
                else None
            )
            if prop_target is not None and prop_target not in visible:
                # openCypher UndefinedVariable.
                raise CoreError(
                    f"MERGE ... SET refers to undefined variable {prop_target!r}", code="UNSUPPORTED"
                )
            if (
                prop_target is not None
                and endpoint_actions is not None
                and writable is not None
                and prop_target not in writable
                and prop_target in outer_bound_names(merge_ctx)
                and len(si.oC_PropertyExpression().oC_PropertyLookup() or []) == 1
            ):
                # A property of a bound endpoint (``ON CREATE SET b.created``):
                # written to that node after the UPSERT (merge_bindings).
                key = si.oC_PropertyExpression().oC_PropertyLookup()[0].oC_PropertyKeyName().getText().strip()
                value = _compile_expression(si.oC_Expression(), bind_vars)
                endpoint_actions.setdefault((prop_target, is_on_create), []).append(f"{key}: {value}")
                continue
            if prop_target is not None and writable is not None and prop_target not in writable:
                # The action's value goes into the UPSERT's own document: a
                # property of another variable was written onto the merged
                # node or relationship instead.
                raise CoreError(
                    f"MERGE ... SET of {prop_target!r}, which the MERGE does not write "
                    f"({si.getText()}), is not supported",
                    code="UNSUPPORTED",
                )
            if si.oC_PropertyExpression() is None:
                whole = si.oC_Variable()
                target = _strip_label_backticks(whole.getText().strip()) if whole is not None else None
                if not map_actions or target is None or writable is None or target not in writable:
                    # ``SET r = map`` / ``SET r += map`` would otherwise be dropped.
                    raise CoreError(
                        f"MERGE ... SET of a whole map ({si.getText()}) is not supported here",
                        code="NOT_IMPLEMENTED",
                    )
                # A node's properties, or the map itself (merge_docs).
                value = _compile_cypher_fragment(f"properties({si.oC_Expression().getText()})", bind_vars)
                action = MapAction(value, replaces=not any(c.getText() == "+=" for c in si.getChildren()))
                (on_create_fields if is_on_create else on_match_fields).append(action)
                continue
            prop_expr = si.oC_PropertyExpression()
            if prop_expr is not None:
                lookups = prop_expr.oC_PropertyLookup() or []
                if not lookups:
                    continue
                prop_name = lookups[-1].oC_PropertyKeyName().getText().strip()
                val = _compile_expression(si.oC_Expression(), bind_vars)
                if is_on_create:
                    on_create_fields.append(f"{prop_name}: {val}")
                else:
                    on_match_fields.append(f"{prop_name}: {val}")

    for prop_name, val in trailing_set_fields(trailing_sets or [], merged_var, bind_vars):
        # The SET runs after ON CREATE / ON MATCH: its value wins.
        for fields in (on_create_fields, on_match_fields):
            fields[:] = [f for f in fields if not f.startswith(f"{prop_name}: ")]
            fields.append(f"{prop_name}: {val}")
    return on_create_fields, on_match_fields


def _translate_merge_relationship(
    spq: CypherParser.OC_SinglePartQueryContext,
    *,
    merge_ctx: CypherParser.OC_MergeContext,
    node: CypherParser.OC_NodePatternContext,
    chains: list,
    resolver: MappingResolver,
    bind_vars: dict[str, Any],
) -> AqlQuery:
    """Translate ``MERGE (a)-[:REL {props}]->(b)`` into AQL UPSERT on an edge collection.

    With an endpoint no earlier clause binds, the whole pattern is merged
    instead (:mod:`merge_patterns`), or refused where that is not covered.
    """
    unbound = unbound_endpoints(merge_ctx, outer_bound_names(merge_ctx))
    refuse_unbound_endpoints(
        merge_ctx,
        unbound,
        first_clause=not spq.oC_ReadingClause()
        and isinstance(spq.parentCtx, CypherParser.OC_SingleQueryContext),
        hops=len(chains),
    )
    if len(chains) != 1:
        return _translate_merge_multi_hop(
            spq,
            merge_ctx=merge_ctx,
            node=node,
            chains=chains,
            resolver=resolver,
            bind_vars=bind_vars,
        )

    chain = chains[0]
    rel_pat = chain.oC_RelationshipPattern()
    target_node = chain.oC_NodePattern()
    if rel_pat is None or target_node is None:
        raise CoreError("Invalid relationship MERGE pattern", code="UNSUPPORTED")

    start_var, _ = _extract_node_var_and_labels(node, default_var="a")
    end_var, _ = _extract_node_var_and_labels(target_node, default_var="b")

    detail = rel_pat.oC_RelationshipDetail()
    if detail is None:
        raise CoreError("Relationship type is required for MERGE", code="UNSUPPORTED")
    types_ctx = detail.oC_RelationshipTypes()
    if types_ctx is None:
        raise CoreError("Relationship type is required for MERGE", code="UNSUPPORTED")
    type_names = types_ctx.oC_RelTypeName()
    if not type_names or len(type_names) != 1:
        raise CoreError("Exactly one relationship type is required for MERGE", code="UNSUPPORTED")
    rel_type = type_names[0].getText().strip()

    direction = _relationship_direction(rel_pat)
    if direction == "INBOUND":
        from_var, to_var = end_var, start_var
    else:
        from_var, to_var = start_var, end_var

    r_map = resolver.resolve_relationship(_strip_label_backticks(rel_type))
    r_style = r_map.get("style")
    edge_coll_name = r_map.get("edgeCollectionName") or r_map.get("collectionName")
    if not isinstance(edge_coll_name, str) or not edge_coll_name:
        raise CoreError(
            f"Invalid relationship mapping collection for: {rel_type}",
            code="INVALID_MAPPING",
        )
    edge_coll_key = _find_or_create_collection_bind_key(
        "@edgeCollection",
        edge_coll_name,
        bind_vars,
    )
    edge_coll_ref = _aql_collection_ref(edge_coll_key)

    search_fields = [f"_from: {from_var}._id", f"_to: {to_var}._id"]
    insert_fields = [f"_from: {from_var}._id", f"_to: {to_var}._id"]

    if r_style == "GENERIC_WITH_TYPE":
        type_field = r_map.get("typeField", "type")
        rtv_key = _pick_bind_key("relTypeValue", bind_vars)
        bind_vars[rtv_key] = r_map.get("typeValue")
        search_fields.append(f"{type_field}: @{rtv_key}")
        insert_fields.append(f"{type_field}: @{rtv_key}")

    # The pattern's properties are part of what the MERGE matches, not only
    # of what it creates: (a)-[:T {name: 'r2'}]->(b) does not match an r1 edge.
    for k, v in _compile_create_rel_props(rel_pat, bind_vars):
        search_fields.append(f"{k}: {v}")
        insert_fields.append(f"{k}: {v}")

    if unbound:
        return _translate_pattern_merge(
            spq,
            merge_ctx=merge_ctx,
            nodes=(node, target_node),
            edge=MergeEdge(
                var=detail.oC_Variable().getText().strip()
                if detail.oC_Variable() is not None
                else "_mergeRel",
                coll_key=edge_coll_key,
                direction=direction,
                search_fields=search_fields[2:],
                insert_fields=insert_fields[2:],
            ),
            resolver=resolver,
            bind_vars=bind_vars,
        )

    endpoint_actions: dict[tuple[str, bool], list[str]] = {}
    on_create_fields, on_match_fields = _extract_merge_actions(
        merge_ctx, bind_vars, map_actions=True, endpoint_actions=endpoint_actions
    )

    search_doc = "{" + ", ".join(search_fields) + "}"
    # What a whole-map ``SET r = …`` keeps: the edge's endpoints and type.
    keep = ["_from", "_to", *([r_map.get("typeField", "type")] if r_style == "GENERIC_WITH_TYPE" else [])]
    insert_doc = insert_document(insert_fields, on_create_fields, keep)

    lines: list[str] = []
    _compile_merge_reading_clauses(spq, resolver=resolver, bind_vars=bind_vars, lines=lines)

    lines.append(f"UPSERT {search_doc}")
    lines.append(f"INSERT {insert_doc}")
    lines.append(update_clause(on_match_fields, keep))
    lines.append(f"IN {edge_coll_ref}{upsert_options(on_match_fields)}")
    if endpoint_actions:
        lines.extend(
            endpoint_action_lines(
                endpoint_actions,
                _infer_var_collections_from_pipeline(lines),
                bind_vars,
                final=spq.oC_Return() is None,
            )
        )

    ret = spq.oC_Return()
    if ret is not None:
        refuse_stale_endpoint_return(ret, endpoint_actions)
        rel_var_name = ""
        if detail.oC_Variable() is not None:
            rel_var_name = detail.oC_Variable().getText().strip()
        if not rel_var_name:
            rel_var_name = "r"
        lines.append(f"LET {rel_var_name} = NEW")
        lines.extend(merge_path_line(merge_ctx, [start_var, end_var], [rel_var_name]))
        _compile_return_for_create(
            ret.oC_ProjectionBody(),
            lines=lines,
            bind_vars=bind_vars,
        )

    return AqlQuery(text="\n".join(lines), bind_vars=bind_vars)


def _translate_pattern_merge(
    spq: CypherParser.OC_SinglePartQueryContext,
    *,
    merge_ctx: CypherParser.OC_MergeContext,
    nodes: tuple[CypherParser.OC_NodePatternContext, CypherParser.OC_NodePatternContext],
    edge: MergeEdge,
    resolver: MappingResolver,
    bind_vars: dict[str, Any],
) -> AqlQuery:
    """``MERGE (a:A)-[r:R]->(b:B)`` as the first clause: merge the whole pattern (:mod:`merge_patterns`)."""
    endpoints: list[MergeEndpoint] = []
    for node, default in zip(nodes, ("_mergeStart", "_mergeEnd"), strict=True):
        name = node.oC_Variable().getText().strip() if node.oC_Variable() is not None else default
        if endpoints and name == endpoints[0].var:
            # A self-loop: ``(a:A)-[:R]->(a)``; the second mention adds nothing.
            if node.oC_NodeLabels() is not None or node.oC_Properties() is not None:
                raise CoreError(
                    f"MERGE cannot add labels or properties to {name!r} on its second mention in the pattern",
                    code="UNSUPPORTED",
                )
            endpoints.append(endpoints[0])
            continue
        _, coll_key, _, search_doc, insert_doc, _ = _build_merge_node_docs(
            node, merge_ctx, resolver=resolver, bind_vars=bind_vars
        )
        endpoints.append(
            MergeEndpoint(var=name, coll_key=coll_key, search_doc=search_doc, insert_doc=insert_doc)
        )
    ret = spq.oC_Return()
    lines = pattern_merge_lines(endpoints[0], endpoints[1], edge, final=ret is None)
    if ret is not None:
        lines.extend(merge_path_line(merge_ctx, [endpoints[0].var, endpoints[1].var], [edge.var]))
        _compile_return_for_create(ret.oC_ProjectionBody(), lines=lines, bind_vars=bind_vars)
    return AqlQuery(text="\n".join(lines), bind_vars=bind_vars)


def _translate_merge_multi_hop(
    spq: CypherParser.OC_SinglePartQueryContext,
    *,
    merge_ctx: CypherParser.OC_MergeContext,
    node: CypherParser.OC_NodePatternContext,
    chains: list,
    resolver: MappingResolver,
    bind_vars: dict[str, Any],
) -> AqlQuery:
    """Translate a multi-hop relationship MERGE — ``MERGE (a)-[:R1]->(b)-[:R2]->(c)``.

    Each hop becomes its own edge UPSERT. Endpoints must be bound by a preceding
    MATCH (same requirement as single-hop relationship MERGE — the UPSERT
    references each node's ``_id``). AQL cannot write a collection twice in one
    query (ERR 1579), so each hop must map to a *distinct* edge collection; a
    repeat fails closed. ``ON CREATE``/``ON MATCH SET`` is ambiguous across hops
    and a trailing RETURN only sees the last edge, so both are rejected.
    """
    if merge_ctx.oC_MergeAction():
        raise CoreError(
            "ON CREATE/ON MATCH SET is not supported with multi-hop MERGE; use single-hop MERGE statements",
            code="NOT_IMPLEMENTED",
        )

    hops: list[tuple[Any, Any, Any]] = []
    current_node = node
    for chain in chains:
        rel_pat = chain.oC_RelationshipPattern()
        end_node = chain.oC_NodePattern()
        if rel_pat is None or end_node is None:
            raise CoreError("Invalid relationship MERGE pattern", code="UNSUPPORTED")
        hops.append((current_node, rel_pat, end_node))
        current_node = end_node

    lines: list[str] = []
    _compile_merge_reading_clauses(spq, resolver=resolver, bind_vars=bind_vars, lines=lines)

    used_edge_keys: set[str] = set()
    for from_node, rel_pat, to_node in hops:
        start_var, _ = _extract_node_var_and_labels(from_node, default_var="a")
        end_var, _ = _extract_node_var_and_labels(to_node, default_var="b")

        detail = rel_pat.oC_RelationshipDetail()
        if detail is None:
            raise CoreError("Relationship type is required for MERGE", code="UNSUPPORTED")
        types_ctx = detail.oC_RelationshipTypes()
        if types_ctx is None:
            raise CoreError("Relationship type is required for MERGE", code="UNSUPPORTED")
        type_names = types_ctx.oC_RelTypeName()
        if not type_names or len(type_names) != 1:
            raise CoreError("Exactly one relationship type is required for MERGE", code="UNSUPPORTED")
        rel_type = type_names[0].getText().strip()

        direction = _relationship_direction(rel_pat)
        if direction == "INBOUND":
            from_var, to_var = end_var, start_var
        else:
            from_var, to_var = start_var, end_var

        r_map = resolver.resolve_relationship(_strip_label_backticks(rel_type))
        r_style = r_map.get("style")
        edge_coll_name = r_map.get("edgeCollectionName") or r_map.get("collectionName")
        if not isinstance(edge_coll_name, str) or not edge_coll_name:
            raise CoreError(
                f"Invalid relationship mapping collection for: {rel_type}",
                code="INVALID_MAPPING",
            )
        edge_coll_key = _find_or_create_collection_bind_key("@edgeCollection", edge_coll_name, bind_vars)
        if edge_coll_key in used_edge_keys:
            raise CoreError(
                f"Multi-hop MERGE reuses edge collection ({edge_coll_name!r}); AQL "
                "cannot write a collection twice in one query (ERR 1579). Split "
                "into separate statements.",
                code="NOT_IMPLEMENTED",
            )
        used_edge_keys.add(edge_coll_key)
        edge_coll_ref = _aql_collection_ref(edge_coll_key)

        search_fields = [f"_from: {from_var}._id", f"_to: {to_var}._id"]
        insert_fields = [f"_from: {from_var}._id", f"_to: {to_var}._id"]
        if r_style == "GENERIC_WITH_TYPE":
            type_field = r_map.get("typeField", "type")
            rtv_key = _pick_bind_key("relTypeValue", bind_vars)
            bind_vars[rtv_key] = r_map.get("typeValue")
            search_fields.append(f"{type_field}: @{rtv_key}")
            insert_fields.append(f"{type_field}: @{rtv_key}")
        for k, v in _compile_create_rel_props(rel_pat, bind_vars):
            search_fields.append(f"{k}: {v}")
            insert_fields.append(f"{k}: {v}")

        lines.append("UPSERT {" + ", ".join(search_fields) + "}")
        lines.append("INSERT {" + ", ".join(insert_fields) + "}")
        lines.append("UPDATE {}")
        lines.append(f"IN {edge_coll_ref}")

    if spq.oC_Return() is not None:
        raise CoreError(
            "RETURN after multi-hop MERGE is not supported; a trailing RETURN only sees the last edge",
            code="NOT_IMPLEMENTED",
        )

    return AqlQuery(text="\n".join(lines), bind_vars=bind_vars)


def _build_merge_node_docs(
    node: CypherParser.OC_NodePatternContext,
    merge_ctx: CypherParser.OC_MergeContext,
    *,
    resolver: MappingResolver,
    bind_vars: dict[str, Any],
    trailing_sets: list[CypherParser.OC_SetContext] | None = None,
) -> tuple[str, str, str, str, str, str]:
    """Build the UPSERT docs for a node MERGE.

    Returns ``(var, coll_key, coll_ref, search_doc, insert_doc, update_doc)``.
    Shared by the single- and multi-MERGE translators so both emit identical
    document literals (only the surrounding statement form differs).
    """
    var, labels = _extract_node_var_and_labels(node, default_var="n")
    if node.oC_Variable() is not None and var in outer_bound_names(merge_ctx):
        # openCypher VariableAlreadyBound: MERGE (a) after MATCH (a).
        raise CoreError(f"MERGE cannot rebind the bound variable {var!r}", code="UNSUPPORTED")
    if any(v.strip() == "null" for _, v in _compile_create_props(node.oC_Properties(), dict(bind_vars))):
        # openCypher MergeReadOwnWrites: a null property can never be matched.
        raise CoreError("MERGE cannot match a null property value", code="UNSUPPORTED")
    if not labels:
        # ``MERGE (a {…})``: any node of the one collection unlabeled nodes
        # live in (failing closed when that is ambiguous), by its properties.
        coll_key = _find_or_create_collection_bind_key(
            "@collection", _infer_unlabeled_collection(resolver), bind_vars
        )
        props = _compile_create_props(node.oC_Properties(), bind_vars)
        search_doc = _build_insert_doc(props, [])
        on_create_fields, on_match_fields = _extract_merge_actions(merge_ctx, bind_vars, trailing_sets)
        insert_fields = [*(f"{k}: {v}" for k, v in props), *on_create_fields]
        insert_doc = "{" + ", ".join(insert_fields) + "}" if on_create_fields else search_doc
        update_doc = "{" + ", ".join(on_match_fields) + "}" if on_match_fields else "{}"
        return var, coll_key, _aql_collection_ref(coll_key), search_doc, insert_doc, update_doc

    primary = _pick_primary_entity_label(labels, resolver)
    entity_mapping = resolver.resolve_entity(_strip_label_backticks(primary))
    style = entity_mapping.get("style")
    if style == "UNION":
        raise CoreError(
            f"MERGE on {primary!r} is not supported: it is a supertype whose members live in "
            "several collections",
            code="UNSUPPORTED",
        )
    if style == "MULTI_LABEL" or _supertype_label_fields(
        _strip_label_backticks(primary), entity_mapping.get("collectionName") or "", resolver
    ):
        raise CoreError(
            f"MERGE on {primary!r} is not supported: it is, or has, a supertype whose "
            "labels array the MERGE would have to match",
            code="UNSUPPORTED",
        )
    coll_key = _find_or_create_collection_bind_key(
        "@collection",
        entity_mapping["collectionName"],
        bind_vars,
    )
    coll_ref = _aql_collection_ref(coll_key)

    props = _compile_create_props(node.oC_Properties(), bind_vars)
    extra_fields: list[str] = []
    if style == "LABEL":
        type_field = entity_mapping.get("typeField", "type")
        tv_key = _pick_bind_key("typeValue", bind_vars)
        bind_vars[tv_key] = entity_mapping.get("typeValue")
        extra_fields.append(f"{type_field}: @{tv_key}")

    search_doc = _build_insert_doc(props, extra_fields)
    insert_doc = search_doc

    on_create_fields, on_match_fields = _extract_merge_actions(merge_ctx, bind_vars, trailing_sets)
    if on_create_fields:
        all_insert_fields = list(extra_fields)
        all_insert_fields.extend(f"{k}: {v}" for k, v in props)
        all_insert_fields.extend(on_create_fields)
        insert_doc = "{" + ", ".join(all_insert_fields) + "}" if all_insert_fields else "{}"

    update_doc = "{" + ", ".join(on_match_fields) + "}" if on_match_fields else "{}"
    return var, coll_key, coll_ref, search_doc, insert_doc, update_doc


def _build_merge_rel_docs(
    node: CypherParser.OC_NodePatternContext,
    chain: CypherParser.OC_PatternElementChainContext,
    merge_ctx: CypherParser.OC_MergeContext,
    *,
    resolver: MappingResolver,
    bind_vars: dict[str, Any],
    var_ids: dict[str, str],
) -> tuple[str, str, str, str, str, str]:
    """Build the UPSERT docs for a single-hop relationship MERGE whose endpoints
    are already bound by earlier MERGE clauses in the same statement.

    Returns ``(var, edge_coll_key, edge_coll_ref, search_doc, insert_doc,
    update_doc)``. Endpoints are referenced by their bound ``_id`` (via
    ``var_ids``) rather than re-scanned, because the multi-MERGE form runs each
    element as a ``LET``-bound UPSERT.
    """
    rel_pat = chain.oC_RelationshipPattern()
    target_node = chain.oC_NodePattern()
    if rel_pat is None or target_node is None:
        raise CoreError("Invalid relationship MERGE pattern", code="UNSUPPORTED")

    start_var, _ = _extract_node_var_and_labels(node, default_var="a")
    end_var, _ = _extract_node_var_and_labels(target_node, default_var="b")
    for endpoint in (start_var, end_var):
        if endpoint not in var_ids:
            raise CoreError(
                f"MERGE relationship endpoint {endpoint!r} must be created by an "
                "earlier MERGE in the same statement (multi-MERGE does not scan "
                "matched nodes); add a MERGE for it or use a separate statement",
                code="NOT_IMPLEMENTED",
            )

    detail = rel_pat.oC_RelationshipDetail()
    if detail is None:
        raise CoreError("Relationship type is required for MERGE", code="UNSUPPORTED")
    types_ctx = detail.oC_RelationshipTypes()
    if types_ctx is None:
        raise CoreError("Relationship type is required for MERGE", code="UNSUPPORTED")
    type_names = types_ctx.oC_RelTypeName()
    if not type_names or len(type_names) != 1:
        raise CoreError("Exactly one relationship type is required for MERGE", code="UNSUPPORTED")
    rel_type = type_names[0].getText().strip()

    direction = _relationship_direction(rel_pat)
    if direction == "INBOUND":
        from_id, to_id = var_ids[end_var], var_ids[start_var]
    else:
        from_id, to_id = var_ids[start_var], var_ids[end_var]

    r_map = resolver.resolve_relationship(_strip_label_backticks(rel_type))
    r_style = r_map.get("style")
    edge_coll_name = r_map.get("edgeCollectionName") or r_map.get("collectionName")
    if not isinstance(edge_coll_name, str) or not edge_coll_name:
        raise CoreError(
            f"Invalid relationship mapping collection for: {rel_type}",
            code="INVALID_MAPPING",
        )
    edge_coll_key = _find_or_create_collection_bind_key(
        "@edgeCollection",
        edge_coll_name,
        bind_vars,
    )
    edge_coll_ref = _aql_collection_ref(edge_coll_key)

    search_fields = [f"_from: {from_id}", f"_to: {to_id}"]
    insert_fields = [f"_from: {from_id}", f"_to: {to_id}"]
    if r_style == "GENERIC_WITH_TYPE":
        type_field = r_map.get("typeField", "type")
        rtv_key = _pick_bind_key("relTypeValue", bind_vars)
        bind_vars[rtv_key] = r_map.get("typeValue")
        search_fields.append(f"{type_field}: @{rtv_key}")
        insert_fields.append(f"{type_field}: @{rtv_key}")

    for k, v in _compile_create_rel_props(rel_pat, bind_vars):
        insert_fields.append(f"{k}: {v}")

    on_create_fields, on_match_fields = _extract_merge_actions(merge_ctx, bind_vars)
    search_doc = "{" + ", ".join(search_fields) + "}"
    insert_doc = "{" + ", ".join(insert_fields + on_create_fields) + "}"
    update_doc = "{" + ", ".join(on_match_fields) + "}" if on_match_fields else "{}"

    rel_var_name = ""
    if detail.oC_Variable() is not None:
        rel_var_name = detail.oC_Variable().getText().strip()
    if not rel_var_name:
        rel_var_name = "r"

    return rel_var_name, edge_coll_key, edge_coll_ref, search_doc, insert_doc, update_doc


def _translate_multi_merge_query(
    spq: CypherParser.OC_SinglePartQueryContext,
    *,
    merge_clauses: list[CypherParser.OC_MergeContext],
    resolver: MappingResolver,
    bind_vars: dict[str, Any],
) -> AqlQuery:
    """Translate several MERGE clauses into a sequence of ``LET``-bound UPSERTs.

    AQL forbids reading a collection after it has been modified in the same
    query (ERR 1579), so each MERGE must target a *distinct* physical
    collection; a repeat is rejected with an actionable error. Relationship
    MERGE endpoints must be bound by an earlier node MERGE in the same
    statement. Preceding MATCH is not supported here (scoping/nesting differs);
    split such queries into separate statements.
    """
    reading_clauses = spq.oC_ReadingClause() or []
    if any(rc.oC_Match() is not None for rc in reading_clauses):
        raise CoreError(
            "MATCH combined with multiple MERGE clauses is not supported; split into separate statements",
            code="NOT_IMPLEMENTED",
        )

    lines: list[str] = []
    used_collection_keys: set[str] = set()
    var_ids: dict[str, str] = {}

    def _claim(coll_key: str) -> None:
        if coll_key in used_collection_keys:
            raise CoreError(
                f"Multiple MERGE clauses target the same collection "
                f"({bind_vars.get(coll_key)!r}); AQL cannot read a collection "
                "after modifying it in the same query (ERR 1579). Split into "
                "separate statements.",
                code="NOT_IMPLEMENTED",
            )
        used_collection_keys.add(coll_key)

    for mc in merge_clauses:
        pattern_part = mc.oC_PatternPart()
        elem = pattern_part.oC_AnonymousPatternPart().oC_PatternElement()
        node = elem.oC_NodePattern()
        chains = elem.oC_PatternElementChain() or []

        if len(chains) > 1:
            raise CoreError(
                "Only single-hop relationship MERGE is supported",
                code="NOT_IMPLEMENTED",
            )

        if chains:
            var, coll_key, coll_ref, search_doc, insert_doc, update_doc = _build_merge_rel_docs(
                node, chains[0], mc, resolver=resolver, bind_vars=bind_vars, var_ids=var_ids
            )
        else:
            var, coll_key, coll_ref, search_doc, insert_doc, update_doc = _build_merge_node_docs(
                node, mc, resolver=resolver, bind_vars=bind_vars
            )

        _claim(coll_key)
        lines.append(
            f"LET {var} = FIRST(UPSERT {search_doc} INSERT {insert_doc} "
            f"UPDATE {update_doc} IN {coll_ref} RETURN NEW)"
        )
        var_ids[var] = f"{var}._id"

    ret = spq.oC_Return()
    if ret is not None:
        _compile_return_for_create(ret.oC_ProjectionBody(), lines=lines, bind_vars=bind_vars)
    else:
        # The LET-bound UPSERTs are subqueries, not top-level modifications, so
        # the query needs a terminal RETURN to be valid AQL. The writes still
        # execute even though the result is discarded.
        lines.append("RETURN null")

    return AqlQuery(text="\n".join(lines), bind_vars=bind_vars)
