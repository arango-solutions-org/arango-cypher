"""FOREACH compilation for the v0 translator (``FOREACH (x IN list | SET/CREATE/DELETE …)``)."""

from __future__ import annotations

from typing import Any

from arango_query_core import AqlQuery, CoreError, MappingResolver

from .._antlr.CypherParser import CypherParser
from .naming import _aql_collection_ref, _pick_bind_key, _strip_label_backticks
from .write_create import _compile_create, _find_or_create_collection_bind_key
from .write_guards import (
    refuse_untranslated_foreach_parts,
)
from .writes import (
    _compile_expression,
    _compile_match_multi_parts_from_parts,
    _compile_match_pipeline,
    _infer_unlabeled_collection,
)


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
