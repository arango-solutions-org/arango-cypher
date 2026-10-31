from __future__ import annotations

import re
from collections.abc import Callable
from types import MappingProxyType
from typing import Any

from arango_query_core import (
    SUPERTYPE_STYLES,
    AqlQuery,
    CoreError,
    MappingBundle,
    MappingResolver,
)

from .._antlr.CypherParser import CypherParser
from ..parser import deep_recursion, parse_cypher
from . import temporal_strings
from .aggregates import (
    aggregate_key,
    aggregate_variable,
    aggregate_variables,
    lower_aggregate,
    nested_aggregates,
    sort_aggregates,
)
from .arithmetic import (
    FLOAT,
    INT,
    NUMBER,
    STRING,
    _numeric_type,
    _static_type,
    additive_chain_propagates_null,
    collecting_null_operands,
    compile_additive_chain,
    compile_null_operand,
    declared_static_type,
    is_arithmetic_transparent,
    null_propagating,
    raw_additive_chain,
    raw_arithmetic,
)
from .bind_names import _merge_bind_vars, _refuse_overwritten_params
from .calls import (
    _inject_in_query_calls,
    _translate_standalone_call,
    _translate_standalone_in_query_call,
    in_query_call_lines,
)
from .columns import _active_cypher_column_names, object_key, written_column
from .const_eval import _const_env, aql_number_literal, bind_with_constants, try_fold
from .conversions import compile_to_boolean, compile_to_float, compile_to_integer, compile_to_string
from .deletes import translate_delete_query
from .distinct_order import append_distinct_return, live_sort_terms, order_keeping_collect
from .formatting import (
    _guard_varlength_edge_types,
    _prepend_with_collections,
    _reindent_aql,
    empty_varlength_ranges,
)
from .graph_functions import compile_keys, compile_labels, compile_properties
from .hints import (
    _build_collection_index_hint,
    _build_vci_options,
    _warn_missing_vci,
    _warn_multi_label_collection,
)
from .inserts import (
    is_create_pipeline,
    single_part_create_needs_pipeline,
    translate_create_pipeline,
    translate_single_part_create,
)
from .label_tests import label_test
from .literals import _aql_string_from_cypher, _aql_string_literal
from .local_scopes import local_aql_name, quantifier_aql, rename_local
from .multi_type import (
    MULTI_TYPE_SEPARATOR,
    _is_edge_collection_value,
    expand_multi_type_bindings,
    multi_type_runtime_expr,
    resolve_multi_type,
)
from .naming import (
    _AQL_RESERVED,
    _affix_name,
    _aql_bind_name,
    _aql_collection_ref,
    _declared_aql_vars,
    _pick_bind_key,
    _pick_fresh_var,
    _quote_aql_reserved_names,
    _reject_unsafe_escaped_names,
    _rewrite_vars,
    _strip_label_backticks,
)
from .pattern_reversal import from_bound_end
from .percentiles import parse_percentile, percentile_value
from .pipeline_writes import append_midquery_writes, refuse_after_write, split_writes
from .plan_steps import translate_step
from .scopes import is_referenced, outer_bound_names
from .semantic_check import check_semantics
from .shortest_path import (
    compile_shortest_path,
    pattern_element,
    refuse_shortest_path_in_writes,
    refuse_unbound_shortest_path,
    shortest_path_pattern,
)
from .sort_keys import (
    SORT_VALUE_TYPES,
    cypher_sort_key,
    extreme_spec,
    extreme_value,
    needs_cypher_sort_key,
    read_grouping_keys,
)
from .state import (
    StepInput,
    TranslateOptions,
    _active_case_insensitive_inline,
    _active_path_vars,
    _active_registry,
    _active_resolver,
    _active_user_params,
    _active_user_vars,
    _active_warnings,
    _HopMeta,
    _object_rows,
    _pending_unwinds,
    _temporal_vars,
)
from .string_predicates import compile_string_predicate
from .subqueries import (
    compile_subquery_call,
    has_subquery_call,
    refuse_unsupported_subquery_calls,
    subquery_call,
    translate_single_part_with_calls,
)
from .subscripts import compile_subscript
from .supertypes import node_collection_filter, node_scan_source, union_membership_filter
from .temporal import (
    bind_with,
    comparison_operands,
    compile_duration,
    compile_property_chain,
    compile_temporal_arithmetic,
    compile_temporal_function,
    has_temporal_operand,
    is_duration,
    refuse_temporal_operands,
)
from .three_valued import (
    _PREDICATE_MODE,
    _closing_bracket,
    _compile_comparison,
    _is_obvious_non_null,
    _is_predicate_transparent,
    _is_two_valued,
    _negate_mode,
    _negate_value,
    _test_truth,
    compile_in,
)
from .unwind import _translate_standalone_unwind, unwind_source
from .write_create import _translate_create_query
from .write_foreach import _translate_foreach_query
from .write_guards import refuse_dropped_writes
from .write_merge import _translate_merge_query
from .write_mutate import _translate_mutating_query


def translate_v0(
    cypher: str,
    *,
    mapping: MappingBundle,
    params: dict[str, Any] | None = None,
    options: TranslateOptions | None = None,
) -> AqlQuery:
    """
    v0 translation for a small Cypher subset:
    - Single MATCH with a single node pattern: MATCH (n:Label)
    - Optional WHERE over simple expressions
    - RETURN with projection items / aliases / DISTINCT
    - LIMIT (only if present in RETURN clause)
    """
    if not mapping:
        raise CoreError("mapping is required", code="INVALID_ARGUMENT")

    opts = options or TranslateOptions()
    registry = opts.registry
    reg_token = _active_registry.set(registry)
    ci_token = _active_case_insensitive_inline.set(opts.case_insensitive_inline_strings)
    columns_token = _active_cypher_column_names.set(opts.cypher_column_names)
    resolver = MappingResolver(mapping)
    res_token = _active_resolver.set(resolver)
    warnings: list[str] = []
    warn_token = _active_warnings.set(warnings)
    path_vars: dict[str, tuple[list[str], list[str]]] = {}
    path_token = _active_path_vars.set(path_vars)
    user_vars_token = _active_user_vars.set([])
    user_params_token = _active_user_params.set(frozenset(params or {}))
    temporal_token = _temporal_vars.set({})
    const_token = _const_env.set(MappingProxyType({}))
    sort_types_token = SORT_VALUE_TYPES.set({})
    step = opts.step_input
    rows_token = _object_rows.set(True) if step is not None and step.object_rows else None

    try:
        with temporal_strings.translation_scope(opts.temporal_strings), deep_recursion():
            result = _translate_v0_inner(
                cypher,
                mapping=mapping,
                params=params,
                step=step,
            )
        _refuse_overwritten_params(params, result.bind_vars)
        # The variable-length guard reads one edge collection per traversal,
        # so it runs before several types expand into several collections.
        guarded = AqlQuery(
            text=_guard_varlength_edge_types(empty_varlength_ranges(result.text)),
            bind_vars=result.bind_vars,
            debug=result.debug,
            warnings=result.warnings,
        )
        result = _prepend_with_collections(expand_multi_type_bindings(guarded), resolver)
        result = AqlQuery(
            text=_reindent_aql(result.text),
            bind_vars=result.bind_vars,
            debug=result.debug,
            warnings=tuple(warnings) if warnings else result.warnings,
        )
        return result
    finally:
        if rows_token is not None:
            _object_rows.reset(rows_token)
        SORT_VALUE_TYPES.reset(sort_types_token)
        _active_user_vars.reset(user_vars_token)
        _active_user_params.reset(user_params_token)
        _temporal_vars.reset(temporal_token)
        _const_env.reset(const_token)
        _active_path_vars.reset(path_token)
        _active_warnings.reset(warn_token)
        _active_resolver.reset(res_token)
        _active_registry.reset(reg_token)
        _active_case_insensitive_inline.reset(ci_token)
        _active_cypher_column_names.reset(columns_token)


def _translate_v0_inner(
    cypher: str,
    *,
    mapping: MappingBundle,
    params: dict[str, Any] | None = None,
    step: StepInput | None = None,
) -> AqlQuery:
    bind_vars: dict[str, Any] = dict(params or {})
    resolver = MappingResolver(mapping)

    pr = parse_cypher(cypher)
    tree = pr.tree
    _reject_unsafe_escaped_names(tree)
    step_imports = (
        {i.name: i.kind for i in step.imports} if step is not None and step.rows_param is not None else None
    )
    check_semantics(tree, imports=step_imports)
    _quote_aql_reserved_names(tree)
    refuse_shortest_path_in_writes(tree)
    refuse_unsupported_subquery_calls(tree)

    query_ctx = tree.oC_Statement().oC_Query()

    standalone_call = query_ctx.oC_StandaloneCall()
    if standalone_call is not None:
        return _translate_standalone_call(standalone_call, bind_vars=bind_vars)

    regular = query_ctx.oC_RegularQuery()
    if regular is None:
        raise CoreError("Only regular queries are supported in v0", code="UNSUPPORTED")

    union_clauses = regular.oC_Union()
    if union_clauses:
        return _translate_union(
            regular,
            resolver=resolver,
            bind_vars=bind_vars,
        )

    single_query = regular.oC_SingleQuery()
    if step is not None and step.rows_param is not None:
        return translate_step(single_query, step, resolver=resolver, bind_vars=bind_vars)
    return _translate_single_query(single_query, resolver=resolver, bind_vars=bind_vars)


def _translate_single_query(
    sq: CypherParser.OC_SingleQueryContext,
    *,
    resolver: MappingResolver,
    bind_vars: dict[str, Any],
) -> AqlQuery:
    """Translate one ``oC_SingleQuery`` (single- or multi-part) into AQL."""

    mpq = sq.oC_MultiPartQuery()
    if mpq is not None:
        return _translate_multi_part_query(
            mpq,
            resolver=resolver,
            bind_vars=bind_vars,
        )

    spq = sq.oC_SinglePartQuery()
    if spq is None:
        raise CoreError(
            "Only single-part queries are supported in v0",
            code="UNSUPPORTED",
        )

    updating_clauses = spq.oC_UpdatingClause() or []
    if updating_clauses:
        create_clauses: list[CypherParser.OC_CreateContext] = []
        set_clauses: list[CypherParser.OC_SetContext] = []
        delete_clauses: list[CypherParser.OC_DeleteContext] = []
        remove_clauses: list[Any] = []
        merge_clauses: list[CypherParser.OC_MergeContext] = []
        foreach_clauses: list[CypherParser.OC_ForeachContext] = []
        for uc in updating_clauses:
            if uc.oC_Create() is not None:
                create_clauses.append(uc.oC_Create())
            elif uc.oC_Set() is not None:
                set_clauses.append(uc.oC_Set())
            elif uc.oC_Delete() is not None:
                delete_clauses.append(uc.oC_Delete())
            elif uc.oC_Remove() is not None:
                remove_clauses.append(uc.oC_Remove())
            elif uc.oC_Merge() is not None:
                merge_clauses.append(uc.oC_Merge())
            elif uc.oC_Foreach() is not None:
                foreach_clauses.append(uc.oC_Foreach())
            else:
                raise CoreError("Unsupported updating clause", code="UNSUPPORTED")

        # A translator below compiles only its own kind of write: any other
        # write clause beside it would be dropped from the AQL. Refuse that.
        if foreach_clauses:
            refuse_dropped_writes(
                "FOREACH",
                CREATE=create_clauses,
                MERGE=merge_clauses,
                SET=set_clauses,
                DELETE=delete_clauses,
                REMOVE=remove_clauses,
            )
            return _translate_foreach_query(
                spq,
                foreach_clauses=foreach_clauses,
                resolver=resolver,
                bind_vars=bind_vars,
            )

        if merge_clauses:
            refuse_dropped_writes(
                "MERGE", CREATE=create_clauses, DELETE=delete_clauses, REMOVE=remove_clauses
            )
            return _translate_merge_query(
                spq,
                merge_clauses=merge_clauses,
                resolver=resolver,
                bind_vars=bind_vars,
                trailing_sets=set_clauses,
            )

        if create_clauses and delete_clauses:
            # DELETE of just-created rows is degenerate and rare; fail closed
            # rather than emit a silently-wrong query.
            raise CoreError(
                "CREATE combined with DELETE is not supported",
                code="NOT_IMPLEMENTED",
            )

        if (
            create_clauses
            and not set_clauses
            and not remove_clauses
            and single_part_create_needs_pipeline(spq, create_clauses, resolver=resolver, bind_vars=bind_vars)
        ):
            return translate_single_part_create(spq, resolver=resolver, bind_vars=bind_vars)

        if create_clauses:
            # Handles CREATE alone and CREATE followed by SET/REMOVE on the
            # created (or matched) variables.
            return _translate_create_query(
                spq,
                create_clauses=create_clauses,
                set_clauses=set_clauses,
                remove_clauses=remove_clauses,
                resolver=resolver,
                bind_vars=bind_vars,
            )

        if delete_clauses and not set_clauses and not remove_clauses:
            return translate_delete_query(
                spq,
                delete_clauses=delete_clauses,
                resolver=resolver,
                bind_vars=bind_vars,
            )

        if set_clauses or delete_clauses or remove_clauses:
            return _translate_mutating_query(
                spq,
                set_clauses=set_clauses,
                delete_clauses=delete_clauses,
                remove_clauses=remove_clauses,
                resolver=resolver,
                bind_vars=bind_vars,
            )

    # Gather reading clauses and return.
    reading_clauses = spq.oC_ReadingClause() or []
    if has_subquery_call(reading_clauses):
        return translate_single_part_with_calls(spq, resolver=resolver, bind_vars=bind_vars)
    if not reading_clauses:
        # A bare RETURN of constant/computed expressions (no MATCH/UNWIND/CALL)
        # maps directly to a top-level AQL ``RETURN <expr>``.
        from .returns import _translate_standalone_return

        return _translate_standalone_return(spq, bind_vars=bind_vars)

    mandatory_matches: list[CypherParser.OC_MatchContext] = []
    optional_matches: list[CypherParser.OC_MatchContext] = []
    pre_unwinds: list[CypherParser.OC_UnwindContext] = []
    post_unwinds: list[CypherParser.OC_UnwindContext] = []
    in_query_calls: list[CypherParser.OC_InQueryCallContext] = []
    seen_match = False
    for rc in reading_clauses:
        m = rc.oC_Match()
        if m is not None:
            seen_match = True
            if m.OPTIONAL() is not None:
                optional_matches.append(m)
            else:
                mandatory_matches.append(m)
            continue
        uw = rc.oC_Unwind()
        if uw is not None:
            if seen_match:
                post_unwinds.append(uw)
            else:
                pre_unwinds.append(uw)
            continue
        iqc = rc.oC_InQueryCall()
        if iqc is not None:
            in_query_calls.append(iqc)
    unwind_clauses = pre_unwinds + post_unwinds

    # Standalone in-query CALL (no MATCH, no UNWIND)
    if not mandatory_matches and not optional_matches and not unwind_clauses:
        if in_query_calls:
            return _translate_standalone_in_query_call(
                in_query_calls,
                spq=spq,
                bind_vars=bind_vars,
            )
        raise CoreError("MATCH is required in v0 subset", code="UNSUPPORTED")

    # Standalone UNWIND (no MATCH at all)
    if not mandatory_matches and not optional_matches:
        if not unwind_clauses:
            raise CoreError("MATCH is required in v0 subset", code="UNSUPPORTED")
        return _translate_standalone_unwind(
            unwind_clauses,
            spq=spq,
            bind_vars=bind_vars,
        )

    if not mandatory_matches and not pre_unwinds and not in_query_calls and spq.oC_Return() is not None:
        # Only OPTIONAL MATCHes: with no match there is one row whose
        # variables are null, and the RETURN projects *that* row
        # (``n.missing IS NULL`` is true, ``count(n)`` is 0) — not a row of
        # nulls substituted for the projection's result.
        opt_lines: list[str] = []
        opt_env = _compile_optional_matches(
            optional_matches,
            resolver=resolver,
            bind_vars=bind_vars,
            forbidden_vars=set(),
            bound_env={},
            lines=opt_lines,
        )
        _active_user_vars.set([v for v in opt_env if not v.startswith("_")])
        opt_forbidden = set(opt_env.values())
        for uw in post_unwinds:
            opt_env = _compile_pipeline_unwind(
                uw, bind_vars=bind_vars, forbidden_vars=opt_forbidden, var_env=opt_env, lines=opt_lines
            )
        _append_return(
            spq.oC_Return().oC_ProjectionBody(), lines=opt_lines, bind_vars=bind_vars, var_env=opt_env
        )
        return AqlQuery(text="\n".join(opt_lines), bind_vars=bind_vars)

    sole_optional = False
    if not mandatory_matches:
        mandatory_matches.append(optional_matches.pop(0))
        sole_optional = True

    unwinds_token = None
    if post_unwinds and not pre_unwinds:
        unwind_lines: list[str] = []
        unwind_vars: list[str] = []
        for uw in post_unwinds:
            expr = unwind_source(uw.oC_Expression(), _compile_expression(uw.oC_Expression(), bind_vars))
            var = uw.oC_Variable().getText().strip()
            unwind_lines.append(f"  FOR {var} IN {expr}")
            unwind_vars.append(var)
        unwinds_token = _pending_unwinds.set((unwind_lines, unwind_vars))
        post_unwinds = []
    try:
        result = _translate_match_body(
            mandatory_matches,
            spq=spq,
            optional_matches=optional_matches,
            resolver=resolver,
            bind_vars=bind_vars,
        )
    finally:
        if unwinds_token is not None:
            _pending_unwinds.reset(unwinds_token)

    if pre_unwinds or post_unwinds:
        result_lines = result.text.splitlines()
        final_lines: list[str] = []
        if pre_unwinds:
            for uw in pre_unwinds:
                expr = _compile_expression(uw.oC_Expression(), bind_vars)
                var = uw.oC_Variable().getText().strip()
                final_lines.append(f"FOR {var} IN {expr}")
            for ln in result_lines:
                final_lines.append(f"  {ln}")
        else:
            return_line = result_lines[-1] if result_lines else ""
            body_lines = result_lines[:-1]
            final_lines.extend(body_lines)
            for uw in post_unwinds:
                expr = _compile_expression(uw.oC_Expression(), bind_vars)
                var = uw.oC_Variable().getText().strip()
                final_lines.append(f"  FOR {var} IN {expr}")
            final_lines.append(return_line)
        result = AqlQuery(
            text="\n".join(final_lines),
            bind_vars=result.bind_vars,
        )

    if in_query_calls:
        result = _inject_in_query_calls(result, in_query_calls, bind_vars)

    if sole_optional:
        return _wrap_optional_match(result, spq, bind_vars)
    return result


def _wrap_optional_match(
    inner: AqlQuery,
    spq: CypherParser.OC_SinglePartQueryContext,
    bind_vars: dict[str, Any],
) -> AqlQuery:
    """Wrap an inner AQL query with OPTIONAL MATCH null-fallback semantics.

    AQL pattern::

        LET _rows = ( <inner AQL> )
        FOR _r IN (LENGTH(_rows) > 0 ? _rows : [<null-row>])
          RETURN _r
    """
    ret = spq.oC_Return()
    if ret is None:
        raise CoreError("RETURN is required in v0 subset", code="UNSUPPORTED")
    proj = ret.oC_ProjectionBody()
    items_ctx = proj.oC_ProjectionItems()
    items = items_ctx.oC_ProjectionItem() or []

    if len(items) == 1 and items[0].oC_Variable() is None and not _object_rows.get():
        null_row = "null"
    else:
        keys = []
        for it in items:
            alias = it.oC_Variable()
            written = written_column(it)
            if alias is not None:
                keys.append(alias.getText().strip())
            elif written is not None:
                keys.append(object_key(written))
            else:
                keys.append(_infer_key(_compile_expression(it.oC_Expression(), {})))
        null_row = "{" + ", ".join(f"{k}: null" for k in keys) + "}"

    indented = "\n".join(f"  {ln}" for ln in inner.text.splitlines())
    aql = f"LET _rows = (\n{indented}\n)\nFOR _r IN (LENGTH(_rows) > 0 ? _rows : [{null_row}])\n  RETURN _r"
    return AqlQuery(text=aql, bind_vars=bind_vars)


def _emit_single_hop(
    h: _HopMeta,
    *,
    current_var: str,
    lines: list[str],
    bind_vars: dict[str, Any],
    rel_type_exprs: dict[str, str],
    resolver: MappingResolver,
) -> None:
    """Emit AQL for a single relationship hop (the original per-chain logic)."""
    if h.r_style == "EMBEDDED":
        embedded_path = h.r_map.get("embeddedPath")
        if not embedded_path:
            raise CoreError(
                f"EMBEDDED relationship '{h.rel_type}' must declare 'embeddedPath'",
                code="INVALID_MAPPING",
            )
        is_array = h.r_map.get("embeddedArray", False)
        if is_array:
            lines.append(f"  FOR {h.v_trav} IN TO_ARRAY({current_var}.{embedded_path})")
        else:
            lines.append(f"  LET {h.v_trav} = {current_var}.{embedded_path}")
        # An untyped hop has no type to report; type(r) is null, not a crash.
        rel_type_exprs[h.rel_var] = (
            _aql_string_literal(h.rel_type)
            if h.rel_type is not None and MULTI_TYPE_SEPARATOR not in h.rel_type
            else "null"
        )
        for f in h.v_prop_filters:
            lines.append(f"    FILTER {f}")
        return

    v_filters: list[str] = []
    if h.v_map is None:
        if not h.v_bound:
            target_filter = _unlabeled_target_filter(h.v_trav, resolver, bind_vars, "vCollection")
            if target_filter is not None:
                v_filters.append(target_filter)
    else:
        skip_coll_filter = (
            h.v_primary is not None
            and h.rel_type is not None
            and resolver.edge_constrains_target(h.rel_type, h.v_primary, h.direction)
        )
        if not skip_coll_filter:
            v_filters.append(
                node_collection_filter(
                    h.v_trav, str(h.v_primary), h.v_map, resolver, bind_vars, key_base="vCollection"
                )
            )

    rmin, rmax = h.rel_range
    trav_vars = f"{h.v_trav}, {h.rel_var}" + (f", {h.hop_path}" if h.hop_path else "")
    trav_line = (
        f"  FOR {trav_vars} IN {rmin}..{rmax} {h.direction} {current_var} {_aql_collection_ref(h.edge_key)}"
    )
    vci_opts = _build_vci_options([h], resolver)
    if vci_opts:
        trav_line += f" {vci_opts}"
    lines.append(trav_line)

    if h.v_bound:
        lines.append(f"    FILTER {h.v_trav}._id == {h.v_var}._id")

    if h.v_map is not None and h.v_primary is not None:
        v_style = h.v_map.get("style")
        if v_style == "LABEL":
            vtf_key = _pick_bind_key("vTypeField", bind_vars)
            vtv_key = _pick_bind_key("vTypeValue", bind_vars)
            bind_vars[vtf_key] = h.v_map.get("typeField")
            bind_vars[vtv_key] = h.v_map.get("typeValue")
            v_filters.append(label_test(h.v_trav, vtf_key, vtv_key, h.v_map, bind_vars))
            v_filters.extend(_extra_label_filters(h.v_trav, h.v_labels, h.v_primary, bind_vars))
        elif v_style == "MULTI_LABEL":
            v_filters.append(_multi_label_filter(h.v_trav, h.v_map, bind_vars))
            v_filters.extend(_extra_label_filters(h.v_trav, h.v_labels, h.v_primary, bind_vars))
        elif v_style == "UNION":
            v_filters.extend(_extra_label_filters(h.v_trav, h.v_labels, h.v_primary, bind_vars))
        elif v_style != "COLLECTION":
            raise CoreError(f"Unsupported entity mapping style: {v_style}", code="INVALID_MAPPING")
        else:
            v_filters.extend(_collection_label_filters(h.v_trav, h.v_labels, h.v_primary, bind_vars))

    r_filters: list[str] = []
    if h.r_style == "GENERIC_WITH_TYPE":
        rtf_key = _pick_bind_key("relTypeField", bind_vars)
        rtv_key = _pick_bind_key("relTypeValue", bind_vars)
        bind_vars[rtf_key] = h.r_map.get("typeField")
        bind_vars[rtv_key] = h.r_map.get("typeValue")
        r_filters.append(f"{h.rel_var}[@{rtf_key}] == @{rtv_key}")
        rel_type_exprs[h.rel_var] = f"{h.rel_var}[@{rtf_key}]"
        if h.rel_type is not None:
            _warn_missing_vci(resolver, h.rel_type, h.r_map)
    elif h.r_style == "DEDICATED_COLLECTION":
        rel_type_exprs[h.rel_var] = _dedicated_rel_type_expr(h.rel_type, h.rel_var, resolver)
    else:
        raise CoreError(f"Unsupported relationship mapping style: {h.r_style}", code="INVALID_MAPPING")

    for f in v_filters + r_filters:
        lines.append(f"    FILTER {f}")
    for f in h.r_prop_filters:
        lines.append(f"    FILTER {f}")
    for f in h.v_prop_filters:
        lines.append(f"    FILTER {f}")
    if h.rel_list is not None and h.hop_path is not None:
        lines.append(f"    LET {h.rel_list} = {h.hop_path}.edges")


def _path_array(pieces: list[str]) -> str:
    """An AQL array of path elements; a ``...expr`` piece is spliced in (an array)."""
    if not any(p.startswith("...") for p in pieces):
        return "[" + ", ".join(pieces) + "]"
    arrays: list[str] = []
    singles: list[str] = []
    for p in pieces:
        if p.startswith("..."):
            if singles:
                arrays.append("[" + ", ".join(singles) + "]")
                singles = []
            arrays.append(p[3:])
        else:
            singles.append(p)
    if singles:
        arrays.append("[" + ", ".join(singles) + "]")
    return arrays[0] if len(arrays) == 1 else f"UNION({', '.join(arrays)})"


def _self_loop_direction(direction: str, *, back_to_start: bool, rel_range: tuple[int, int]) -> str:
    """OUTBOUND for an undirected single hop back to the node it starts from.

    Such a hop (``(n)-[r]-(n)``) matches only self-loops, and an ANY
    traversal yields each loop twice (once per direction) where Cypher
    matches it once (Match3 [12]).
    """
    if direction == "ANY" and back_to_start and rel_range == (1, 1):
        return "OUTBOUND"
    return direction


def _emit_merged_hops(
    hops: list[_HopMeta],
    *,
    current_var: str,
    lines: list[str],
    bind_vars: dict[str, Any],
    rel_type_exprs: dict[str, str],
    resolver: MappingResolver,
    forbidden_vars: set[str],
    path_node_vars: list[str],
    path_edge_vars: list[str],
) -> None:
    """Emit a single multi-hop AQL traversal for N consecutive mergeable hops.

    Instead of N nested ``FOR ... IN 1..1`` traversals, emits one
    ``FOR v, e, p IN N..N`` and extracts intermediate vertices/edges
    from the path variable.

    LET bindings for intermediate vertices/edges are only emitted when
    they are needed for filters (labels, properties).  Edge type
    discriminator filters reference the path directly to avoid
    unnecessary bindings.  Any remaining unused LETs are cleaned up by
    ``_eliminate_dead_lets`` at the end of query assembly.
    """
    n = len(hops)
    last = hops[-1]
    path_var = _pick_fresh_var("_path", forbidden_vars=forbidden_vars)

    edge_key = hops[0].edge_key
    for h in hops[1:]:
        if h.edge_key != edge_key and h.edge_key in bind_vars:
            del bind_vars[h.edge_key]

    trav_line = (
        f"  FOR {last.v_trav}, {last.rel_var}, {path_var}"
        f" IN {n}..{n} {last.direction} {current_var}"
        f" {_aql_collection_ref(edge_key)}"
    )
    vci_opts = _build_vci_options(hops, resolver)
    if vci_opts:
        trav_line += f" {vci_opts}"
    lines.append(trav_line)

    for idx, h in enumerate(hops[:-1]):
        vertex_idx = idx + 1
        edge_ref = f"{path_var}.edges[{idx}]"
        vertex_ref = f"{path_var}.vertices[{vertex_idx}]"

        # Only emit LET for the vertex if it has filters that need it,
        # or unconditionally so downstream WHERE/RETURN can reference it.
        # _eliminate_dead_lets will clean up if truly unused.
        lines.append(f"    LET {h.v_trav} = {vertex_ref}")
        path_node_vars.append(h.v_trav)

        # Use path expression directly for edge type filters;
        # only create a LET if the edge has property filters or was
        # explicitly named by the user.
        needs_edge_let = h.r_prop_filters or h.rel_named
        if needs_edge_let:
            lines.append(f"    LET {h.rel_var} = {edge_ref}")
            edge_filter_ref = h.rel_var
        else:
            edge_filter_ref = edge_ref
        # A named path reads an unnamed edge from the traversal path: no
        # variable holds it (``MATCH p = ()-->()-->()``, Delete3 [1]).
        path_edge_vars.append(h.rel_var if needs_edge_let else edge_ref)

        _emit_vertex_filters_for_hop(h, lines=lines, bind_vars=bind_vars, resolver=resolver)
        _emit_edge_type_filters_for_hop(
            h,
            lines=lines,
            bind_vars=bind_vars,
            rel_type_exprs=rel_type_exprs,
            resolver=resolver,
            edge_ref_override=edge_filter_ref,
        )
        for f in h.v_prop_filters:
            lines.append(f"    FILTER {f}")
        for f in h.r_prop_filters:
            lines.append(f"    FILTER {f}")

    _emit_vertex_filters_for_hop(last, lines=lines, bind_vars=bind_vars, resolver=resolver)
    _emit_edge_type_filters_for_hop(
        last, lines=lines, bind_vars=bind_vars, rel_type_exprs=rel_type_exprs, resolver=resolver
    )
    for f in last.v_prop_filters:
        lines.append(f"    FILTER {f}")
    for f in last.r_prop_filters:
        lines.append(f"    FILTER {f}")

    path_node_vars.append(last.v_trav)
    path_edge_vars.append(last.rel_var)


def _emit_vertex_filters_for_hop(
    h: _HopMeta,
    *,
    lines: list[str],
    bind_vars: dict[str, Any],
    resolver: MappingResolver,
) -> None:
    """Emit FILTER lines for a hop's target vertex (label/collection checks)."""
    if h.v_map is None:
        if not h.v_bound:
            target_filter = _unlabeled_target_filter(h.v_trav, resolver, bind_vars, "vCollection")
            if target_filter is not None:
                lines.append(f"    FILTER {target_filter}")
        return

    skip_coll_filter = (
        h.v_primary is not None
        and h.rel_type is not None
        and resolver.edge_constrains_target(h.rel_type, h.v_primary, h.direction)
    )
    if not skip_coll_filter:
        coll_filter = node_collection_filter(
            h.v_trav, str(h.v_primary), h.v_map, resolver, bind_vars, key_base="vCollection"
        )
        lines.append(f"    FILTER {coll_filter}")

    if h.v_primary is not None:
        v_style = h.v_map.get("style")
        if v_style == "LABEL":
            vtf_key = _pick_bind_key("vTypeField", bind_vars)
            vtv_key = _pick_bind_key("vTypeValue", bind_vars)
            bind_vars[vtf_key] = h.v_map.get("typeField")
            bind_vars[vtv_key] = h.v_map.get("typeValue")
            lines.append("    FILTER " + label_test(h.v_trav, vtf_key, vtv_key, h.v_map, bind_vars))
            for f in _extra_label_filters(h.v_trav, h.v_labels, h.v_primary, bind_vars):
                lines.append(f"    FILTER {f}")
        elif v_style == "MULTI_LABEL":
            lines.append(f"    FILTER {_multi_label_filter(h.v_trav, h.v_map, bind_vars)}")
            for f in _extra_label_filters(h.v_trav, h.v_labels, h.v_primary, bind_vars):
                lines.append(f"    FILTER {f}")
        elif v_style == "UNION":
            for f in _extra_label_filters(h.v_trav, h.v_labels, h.v_primary, bind_vars):
                lines.append(f"    FILTER {f}")
        elif v_style != "COLLECTION":
            raise CoreError(f"Unsupported entity mapping style: {v_style}", code="INVALID_MAPPING")
        else:
            for f in _collection_label_filters(h.v_trav, h.v_labels, h.v_primary, bind_vars):
                lines.append(f"    FILTER {f}")


def _emit_edge_type_filters_for_hop(
    h: _HopMeta,
    *,
    lines: list[str],
    bind_vars: dict[str, Any],
    rel_type_exprs: dict[str, str],
    resolver: MappingResolver,
    edge_ref_override: str | None = None,
) -> None:
    """Emit FILTER lines for a hop's edge type discriminator.

    *edge_ref_override*, when provided, is used in the FILTER expression
    instead of the hop's ``rel_var``.  This allows merged traversals to
    reference path edges directly (e.g. ``_path.edges[0]``) without
    creating a LET binding.
    """
    ref = edge_ref_override or h.rel_var
    if h.r_style == "GENERIC_WITH_TYPE":
        rtf_key = _pick_bind_key("relTypeField", bind_vars)
        rtv_key = _pick_bind_key("relTypeValue", bind_vars)
        bind_vars[rtf_key] = h.r_map.get("typeField")
        bind_vars[rtv_key] = h.r_map.get("typeValue")
        lines.append(f"    FILTER {ref}[@{rtf_key}] == @{rtv_key}")
        rel_type_exprs[h.rel_var] = f"{ref}[@{rtf_key}]"
        if h.rel_type is not None:
            _warn_missing_vci(resolver, h.rel_type, h.r_map)
    elif h.r_style == "DEDICATED_COLLECTION":
        rel_type_exprs[h.rel_var] = _dedicated_rel_type_expr(h.rel_type, ref, resolver)
    else:
        raise CoreError(f"Unsupported relationship mapping style: {h.r_style}", code="INVALID_MAPPING")


_LET_PATTERN = re.compile(r"^\s*LET\s+(\w+)\s*=")


def _eliminate_dead_lets(lines: list[str]) -> list[str]:
    """Remove LET bindings whose variable is not referenced elsewhere in the query."""
    result: list[str] = []
    for i, line in enumerate(lines):
        m = _LET_PATTERN.match(line)
        if m:
            var = m.group(1)
            var_re = re.compile(r"\b" + re.escape(var) + r"\b")
            used = any(var_re.search(other) for j, other in enumerate(lines) if j != i)
            if not used:
                continue
        result.append(line)
    return result


_SIMPLE_CMP_RE = re.compile(r"^\(?\s*(\w+(?:\.\w+(?:\[.*?\])?)*)\s*(==|!=|<|>|<=|>=)\s*(.+?)\s*\)?$")


def _is_prunable_condition(
    filter_expr: str,
    trav_var: str,
    all_vars: set[str],
) -> bool:
    """Return True if *filter_expr* is a simple comparison referencing only *trav_var*.

    We are conservative: only ``==``, ``!=``, ``<``, ``>``, ``<=``, ``>=``
    on a single property access of the traversal target variable qualify.
    """
    stripped = filter_expr.strip()
    if stripped.startswith("(") and stripped.endswith(")"):
        stripped = stripped[1:-1].strip()
    upper = stripped.upper()
    if " AND " in upper or " OR " in upper or upper.startswith("NOT "):
        return False

    m = _SIMPLE_CMP_RE.match(stripped)
    if not m:
        return False
    lhs = m.group(1)
    if not lhs.startswith(f"{trav_var}."):
        return False
    for v in all_vars:
        if v == trav_var:
            continue
        if re.search(r"\b" + re.escape(v) + r"\b", stripped):
            return False
    return True


def edge_uniqueness_across(groups: list[list[str]], *, singles: list[str]) -> list[str]:
    """Filters keeping each edge of a group (an edge, or ``...list`` entry) out
    of every other group and out of the *singles* (single-edge groups)."""

    def ids(entry: str) -> tuple[str, bool]:
        return (f"{entry[3:]}[*]._id", True) if entry.startswith("...") else (f"{entry}._id", False)

    out: list[str] = []
    others: list[list[str]] = [*groups, *([s] for s in singles)]
    for gi, group in enumerate(groups):
        for other in others[gi + 1 :]:
            for a in group:
                for b in other:
                    (ea, la), (eb, lb) = ids(a), ids(b)
                    if la and lb:
                        out.append(f"LENGTH(INTERSECTION({ea}, {eb})) == 0")
                    elif la or lb:
                        single, many = (eb, ea) if la else (ea, eb)
                        out.append(f"{single} NOT IN {many}")
                    else:
                        out.append(f"{ea} != {eb}")
    return out


def _emit_relationship_uniqueness(
    named_rel_vars: list[str],
    lines: list[str],
    indent: str = "    ",
) -> None:
    """Emit FILTER to enforce Cypher's relationship uniqueness guarantee.

    When a pattern contains 2+ explicitly named relationship variables,
    Cypher guarantees no edge appears in more than one binding.  ArangoDB
    handles this within a single traversal but NOT across multiple
    relationship variables in the same pattern.
    """
    if len(named_rel_vars) < 2:
        return
    for i in range(len(named_rel_vars)):
        for j in range(i + 1, len(named_rel_vars)):
            lines.append(f"{indent}FILTER {named_rel_vars[i]}._id != {named_rel_vars[j]}._id")


def _emit_prune_and_filter(
    filter_expr: str,
    trav_var: str,
    all_vars: set[str],
    is_varlen: bool,
    lines: list[str],
    indent: str = "    ",
) -> None:
    """Emit PRUNE (for variable-length traversals) and FILTER lines.

    For variable-length traversals with simple, single-variable conditions,
    a ``PRUNE`` is emitted before the ``FILTER`` to let ArangoDB terminate
    branches early.
    """
    if is_varlen and _is_prunable_condition(filter_expr, trav_var, all_vars):
        lines.append(f"{indent}PRUNE NOT ({filter_expr})")
    lines.append(f"{indent}FILTER {filter_expr}")


def _try_compile_edge_only_scan(
    *,
    spq: CypherParser.OC_SinglePartQueryContext,
    match_ctx: CypherParser.OC_MatchContext,
    node: CypherParser.OC_NodePatternContext,
    u_labels: list[str],
    u_prop_filters: list[str],
    chains: list,
    path_var_name: str | None,
    optional_matches: list[CypherParser.OC_MatchContext],
    resolver: MappingResolver,
    bind_vars: dict[str, Any],
) -> AqlQuery | None:
    """Compile a fully-anonymous single hop as a direct edge-collection scan.

    For patterns where *both* endpoints are anonymous and unconstrained — e.g.
    ``MATCH ()-[r]->()`` or ``MATCH ()-[r:TYPE]->()`` — the relationship is the
    only thing the query references. Anchoring such a pattern on a vertex
    (``FOR u IN @@uCollection FOR v, r IN 1..1 OUTBOUND u @@edge``) forces a
    full traversal over every start node, which is catastrophically slow on
    large graphs (e.g. meta-queries like ``RETURN type(r), count(r)`` over
    millions of edges). Scanning the edge collection directly is equivalent
    here because neither endpoint is filtered, and it is dramatically faster.

    Returns the compiled :class:`AqlQuery` when the pattern qualifies, or
    ``None`` to fall back to the normal vertex-anchored compilation. All
    validation happens before ``bind_vars`` is mutated so a fall-through never
    leaves stray (unused) bind parameters behind.
    """
    if path_var_name is not None:
        return None
    if optional_matches:
        return None
    if len(chains) != 1:
        return None
    # Start node must be anonymous and unconstrained.
    if node.oC_Variable() is not None or u_labels or u_prop_filters:
        return None

    chain = chains[0]
    rel_pat = chain.oC_RelationshipPattern()
    v_node = chain.oC_NodePattern()
    if rel_pat is None or v_node is None:
        return None
    # End node must also be anonymous and unconstrained.
    if v_node.oC_Variable() is not None:
        return None
    _, v_labels = _extract_node_var_and_labels(v_node, default_var="v")
    if v_labels:
        return None
    # Probe end-node inline properties without mutating the real bind_vars.
    if _compile_node_pattern_properties(v_node, var="v", bind_vars={}):
        return None

    rel_type, rel_var, rel_range = _extract_relationship_type_and_var(rel_pat, default_var="r")
    if rel_range != (1, 1):
        return None

    r_map = _resolve_relationship_for_pattern(resolver, rel_type)
    r_style = r_map.get("style")
    if r_style not in ("GENERIC_WITH_TYPE", "DEDICATED_COLLECTION"):
        return None
    edge_collection = r_map.get("edgeCollectionName") or r_map.get("collectionName") or ""
    if not isinstance(edge_collection, str) or not edge_collection:
        return None

    # --- Committed: from here on we may mutate bind_vars. ---
    edge_key = _pick_bind_key("@edgeCollection", bind_vars)
    bind_vars[edge_key] = edge_collection

    lines: list[str] = [f"FOR {rel_var} IN {_aql_collection_ref(edge_key)}"]
    rel_type_exprs: dict[str, str] = {}

    if r_style == "GENERIC_WITH_TYPE":
        rtf_key = _pick_bind_key("relTypeField", bind_vars)
        rtv_key = _pick_bind_key("relTypeValue", bind_vars)
        bind_vars[rtf_key] = r_map.get("typeField")
        bind_vars[rtv_key] = r_map.get("typeValue")
        lines.append(f"  FILTER {rel_var}[@{rtf_key}] == @{rtv_key}")
        rel_type_exprs[rel_var] = f"{rel_var}[@{rtf_key}]"
    else:  # DEDICATED_COLLECTION
        rel_type_exprs[rel_var] = _dedicated_rel_type_expr(rel_type, rel_var, resolver)

    if _relationship_direction(rel_pat) == "ANY":
        # An undirected pattern matches an edge once per orientation, a
        # self-loop once (CountingSubgraphMatches1 [3]): one row per side.
        side = _pick_fresh_var("_side", forbidden_vars={rel_var})
        lines.append(f"  FOR {side} IN ({rel_var}._from == {rel_var}._to ? [0] : [0, 1])")

    for f in _compile_relationship_pattern_properties(rel_pat, var=rel_var, bind_vars=bind_vars):
        lines.append(f"  FILTER {f}")

    where_ctx = match_ctx.oC_Where()
    user_filter = _compile_where(where_ctx.oC_Expression(), bind_vars) if where_ctx is not None else None
    if user_filter:
        lines.append(f"  FILTER {user_filter}")

    ret = spq.oC_Return()
    if ret is None:
        raise CoreError("RETURN is required in v0 subset", code="UNSUPPORTED")
    _append_return(
        ret.oC_ProjectionBody(),
        lines=lines,
        bind_vars=bind_vars,
        var_env={},
        rel_type_exprs=rel_type_exprs,
    )
    lines = _eliminate_dead_lets(lines)
    return AqlQuery(text="\n".join(lines), bind_vars=bind_vars)


def _translate_match_body(
    match_ctxs: list[CypherParser.OC_MatchContext],
    *,
    spq: CypherParser.OC_SinglePartQueryContext,
    optional_matches: list[CypherParser.OC_MatchContext],
    resolver: MappingResolver,
    bind_vars: dict[str, Any],
) -> AqlQuery:
    """Core MATCH translation logic shared by regular and optional-match paths."""
    all_parts: list = []
    extra_wheres: list[CypherParser.OC_WhereContext] = []
    for mc in match_ctxs:
        pattern = mc.oC_Pattern()
        pp = pattern.oC_PatternPart()
        if not pp:
            raise CoreError("MATCH pattern is required", code="UNSUPPORTED")
        all_parts.extend(pp)
        wc = mc.oC_Where()
        if wc is not None:
            extra_wheres.append(wc)

    # Multi-pattern-part or multi-MATCH: compile all pattern parts together
    if len(all_parts) > 1 or len(match_ctxs) > 1:
        lines, forbidden = _compile_match_multi_parts_from_parts(
            all_parts,
            extra_wheres=extra_wheres,
            resolver=resolver,
            bind_vars=bind_vars,
        )
        var_env = {v: v for v in forbidden}

        multi_rel_type_exprs: dict[str, str] = {}
        if optional_matches:
            opt_env = _compile_optional_matches(
                optional_matches,
                resolver=resolver,
                bind_vars=bind_vars,
                forbidden_vars=forbidden,
                bound_env={v: v for v in _pattern_parts_user_vars(all_parts)},
                lines=lines,
                rel_type_exprs=multi_rel_type_exprs,
            )
            var_env.update(opt_env)

        ret = spq.oC_Return()
        if ret is None:
            raise CoreError("RETURN is required in v0 subset", code="UNSUPPORTED")
        _set_user_vars_from_parts(all_parts)
        _append_return(
            ret.oC_ProjectionBody(),
            lines=lines,
            bind_vars=bind_vars,
            var_env=var_env,
            rel_type_exprs=multi_rel_type_exprs or None,
        )
        return AqlQuery(text="\n".join(lines), bind_vars=bind_vars)

    match_ctx = match_ctxs[0]

    # Extract single pattern element (existing v0 behavior)
    anon = all_parts[0].oC_AnonymousPatternPart()
    refuse_unbound_shortest_path(anon)
    elem = anon.oC_PatternElement()
    node = elem.oC_NodePattern()
    if node is None:
        raise CoreError("Only node patterns are supported in v0", code="UNSUPPORTED")
    chains = elem.oC_PatternElementChain() or []

    # Case A: single node pattern (existing v0 behavior)
    if not chains:
        var, labels = _extract_node_var_and_labels(node, default_var="n")
        prop_filters = _compile_node_pattern_properties(node, var=var, bind_vars=bind_vars)

        base_filters: list[str] = []
        if not labels:
            # Unlabeled node match: only supported when we can infer a single underlying collection.
            bind_vars["@collection"] = _infer_unlabeled_collection(resolver)
            for_line = f"FOR {var} IN @@collection"
        else:
            primary = _pick_primary_entity_label(labels, resolver)
            entity_mapping = resolver.resolve_entity(_strip_label_backticks(primary))
            entity_style = entity_mapping.get("style")
            if entity_style == "COLLECTION":
                base_filters.extend(_collection_label_filters(var, labels, primary, bind_vars))
                bind_vars["@collection"] = entity_mapping["collectionName"]
                for_line = f"FOR {var} IN @@collection"
            elif entity_style == "LABEL":
                bind_vars["@collection"] = entity_mapping["collectionName"]
                bind_vars["typeField"] = entity_mapping["typeField"]
                bind_vars["typeValue"] = entity_mapping["typeValue"]
                for_line = f"FOR {var} IN @@collection"
                base_filters.append(label_test(var, "typeField", "typeValue", entity_mapping, bind_vars))
                base_filters.extend(_extra_label_filters(var, labels, primary, bind_vars))
            elif entity_style == "MULTI_LABEL":
                bind_vars["@collection"] = entity_mapping["collectionName"]
                for_line = f"FOR {var} IN @@collection"
                base_filters.append(_multi_label_filter(var, entity_mapping, bind_vars))
                base_filters.extend(_extra_label_filters(var, labels, primary, bind_vars))
            elif entity_style == "UNION":
                source, _ = node_scan_source(
                    var,
                    primary,
                    entity_mapping,
                    resolver,
                    bind_vars,
                    coll_key="@collection",
                    filters=prop_filters,
                )
                for_line = f"FOR {var} IN {source}"
                prop_filters = []
                base_filters.extend(_extra_label_filters(var, labels, primary, bind_vars))
            else:
                raise CoreError(f"Unsupported entity mapping style: {entity_style}", code="INVALID_MAPPING")

        where_ctx = match_ctx.oC_Where()
        user_filter = _compile_where(where_ctx.oC_Expression(), bind_vars) if where_ctx is not None else None

        filters = list(base_filters) + prop_filters
        if user_filter:
            filters.append(user_filter)

        # Build AQL
        lines = [for_line]
        if labels and entity_style != "UNION":
            idx_hint = _build_collection_index_hint(primary, prop_filters, resolver)
            if idx_hint:
                lines.append(f"  {idx_hint}")
        for f in filters:
            lines.append(f"  FILTER {f}")
        # ``MATCH p = (n)``: a zero-length path of the one node.
        single_path_ctx = all_parts[0].oC_Variable()
        if single_path_ctx is not None:
            single_path = single_path_ctx.getText().strip()
            lines.append(f"  LET {single_path} = {{nodes: [{var}], edges: []}}")
            _active_path_vars.get()[single_path] = ([var], [])

        opt_var_env: dict[str, str] = {}
        node_rel_type_exprs: dict[str, str] = {}
        if optional_matches:
            fv: set[str] = {var}
            opt_var_env = _compile_optional_matches(
                optional_matches,
                resolver=resolver,
                bind_vars=bind_vars,
                forbidden_vars=fv,
                bound_env={var: var},
                lines=lines,
                rel_type_exprs=node_rel_type_exprs,
            )

        ret = spq.oC_Return()
        if ret is None:
            raise CoreError("RETURN is required in v0 subset", code="UNSUPPORTED")
        _set_user_vars_from_parts(all_parts)
        _append_return(
            ret.oC_ProjectionBody(),
            lines=lines,
            bind_vars=bind_vars,
            var_env=opt_var_env,
            rel_type_exprs=node_rel_type_exprs or None,
        )
        return AqlQuery(text="\n".join(lines), bind_vars=bind_vars)

    # Check for named path: p = (a)-[:REL]->(b)
    path_var_name: str | None = None
    pp_var_ctx = all_parts[0].oC_Variable()
    if pp_var_ctx is not None:
        path_var_name = pp_var_ctx.getText().strip()

    # Case B: relationship pattern (1+ hops)
    u_var, u_labels = _extract_node_var_and_labels(node, default_var="u")
    if node.oC_Variable() is None:
        u_var = _anonymous_var(u_var, node, set())
    u_prop_filters = _compile_node_pattern_properties(node, var=u_var, bind_vars=bind_vars)

    # Fast path: a fully-anonymous single hop (e.g. ``MATCH ()-[r]->()``) is an
    # edge-only query. Scan the edge collection directly instead of traversing
    # from every start node — orders of magnitude faster on large graphs.
    edge_only = _try_compile_edge_only_scan(
        spq=spq,
        match_ctx=match_ctx,
        node=node,
        u_labels=u_labels,
        u_prop_filters=u_prop_filters,
        chains=chains,
        path_var_name=path_var_name,
        optional_matches=optional_matches,
        resolver=resolver,
        bind_vars=bind_vars,
    )
    if edge_only is not None:
        return edge_only

    # A leading unlabeled anchor with a typed first hop can be resolved from the
    # relationship's domain/range (e.g. ()-[:REVIEWED]->(m:Movie) → the anchor is
    # a Person). The anchor is the *opposite* endpoint of the first hop's target,
    # so the traversal direction is reversed.
    if not u_labels and chains:
        _rp0 = chains[0].oC_RelationshipPattern()
        if _rp0 is not None:
            _rt0, _, _ = _extract_relationship_type_and_var(_rp0, default_var="r")
            _rev0 = {"OUTBOUND": "INBOUND", "INBOUND": "OUTBOUND"}.get(_relationship_direction(_rp0), "ANY")
            if _rt0:
                _inferred_u = resolver.infer_endpoint_label(_rt0, _rev0)
                if _inferred_u:
                    u_labels = [_inferred_u]

    u_filters: list[str] = []
    u_source = "@@uCollection"
    if not u_labels:
        bind_vars["@uCollection"] = _infer_unlabeled_collection(resolver)
    else:
        u_primary = _pick_primary_entity_label(u_labels, resolver)
        u_map = resolver.resolve_entity(_strip_label_backticks(u_primary))
        u_source, pushed = node_scan_source(
            u_var, u_primary, u_map, resolver, bind_vars, coll_key="@uCollection", filters=u_prop_filters
        )
        if pushed:
            u_prop_filters = []

        u_style = u_map.get("style")
        if u_style == "LABEL":
            bind_vars["uTypeField"] = u_map.get("typeField")
            bind_vars["uTypeValue"] = u_map.get("typeValue")
            u_filters.append(label_test(u_var, "uTypeField", "uTypeValue", u_map, bind_vars))
            u_filters.extend(_extra_label_filters(u_var, u_labels, u_primary, bind_vars))
        elif u_style == "MULTI_LABEL":
            u_filters.append(_multi_label_filter(u_var, u_map, bind_vars))
            u_filters.extend(_extra_label_filters(u_var, u_labels, u_primary, bind_vars))
        elif u_style == "UNION":
            u_filters.extend(_extra_label_filters(u_var, u_labels, u_primary, bind_vars))
        elif u_style != "COLLECTION":
            raise CoreError(f"Unsupported entity mapping style: {u_style}", code="INVALID_MAPPING")
        else:
            u_filters.extend(_collection_label_filters(u_var, u_labels, u_primary, bind_vars))

    lines = [f"FOR {u_var} IN {u_source}"]
    for f in u_filters:
        lines.append(f"  FILTER {f}")
    for f in u_prop_filters:
        lines.append(f"  FILTER {f}")

    forbidden_vars: set[str] = {u_var}
    rel_type_exprs: dict[str, str] = {}
    current_var = u_var
    path_node_vars: list[str] = [u_var]
    path_edge_vars: list[str] = []

    # --- Phase 1: pre-process chains into _HopMeta list ---
    hops: list[_HopMeta] = []
    hop_start = u_var  # the Cypher node each hop leaves from
    for chain in chains:
        rel_pat = chain.oC_RelationshipPattern()
        v_node = chain.oC_NodePattern()
        if rel_pat is None or v_node is None:
            raise CoreError("Invalid relationship pattern", code="UNSUPPORTED")

        v_var, v_labels = _extract_node_var_and_labels(v_node, default_var="v")
        if v_node.oC_Variable() is None:
            v_var = _anonymous_var(v_var, v_node, forbidden_vars)
        v_bound = v_node.oC_Variable() is not None and v_var in forbidden_vars
        v_trav = (
            v_var
            if not v_bound
            else _pick_fresh_var(_affix_name(v_var, suffix="_m"), forbidden_vars=forbidden_vars)
        )
        v_prop_filters = _compile_node_pattern_properties(v_node, var=v_trav, bind_vars=bind_vars)

        rel_type, rel_var, rel_range = _extract_relationship_type_and_var(rel_pat, default_var="r")
        detail = rel_pat.oC_RelationshipDetail()
        rel_named = detail is not None and detail.oC_Variable() is not None
        if not rel_named:
            rel_var = _anonymous_var(rel_var, rel_pat, forbidden_vars)
        elif rel_var in forbidden_vars:
            raise CoreError("Relationship variable must not shadow node variables", code="UNSUPPORTED")
        forbidden_vars.add(rel_var)

        r_prop_filters = _compile_relationship_pattern_properties(rel_pat, var=rel_var, bind_vars=bind_vars)
        hop_path: str | None = None
        rel_list: str | None = None
        is_varlen = detail is not None and detail.oC_RangeLiteral() is not None
        rel_used = rel_named and is_referenced(rel_var, detail.oC_Variable())
        if is_varlen and (rel_used or path_var_name is not None or r_prop_filters or len(chains) > 1):
            # A variable-length relationship is the list of its edges: the
            # traversal's path supplies them, every one must match the
            # pattern's properties, and ``r`` names the list.
            hop_path = _pick_fresh_var(f"_vp_{rel_var}", forbidden_vars=forbidden_vars)
            if rel_used:
                rel_list = rel_var
                rel_var = _pick_fresh_var(_affix_name(rel_var, suffix="_last"), forbidden_vars=forbidden_vars)
            if r_prop_filters:
                each = _compile_relationship_pattern_properties(rel_pat, var="CURRENT", bind_vars=bind_vars)
                r_prop_filters = [f"LENGTH({hop_path}.edges[* FILTER NOT ({c})]) == 0" for c in each]
        direction = _self_loop_direction(
            _relationship_direction(rel_pat),
            back_to_start=v_bound and v_var == hop_start,
            rel_range=rel_range,
        )

        if direction == "ANY" and rel_type:
            stats_dir = resolver.preferred_traversal_direction(rel_type)
            if stats_dir:
                direction = stats_dir

        # An unlabeled endpoint of a typed relationship can be resolved from the
        # relationship's domain/range (e.g. (m:Movie)<-[:REVIEWED]-() → Person),
        # so it is treated as if it carried that label. Use the *pattern*
        # direction (not the stats-adjusted one) so an undirected -[:REL]- stays
        # ambiguous and falls back to single-collection inference.
        if not v_labels and not v_bound and rel_type:
            _inferred_v = resolver.infer_endpoint_label(rel_type, _relationship_direction(rel_pat))
            if _inferred_v:
                v_labels = [_inferred_v]

        v_primary: str | None = None
        v_map: dict[str, Any] | None = None
        if v_labels:
            v_primary = _pick_primary_entity_label(v_labels, resolver)
            v_map = resolver.resolve_entity(_strip_label_backticks(v_primary))
        r_map = _resolve_relationship_for_pattern(resolver, rel_type)
        r_style = r_map.get("style")

        edge_collection = ""
        edge_key = ""
        r_type_field: str | None = None
        r_type_value: str | None = None

        if r_style != "EMBEDDED":
            edge_key = _pick_bind_key("@edgeCollection", bind_vars)
            edge_collection = r_map.get("edgeCollectionName") or r_map.get("collectionName") or ""
            bind_vars[edge_key] = edge_collection
            if not _is_edge_collection_value(bind_vars[edge_key]):
                raise CoreError(
                    f"Invalid relationship mapping collection for: {rel_type}", code="INVALID_MAPPING"
                )
            r_type_field = r_map.get("typeField")
            r_type_value = r_map.get("typeValue")

        hops.append(
            _HopMeta(
                v_var=v_var,
                v_trav=v_trav,
                v_labels=v_labels,
                v_primary=v_primary,
                v_map=v_map,
                v_bound=v_bound,
                v_prop_filters=v_prop_filters,
                rel_type=rel_type,
                rel_var=rel_var,
                rel_range=rel_range,
                rel_named=rel_named,
                r_prop_filters=r_prop_filters,
                direction=direction,
                r_map=r_map,
                r_style=r_style,
                edge_collection=edge_collection,
                edge_key=edge_key,
                r_type_field=r_type_field,
                r_type_value=r_type_value,
                hop_path=hop_path,
                rel_list=rel_list,
            )
        )

        if not v_bound:
            forbidden_vars.add(v_var)
        hop_start = v_var

    # --- Phase 2: group consecutive mergeable hops ---
    groups: list[list[int]] = []
    for i, h in enumerate(hops):
        if not groups:
            groups.append([i])
            continue
        prev = hops[groups[-1][-1]]
        mergeable = (
            h.r_style != "EMBEDDED"
            and prev.r_style != "EMBEDDED"
            and h.edge_collection == prev.edge_collection
            and h.direction == prev.direction
            and h.rel_range == (1, 1)
            and prev.rel_range == (1, 1)
            and h.hop_path is None
            and prev.hop_path is None
            and not h.v_bound
            and not prev.v_bound
            and h.r_type_field == prev.r_type_field
            and h.r_type_value == prev.r_type_value
            and not prev.r_prop_filters
        )
        if mergeable:
            groups[-1].append(i)
        else:
            groups.append([i])

    # --- Phase 3: emit AQL for each group ---
    # Each group's edges as path entries (an edge, or ``...list``), for the
    # cross-group uniqueness filters below.
    group_edges: list[list[str]] = []
    for group in groups:
        edges_before = len(path_edge_vars)
        if len(group) == 1:
            _emit_single_hop(
                hops[group[0]],
                current_var=current_var,
                lines=lines,
                bind_vars=bind_vars,
                rel_type_exprs=rel_type_exprs,
                resolver=resolver,
            )
            h = hops[group[0]]
            current_var = h.v_trav
            if h.hop_path is not None:
                # Every vertex after the start, and every edge, of the hop.
                path_node_vars.append(f"...SLICE({h.hop_path}.vertices, 1)")
                path_edge_vars.append(f"...{h.hop_path}.edges")
            else:
                path_node_vars.append(h.v_trav)
                path_edge_vars.append(h.rel_var)
        else:
            _emit_merged_hops(
                [hops[i] for i in group],
                current_var=current_var,
                lines=lines,
                bind_vars=bind_vars,
                rel_type_exprs=rel_type_exprs,
                resolver=resolver,
                forbidden_vars=forbidden_vars,
                path_node_vars=path_node_vars,
                path_edge_vars=path_edge_vars,
            )
            last = hops[group[-1]]
            current_var = last.v_trav
        group_edges.append(path_edge_vars[edges_before:])

    # Cypher's relationship-uniqueness rule states that no edge may bind
    # to two relationship slots in the same MATCH pattern, regardless of
    # whether the user assigned a variable.  AQL traversals enforce this
    # within a single FOR/IN, so explicit filters are only needed across
    # *separate* traversal groups.  Within merged groups the engine
    # already prevents repeated edges.
    if len(groups) > 1:
        cross_group_rels: list[str] = []
        # Edges of merged groups and edge lists of variable-length hops: each
        # traversal keeps its own edges distinct, so only pairs across groups
        # need a filter (Match6 [11]).
        grouped_edges: list[list[str]] = []
        for gi, group in enumerate(groups):
            if any(hops[i].r_style == "EMBEDDED" for i in group):
                continue
            if len(group) == 1:
                h = hops[group[0]]
                if h.hop_path is None:
                    if h.rel_range != (1, 1):
                        # Only the final edge is bound: no complete check.
                        continue
                    cross_group_rels.append(h.rel_var)
                    continue
            grouped_edges.append(group_edges[gi])
        _emit_relationship_uniqueness(cross_group_rels, lines)
        for filt in edge_uniqueness_across(grouped_edges, singles=cross_group_rels):
            lines.append(f"    FILTER {filt}")
    else:
        # Single group: only named rels need cross-checks if any exist
        # (merged groups already enforce uniqueness internally).
        named_rels = [h.rel_var for h in hops if h.rel_named]
        _emit_relationship_uniqueness(named_rels, lines)

    # Emit named path LET if a path variable was declared
    if path_var_name is not None:
        nodes_arr = _path_array(path_node_vars)
        edges_arr = _path_array(path_edge_vars)
        lines.append(f"    LET {path_var_name} = {{nodes: {nodes_arr}, edges: {edges_arr}}}")
        forbidden_vars.add(path_var_name)
        pvars = _active_path_vars.get()
        pvars[path_var_name] = (path_node_vars, path_edge_vars)

    is_varlen_trav = any(h.rel_range[0] != h.rel_range[1] for h in hops)

    where_ctx = match_ctx.oC_Where()
    user_filter = _compile_where(where_ctx.oC_Expression(), bind_vars) if where_ctx is not None else None
    if user_filter:
        _emit_prune_and_filter(
            user_filter,
            current_var,
            forbidden_vars,
            is_varlen=is_varlen_trav,
            lines=lines,
        )

    opt_var_env_rel: dict[str, str] = {}
    if optional_matches:
        opt_var_env_rel = _compile_optional_matches(
            optional_matches,
            resolver=resolver,
            bind_vars=bind_vars,
            forbidden_vars=forbidden_vars,
            bound_env={v: v for v in _pattern_parts_user_vars(all_parts)},
            lines=lines,
            rel_type_exprs=rel_type_exprs,
        )

    ret = spq.oC_Return()
    if ret is None:
        raise CoreError("RETURN is required in v0 subset", code="UNSUPPORTED")
    _set_user_vars_from_parts(all_parts)
    _append_return(
        ret.oC_ProjectionBody(),
        lines=lines,
        bind_vars=bind_vars,
        var_env=opt_var_env_rel,
        rel_type_exprs=rel_type_exprs,
    )
    lines = _eliminate_dead_lets(lines)
    return AqlQuery(text="\n".join(lines), bind_vars=bind_vars)


def _translate_union(
    regular: CypherParser.OC_RegularQueryContext,
    *,
    resolver: MappingResolver,
    bind_vars: dict[str, Any],
) -> AqlQuery:
    """
    Translate ``query UNION [ALL] query ...`` into AQL using
    ``UNION()`` / ``UNION_DISTINCT()`` array functions with subqueries.

    AQL shape::

        FOR _u IN UNION_DISTINCT(
          (FOR ... RETURN ...),
          (FOR ... RETURN ...)
        )
          RETURN _u
    """
    first_sq = regular.oC_SingleQuery()
    union_clauses = regular.oC_Union()
    if not union_clauses:
        raise CoreError(
            "Internal: _translate_union called without union clauses",
            code="INTERNAL_ERROR",
        )

    branches: list[CypherParser.OC_SingleQueryContext] = [first_sq]
    is_all = True
    for uc in union_clauses:
        branches.append(uc.oC_SingleQuery())
        if uc.ALL() is None:
            is_all = False

    # The caller seeds bind_vars with the user's parameters before any branch
    # is translated; those names must never be renamed (see _merge_bind_vars).
    user_params = frozenset(bind_vars)
    subqueries: list[str] = []
    for index, branch in enumerate(branches):
        # Seeded with the user's parameters, as a single query's bind vars are,
        # so names the branch picks for itself (_pick_bind_key) avoid them.
        branch_bv: dict[str, Any] = {k: bind_vars[k] for k in user_params}
        bq = _translate_single_query(
            branch,
            resolver=resolver,
            bind_vars=branch_bv,
        )
        text = _merge_bind_vars(bind_vars, branch_bv, bq.text, branch_index=index, user_params=user_params)
        subqueries.append(f"({text})")

    fn = "UNION" if is_all else "UNION_DISTINCT"
    joined = ",\n  ".join(subqueries)
    aql = f"FOR _u IN {fn}(\n  {joined}\n)\n  RETURN _u"
    return AqlQuery(text=aql, bind_vars=bind_vars)


def _translate_computational_multi_part(
    mpq: CypherParser.OC_MultiPartQueryContext,
    *,
    bind_vars: dict[str, Any],
    resolver: MappingResolver,
    imports: dict[str, str] | None = None,
    outer_names: frozenset[str] = frozenset(),
    prefix_lines: list[str] | None = None,
) -> AqlQuery:
    """Translate a multi-part query that has **no MATCH and no writes** — a pure
    computational pipeline over literals: leading ``WITH`` (constant projections)
    and/or leading ``UNWIND``, chained ``WITH``/``UNWIND`` stages, and a tail
    ``RETURN``. Clauses are processed in source order.

    Examples::

        WITH [1, 2, 3] AS list UNWIND list AS x RETURN x
        UNWIND range(1, 2) AS row WITH collect(row) AS rows UNWIND rows AS x RETURN x
        WITH {a: {b: 1}} AS m RETURN m.a.b

    A ``CALL { … }`` stage compiles in place, and *imports* / *outer_names* /
    *prefix_lines* are as for :func:`_translate_multi_part_query`.
    """
    lines: list[str] = list(prefix_lines or [])
    var_env: dict[str, str] = dict(imports or {})
    forbidden: set[str] = set(outer_names) | set(var_env.values())

    def _emit_unwind(uw: CypherParser.OC_UnwindContext) -> None:
        expr = unwind_source(uw.oC_Expression(), _compile_expression(uw.oC_Expression(), bind_vars))
        if var_env:
            expr = _rewrite_vars(expr, var_env)
        var = uw.oC_Variable().getText().strip()
        # Cypher may re-bind a name a WITH dropped (UNWIND l AS x … WITH l
        # UNWIND l AS x); AQL may not reassign a variable (ERR 1511).
        aql_var = _pick_fresh_var(var, forbidden_vars=forbidden)
        lines.append(f"FOR {aql_var} IN {expr}")
        var_env[var] = aql_var
        forbidden.add(aql_var)
        # In scope for ``RETURN *`` / ``WITH *`` (Unwind1 [11]).
        user_vars = list(_active_user_vars.get())
        if var not in user_vars:
            _active_user_vars.set([*user_vars, var])

    def _emit_procedure_call(iqc: CypherParser.OC_InQueryCallContext) -> None:
        # Yielded names are fresh in AQL: a WITH may have dropped a name a
        # later CALL yields again (Call6 [1]), and AQL may not reassign it.
        names: dict[str, str] = {}

        def aql_name(var: str) -> str:
            names[var] = _pick_fresh_var(var, forbidden_vars=forbidden)
            return names[var]

        incoming = dict(var_env)
        call_lines, yielded = in_query_call_lines(
            iqc,
            bind_vars,
            row=_pick_fresh_var("_call_row", forbidden_vars=forbidden),
            aql_name=aql_name,
            rewrite=lambda expr: _rewrite_vars(expr, incoming) if incoming else expr,
        )
        lines.extend(call_lines)
        var_env.update(names)
        in_scope = list(_active_user_vars.get())
        _active_user_vars.set([*in_scope, *(v for v in yielded if v not in in_scope)])

    def _emit_reading(rc: CypherParser.OC_ReadingClauseContext) -> None:
        call = subquery_call(rc)
        if call is not None:
            var_env.update(
                compile_subquery_call(
                    call,
                    resolver=resolver,
                    bind_vars=bind_vars,
                    var_env=var_env,
                    forbidden_vars=forbidden,
                    lines=lines,
                )
            )
            return
        iqc = rc.oC_InQueryCall()
        if iqc is not None:
            _emit_procedure_call(iqc)
            return
        uw = rc.oC_Unwind()
        if uw is None:
            # Routing guarantees no MATCH here; anything else is unsupported.
            raise CoreError(
                "Only UNWIND/WITH reading clauses are supported without MATCH",
                code="NOT_IMPLEMENTED",
            )
        _emit_unwind(uw)

    importing = mpq.oC_With(0) if imports is not None else None
    for child in mpq.getChildren():
        if child is importing:
            continue
        if isinstance(child, CypherParser.OC_ReadingClauseContext):
            _emit_reading(child)
        elif isinstance(child, CypherParser.OC_WithContext):
            var_env = _apply_with(
                child,
                lines=lines,
                bind_vars=bind_vars,
                forbidden_vars=forbidden,
                incoming_env=var_env,
            )
        elif isinstance(child, CypherParser.OC_SinglePartQueryContext):
            if child.oC_UpdatingClause():
                raise CoreError("Updating clauses are not supported in v0", code="UNSUPPORTED")
            for rc in child.oC_ReadingClause() or []:
                _emit_reading(rc)
            ret = child.oC_Return()
            if ret is None:
                raise CoreError("RETURN is required in v0 subset", code="UNSUPPORTED")
            _append_return(
                ret.oC_ProjectionBody(),
                lines=lines,
                bind_vars=bind_vars,
                var_env=var_env,
            )

    return AqlQuery(text="\n".join(lines), bind_vars=bind_vars)


def _compile_leading_match(
    match_ctx: CypherParser.OC_MatchContext,
    *,
    resolver: MappingResolver,
    bind_vars: dict[str, Any],
) -> tuple[list[str], set[str], dict[str, str]]:
    """Compile the first MATCH of a multi-part query, which binds from nothing.

    Returns the AQL lines, the AQL names in use, and the variable environment.
    A leading OPTIONAL MATCH yields one null-extended row when nothing matches
    (``OPTIONAL MATCH (a:Missing) WITH a …`` still has a row), so it compiles
    like any other OPTIONAL MATCH, with nothing bound.
    """
    if match_ctx.OPTIONAL() is not None:
        lines: list[str] = []
        forbidden_vars: set[str] = set()
        env = _compile_optional_matches(
            [match_ctx],
            resolver=resolver,
            bind_vars=bind_vars,
            forbidden_vars=forbidden_vars,
            bound_env={},
            lines=lines,
        )
        return lines, forbidden_vars, env
    parts = match_ctx.oC_Pattern().oC_PatternPart() or []
    if len(parts) > 1 or any(part.oC_Variable() is not None for part in parts):
        # ``MATCH (a {…}), (b {…}), … WITH …`` — the disconnected-parts compiler
        # the single-query path already uses (LDBC IC3, IC4, BI11, BI14); it
        # also binds a named path (``MATCH p = (a)-->(b) WITH p …``).
        where = match_ctx.oC_Where()
        lines, forbidden_vars = _compile_match_multi_parts_from_parts(
            list(parts),
            extra_wheres=[where] if where is not None else [],
            resolver=resolver,
            bind_vars=bind_vars,
        )
    else:
        lines, forbidden_vars = _compile_match_pipeline(match_ctx, resolver=resolver, bind_vars=bind_vars)
    return lines, forbidden_vars, {v: v for v in forbidden_vars}


def _compile_pipeline_unwind(
    unwind_ctx: CypherParser.OC_UnwindContext,
    *,
    bind_vars: dict[str, Any],
    forbidden_vars: set[str],
    var_env: dict[str, str],
    lines: list[str],
) -> dict[str, str]:
    """Compile an UNWIND that reads rows already bound into ``FOR x IN expr``.

    Returns the variable environment extended with the unwound variable.
    """
    expr = unwind_source(
        unwind_ctx.oC_Expression(), _compile_expression(unwind_ctx.oC_Expression(), bind_vars)
    )
    if var_env:
        expr = _rewrite_vars(expr, var_env)
    cypher_var = unwind_ctx.oC_Variable().getText().strip()
    aql_var = _pick_fresh_var(cypher_var, forbidden_vars=forbidden_vars)
    forbidden_vars.add(aql_var)
    lines.append(f"  FOR {aql_var} IN {expr}")
    user_vars = list(_active_user_vars.get())
    if cypher_var not in user_vars:
        _active_user_vars.set([*user_vars, cypher_var])
    return {**var_env, cypher_var: aql_var}


def _compile_following_match(
    match_ctx: CypherParser.OC_MatchContext,
    *,
    resolver: MappingResolver,
    bind_vars: dict[str, Any],
    forbidden_vars: set[str],
    var_env: dict[str, str],
    lines: list[str],
) -> tuple[dict[str, str], dict[str, str]]:
    """Compile a MATCH or OPTIONAL MATCH that reads rows already bound.

    Returns the extended variable environment and the ``type(r)`` expressions
    of the relationships it binds. An OPTIONAL MATCH keeps every incoming row
    (null-extended when nothing matches) — see :func:`_compile_optional_matches`.
    """
    if match_ctx.OPTIONAL() is None:
        out_env, rel_type_exprs = _compile_match_from_bound(
            match_ctx,
            resolver=resolver,
            bind_vars=bind_vars,
            forbidden_vars=forbidden_vars,
            var_env=var_env,
            lines=lines,
        )
        forbidden_vars.update(out_env.keys())
        return out_env, rel_type_exprs
    rel_type_exprs = {}
    opt_env = _compile_optional_matches(
        [match_ctx],
        resolver=resolver,
        bind_vars=bind_vars,
        forbidden_vars=forbidden_vars,
        bound_env=var_env,
        lines=lines,
        rel_type_exprs=rel_type_exprs,
    )
    return {**var_env, **opt_env}, rel_type_exprs


def _translate_multi_part_query(
    mpq: CypherParser.OC_MultiPartQueryContext,
    *,
    resolver: MappingResolver,
    bind_vars: dict[str, Any],
    imports: dict[str, str] | None = None,
    outer_names: frozenset[str] = frozenset(),
    prefix_lines: list[str] | None = None,
) -> AqlQuery:
    """
    Minimal MultiPartQuery support for v0.1 corpus:
    MATCH ... WITH ... RETURN ...

    *imports* is set for the body of a ``CALL { WITH a, b … }`` subquery: the
    imported Cypher variables and the outer AQL names they are bound to. The
    importing ``WITH`` is then skipped, and *outer_names* (every AQL name in
    scope outside) are never declared again. *prefix_lines* bind the imports
    first, for a plan step reading the previous step's rows (plan_steps.py).
    """
    with_ctxs = mpq.oC_With() or []
    if not with_ctxs:
        raise CoreError("WITH is required for multi-part queries", code="UNSUPPORTED")

    # No-MATCH, no-write computational pipeline (leading WITH-constants / UNWIND).
    # Route these to a dedicated order-walking handler so they no longer fail the
    # "MATCH is required before WITH" guard below.
    _prefix_reading = mpq.oC_ReadingClause() or []
    _tail = mpq.oC_SinglePartQuery()
    _tail_reading = (_tail.oC_ReadingClause() or []) if _tail is not None else []
    _has_match = any(rc.oC_Match() is not None for rc in _prefix_reading) or any(
        rc.oC_Match() is not None for rc in _tail_reading
    )
    _has_updating = bool(mpq.oC_UpdatingClause()) or (_tail is not None and bool(_tail.oC_UpdatingClause()))
    if not _has_match and not _has_updating:
        return _translate_computational_multi_part(
            mpq,
            bind_vars=bind_vars,
            resolver=resolver,
            imports=imports,
            outer_names=outer_names,
            prefix_lines=prefix_lines,
        )
    if is_create_pipeline(mpq):
        if prefix_lines is not None:
            # Its stages compile from scratch and would not read the step's rows.
            raise CoreError(
                "A CREATE before a WITH in a plan step that reads earlier rows is not supported",
                code="NOT_IMPLEMENTED",
            )
        return translate_create_pipeline(mpq, resolver=resolver, bind_vars=bind_vars)
    # SET / REMOVE / DELETE before a WITH write mid-pipeline (pipeline_writes).
    split_writes(mpq.oC_UpdatingClause() or [])

    reading_clauses = mpq.oC_ReadingClause() or []
    if not reading_clauses and imports is None:
        raise CoreError("MATCH is required before WITH in v0 subset", code="UNSUPPORTED")

    # Walk the clauses in query order: a MATCH after a WITH reads the rows that
    # WITH produced (``WITH p LIMIT 1 MATCH (p)--(f)`` limits *before* the
    # traversal), so it must be compiled at its own position, never hoisted
    # above the WITH stages.
    lines: list[str] = list(prefix_lines or [])
    forbidden_vars: set[str] = set(outer_names) | set((imports or {}).values())
    var_env: dict[str, str] = dict(imports or {})
    seen_match = imports is not None
    children = list(mpq.children or [])
    if imports is not None:
        children.remove(mpq.oC_With(0))  # the importing WITH: var_env holds the imports
    pending_writes: list[CypherParser.OC_UpdatingClauseContext] = []
    wrote = False
    for child in children:
        if isinstance(child, CypherParser.OC_UpdatingClauseContext):
            pending_writes.append(child)
            continue
        if pending_writes:  # a part's writes end at its WITH
            var_env = append_midquery_writes(
                pending_writes,
                lines=lines,
                var_env=var_env,
                bind_vars=bind_vars,
                resolver=resolver,
                forbidden_vars=forbidden_vars,
                already_wrote=wrote,
            )
            pending_writes, wrote = [], True
        if wrote and isinstance(child, CypherParser.OC_ReadingClauseContext) and child.oC_Unwind() is None:
            raise refuse_after_write("MATCH or CALL")
        if isinstance(child, CypherParser.OC_WithContext):
            if not seen_match:
                raise CoreError("MATCH is required before WITH in v0 subset", code="UNSUPPORTED")
            var_env = _apply_with(
                child,
                lines=lines,
                bind_vars=bind_vars,
                forbidden_vars=forbidden_vars,
                incoming_env=var_env,
            )
            continue
        if not isinstance(child, CypherParser.OC_ReadingClauseContext):
            continue
        call = subquery_call(child)
        if call is not None:
            var_env = compile_subquery_call(
                call,
                resolver=resolver,
                bind_vars=bind_vars,
                var_env=var_env,
                forbidden_vars=forbidden_vars,
                lines=lines,
            )
            seen_match = True
            continue
        uw = child.oC_Unwind()
        if uw is not None and seen_match:
            var_env = _compile_pipeline_unwind(
                uw, bind_vars=bind_vars, forbidden_vars=forbidden_vars, var_env=var_env, lines=lines
            )
            continue
        m = child.oC_Match()
        if m is None:
            raise CoreError(
                "Only MATCH, and UNWIND after a MATCH, are supported before WITH in a query with MATCH",
                code="NOT_IMPLEMENTED",
            )
        if not seen_match:
            lines, forbidden_vars, var_env = _compile_leading_match(m, resolver=resolver, bind_vars=bind_vars)
            seen_match = True
        else:
            # A WITH ends every part here, and it projects ``type(r)`` as a
            # value, so these relationships' type expressions are not needed.
            var_env, _ = _compile_following_match(
                m,
                resolver=resolver,
                bind_vars=bind_vars,
                forbidden_vars=forbidden_vars,
                var_env=var_env,
                lines=lines,
            )
        _set_user_vars_from_parts(m.oC_Pattern().oC_PatternPart() or [])

    # Final RETURN is in the trailing SinglePartQuery
    tail = mpq.oC_SinglePartQuery()
    if tail is None:
        raise CoreError("RETURN is required in v0 subset", code="UNSUPPORTED")

    # Reading clauses after the last WITH (MATCH … WITH … [OPTIONAL] MATCH /
    # UNWIND … RETURN …), in order.
    tail_reading = tail.oC_ReadingClause() or []
    if wrote and (tail.oC_UpdatingClause() or any(rc.oC_Unwind() is None for rc in tail_reading)):
        raise refuse_after_write("A write, MATCH or CALL")
    rel_type_exprs: dict[str, str] = {}
    for rc in tail_reading:
        tail_call = subquery_call(rc)
        if tail_call is not None:
            var_env = compile_subquery_call(
                tail_call,
                resolver=resolver,
                bind_vars=bind_vars,
                var_env=var_env,
                forbidden_vars=forbidden_vars,
                lines=lines,
            )
            continue
        tail_uw = rc.oC_Unwind()
        if tail_uw is not None:
            var_env = _compile_pipeline_unwind(
                tail_uw, bind_vars=bind_vars, forbidden_vars=forbidden_vars, var_env=var_env, lines=lines
            )
            continue
        tm = rc.oC_Match()
        if tm is None:
            raise CoreError(
                "Only MATCH and UNWIND are supported after WITH in v0 subset", code="NOT_IMPLEMENTED"
            )
        var_env, part_rel_type_exprs = _compile_following_match(
            tm,
            resolver=resolver,
            bind_vars=bind_vars,
            forbidden_vars=forbidden_vars,
            var_env=var_env,
            lines=lines,
        )
        rel_type_exprs.update(part_rel_type_exprs)
        _set_user_vars_from_parts(tm.oC_Pattern().oC_PatternPart() or [])

    # A tail write after WITH (e.g. MATCH … WITH … CREATE/MERGE/SET …). The WITH
    # pipeline is already in ``lines`` with ``var_env`` bound; the write nests
    # inside it, referencing the bound variables.
    tail_updating = tail.oC_UpdatingClause() or []
    if tail_updating:
        merge_clauses = [uc.oC_Merge() for uc in tail_updating if uc.oC_Merge() is not None]
        if merge_clauses:
            return _append_multipart_merge_tail(
                merge_clauses,
                tail=tail,
                tail_reading=tail_reading,
                lines=lines,
                resolver=resolver,
                bind_vars=bind_vars,
            )
        create_clauses = [uc.oC_Create() for uc in tail_updating if uc.oC_Create() is not None]
        if create_clauses:
            return _append_multipart_create_tail(
                tail_updating,
                tail=tail,
                lines=lines,
                var_env=var_env,
                bind_vars=bind_vars,
            )
        set_clauses = [uc.oC_Set() for uc in tail_updating if uc.oC_Set() is not None]
        delete_clauses = [uc.oC_Delete() for uc in tail_updating if uc.oC_Delete() is not None]
        remove_clauses = [uc.oC_Remove() for uc in tail_updating if uc.oC_Remove() is not None]
        if set_clauses or delete_clauses or remove_clauses:
            # A FOREACH beside them would be left out of the AQL.
            refuse_dropped_writes(
                "SET/DELETE/REMOVE", FOREACH=[uc for uc in tail_updating if uc.oC_Foreach() is not None]
            )
            if tail_reading:
                raise CoreError(
                    "MATCH after WITH before SET/DELETE/REMOVE is not supported",
                    code="NOT_IMPLEMENTED",
                )
            from .writes import _append_multipart_mutate_tail

            return _append_multipart_mutate_tail(
                set_clauses=set_clauses,
                delete_clauses=delete_clauses,
                remove_clauses=remove_clauses,
                tail=tail,
                lines=lines,
                var_env=var_env,
                bind_vars=bind_vars,
                resolver=resolver,
            )
        raise CoreError(
            "Unsupported write clause after WITH",
            code="NOT_IMPLEMENTED",
        )

    ret = tail.oC_Return()
    if ret is None:
        raise CoreError("RETURN is required in v0 subset", code="UNSUPPORTED")
    _append_return(
        ret.oC_ProjectionBody(),
        lines=lines,
        bind_vars=bind_vars,
        var_env=var_env,
        rel_type_exprs=rel_type_exprs,
    )

    return AqlQuery(text="\n".join(lines), bind_vars=bind_vars)


def _append_multipart_merge_tail(
    merge_clauses: list[CypherParser.OC_MergeContext],
    *,
    tail: CypherParser.OC_SinglePartQueryContext,
    tail_reading: list[CypherParser.OC_ReadingClauseContext],
    lines: list[str],
    resolver: MappingResolver,
    bind_vars: dict[str, Any],
) -> AqlQuery:
    """Emit a MERGE tail of a multi-part query, nested in the WITH pipeline.

    Handles ``MATCH … WITH … MERGE …`` (single-node and relationship MERGE,
    incl. between WITH-bound nodes, e.g. ``MATCH (a) MATCH (b) WITH a, b MERGE
    (a)-[:R]->(b)``). The MERGE translator already renders the UPSERT against
    the bound variables by name (``a._id`` / ``b._id`` / ``a.prop``); the
    tail's own reading clauses are empty here (the MATCHes live in the WITH
    parts), so it emits just the UPSERT, which we splice after the pipeline.

    A tail MATCH before the MERGE would be double-compiled (the multi-part
    handler already folded it into ``lines``), and mixing MERGE with other
    write clauses is ambiguous — both are refused rather than mis-compiled.
    """
    from .write_merge import _translate_merge_query

    if tail_reading:
        raise CoreError(
            "MATCH after WITH before MERGE is not supported",
            code="NOT_IMPLEMENTED",
        )
    non_merge = [uc for uc in (tail.oC_UpdatingClause() or []) if uc.oC_Merge() is None]
    if non_merge:
        raise CoreError(
            "MERGE after WITH cannot be combined with other write clauses",
            code="NOT_IMPLEMENTED",
        )

    merge_q = _translate_merge_query(
        tail,
        merge_clauses=merge_clauses,
        resolver=resolver,
        bind_vars=bind_vars,
    )
    pipeline = "\n".join(lines)
    text = (pipeline + "\n" + merge_q.text) if pipeline else merge_q.text
    return AqlQuery(text=text, bind_vars=bind_vars)


def _append_multipart_create_tail(
    updating_clauses: list[CypherParser.OC_UpdatingClauseContext],
    *,
    tail: CypherParser.OC_SinglePartQueryContext,
    lines: list[str],
    var_env: dict[str, str],
    bind_vars: dict[str, Any],
) -> AqlQuery:
    """Emit a CREATE (optionally with trailing SET/REMOVE and a RETURN) tail of
    a multi-part query, nested inside the already-built WITH pipeline.

    Handles ``MATCH … WITH … CREATE …`` (and ``UNWIND … WITH … CREATE …``):
    the CREATE reuses the pipeline's ``var_env`` so it can reference / connect
    already-bound variables (``CREATE (a)-[:R]->(b)`` with ``a``/``b`` bound).
    Trailing SET/REMOVE on the created variables are supported.  Standalone
    SET/DELETE/REMOVE after WITH are handled by ``_append_multipart_mutate_tail``
    before this helper is called.
    """
    from .write_create import _compile_create, _compile_return_for_create
    from .writes import _property_writes

    create_clauses: list[CypherParser.OC_CreateContext] = []
    set_clauses: list[CypherParser.OC_SetContext] = []
    remove_clauses: list[Any] = []
    for uc in updating_clauses:
        if uc.oC_Create() is not None:
            create_clauses.append(uc.oC_Create())
        elif uc.oC_Set() is not None:
            set_clauses.append(uc.oC_Set())
        elif uc.oC_Remove() is not None:
            remove_clauses.append(uc.oC_Remove())
        else:
            raise CoreError(
                "Only CREATE (optionally with SET/REMOVE) is supported as a write after WITH",
                code="NOT_IMPLEMENTED",
            )

    if not create_clauses:
        raise CoreError(
            "CREATE after WITH requires at least one CREATE clause",
            code="UNSUPPORTED",
        )

    ret = tail.oC_Return()
    # Folded into the inserted documents; a separate UPDATE would write the
    # same collection twice (ERR 1579).
    writes = _property_writes(set_clauses, remove_clauses, bind_vars=bind_vars)
    var_collections: dict[str, str] = {}
    num_creates = len(create_clauses)
    create_resolver = _active_resolver.get()
    if create_clauses and create_resolver is None:
        raise CoreError("CREATE requires a mapping resolver", code="UNSUPPORTED")
    for ci, cc in enumerate(create_clauses):
        assert create_resolver is not None  # narrowed above; loop runs only with clauses
        _compile_create(
            cc,
            resolver=create_resolver,
            bind_vars=bind_vars,
            var_env=var_env,
            lines=lines,
            indent="  ",
            has_return=ret is not None or ci < num_creates - 1,
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
            indent="  ",
        )

    return AqlQuery(text="\n".join(lines), bind_vars=bind_vars)


def _compile_optional_matches(
    opt_matches: list[CypherParser.OC_MatchContext],
    *,
    resolver: MappingResolver,
    bind_vars: dict[str, Any],
    forbidden_vars: set[str],
    bound_env: dict[str, str],
    lines: list[str],
    rel_type_exprs: dict[str, str] | None = None,
) -> dict[str, str]:
    """Compile OPTIONAL MATCH clauses with Cypher's row semantics.

    Each pattern (with its WHERE) is compiled by the bound-MATCH compiler into a
    subquery that returns one object per match. The outer query then iterates
    those rows — or one empty row when there is none — and rebinds each new
    variable from it::

        LET _opt_rows = (FOR f, r IN 1..1 ANY p @@edges ... RETURN {f: f})
        FOR _opt_row IN (LENGTH(_opt_rows) > 0 ? _opt_rows : [{}])
          LET f = _opt_row.f

    A row with no match survives with the new variables null (``{}.f`` is
    null), and a row with several matches yields one row per match. The earlier
    ``LET f = FIRST(...)`` kept only the first match, so ``count(f)`` was capped
    at one and every other match was silently dropped.

    *bound_env* maps the user variables already in scope (from the preceding
    MATCH or WITH) to their AQL names; a pattern node or relationship naming one
    is correlated with it, not rebound. Returns the Cypher → AQL names of the
    variables the clauses introduce.
    """
    env: dict[str, str] = dict(bound_env)
    extra_env: dict[str, str] = {}
    for om in opt_matches:
        sub_lines: list[str] = []
        out_env, sub_rel_type_exprs = _compile_match_from_bound(
            om,
            resolver=resolver,
            bind_vars=bind_vars,
            forbidden_vars=forbidden_vars,
            var_env=env,
            lines=sub_lines,
        )
        new_vars = [cv for cv in out_env if cv not in env]
        rows_var = _pick_fresh_var("_opt_rows", forbidden_vars=forbidden_vars)
        forbidden_vars.add(rows_var)
        row_var = _pick_fresh_var("_opt_row", forbidden_vars=forbidden_vars)
        forbidden_vars.add(row_var)

        fields = ", ".join(f"{out_env[cv]}: {out_env[cv]}" for cv in new_vars)
        lines.append(f"  LET {rows_var} = (")
        lines.extend(f"  {ln}" for ln in sub_lines)
        lines.append(f"    RETURN {{{fields}}}")
        lines.append("  )")
        lines.append(f"  FOR {row_var} IN (LENGTH({rows_var}) > 0 ? {rows_var} : [{{}}])")
        # The outer LET reuses the subquery's AQL name, so a ``type(r)``
        # expression compiled against it stays valid (and is null for no match).
        for cv in new_vars:
            lines.append(f"    LET {out_env[cv]} = {row_var}.{out_env[cv]}")
            env[cv] = out_env[cv]
            extra_env[cv] = out_env[cv]
        if rel_type_exprs is not None:
            rel_type_exprs.update({cv: e for cv, e in sub_rel_type_exprs.items() if cv in new_vars})
    return extra_env


def _compile_match_multi_parts(
    match_ctx: CypherParser.OC_MatchContext,
    *,
    resolver: MappingResolver,
    bind_vars: dict[str, Any],
) -> tuple[list[str], set[str]]:
    """Compile MATCH with multiple pattern parts (comma-separated)."""
    pattern = match_ctx.oC_Pattern()
    parts = pattern.oC_PatternPart()
    if not parts or len(parts) < 2:
        raise CoreError("Expected multiple pattern parts", code="INTERNAL_ERROR")
    wheres = []
    wc = match_ctx.oC_Where()
    if wc is not None:
        wheres.append(wc)
    return _compile_match_multi_parts_from_parts(
        parts,
        extra_wheres=wheres,
        resolver=resolver,
        bind_vars=bind_vars,
    )


#: The variables a ``FOR v[, e[, p]] IN`` line declares.
_FOR_LOOP_VARS = re.compile(
    r"\s*FOR\s+(\w+|`[^`]+`)(?:\s*,\s*(\w+|`[^`]+`))?(?:\s*,\s*(\w+|`[^`]+`))?\s+IN\b"
)


def _compile_match_multi_parts_from_parts(
    parts: list,
    *,
    extra_wheres: list[CypherParser.OC_WhereContext] | None = None,
    resolver: MappingResolver,
    bind_vars: dict[str, Any],
) -> tuple[list[str], set[str]]:
    """Compile multiple pattern parts (from one or more MATCH clauses) into AQL."""

    lines: list[str] = []
    forbidden: set[str] = set()
    for_count = 0
    bound_labels: dict[str, str] = {}

    def indent_for_line() -> str:
        # For readability and stable goldens, we keep all FOR lines after the first
        # at a single indentation level (2 spaces), even if there are multiple loops.
        return "" if for_count == 0 else "  "

    def indent_filter_line() -> str:
        # Filters align under the most recent FOR block:
        # - after 1 FOR: 2 spaces
        # - after >=2 FORs: 4 spaces
        level = 1 if for_count <= 1 else 2
        return "  " * level

    # Variables of the FOR lines so far, in order: a later part's member scan
    # runs below the latest (see union_scan_source).
    loop_vars: list[str] = []

    def add_for(line: str) -> None:
        nonlocal for_count
        lines.append(f"{indent_for_line()}{line}")
        for_count += 1
        declared = _FOR_LOOP_VARS.match(line)
        if declared is not None:
            loop_vars.extend(g for g in declared.groups() if g)

    def latest_loop_var() -> str | None:
        return loop_vars[-1] if loop_vars else None

    def add_filter(expr: str) -> None:
        lines.append(f"{indent_filter_line()}FILTER {expr}")

    def emit_entity_filters(
        var: str,
        labels: list[str] | None,
        *,
        via_edge: str | None = None,
        edge_direction: str | None = None,
    ) -> None:
        if not labels:
            return
        primary = _pick_primary_entity_label(labels, resolver)
        if bound_labels.get(var) == primary and len(labels) == 1:
            return
        entity_mapping = resolver.resolve_entity(_strip_label_backticks(primary))
        entity_style = entity_mapping.get("style")

        skip_coll_filter = (
            via_edge is not None
            and edge_direction is not None
            and resolver.edge_constrains_target(via_edge, primary, edge_direction)
        )
        if not skip_coll_filter:
            add_filter(
                node_collection_filter(
                    var, primary, entity_mapping, resolver, bind_vars, key_base=f"{var}Collection"
                )
            )

        if entity_style == "LABEL":
            tf_key = _pick_bind_key(f"{var}TypeField", bind_vars)
            tv_key = _pick_bind_key(f"{var}TypeValue", bind_vars)
            bind_vars[tf_key] = entity_mapping.get("typeField")
            bind_vars[tv_key] = entity_mapping.get("typeValue")
            add_filter(label_test(var, tf_key, tv_key, entity_mapping, bind_vars))
            for extra in _extra_label_filters(var, labels, primary, bind_vars, bare=True):
                add_filter(extra)
        elif entity_style == "MULTI_LABEL":
            add_filter(_multi_label_filter(var, entity_mapping, bind_vars))
            for extra in _extra_label_filters(var, labels, primary, bind_vars, bare=True):
                add_filter(extra)
        elif entity_style == "UNION":
            for extra in _extra_label_filters(var, labels, primary, bind_vars, bare=True):
                add_filter(extra)
        elif entity_style != "COLLECTION":
            raise CoreError(f"Unsupported entity mapping style: {entity_style}", code="INVALID_MAPPING")
        else:
            for extra in _collection_label_filters(var, labels, primary, bind_vars, bare=True):
                add_filter(extra)
        bound_labels[var] = primary

    def emit_rel_type_filter(rel_var: str, rel_type: str | None) -> str | None:
        r_map = _resolve_relationship_for_pattern(resolver, rel_type)
        r_style = r_map.get("style")
        if r_style == "GENERIC_WITH_TYPE":
            rtf_key = _pick_bind_key(f"{rel_var}TypeField", bind_vars)
            rtv_key = _pick_bind_key(f"{rel_var}TypeValue", bind_vars)
            bind_vars[rtf_key] = r_map.get("typeField")
            bind_vars[rtv_key] = r_map.get("typeValue")
            return f"{rel_var}[@{rtf_key}] == @{rtv_key}"
        if r_style == "DEDICATED_COLLECTION":
            return None
        raise CoreError(f"Unsupported relationship mapping style: {r_style}", code="INVALID_MAPPING")

    def extract_node_var_and_optional_labels(
        node_pat: CypherParser.OC_NodePatternContext, *, default_var: str
    ) -> tuple[str, list[str]]:
        var = (
            node_pat.oC_Variable().getText() if node_pat.oC_Variable() is not None else default_var
        ).strip()
        labels_ctx = node_pat.oC_NodeLabels()
        if labels_ctx is None:
            return var, []
        node_labels = labels_ctx.oC_NodeLabel() or []
        labels: list[str] = []
        for nl in node_labels:
            label = nl.oC_LabelName().getText().strip()
            if not label:
                raise CoreError("Invalid label in MATCH", code="UNSUPPORTED")
            labels.append(label)
        return var, labels

    def bind_named_path(part: Any, node_pieces: list[str], edge_pieces: list[str]) -> None:
        """``p = (…)``: ``LET p = {nodes, edges}`` from the part's hops, in pattern order."""
        if part.oC_Variable() is None:
            return
        name = part.oC_Variable().getText().strip()
        nodes = node_pieces[0] if len(node_pieces) == 1 else f"UNION({', '.join(node_pieces)})"
        if not edge_pieces:
            edges = "[]"
        elif len(edge_pieces) == 1:
            edges = edge_pieces[0]
        else:
            edges = f"UNION({', '.join(edge_pieces)})"
        lines.append(f"{indent_filter_line()}LET {name} = {{nodes: {nodes}, edges: {edges}}}")
        forbidden.add(name)
        _active_path_vars.get()[name] = ([], [])

    rel_part_i = 0
    all_named_rel_vars: list[str] = []
    # A shortestPath() part needs its endpoints bound: compiled after the others.
    shortest_parts = [part for part in parts if shortest_path_pattern(part) is not None]
    parts = [part for part in parts if shortest_path_pattern(part) is None]
    for idx, part in enumerate(parts):
        anon = part.oC_AnonymousPatternPart()
        if anon is None:
            raise CoreError("Named pattern parts are not supported in v0", code="UNSUPPORTED")
        elem = anon.oC_PatternElement()
        node = elem.oC_NodePattern()
        if node is None:
            raise CoreError("Only node-based pattern parts are supported in v0", code="UNSUPPORTED")
        chains = elem.oC_PatternElementChain() or []

        # Case 1: node-only pattern part: (u:Label {props})
        if not chains:
            var, labels = extract_node_var_and_optional_labels(node, default_var=f"n{idx + 1}")
            prop_filters = _compile_node_pattern_properties(node, var=var, bind_vars=bind_vars)

            if var in forbidden:
                # Join semantics: variable already bound by a previous pattern part.
                emit_entity_filters(var, labels)
                for f in prop_filters:
                    add_filter(f)
                continue

            coll_key = _pick_bind_key("@collection", bind_vars)
            if not labels:
                bind_vars[coll_key] = _infer_unlabeled_collection(resolver)
                add_for(f"FOR {var} IN {_aql_collection_ref(coll_key)}")
            else:
                primary = _pick_primary_entity_label(labels, resolver)
                entity_mapping = resolver.resolve_entity(_strip_label_backticks(primary))
                entity_style = entity_mapping.get("style")

                source, pushed = node_scan_source(
                    var,
                    primary,
                    entity_mapping,
                    resolver,
                    bind_vars,
                    coll_key=coll_key,
                    filters=prop_filters,
                    after=latest_loop_var(),
                )
                if pushed:
                    prop_filters = []
                add_for(f"FOR {var} IN {source}")

                if entity_style == "LABEL":
                    tf_key = _pick_bind_key(f"{var}TypeField", bind_vars)
                    tv_key = _pick_bind_key(f"{var}TypeValue", bind_vars)
                    bind_vars[tf_key] = entity_mapping.get("typeField")
                    bind_vars[tv_key] = entity_mapping.get("typeValue")
                    add_filter(label_test(var, tf_key, tv_key, entity_mapping, bind_vars))
                    for extra in _extra_label_filters(var, labels, primary, bind_vars, bare=True):
                        add_filter(extra)
                elif entity_style == "MULTI_LABEL":
                    add_filter(_multi_label_filter(var, entity_mapping, bind_vars))
                    for extra in _extra_label_filters(var, labels, primary, bind_vars, bare=True):
                        add_filter(extra)
                elif entity_style == "UNION":
                    for extra in _extra_label_filters(var, labels, primary, bind_vars, bare=True):
                        add_filter(extra)
                elif entity_style != "COLLECTION":
                    raise CoreError(
                        f"Unsupported entity mapping style: {entity_style}", code="INVALID_MAPPING"
                    )
                else:
                    for extra in _collection_label_filters(var, labels, primary, bind_vars, bare=True):
                        add_filter(extra)

            for f in prop_filters:
                add_filter(f)

            forbidden.add(var)
            if labels:
                bound_labels[var] = primary
            bind_named_path(part, [f"[{var}]"], [])
            continue

        # Case 2: relationship pattern part: (u)-[:T]->(v) ... (1+ hops)
        u_var, u_labels = extract_node_var_and_optional_labels(node, default_var=f"u{idx + 1}")
        u_prop_filters = _compile_node_pattern_properties(node, var=u_var, bind_vars=bind_vars)

        # Bind (or refine) start node u.
        if u_var in forbidden:
            emit_entity_filters(u_var, u_labels)
            for f in u_prop_filters:
                add_filter(f)
        else:
            ucoll_key = _pick_bind_key(f"@{u_var}Collection", bind_vars)
            if not u_labels:
                bind_vars[ucoll_key] = _infer_unlabeled_collection(resolver)
                add_for(f"FOR {u_var} IN {_aql_collection_ref(ucoll_key)}")
            else:
                u_primary = _pick_primary_entity_label(u_labels, resolver)
                u_map = resolver.resolve_entity(_strip_label_backticks(u_primary))
                u_style = u_map.get("style")
                u_source, pushed = node_scan_source(
                    u_var,
                    u_primary,
                    u_map,
                    resolver,
                    bind_vars,
                    coll_key=ucoll_key,
                    filters=u_prop_filters,
                    after=latest_loop_var(),
                )
                if pushed:
                    u_prop_filters = []
                add_for(f"FOR {u_var} IN {u_source}")

                if u_style == "LABEL":
                    tf_key = _pick_bind_key(f"{u_var}TypeField", bind_vars)
                    tv_key = _pick_bind_key(f"{u_var}TypeValue", bind_vars)
                    bind_vars[tf_key] = u_map.get("typeField")
                    bind_vars[tv_key] = u_map.get("typeValue")
                    add_filter(label_test(u_var, tf_key, tv_key, u_map, bind_vars))
                    for extra in _extra_label_filters(u_var, u_labels, u_primary, bind_vars, bare=True):
                        add_filter(extra)
                elif u_style == "MULTI_LABEL":
                    add_filter(_multi_label_filter(u_var, u_map, bind_vars))
                    for extra in _extra_label_filters(u_var, u_labels, u_primary, bind_vars, bare=True):
                        add_filter(extra)
                elif u_style == "UNION":
                    for extra in _extra_label_filters(u_var, u_labels, u_primary, bind_vars, bare=True):
                        add_filter(extra)
                elif u_style != "COLLECTION":
                    raise CoreError(f"Unsupported entity mapping style: {u_style}", code="INVALID_MAPPING")
                else:
                    for extra in _collection_label_filters(u_var, u_labels, u_primary, bind_vars, bare=True):
                        add_filter(extra)
            for f in u_prop_filters:
                add_filter(f)
            forbidden.add(u_var)
            if u_labels:
                bound_labels[u_var] = u_primary

        current_u = u_var
        path_nodes: list[str] = [f"[{u_var}]"]
        path_edges: list[str] = []
        for hop_i, chain in enumerate(chains):
            rel_pat = chain.oC_RelationshipPattern()
            v_node = chain.oC_NodePattern()
            if rel_pat is None or v_node is None:
                raise CoreError("Invalid relationship pattern", code="UNSUPPORTED")

            v_var, v_labels = extract_node_var_and_optional_labels(
                v_node, default_var=f"v{idx + 1}_{hop_i + 1}"
            )
            v_bound = v_var in forbidden
            v_trav = (
                v_var
                if not v_bound
                else _pick_fresh_var(_affix_name(v_var, suffix="_m"), forbidden_vars=forbidden)
            )

            rel_default = "r" if rel_part_i == 0 else f"r{rel_part_i + 1}"
            rel_part_i += 1
            rel_type, rel_var, rel_range = _extract_relationship_type_and_var(
                rel_pat, default_var=rel_default
            )
            # An unlabeled endpoint of a typed relationship can be resolved from
            # the relationship's domain/range (e.g. (m:Movie)<-[:REVIEWED]-() →
            # Person). This is treated as if the node carried that label, so all
            # the normal entity-filter logic (collection + type discriminator +
            # edge-constrains optimization) applies. Falls through to the
            # single-collection inference below when it cannot be resolved.
            if not v_bound and not v_labels and rel_type:
                _inferred_v = resolver.infer_endpoint_label(rel_type, _relationship_direction(rel_pat))
                if _inferred_v:
                    v_labels = [_inferred_v]
            detail = rel_pat.oC_RelationshipDetail()
            rel_is_named = detail is not None and detail.oC_Variable() is not None
            r_prop_filters = _compile_relationship_pattern_properties(
                rel_pat, var=rel_var, bind_vars=bind_vars
            )

            if rel_var in forbidden:
                raise CoreError(
                    "Shared relationship variables across pattern parts not supported in v0",
                    code="NOT_IMPLEMENTED",
                )

            direction = _relationship_direction(rel_pat)

            # Relationship mapping (edge collection + optional type filter)
            r_map = _resolve_relationship_for_pattern(resolver, rel_type)
            edge_key = _pick_bind_key("@edgeCollection", bind_vars)
            bind_vars[edge_key] = r_map.get("edgeCollectionName") or r_map.get("collectionName")
            if not _is_edge_collection_value(bind_vars[edge_key]):
                raise CoreError(
                    f"Invalid relationship mapping collection for: {rel_type}", code="INVALID_MAPPING"
                )

            rmin, rmax = rel_range
            hop_path = ""
            if part.oC_Variable() is not None:
                # A named path collects every hop's vertices and edges.
                hop_path = _pick_fresh_var(f"_pp{idx + 1}_{hop_i + 1}", forbidden_vars=forbidden)
                path_nodes.append(f"SLICE({hop_path}.vertices, 1)")
                path_edges.append(f"{hop_path}.edges")
            trav_vars = f"{v_trav}, {rel_var}, {hop_path}" if hop_path else f"{v_trav}, {rel_var}"
            add_for(
                f"FOR {trav_vars} IN {rmin}..{rmax} {direction} {current_u} {_aql_collection_ref(edge_key)}"
            )

            if v_bound:
                add_filter(f"{v_trav}._id == {v_var}._id")

            if v_labels:
                emit_entity_filters(v_trav, v_labels, via_edge=rel_type, edge_direction=direction)
            elif not v_bound:
                target_filter = _unlabeled_target_filter(v_trav, resolver, bind_vars, f"{v_trav}Collection")
                if target_filter is not None:
                    add_filter(target_filter)
            for f in _compile_node_pattern_properties(v_node, var=v_trav, bind_vars=bind_vars):
                add_filter(f)

            rel_type_filter = emit_rel_type_filter(rel_var, rel_type)
            if rel_type_filter:
                add_filter(rel_type_filter)
            for f in r_prop_filters:
                add_filter(f)

            forbidden.add(rel_var)
            if rel_is_named:
                all_named_rel_vars.append(rel_var)
            if not v_bound:
                forbidden.add(v_var)
                if v_labels:
                    bound_labels[v_var] = _pick_primary_entity_label(v_labels, resolver)

            current_u = v_trav
        bind_named_path(part, path_nodes, path_edges)

    _emit_relationship_uniqueness(all_named_rel_vars, lines, indent=indent_filter_line())

    for part in shortest_parts:
        env = {v: v for v in forbidden}
        lines += compile_shortest_path(
            part,
            var_env=env,
            forbidden=forbidden,
            resolver=resolver,
            bind_vars=bind_vars,
            indent=indent_filter_line(),
        )
        forbidden.update(env.values())

    for wc in extra_wheres or []:
        f = _compile_where(wc.oC_Expression(), bind_vars)
        add_filter(f)

    return lines, forbidden


def _compile_match_from_bound(
    match_ctx: CypherParser.OC_MatchContext,
    *,
    resolver: MappingResolver,
    bind_vars: dict[str, Any],
    forbidden_vars: set[str],
    var_env: dict[str, str],
    lines: list[str],
) -> tuple[dict[str, str], dict[str, str]]:
    """
    Compile a MATCH that occurs after a WITH, where variables may already be bound.
    Supported:
    - MATCH (u) WHERE ... (filter only)
    - MATCH (u)-[:TYPE]->(v:Label), any number of hops
    - several comma-separated pattern parts, compiled in order; each part may
      start at a variable bound by the WITH or by an earlier part, and a node
      or relationship already bound is correlated by ``_id`` rather than rebound.
    """
    pattern = match_ctx.oC_Pattern()
    parts = pattern.oC_PatternPart() or []
    if not parts:
        raise CoreError("MATCH after WITH requires a pattern", code="UNSUPPORTED")
    out_env = dict(var_env)
    rel_type_exprs: dict[str, str] = {}
    any_chain = False
    shortest_parts = [part for part in parts if shortest_path_pattern(part) is not None]
    for part in parts:
        if shortest_path_pattern(part) is not None:
            continue
        if part.oC_AnonymousPatternPart() is None:
            raise CoreError("Named paths in a MATCH after WITH are not supported", code="UNSUPPORTED")
        # Read from the bound end when only the last node is bound (pattern_reversal).
        written = part.oC_AnonymousPatternPart().oC_PatternElement()
        elem = from_bound_end(written, out_env)
        path_name = part.oC_Variable().getText().strip() if part.oC_Variable() is not None else None
        has_chain = _compile_bound_part(
            elem,
            resolver=resolver,
            bind_vars=bind_vars,
            forbidden_vars=forbidden_vars,
            out_env=out_env,
            rel_type_exprs=rel_type_exprs,
            lines=lines,
            path_name=path_name,
            reversed_part=elem is not written,
        )
        any_chain = any_chain or has_chain

    for part in shortest_parts:
        lines += compile_shortest_path(
            part,
            var_env=out_env,
            forbidden=forbidden_vars,
            resolver=resolver,
            bind_vars=bind_vars,
            indent="    ",
        )
        forbidden_vars.update(out_env.values())

    where_ctx = match_ctx.oC_Where()
    if where_ctx is not None:
        f = _rewrite_vars(_compile_where(where_ctx.oC_Expression(), bind_vars), out_env)
        lines.append(f"    FILTER {f}" if any_chain else f"  FILTER {f}")
    return out_env, rel_type_exprs


def _compile_bound_part(
    elem: CypherParser.OC_PatternElementContext,
    *,
    resolver: MappingResolver,
    bind_vars: dict[str, Any],
    forbidden_vars: set[str],
    out_env: dict[str, str],
    rel_type_exprs: dict[str, str],
    lines: list[str],
    path_name: str | None = None,
    reversed_part: bool = False,
) -> bool:
    """Compile one pattern part of a MATCH after WITH into *lines*, extending
    *out_env* with the variables it binds. Returns whether it has relationships.

    *path_name* names the part's path (``OPTIONAL MATCH p = (a)-->(b)``): it is
    bound to ``{nodes, edges}`` as in a leading MATCH, a variable-length hop
    contributing its traversal's vertices and edges. *reversed_part* says the
    part is compiled from its bound end (``pattern_reversal``), so the path
    is reversed back to the order it was written in.
    """
    node = elem.oC_NodePattern()
    if node is None:
        raise CoreError("Only node patterns are supported in v0", code="UNSUPPORTED")
    chains = elem.oC_PatternElementChain() or []

    if node.oC_Variable() is None:
        # An unnamed start (``MATCH (:Person {id: $p})-[:KNOWS]-(f)`` after a
        # WITH, LDBC IC12) is scanned under a name no one else uses.
        u_cy = _anonymous_var("_start", node, forbidden_vars | set(out_env) | set(out_env.values()))
    else:
        u_cy = node.oC_Variable().getText().strip()
    bound_start = u_cy in out_env
    u_aql = out_env.get(u_cy)

    # If the start is unbound, start a fresh scan over (u:Label) using the mapping.
    if not bound_start:
        labels_ctx = node.oC_NodeLabels()
        label: str | None = None
        if labels_ctx is not None:
            node_labels = labels_ctx.oC_NodeLabel()
            if not node_labels or len(node_labels) != 1:
                raise CoreError("MATCH after WITH requires exactly one label", code="UNSUPPORTED")
            label = node_labels[0].oC_LabelName().getText().strip()
            if not label:
                raise CoreError("Invalid label in MATCH", code="UNSUPPORTED")

        # The scan runs below the variables bound so far (see union_scan_source).
        after = next((v for v in reversed(out_env.values()) if _BARE_IDENTIFIER_RE.fullmatch(v)), None)
        u_aql = _pick_fresh_var(u_cy, forbidden_vars=forbidden_vars)
        out_env[u_cy] = u_aql

        coll_key = _pick_bind_key("@tailCollection", bind_vars)
        tail_filters: list[str] = []
        tail_props = _compile_node_pattern_properties(node, var=u_aql, bind_vars=bind_vars, var_env=out_env)
        if label is None:
            bind_vars[coll_key] = _unlabeled_start_collection(chains[0] if chains else None, resolver)
            tail_source = _aql_collection_ref(coll_key)
        else:
            entity_mapping = resolver.resolve_entity(_strip_label_backticks(label))
            entity_style = entity_mapping.get("style")
            tail_source, pushed = node_scan_source(
                u_aql,
                label,
                entity_mapping,
                resolver,
                bind_vars,
                coll_key=coll_key,
                filters=tail_props,
                after=after,
            )
            if pushed:
                tail_props = []

            if entity_style == "LABEL":
                tf_key = _pick_bind_key("tailTypeField", bind_vars)
                tv_key = _pick_bind_key("tailTypeValue", bind_vars)
                bind_vars[tf_key] = entity_mapping.get("typeField")
                bind_vars[tv_key] = entity_mapping.get("typeValue")
                tail_filters.append(label_test(u_aql, tf_key, tv_key, entity_mapping, bind_vars))
            elif entity_style == "MULTI_LABEL":
                tail_filters.append(_multi_label_filter(u_aql, entity_mapping, bind_vars))
            elif entity_style not in ("COLLECTION", "UNION"):
                raise CoreError(f"Unsupported entity mapping style: {entity_style}", code="INVALID_MAPPING")

        lines.append(f"  FOR {u_aql} IN {tail_source}")
        for f in tail_filters:
            lines.append(f"    FILTER {f}")

        # Inline properties on fresh scan node.
        for f in tail_props:
            lines.append(f"    FILTER {f}")
    else:
        # A bound start still has to carry the labels and properties this
        # pattern gives it (``WITH a MATCH (a:X)-->(b)``): they filter.
        bound_aql = out_env[u_cy]
        if node.oC_NodeLabels() is not None:
            pred = _compile_label_predicate(bound_aql, node.oC_NodeLabels(), bind_vars)
            if pred is not None:
                lines.append(f"    FILTER {pred}")
        for f in _compile_node_pattern_properties(node, var=bound_aql, bind_vars=bind_vars, var_env=out_env):
            lines.append(f"    FILTER {f}")

    node_segments: list[str] = [f"[{u_aql}]"]
    edge_segments: list[str] = []
    if not chains:
        _bind_bound_path(
            path_name, node_segments, edge_segments, out_env, forbidden_vars, lines, reversed_part
        )
        return False

    current_aql = u_aql
    # Single hops of this part, with their edge collections: Cypher never
    # matches one relationship twice within a pattern.
    single_hops: list[tuple[str, set[str]]] = []
    for hop_idx, chain in enumerate(chains):
        rel_pat = chain.oC_RelationshipPattern()
        v_node = chain.oC_NodePattern()
        if rel_pat is None or v_node is None:
            raise CoreError("Invalid relationship pattern", code="UNSUPPORTED")

        node_default = ("v", "w")[hop_idx] if hop_idx < 2 else f"w{hop_idx}"
        rel_default = ("r", "r_1")[hop_idx] if hop_idx < 2 else f"r_{hop_idx}"
        v_cy, v_labels = _extract_node_var_and_labels(v_node, default_var=node_default)
        rel_type, rel_cy, rel_range = _extract_relationship_type_and_var(rel_pat, default_var=rel_default)
        _rdetail = rel_pat.oC_RelationshipDetail()
        rel_named = _rdetail is not None and _rdetail.oC_Variable() is not None
        v_named = v_node.oC_Variable() is not None
        v_is_backref = v_named and v_cy in out_env

        # A node or relationship already bound (by the WITH or an earlier
        # part) is not rebound: AQL rejects assigning a variable twice (ERR
        # 1511 — LDBC IC5 on LPG). The traversal binds a fresh variable and is
        # correlated with the bound one by ``_id``. Only an *explicitly named*
        # relationship may back-reference: an unnamed edge's synthetic default
        # name (``r``) must never alias a prior binding.
        v_aql = _pick_fresh_var(v_cy, forbidden_vars=forbidden_vars)
        r_is_backref = rel_named and rel_cy in out_env
        r_aql = _pick_fresh_var(rel_cy, forbidden_vars=forbidden_vars)
        backref_filters: list[str] = []
        if v_is_backref:
            backref_filters.append(f"{v_aql}._id == {out_env[v_cy]}._id")
        if r_is_backref:
            backref_filters.append(f"{r_aql}._id == {out_env[rel_cy]}._id")
        r_prop_filters = _compile_relationship_pattern_properties(
            rel_pat, var=r_aql, bind_vars=bind_vars, var_env=out_env
        )

        if v_named and not v_is_backref:
            out_env[v_cy] = v_aql
        if rel_named and not r_is_backref:
            out_env[rel_cy] = r_aql
        forbidden_vars.add(v_aql)
        forbidden_vars.add(r_aql)

        direction = _self_loop_direction(
            _relationship_direction(rel_pat),
            back_to_start=v_is_backref and out_env[v_cy] == current_aql,
            rel_range=rel_range,
        )
        # Resolve an unlabeled (non-back-reference) endpoint of a typed
        # relationship from the relationship's domain/range.
        if not v_labels and not v_is_backref and rel_type:
            _inferred_v = resolver.infer_endpoint_label(rel_type, direction)
            if _inferred_v:
                v_labels = [_inferred_v]

        v_primary: str | None = None
        v_map: dict[str, Any] | None = None
        if v_labels:
            v_primary = _pick_primary_entity_label(v_labels, resolver)
            v_map = resolver.resolve_entity(_strip_label_backticks(v_primary))
        r_map = _resolve_relationship_for_pattern(resolver, rel_type)

        edge_key = _pick_bind_key("@edgeCollection", bind_vars)
        bind_vars[edge_key] = r_map.get("edgeCollectionName") or r_map.get("collectionName")
        if not _is_edge_collection_value(bind_vars[edge_key]):
            raise CoreError(
                f"Invalid relationship mapping collection for: {rel_type}", code="INVALID_MAPPING"
            )

        # A correlated (already bound) end node needs no collection check: the
        # ``_id`` filter pins it to the bound document.
        skip_coll_filter = (v_is_backref and not v_labels) or (
            v_primary is not None
            and rel_type is not None
            and resolver.edge_constrains_target(rel_type, v_primary, direction)
        )
        coll_filter: str | None = None
        if not skip_coll_filter:
            if v_map is None:
                coll_filter = _unlabeled_target_filter(v_aql, resolver, bind_vars, "vCollection")
            else:
                coll_filter = node_collection_filter(
                    v_aql, str(v_primary), v_map, resolver, bind_vars, key_base="vCollection"
                )

        rmin, rmax = rel_range
        trav_vars = f"{v_aql}, {r_aql}"
        named_list = rel_named and not r_is_backref and rel_range != (1, 1)
        hop_path: str | None = None
        if rel_range != (1, 1) and (path_name is not None or named_list):
            hop_path = _pick_fresh_var("_hop_path", forbidden_vars=forbidden_vars)
            trav_vars += f", {hop_path}"
            node_segments.append(f"SLICE({hop_path}.vertices, 1)")
            edge_segments.append(f"{hop_path}.edges")
        else:
            node_segments.append(f"[{v_aql}]")
            edge_segments.append(f"[{r_aql}]")
        lines.append(
            f"  FOR {trav_vars} IN {rmin}..{rmax} {direction} {current_aql} {_aql_collection_ref(edge_key)}"
        )
        if named_list and hop_path is not None:
            # A named variable-length relationship is the list of its edges
            # (Match9 [9]), not the traversal's last edge.
            rel_list = _pick_fresh_var(_affix_name(r_aql, suffix="_list"), forbidden_vars=forbidden_vars)
            lines.append(f"    LET {rel_list} = {hop_path}.edges")
            out_env[rel_cy] = rel_list

        v_filters: list[str] = []
        if coll_filter is not None:
            v_filters.append(coll_filter)
        if v_map is not None and v_primary is not None:
            v_style = v_map.get("style")
            if v_style == "LABEL":
                vtf_key = _pick_bind_key("vTypeField", bind_vars)
                vtv_key = _pick_bind_key("vTypeValue", bind_vars)
                bind_vars[vtf_key] = v_map.get("typeField")
                bind_vars[vtv_key] = v_map.get("typeValue")
                v_filters.append(label_test(v_aql, vtf_key, vtv_key, v_map, bind_vars))
                v_filters.extend(_extra_label_filters(v_aql, v_labels, v_primary, bind_vars))
            elif v_style == "MULTI_LABEL":
                v_filters.append(_multi_label_filter(v_aql, v_map, bind_vars))
                v_filters.extend(_extra_label_filters(v_aql, v_labels, v_primary, bind_vars))
            elif v_style == "UNION":
                v_filters.extend(_extra_label_filters(v_aql, v_labels, v_primary, bind_vars))
            elif v_style != "COLLECTION":
                raise CoreError(f"Unsupported entity mapping style: {v_style}", code="INVALID_MAPPING")
            else:
                v_filters.extend(_collection_label_filters(v_aql, v_labels, v_primary, bind_vars))

        r_filters: list[str] = []
        r_style = r_map.get("style")
        if r_style == "GENERIC_WITH_TYPE":
            rtf_key = _pick_bind_key("relTypeField", bind_vars)
            rtv_key = _pick_bind_key("relTypeValue", bind_vars)
            bind_vars[rtf_key] = r_map.get("typeField")
            bind_vars[rtv_key] = r_map.get("typeValue")
            r_filters.append(f"{r_aql}[@{rtf_key}] == @{rtv_key}")
            rel_type_exprs[rel_cy] = f"{r_aql}[@{rtf_key}]"
        elif r_style != "DEDICATED_COLLECTION":
            raise CoreError(f"Unsupported relationship mapping style: {r_style}", code="INVALID_MAPPING")
        else:
            rel_type_exprs[rel_cy] = _dedicated_rel_type_expr(rel_type, r_aql, resolver)

        if rel_range == (1, 1) and not r_is_backref:
            edge_value = bind_vars[edge_key]
            collections = set(edge_value) if isinstance(edge_value, list) else {edge_value}
            r_filters += [f"{r_aql}._id != {other}._id" for other, seen in single_hops if seen & collections]
            single_hops.append((r_aql, collections))

        for f in backref_filters + v_filters + r_filters:
            lines.append(f"    FILTER {f}")
        for f in r_prop_filters:
            lines.append(f"    FILTER {f}")

        # Inline properties on traversed node.
        for f in _compile_node_pattern_properties(v_node, var=v_aql, bind_vars=bind_vars, var_env=out_env):
            lines.append(f"    FILTER {f}")

        current_aql = out_env[v_cy] if v_is_backref else v_aql

    _bind_bound_path(path_name, node_segments, edge_segments, out_env, forbidden_vars, lines, reversed_part)
    return True


def _bind_bound_path(
    path_name: str | None,
    node_segments: list[str],
    edge_segments: list[str],
    out_env: dict[str, str],
    forbidden_vars: set[str],
    lines: list[str],
    reversed_part: bool,
) -> None:
    """Bind *path_name* (if any) to the ``{nodes, edges}`` of a bound part."""
    if path_name is None:
        return
    path_aql = _pick_fresh_var(path_name, forbidden_vars=forbidden_vars)
    nodes = f"FLATTEN([{', '.join(node_segments)}])"
    edges = f"FLATTEN([{', '.join(edge_segments)}])" if edge_segments else "[]"
    if reversed_part:
        nodes, edges = f"REVERSE({nodes})", f"REVERSE({edges})"
    lines.append(f"    LET {path_aql} = {{nodes: {nodes}, edges: {edges}}}")
    out_env[path_name] = path_aql
    _active_path_vars.get()[path_name] = ([], [])


def _compile_match_pipeline(
    match_ctx: CypherParser.OC_MatchContext,
    *,
    resolver: MappingResolver,
    bind_vars: dict[str, Any],
) -> tuple[list[str], set[str]]:
    """
    Compile the MATCH clause into AQL lines, without emitting RETURN.
    Supports:
    - MATCH (n:Label)
    - MATCH (u:Label)-[:TYPE]->(v:Label) (1 hop)
    """
    pattern = match_ctx.oC_Pattern()
    parts = pattern.oC_PatternPart()
    if not parts or len(parts) != 1:
        raise CoreError("Only a single pattern part is supported in v0", code="UNSUPPORTED")
    anon = parts[0].oC_AnonymousPatternPart()
    refuse_unbound_shortest_path(anon)
    elem = anon.oC_PatternElement()
    node = elem.oC_NodePattern()
    if node is None:
        raise CoreError("Only node patterns are supported in v0", code="UNSUPPORTED")
    chains = elem.oC_PatternElementChain() or []

    # Node-only
    if not chains:
        var, labels = _extract_node_var_and_labels(node, default_var="n")
        prop_filters = _compile_node_pattern_properties(node, var=var, bind_vars=bind_vars)
        lines: list[str]
        base_filters: list[str] = []
        if not labels:
            bind_vars["@collection"] = _infer_unlabeled_collection(resolver)
            lines = [f"FOR {var} IN @@collection"]
        else:
            primary = _pick_primary_entity_label(labels, resolver)
            entity_mapping = resolver.resolve_entity(_strip_label_backticks(primary))
            entity_style = entity_mapping.get("style")
            if entity_style == "COLLECTION":
                base_filters.extend(_collection_label_filters(var, labels, primary, bind_vars))
                bind_vars["@collection"] = entity_mapping["collectionName"]
                lines = [f"FOR {var} IN @@collection"]
            elif entity_style == "LABEL":
                bind_vars["@collection"] = entity_mapping["collectionName"]
                bind_vars["typeField"] = entity_mapping["typeField"]
                bind_vars["typeValue"] = entity_mapping["typeValue"]
                lines = [f"FOR {var} IN @@collection"]
                base_filters.append(label_test(var, "typeField", "typeValue", entity_mapping, bind_vars))
                base_filters.extend(_extra_label_filters(var, labels, primary, bind_vars))
            elif entity_style == "MULTI_LABEL":
                bind_vars["@collection"] = entity_mapping["collectionName"]
                lines = [f"FOR {var} IN @@collection"]
                base_filters.append(_multi_label_filter(var, entity_mapping, bind_vars))
                base_filters.extend(_extra_label_filters(var, labels, primary, bind_vars))
            elif entity_style == "UNION":
                source, _ = node_scan_source(
                    var,
                    primary,
                    entity_mapping,
                    resolver,
                    bind_vars,
                    coll_key="@collection",
                    filters=prop_filters,
                )
                lines = [f"FOR {var} IN {source}"]
                prop_filters = []
                base_filters.extend(_extra_label_filters(var, labels, primary, bind_vars))
            else:
                raise CoreError(f"Unsupported entity mapping style: {entity_style}", code="INVALID_MAPPING")

        where_ctx = match_ctx.oC_Where()
        user_filter = _compile_where(where_ctx.oC_Expression(), bind_vars) if where_ctx is not None else None
        filters = list(base_filters)
        if user_filter:
            filters.append(user_filter)
        filters.extend(prop_filters)
        if labels and entity_style != "UNION":
            idx_hint = _build_collection_index_hint(primary, prop_filters, resolver)
            if idx_hint:
                lines.append(f"  {idx_hint}")
        for f in filters:
            lines.append(f"  FILTER {f}")
        return lines, {var}

    # Relationship pattern (1+ hops)
    u_var, u_labels = _extract_node_var_and_labels(node, default_var="u")
    if node.oC_Variable() is None:
        u_var = _anonymous_var(u_var, node, set())
    u_prop_filters = _compile_node_pattern_properties(node, var=u_var, bind_vars=bind_vars)

    u_filters: list[str] = []
    u_source = "@@uCollection"
    if not u_labels:
        bind_vars["@uCollection"] = _infer_unlabeled_collection(resolver)
    else:
        u_primary = _pick_primary_entity_label(u_labels, resolver)
        u_map = resolver.resolve_entity(_strip_label_backticks(u_primary))

        u_source, pushed = node_scan_source(
            u_var, u_primary, u_map, resolver, bind_vars, coll_key="@uCollection", filters=u_prop_filters
        )
        if pushed:
            u_prop_filters = []

        u_style = u_map.get("style")
        if u_style == "LABEL":
            bind_vars["uTypeField"] = u_map.get("typeField")
            bind_vars["uTypeValue"] = u_map.get("typeValue")
            u_filters.append(label_test(u_var, "uTypeField", "uTypeValue", u_map, bind_vars))
            u_filters.extend(_extra_label_filters(u_var, u_labels, u_primary, bind_vars))
        elif u_style == "MULTI_LABEL":
            u_filters.append(_multi_label_filter(u_var, u_map, bind_vars))
            u_filters.extend(_extra_label_filters(u_var, u_labels, u_primary, bind_vars))
        elif u_style == "UNION":
            u_filters.extend(_extra_label_filters(u_var, u_labels, u_primary, bind_vars))
        elif u_style != "COLLECTION":
            raise CoreError(f"Unsupported entity mapping style: {u_style}", code="INVALID_MAPPING")
        else:
            u_filters.extend(_collection_label_filters(u_var, u_labels, u_primary, bind_vars))

    lines = [f"FOR {u_var} IN {u_source}"]
    for f in u_filters:
        lines.append(f"  FILTER {f}")
    for f in u_prop_filters:
        lines.append(f"  FILTER {f}")

    forbidden: set[str] = {u_var}
    current_var = u_var
    last_indent = "    "
    has_varlen = False

    for chain in chains:
        rel_pat = chain.oC_RelationshipPattern()
        v_node = chain.oC_NodePattern()
        if rel_pat is None or v_node is None:
            raise CoreError("Invalid relationship pattern", code="UNSUPPORTED")

        v_var, v_labels = _extract_node_var_and_labels(v_node, default_var="v")
        if v_node.oC_Variable() is None:
            v_var = _anonymous_var(v_var, v_node, forbidden)
        # A back-reference to an already-bound node (e.g. (p)-[:R]->(m)<-[:R2]-(p))
        # must traverse to a *fresh* variable and constrain it by _id, not re-open
        # a FOR loop on the bound name — AQL rejects the duplicate variable
        # ("assigned multiple times").
        v_bound = v_var in forbidden
        v_trav = (
            v_var
            if not v_bound
            else _pick_fresh_var(_affix_name(v_var, suffix="_m"), forbidden_vars=forbidden)
        )

        v_prop_filters = _compile_node_pattern_properties(v_node, var=v_trav, bind_vars=bind_vars)
        rel_type, rel_var, rel_range = _extract_relationship_type_and_var(rel_pat, default_var="r")
        detail = rel_pat.oC_RelationshipDetail()
        rel_named = detail is not None and detail.oC_Variable() is not None
        if not rel_named:
            rel_var = _anonymous_var(rel_var, rel_pat, forbidden)
        elif rel_var in forbidden:
            raise CoreError("Relationship variable must not shadow node variables", code="UNSUPPORTED")
        if rel_var in {u_var, v_var}:
            raise CoreError("Relationship variable must not shadow node variables", code="UNSUPPORTED")

        r_prop_filters = _compile_relationship_pattern_properties(rel_pat, var=rel_var, bind_vars=bind_vars)
        direction = _self_loop_direction(
            _relationship_direction(rel_pat),
            back_to_start=v_bound and v_var == current_var,
            rel_range=rel_range,
        )

        if direction == "ANY" and rel_type:
            stats_dir = resolver.preferred_traversal_direction(rel_type)
            if stats_dir:
                direction = stats_dir

        # Resolve an unlabeled endpoint of a typed relationship from the
        # relationship's domain/range (e.g. (m:Movie)<-[:REVIEWED]-() → Person).
        if not v_labels and rel_type:
            _inferred_v = resolver.infer_endpoint_label(rel_type, _relationship_direction(rel_pat))
            if _inferred_v:
                v_labels = [_inferred_v]

        v_primary: str | None = None
        v_map: dict[str, Any] | None = None
        if v_labels:
            v_primary = _pick_primary_entity_label(v_labels, resolver)
            v_map = resolver.resolve_entity(_strip_label_backticks(v_primary))
        r_map = _resolve_relationship_for_pattern(resolver, rel_type)

        edge_key = _pick_bind_key("@edgeCollection", bind_vars)
        bind_vars[edge_key] = r_map.get("edgeCollectionName") or r_map.get("collectionName")
        if not _is_edge_collection_value(bind_vars[edge_key]):
            raise CoreError(
                f"Invalid relationship mapping collection for: {rel_type}", code="INVALID_MAPPING"
            )

        skip_coll_filter = (
            v_primary is not None
            and rel_type is not None
            and resolver.edge_constrains_target(rel_type, v_primary, direction)
        )
        coll_filter: str | None = None
        if not skip_coll_filter:
            if v_map is None:
                coll_filter = _unlabeled_target_filter(v_trav, resolver, bind_vars, "vCollection")
            else:
                coll_filter = node_collection_filter(
                    v_trav, str(v_primary), v_map, resolver, bind_vars, key_base="vCollection"
                )

        rmin, rmax = rel_range
        if rmin != rmax:
            has_varlen = True
        lines.append(
            f"  FOR {v_trav}, {rel_var} IN {rmin}..{rmax} {direction} {current_var} {_aql_collection_ref(edge_key)}"
        )

        v_filters: list[str] = []
        if v_bound:
            v_filters.append(f"{v_trav}._id == {v_var}._id")
        if coll_filter is not None:
            v_filters.append(coll_filter)
        if v_map is not None and v_primary is not None:
            v_style = v_map.get("style")
            if v_style == "LABEL":
                vtf_key = _pick_bind_key("vTypeField", bind_vars)
                vtv_key = _pick_bind_key("vTypeValue", bind_vars)
                bind_vars[vtf_key] = v_map.get("typeField")
                bind_vars[vtv_key] = v_map.get("typeValue")
                v_filters.append(label_test(v_trav, vtf_key, vtv_key, v_map, bind_vars))
                v_filters.extend(_extra_label_filters(v_trav, v_labels, v_primary, bind_vars))
            elif v_style == "MULTI_LABEL":
                v_filters.append(_multi_label_filter(v_trav, v_map, bind_vars))
                v_filters.extend(_extra_label_filters(v_trav, v_labels, v_primary, bind_vars))
            elif v_style == "UNION":
                v_filters.extend(_extra_label_filters(v_trav, v_labels, v_primary, bind_vars))
            elif v_style != "COLLECTION":
                raise CoreError(f"Unsupported entity mapping style: {v_style}", code="INVALID_MAPPING")
            else:
                v_filters.extend(_collection_label_filters(v_trav, v_labels, v_primary, bind_vars))

        r_filters: list[str] = []
        r_style = r_map.get("style")
        if r_style == "GENERIC_WITH_TYPE":
            rtf_key = _pick_bind_key("relTypeField", bind_vars)
            rtv_key = _pick_bind_key("relTypeValue", bind_vars)
            bind_vars[rtf_key] = r_map.get("typeField")
            bind_vars[rtv_key] = r_map.get("typeValue")
            r_filters.append(f"{rel_var}[@{rtf_key}] == @{rtv_key}")
        elif r_style != "DEDICATED_COLLECTION":
            raise CoreError(f"Unsupported relationship mapping style: {r_style}", code="INVALID_MAPPING")

        for f in v_filters + r_filters:
            lines.append(f"    FILTER {f}")
        for f in r_prop_filters:
            lines.append(f"    FILTER {f}")
        for f in v_prop_filters:
            lines.append(f"    FILTER {f}")

        forbidden.add(v_trav)
        forbidden.add(v_var)
        forbidden.add(rel_var)
        current_var = v_trav
        last_indent = "    "

    where_ctx = match_ctx.oC_Where()
    user_filter = _compile_where(where_ctx.oC_Expression(), bind_vars) if where_ctx is not None else None
    if user_filter:
        _emit_prune_and_filter(
            user_filter,
            current_var,
            forbidden,
            is_varlen=has_varlen,
            lines=lines,
            indent=last_indent,
        )

    return lines, forbidden


_AGGREGATE_CALL_RE = re.compile(r"(count|avg|sum|min|max)\s*\(", re.IGNORECASE)


def _aggregate_call(expr_text: str) -> tuple[str, str, bool] | None:
    """``(function, argument text, distinct)`` when *expr_text* is exactly one
    ``count``/``avg``/``sum``/``min``/``max`` call, else ``None``.

    The call's parenthesis must close at the end of the text:
    ``count(a) + count(b)`` is an expression over two aggregates, not a count.
    ``collect`` is :func:`_parse_collect`'s.
    """
    t = expr_text.strip()
    m = _AGGREGATE_CALL_RE.match(t)
    if m is None or _closing_bracket(t[m.end() - 1 :]) != len(t) - m.end():
        return None
    inner = t[m.end() : -1].strip()
    distinct = False
    if inner[:8].lower() == "distinct" and (len(inner) == 8 or not (inner[8].isalnum() or inner[8] == "_")):
        distinct, inner = True, inner[8:].strip()
    return m.group(1).lower(), inner, distinct


def _compile_agg_expr(expr_text: str, bind_vars: dict[str, Any]) -> tuple[str, str] | None:
    """Compile a ``count``/``avg``/``sum``/``min``/``max`` call to its AQL aggregate.

    The argument goes through the expression compiler like any expression
    (``sum(toInteger(x))``, ``count(n.x = 1)``).
    """
    call = _aggregate_call(expr_text)
    if call is None:
        return None
    fn, inner, distinct = call
    if fn == "count" and inner == "*":
        return ("aggregate", "COUNT(1)")
    arg = _compile_cypher_fragment(inner, bind_vars)
    if fn == "count":
        return ("aggregate", f"COUNT_DISTINCT({arg})" if distinct else f"COUNT({arg})")
    return ("aggregate", f"{fn.upper()}({arg})")


def _compile_cypher_fragment(text: str, bind_vars: dict[str, Any]) -> str:
    """Compile the Cypher expression *text* (an aggregate's argument)."""
    parsed = parse_cypher(f"RETURN {text}")
    single_query = parsed.tree.oC_Statement().oC_Query().oC_RegularQuery().oC_SingleQuery()
    projection_item = (
        single_query.oC_SinglePartQuery()
        .oC_Return()
        .oC_ProjectionBody()
        .oC_ProjectionItems()
        .oC_ProjectionItem(0)
    )
    return _compile_expression(projection_item.oC_Expression(), bind_vars)


def _parse_collect(expr_text: str, ctx: Any = None) -> dict[str, Any] | None:
    """Parse a ``collect(...)`` projection, including ``DISTINCT`` and an
    optional trailing subscript/slice (``collect(DISTINCT x)[0..5]``).

    Returns ``{"inner", "distinct", "slice"}`` where ``slice`` is ``None``,
    ``("index", expr_text)``, or ``("slice", start_text|None, end_text|None)``.
    Returns ``None`` if *expr_text* is not a collect call. A percentile call,
    and a ``min``/``max`` the projection item *ctx* shows must order as Cypher
    does, are gathered the same way (``percentile``/``extreme`` in the spec).
    """
    t = expr_text.strip()
    if not t[:7].lower() == "collect":
        # percentileDisc/Cont, ordered min/max: a collect finished after grouping.
        return parse_percentile(t) or (extreme_spec(t, ctx) if ctx is not None else None)
    i = 7
    while i < len(t) and t[i].isspace():
        i += 1
    if i >= len(t) or t[i] != "(":
        return None
    depth = 0
    j = i
    while j < len(t):
        if t[j] == "(":
            depth += 1
        elif t[j] == ")":
            depth -= 1
            if depth == 0:
                break
        j += 1
    if depth != 0:
        return None
    inner = t[i + 1 : j].strip()
    rest = t[j + 1 :].strip()
    distinct = False
    if inner.lower().startswith("distinct"):
        tail = inner[len("distinct") :]
        if tail == "" or tail[0].isspace():
            distinct = True
            inner = tail.strip()
    slice_spec: tuple[Any, ...] | None = None
    if rest:
        if not (rest.startswith("[") and rest.endswith("]")):
            return None
        sub = rest[1:-1]
        if ".." in sub:
            left, _, right = sub.partition("..")
            slice_spec = ("slice", left.strip() or None, right.strip() or None)
        else:
            slice_spec = ("index", sub.strip())
    return {"inner": inner, "distinct": distinct, "slice": slice_spec}


def _compile_collect_inner(spec: dict[str, Any], bind_vars: dict[str, Any]) -> dict[str, Any]:
    """Compile a parsed ``collect`` argument through the expression compiler.

    Aggregates are recognized from projection text because their result changes
    the AQL pipeline shape.  Their argument must still use the normal AST
    compiler: passing its raw Cypher text to ``PUSH`` leaves operators such as
    ``=`` and ``<>`` invalid in AQL (notably in precedence test cases).
    """
    compiled = dict(spec)
    compiled["inner"] = _compile_cypher_fragment(spec["inner"], bind_vars)
    if spec.get("percentile") is not None:
        kind, p_text = spec["percentile"]
        compiled["percentile"] = (kind, _compile_cypher_fragment(p_text, bind_vars))
    return compiled


def _collect_post_expr(raw_var: str, spec: dict[str, Any]) -> str:
    """Build the AQL value expression for a parsed collect projection.

    ``raw_var`` holds the list gathered by ``COLLECT ... INTO raw_var = inner``.
    Drops nulls (Cypher's ``collect()`` skips them — a null-extended OPTIONAL
    MATCH row contributes nothing), then applies DISTINCT (``UNIQUE``) and any
    slice/index. Slice bounds are emitted verbatim, which is correct for the
    common non-negative literal bounds; a negative slice bound is a known
    limitation (documented in the coverage plan).
    """
    if spec.get("percentile") is not None:
        kind, p = spec["percentile"]
        return percentile_value(raw_var, kind, p)
    if spec.get("extreme") is not None:
        return extreme_value(raw_var, spec["extreme"])
    e = f"REMOVE_VALUE({raw_var}, null)"
    if spec.get("distinct"):
        e = f"UNIQUE({e})"
    sl = spec.get("slice")
    if sl is not None:
        if sl[0] == "index":
            e = f"({e})[{sl[1]}]"
        else:
            _, start, end = sl
            if start is not None and end is not None:
                e = f"SLICE({e}, {start}, ({end}) - ({start}))"
            elif start is not None:
                e = f"SLICE({e}, {start})"
            elif end is not None:
                e = f"SLICE({e}, 0, {end})"
    return e


def _aggregate_clause(
    compiled_agg: list[tuple[str, str, str]],
    reserve: Any,
) -> tuple[list[str], list[tuple[str, str]]]:
    """``COLLECT … AGGREGATE`` assignments and post-COLLECT ``LET``s for a
    projection's aggregates, with Cypher's null semantics.

    Each aggregate is lowered by :func:`lower_aggregate`. A projection with a
    ``collect()`` goes through :func:`_collect_into_rows` instead: ``PUSH`` is
    not a valid AGGREGATE expression on ArangoDB 3.11 (ERR 1574). *reserve*
    returns a fresh, now-used name.
    """
    pieces: list[str] = []
    post_lets: list[tuple[str, str]] = []
    for _key, aql, expr in compiled_agg:
        agg, post = lower_aggregate(aql, expr, reserve)
        pieces.extend(agg)
        if post is not None:
            post_lets.append((aql, post))
    return pieces, post_lets


def _collect_into_rows(
    group_parts: str,
    compiled_agg: list[tuple[str, str, str]],
    compiled_collects: list[tuple[str, str, dict[str, Any]]],
    rows_var: str,
) -> tuple[str, list[tuple[str, str]]]:
    """The ``COLLECT … INTO`` line and the ``LET``s that finish each aggregate.

    For a projection with a ``collect()``: each collect input and each other
    aggregate's input is kept in the group's rows, and the aggregates are
    computed from the rows after grouping (:func:`_aggregate_from_collected_rows`).
    """
    projection_parts = [f"{aql}: {spec['inner']}" for _, aql, spec in compiled_collects]
    aggregate_fields: list[tuple[str, str, str | None]] = []
    for _key, aql_var, agg_expr in compiled_agg:
        field: str | None = None
        if agg_expr != "COUNT(1)":
            field = _affix_name(aql_var, prefix="_", suffix="_input")
            projection_parts.append(f"{field}: {agg_expr[agg_expr.find('(') + 1 : -1]}")
        aggregate_fields.append((aql_var, agg_expr, field))
    line = (
        f"  COLLECT {group_parts or '_collect_group = 1'} INTO {rows_var} = {{{', '.join(projection_parts)}}}"
    )
    lets = [(aql, _collect_post_expr(f"{rows_var}[*].{aql}", spec)) for _, aql, spec in compiled_collects]
    lets += [
        (aql, _aggregate_from_collected_rows(agg_expr, rows_var, field))
        for aql, agg_expr, field in aggregate_fields
    ]
    return line, lets


def _hoist_nested_aggregates(
    expr_ctx: Any,
    *,
    incoming_env: dict[str, str] | None,
    bind_vars: dict[str, Any],
    reserve: Callable[[str], str],
    compiled_agg: list[tuple[str, str, str]],
    compiled_collects: list[tuple[str, str, dict[str, Any]]],
) -> dict[int, str]:
    """Add each aggregate nested in a projection expression to the COLLECT.

    ``head(collect(x))``, ``100 * count(x)``, ``list + collect(x)``: each
    aggregate call becomes a COLLECT variable of its own, and the expression
    is evaluated after grouping with the call standing for that variable
    (:func:`aggregate_variables`). Returns node id → variable; empty when the
    expression has no aggregate.
    """
    variables: dict[int, str] = {}
    for node in nested_aggregates(expr_ctx):
        text = node.getText().strip()
        if incoming_env:
            text = _rewrite_vars(text, incoming_env)
        aql_var = reserve("_agg")
        spec = _parse_collect(text)
        if spec is not None:
            compiled_collects.append((aql_var, aql_var, _compile_collect_inner(spec, bind_vars)))
        else:
            agg = _compile_agg_expr(text, bind_vars)
            if agg is None:
                raise CoreError(f"Unsupported aggregate in an expression: {text}", code="UNSUPPORTED")
            compiled_agg.append((aql_var, aql_var, agg[1]))
        variables[id(node)] = aql_var
    return variables


def _grouped_expression_vars(compiled_nonagg: list[tuple[str, str, str]]) -> dict[str, str]:
    """Grouping expression → the variable COLLECT binds it to (when renamed)."""
    return {expr: aql for _, aql, expr in compiled_nonagg if expr != aql}


def _aggregate_from_collected_rows(agg_expr: str, rows_var: str, field: str | None) -> str:
    """Rebuild a supported Cypher aggregate from an AQL COLLECT INTO group."""
    match = re.fullmatch(r"(COUNT|COUNT_DISTINCT|AVG|SUM|MIN|MAX)\((.*)\)", agg_expr.strip())
    if match is None:
        raise CoreError(f"Unsupported aggregate expression: {agg_expr}", code="UNSUPPORTED")
    fn, inner = match.group(1), match.group(2).strip()
    if fn == "COUNT" and inner == "1":
        return f"LENGTH({rows_var})"
    if field is None:
        raise CoreError(f"Aggregate {agg_expr} requires an input field", code="UNSUPPORTED")

    values = f"FOR _agg_row IN {rows_var} FILTER _agg_row.{field} != null RETURN _agg_row.{field}"
    if fn == "COUNT":
        return f"LENGTH({values})"
    if fn == "COUNT_DISTINCT":
        return f"LENGTH(UNIQUE({values}))"
    return f"{fn}({rows_var}[*].{field})"


# A plain variable, or a backtick-quoted one (```a b```, or a reserved word such
# as ```like``` quoted by ``_quote_aql_reserved_names``).
_BARE_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|`[^`]+`")


def _apply_with(
    with_ctx: CypherParser.OC_WithContext,
    *,
    lines: list[str],
    bind_vars: dict[str, Any],
    forbidden_vars: set[str],
    incoming_env: dict[str, str] | None = None,
) -> dict[str, str]:
    """Compile a WITH stage; see :func:`_apply_with_projection`. Tracks the
    variables it binds to temporal values, whose ``.year`` etc. are lowered."""
    during, after = bind_with(with_ctx)
    consts_during, consts_after = bind_with_constants(with_ctx)
    token = _temporal_vars.set(during)
    const_token = _const_env.set(consts_during)
    try:
        env = _apply_with_projection(
            with_ctx,
            lines=lines,
            bind_vars=bind_vars,
            forbidden_vars=forbidden_vars,
            incoming_env=incoming_env,
        )
    except BaseException:
        _temporal_vars.reset(token)
        _const_env.reset(const_token)
        raise
    _temporal_vars.set(after)
    _const_env.set(consts_after)
    return env


def _apply_with_projection(
    with_ctx: CypherParser.OC_WithContext,
    *,
    lines: list[str],
    bind_vars: dict[str, Any],
    forbidden_vars: set[str],
    incoming_env: dict[str, str] | None = None,
) -> dict[str, str]:
    proj = with_ctx.oC_ProjectionBody()
    distinct = proj.DISTINCT() is not None

    items_ctx = proj.oC_ProjectionItems()
    items = items_ctx.oC_ProjectionItem()
    is_star = _is_star_projection(items_ctx)
    if not items and not is_star:
        raise CoreError("WITH projection items required", code="UNSUPPORTED")

    if is_star:
        # `WITH *` carries every in-scope variable forward unchanged; an
        # optional trailing WHERE becomes a FILTER. `WITH *, <items>` and
        # DISTINCT/ORDER/SKIP/LIMIT on `*` are deferred (refused, not
        # mis-compiled).
        if items:
            raise CoreError(
                "WITH *, <projection items> is not supported; list variables explicitly",
                code="NOT_IMPLEMENTED",
            )
        if (
            distinct
            or proj.oC_Order() is not None
            or proj.oC_Skip() is not None
            or proj.oC_Limit() is not None
        ):
            raise CoreError(
                "WITH * with DISTINCT/ORDER BY/SKIP/LIMIT is not supported",
                code="NOT_IMPLEMENTED",
            )
        env = dict(incoming_env) if incoming_env else {v: v for v in forbidden_vars}
        where_ctx = with_ctx.oC_Where()
        if where_ctx is not None:
            cond = _compile_where(where_ctx.oC_Expression(), bind_vars)
            if env:
                cond = _rewrite_vars(cond, env)
            lines.append(f"  FILTER {cond}")
        return env

    compiled_nonagg: list[tuple[str, str, str]] = []  # (cypher_var, aql_var, expr)
    compiled_agg: list[tuple[str, str, str]] = []  # (cypher_var, aql_var, agg_expr)
    # collect() → AGGREGATE PUSH/UNIQUE parts; a list so multiple collect()
    # coexist (AQL allows many AGGREGATE parts, only one INTO).
    compiled_collects: list[tuple[str, str, dict[str, Any]]] = []  # (cypher_var, aql_var, spec)
    # Expressions over nested aggregates, evaluated after grouping, and the
    # COLLECT variables of those aggregates, which are not projected.
    compiled_post: list[tuple[str, str, str]] = []  # (cypher_var, aql_var, expr)
    hidden: set[str] = set()
    # Each whole-item aggregate's Cypher text -> its variable, for ORDER BY.
    aggregate_items: dict[str, str] = {}
    pre_existing = set(forbidden_vars)

    def pick_name(cypher_var: str) -> str:
        if cypher_var not in forbidden_vars:
            return cypher_var
        i = 1
        while _affix_name(cypher_var, suffix=f"_{i}") in forbidden_vars:
            i += 1
        return _affix_name(cypher_var, suffix=f"_{i}")

    def reserve(name: str) -> str:
        picked = pick_name(name)
        forbidden_vars.add(picked)
        return picked

    for it in items:
        expr_ctx = it.oC_Expression()
        expr_txt = expr_ctx.getText().strip()
        expr_txt_for_agg = _rewrite_vars(expr_txt, incoming_env) if incoming_env else expr_txt
        alias = it.oC_Variable().getText().strip() if it.oC_Variable() is not None else None

        collect_spec = _parse_collect(expr_txt_for_agg, expr_ctx)
        if collect_spec is not None:
            collect_spec = _compile_collect_inner(collect_spec, bind_vars)
            cypher_var = alias or f"collected{len(compiled_collects) + 1}"
            aql_var = pick_name(cypher_var)
            forbidden_vars.add(aql_var)
            compiled_collects.append((cypher_var, aql_var, collect_spec))
            aggregate_items[aggregate_key(expr_txt)] = aql_var
            continue

        agg = _compile_agg_expr(expr_txt_for_agg, bind_vars)
        if agg is not None:
            kind, agg_expr = agg
            cypher_var = (
                alias or _infer_key(expr_txt) or f"expr{len(compiled_nonagg) + len(compiled_agg) + 1}"
            )
            aql_var = pick_name(cypher_var)
            forbidden_vars.add(aql_var)
            compiled_agg.append((cypher_var, aql_var, agg_expr))
            aggregate_items[aggregate_key(expr_txt)] = aql_var
            continue

        nested = _hoist_nested_aggregates(
            expr_ctx,
            incoming_env=incoming_env,
            bind_vars=bind_vars,
            reserve=reserve,
            compiled_agg=compiled_agg,
            compiled_collects=compiled_collects,
        )
        if nested:
            hidden.update(nested.values())
            with aggregate_variables(nested):
                post = _compile_expression(expr_ctx, bind_vars)
            if incoming_env:
                post = _rewrite_vars(post, incoming_env)
            cypher_var = (
                alias or _infer_key(expr_txt) or f"expr{len(compiled_nonagg) + len(compiled_post) + 1}"
            )
            compiled_post.append((cypher_var, reserve(cypher_var), post))
            continue

        expr = _compile_expression(expr_ctx, bind_vars)
        if incoming_env:
            expr = _rewrite_vars(expr, incoming_env)
        # The projected name comes from the Cypher text: after an earlier WITH
        # renamed ``b`` to ``b_1``, ``WITH b`` still projects ``b``.
        is_bare_var = _BARE_IDENTIFIER_RE.fullmatch(expr_txt) is not None
        cypher_var = (
            alias
            or (expr_txt if is_bare_var else None)
            or _infer_key(expr)
            or f"expr{len(compiled_nonagg) + len(compiled_agg) + 1}"
        )
        # A pass-through (WITH u) introduces no binding: u keeps its AQL name.
        if alias is None and is_bare_var and _BARE_IDENTIFIER_RE.fullmatch(expr):
            compiled_nonagg.append((cypher_var, expr, expr))
            continue

        aql_var = cypher_var if cypher_var not in forbidden_vars else pick_name(cypher_var)
        forbidden_vars.add(aql_var)
        compiled_nonagg.append((cypher_var, aql_var, expr))

    # If this WITH performs a COLLECT (aggregation *or* DISTINCT), any
    # non-aggregate projected variable is (re)bound by COLLECT, so it must not
    # reuse a pre-existing FOR variable — ``WITH DISTINCT m`` otherwise emits
    # ``COLLECT m = m`` (ERR 1511, "assigned multiple times").
    if compiled_agg or compiled_collects or distinct:
        adjusted: list[tuple[str, str, str]] = []
        for cypher_var, aql_var, expr in compiled_nonagg:
            if aql_var in pre_existing:
                new_name = pick_name(cypher_var)
                forbidden_vars.add(new_name)
                adjusted.append((cypher_var, new_name, expr))
            else:
                adjusted.append((cypher_var, aql_var, expr))
        compiled_nonagg = adjusted

    where_ctx = with_ctx.oC_Where()
    with_filter_raw = _compile_where(where_ctx.oC_Expression(), bind_vars) if where_ctx is not None else None

    order_ctx = proj.oC_Order()
    skip_value, limit_value = _parse_skip_limit(proj, bind_vars)

    env = {cy: aql for cy, aql, _ in compiled_nonagg}
    env.update({cy: aql for cy, aql, _ in compiled_agg if cy not in hidden})
    env.update({cy: aql for cy, aql, _ in compiled_post})
    # After a COLLECT only its keys remain: ORDER BY a grouping key's own
    # expression (``WITH a.name AS name … ORDER BY a.name``) reads the key.
    grouped = _grouped_expression_vars(compiled_nonagg)
    if order_ctx is not None and incoming_env is not None:
        projected = set(env) | {cy for cy, _, _ in compiled_collects if cy not in hidden}
        _refuse_out_of_scope_sort_vars(order_ctx, visible=set(incoming_env) | projected)

    # AQL 3.11 does not accept PUSH()/UNIQUE() as COLLECT AGGREGATE
    # expressions. For collect-only projections, preserve each collect input
    # in the INTO projection and derive its list after grouping instead.
    if compiled_collects:
        group_parts = ", ".join(f"{aql} = {expr}" for _, aql, expr in compiled_nonagg)
        collect_line, collect_lets = _collect_into_rows(
            group_parts, compiled_agg, compiled_collects, reserve("_collect_rows")
        )
        lines.append(collect_line)
        for aql_var, finish in collect_lets:
            lines.append(f"  LET {aql_var} = {finish}")
        env.update({cy: aql for cy, aql, _spec in compiled_collects if cy not in hidden})
        _append_post_aggregate_lets(lines, compiled_post, compiled_nonagg)

        with_filter = _rewrite_vars(with_filter_raw, {**grouped, **env}) if with_filter_raw else None
        if with_filter:
            lines.append(f"  FILTER {with_filter}")
        if order_ctx is not None:
            lines.append(
                "  "
                + _compile_order_by(
                    order_ctx, bind_vars, var_env=env, grouped=grouped, aggregates=aggregate_items
                )
            )
        _append_skip_limit(lines, skip_value, limit_value)
        _active_user_vars.set(list(env.keys()))
        return env

    # Aggregation path (AGGREGATE and/or collect). Each collect() lowers to an
    # AGGREGATE PUSH (UNIQUE for DISTINCT); a trailing slice/index becomes a
    # post-COLLECT LET over a PUSH temp. Multiple collect() coexist as multiple
    # AGGREGATE parts.
    if compiled_agg:
        group_parts = ", ".join(f"{aql} = {expr}" for _, aql, expr in compiled_nonagg)
        agg_pieces, post_lets = _aggregate_clause(compiled_agg, reserve)
        agg_parts = ", ".join(agg_pieces)

        collect_line = "  COLLECT"
        if group_parts:
            collect_line += f" {group_parts}"
        if agg_parts:
            collect_line += f" AGGREGATE {agg_parts}"

        lines.append(collect_line)
        for pk, pe in post_lets:
            lines.append(f"  LET {pk} = {pe}")
        _append_post_aggregate_lets(lines, compiled_post, compiled_nonagg)

        with_filter = _rewrite_vars(with_filter_raw, {**grouped, **env}) if with_filter_raw else None
        if with_filter:
            lines.append(f"  FILTER {with_filter}")
        if order_ctx is not None:
            lines.append(
                "  "
                + _compile_order_by(
                    order_ctx, bind_vars, var_env=env, grouped=grouped, aggregates=aggregate_items
                )
            )
        _append_skip_limit(lines, skip_value, limit_value)
        _active_user_vars.set(list(env.keys()))
        return env

    # Non-aggregation path: LETs + (optional) DISTINCT (COLLECT)
    if distinct:
        collect_parts = ", ".join(f"{aql} = {expr}" for _, aql, expr in compiled_nonagg)
        # A DISTINCT after a SORT keeps that order (see distinct_order).
        sort_terms = live_sort_terms(lines) if order_ctx is None else None
        restore_order: list[str] = []
        if sort_terms:
            collect, restore_order = order_keeping_collect(
                collect_parts, sort_terms, forbidden_vars=forbidden_vars
            )
            lines.extend(collect)
        else:
            lines.append(f"  COLLECT {collect_parts}")
        with_filter = _rewrite_vars(with_filter_raw, {**grouped, **env}) if with_filter_raw else None
        if with_filter:
            lines.append(f"  FILTER {with_filter}")
        lines.extend(restore_order)
        if order_ctx is not None:
            lines.append("  " + _compile_order_by(order_ctx, bind_vars, var_env=env, grouped=grouped))
            order_ctx = None
    else:
        for _, aql, expr in compiled_nonagg:
            if aql != expr:
                lines.append(f"  LET {aql} = {expr}")
        with_filter = _rewrite_vars(with_filter_raw, {**grouped, **env}) if with_filter_raw else None
        if with_filter:
            lines.append(f"  FILTER {with_filter}")

    if order_ctx is not None:
        lines.append("  " + _compile_order_by(order_ctx, bind_vars, var_env=env))
    _append_skip_limit(lines, skip_value, limit_value)

    _active_user_vars.set(list(env.keys()))
    return env


def _append_post_aggregate_lets(
    lines: list[str],
    compiled_post: list[tuple[str, str, str]],
    compiled_nonagg: list[tuple[str, str, str]],
) -> None:
    """``LET`` each expression over nested aggregates, after the COLLECT.

    Its grouping-key references are rewritten to the variables COLLECT bound
    them to (``WITH tag, list + collect(x)`` may group ``tag`` as ``tag_1``).
    """
    group_vars = _grouped_expression_vars(compiled_nonagg)
    for _cypher_var, aql_var, expr in compiled_post:
        lines.append(f"  LET {aql_var} = {_rewrite_vars(expr, group_vars)}")


def _refuse_out_of_scope_sort_vars(order_ctx: Any, *, visible: set[str]) -> None:
    """Refuse a WITH's ORDER BY that names a variable no longer (or never) in scope.

    ``MATCH (a), (b), (c) WITH a, b WITH a ORDER BY c`` is a Cypher
    UndefinedVariable error, but AQL never drops ``c`` from scope, so the
    translation sorted by it silently. Conservative: a sort item containing a
    pattern or a construct that binds its own variables is not checked.
    """
    local_binders = (
        CypherParser.OC_IdInCollContext,
        CypherParser.OC_ReduceExpressionContext,
        CypherParser.OC_PatternComprehensionContext,
        CypherParser.OC_RelationshipsPatternContext,
    )

    def walk(node: Any, out: set[str]) -> bool:
        if isinstance(node, local_binders):
            return False
        if isinstance(node, CypherParser.OC_AtomContext) and node.oC_Variable() is not None:
            out.add(node.oC_Variable().getText().strip())
        for i in range(node.getChildCount() if hasattr(node, "getChildCount") else 0):
            if not walk(node.getChild(i), out):
                return False
        return True

    for item in order_ctx.oC_SortItem() or []:
        names: set[str] = set()
        if not walk(item.oC_Expression(), names):
            continue
        undefined = sorted(n.strip("`") for n in names if n.strip("`") not in visible and n not in visible)
        if undefined:
            raise CoreError(
                f"ORDER BY refers to variable(s) not in scope after WITH: {', '.join(undefined)}",
                code="UNSUPPORTED",
            )


def _compile_additive(terms: list[Any], ops: list[str], bind_vars: dict[str, Any]) -> str:
    """``t0 ± t1 ± …`` with Cypher's ``+`` (numbers, strings, lists) and ``-``."""
    refuse_temporal_operands(list(terms))
    types = [_static_type(t) for t in terms]
    if additive_chain_propagates_null(types, ops):
        return null_propagating(
            lambda: raw_additive_chain([_compile_expression(t, bind_vars) for t in terms], types, ops)
        )
    return compile_additive_chain(
        [(_compile_expression(t, bind_vars), ty) for t, ty in zip(terms, types, strict=True)], ops
    )


def _maybe_temporal_pair(terms: list[Any]) -> bool:
    """Two operands either of which may be a temporal string or a duration."""
    return len(terms) == 2 and all(_static_type(t) in (None, STRING) for t in terms)


def _maybe_duration_product(terms: list[Any]) -> bool:
    """A possible duration string and a possible number (in either order)."""
    types = [_static_type(t) for t in terms]
    maybe_duration = (None, STRING)
    maybe_number = (None, INT, FLOAT, NUMBER)
    return (types[0] in maybe_duration and types[1] in maybe_number) or (
        types[1] in maybe_duration and types[0] in maybe_number
    )


def _compile_order_by(
    order_ctx: CypherParser.OC_OrderContext,
    bind_vars: dict[str, Any],
    *,
    var_env: dict[str, str] | None = None,
    aliases: dict[str, str] | None = None,
    alias_types: dict[str, str | None] | None = None,
    grouped: dict[str, str] | None = None,
    aggregates: dict[str, str] | None = None,
) -> str:
    """``SORT`` for an ORDER BY. A sort item that is exactly a projection
    alias in *aliases* sorts by that alias's expression, even where a
    variable has the same name (``RETURN a.name AS a ORDER BY a``), and
    *alias_types* gives that expression's static type. After a ``COLLECT``,
    *grouped* maps each grouping key's source expression to the key variable,
    which is all the sort can still read (WithOrderBy2 [22]-[24]), and
    *aggregates* each aggregate the projection computes to its variable, so
    ``ORDER BY max(n.age)`` reads the projected ``max(n.age)``."""
    items = order_ctx.oC_SortItem() or []
    if not items:
        raise CoreError("ORDER BY requires at least one sort item", code="UNSUPPORTED")
    parts: list[str] = []
    for it in items:
        text = it.oC_Expression().getText().strip()
        static_type = _string_kind(it.oC_Expression())
        if aliases and text in aliases:
            expr = aliases[text]
            static_type = (alias_types or {}).get(text)
        else:
            with aggregate_variables(sort_aggregates(it.oC_Expression(), aggregates)):
                expr = _compile_expression(it.oC_Expression(), bind_vars)
            if grouped:
                expr = read_grouping_keys(expr, grouped)
            if var_env:
                expr = _rewrite_vars(expr, var_env)
        descending = it.DESC() is not None or it.DESCENDING() is not None
        if needs_cypher_sort_key(it.oC_Expression()):
            # Type-ranked key: Cypher's order across types, nulls included.
            ranked = cypher_sort_key(expr)
            ascending = it.ASC() is not None or it.ASCENDING() is not None
            parts.append(f"{ranked} DESC" if descending else f"{ranked} ASC" if ascending else ranked)
            continue
        key = _code_point_sort_key(expr, static_type)
        if (
            temporal_strings.enabled()
            and static_type in (None, STRING)
            and _static_type(it.oC_Expression()) in (None, STRING)
        ):
            # A Time orders by its instant on the day, a DateTime by its instant.
            key = temporal_strings.sort_key(expr, key)
        if not _is_obvious_non_null(expr):
            # Cypher sorts null after every value ascending and before every
            # value descending; AQL sorts null below every value. A first key
            # "is null" puts it back.
            parts.append(f"({expr} == null) DESC" if descending else f"({expr} == null)")
        if descending:
            parts.append(f"{key} DESC")
        elif it.ASC() is not None or it.ASCENDING() is not None:
            parts.append(f"{key} ASC")
        else:
            parts.append(key)
    return "SORT " + ", ".join(parts)


#: A property the mapping declares ``string``: not checked against the data
#: (the analyzer declares numeric properties ``string``), so its code-point
#: key is taken only when the value is a string at run time.
DECLARED_STRING = "declared_string"


def _string_kind(ctx: Any) -> str | None:
    """The static type of *ctx*, or ``DECLARED_STRING`` for a property the
    mapping declares a string (see ``arithmetic.declared_static_type``)."""
    static_type = _static_type(ctx)
    if static_type is None and declared_static_type(ctx) == STRING:
        return DECLARED_STRING
    return static_type


def _code_point_sort_key(expr: str, kind: str | None) -> str:
    """A sort key that orders a string as Cypher does, by code point.

    AQL compares strings with the server's ICU collation: case is ignored at
    the first level and ``é`` sorts beside ``e``, so ``Charles_de_Gaulle``
    came before ``Charles_Haughey`` (LDBC BI2's tie-break by tag name). The
    hex of a string's UTF-8 bytes sorts in code-point order, since UTF-8
    keeps it. A value the query shows to be a string (*kind* ``STRING``) is
    converted; one the mapping declares a string (``DECLARED_STRING``) only
    if it is one at run time. Any other value keeps AQL's order, as its ``<``
    does (``_code_point_operands``): sorting and comparing must agree, and a
    run-time check on every unknown value would keep an index from serving
    range filters.
    """
    if kind == STRING:
        return f"TO_HEX({expr})"
    if kind == DECLARED_STRING:
        return f"(IS_STRING({expr}) ? TO_HEX({expr}) : {expr})"
    return expr


def _code_point_operands(left: str, lk: str | None, right: str, rk: str | None) -> tuple[str, str]:
    """``<`` / ``<=`` / ``>`` / ``>=`` operands, compared by code point when a side
    is a string the query shows or the mapping declares (see
    :func:`_code_point_sort_key`). A side that is not certainly a string is
    converted only if it is one at run time."""
    if STRING not in (lk, rk) and DECLARED_STRING not in (lk, rk):
        return left, right

    def side(expr: str, kind: str | None) -> str:
        if kind == STRING:
            return f"TO_HEX({expr})"
        if kind is None or kind == DECLARED_STRING:
            return f"(IS_STRING({expr}) ? TO_HEX({expr}) : {expr})"
        return expr

    return side(left, lk), side(right, rk)


def _order_env(
    var_env: dict[str, str] | None,
    compiled_items: list[tuple[str | None, str]],
) -> dict[str, str]:
    """Env for compiling ORDER BY in a (non-aggregation) RETURN.

    Cypher lets ``ORDER BY`` reference a projection alias (``RETURN size(r.roles)
    AS n ORDER BY n``), but in AQL the ``SORT`` precedes the ``RETURN`` so the
    alias is not yet a bound variable — emitting ``SORT n`` makes AQL read ``n``
    as a collection ("collection or view not found: n"). Mapping each alias to
    its already-compiled expression inlines it (``SORT LENGTH(r.roles)``), which
    references the still-live FOR variables.
    """
    env: dict[str, str] = dict(var_env) if var_env else {}
    for alias, expr in compiled_items:
        if alias and alias not in env:
            env[alias] = expr
    return env


def _is_star_projection(items_ctx: CypherParser.OC_ProjectionItemsContext) -> bool:
    """Whether the projection is a ``*`` (RETURN */WITH *) — the ``*`` token
    produces no ``oC_ProjectionItem`` children, so it's detected by the leading
    ``*`` in the rule's text (``*`` or ``*, extra``)."""
    return items_ctx.getText().lstrip().startswith("*")


def _pattern_parts_user_vars(parts: list) -> list[str]:
    """Ordered, de-duplicated list of *user-named* variables in a set of
    pattern parts (named path var, node vars, relationship vars) — the
    variables a following ``RETURN *`` / ``WITH *`` should surface. Anonymous
    nodes / relationships (no ``oC_Variable``) contribute nothing."""
    out: list[str] = []
    seen: set[str] = set()

    def add(name: str | None) -> None:
        n = (name or "").strip()
        if n and n not in seen:
            seen.add(n)
            out.append(n)

    for part in parts:
        pv = part.oC_Variable() if hasattr(part, "oC_Variable") else None
        if pv is not None:  # named path: MATCH p = (...)
            add(pv.getText())
        anon = part.oC_AnonymousPatternPart()
        if anon is None:
            continue
        elem = pattern_element(anon)
        node = elem.oC_NodePattern()
        if node is not None and node.oC_Variable() is not None:
            add(node.oC_Variable().getText())
        for chain in elem.oC_PatternElementChain() or []:
            rp = chain.oC_RelationshipPattern()
            detail = rp.oC_RelationshipDetail() if rp is not None else None
            if detail is not None and detail.oC_Variable() is not None:
                add(detail.oC_Variable().getText())
            tn = chain.oC_NodePattern()
            if tn is not None and tn.oC_Variable() is not None:
                add(tn.oC_Variable().getText())
    return out


def _set_user_vars_from_parts(parts: list) -> None:
    """Set the in-scope user-variable list (for ``*`` expansion) from a MATCH
    pattern, appending to any already in scope (multiple MATCH clauses)."""
    current = list(_active_user_vars.get())
    for v in _pattern_parts_user_vars(parts):
        if v not in current:
            current.append(v)
    _active_user_vars.set(current)


def _expand_star_projection(var_env: dict[str, str] | None) -> list[tuple[str, str]]:
    """Expand ``*`` to ``(cypher_var, aql_expr)`` for every user variable in
    scope, in declaration order. The user-variable list is tracked in
    :data:`_active_user_vars` (auto-generated traversal / anonymous-node
    bindings are never added there), mapped to their AQL names via *var_env*."""
    user_vars = [v for v in _active_user_vars.get() if v and not v.startswith("_")]
    if not user_vars:
        raise CoreError("RETURN * / WITH * requires variables in scope", code="UNSUPPORTED")
    env = var_env or {}
    return [(v, env.get(v, v)) for v in user_vars]


def _append_return(
    proj: CypherParser.OC_ProjectionBodyContext,
    *,
    lines: list[str],
    bind_vars: dict[str, Any],
    var_env: dict[str, str] | None = None,
    rel_type_exprs: dict[str, str] | None = None,
) -> None:
    pending = _pending_unwinds.get()
    if pending is not None:
        # ``MATCH … UNWIND … RETURN``: the UNWIND loops come before the
        # projection's COLLECT/SORT/LIMIT, and their variables are in scope.
        _pending_unwinds.set(None)
        unwind_lines, unwind_vars = pending
        lines.extend(unwind_lines)
        _active_user_vars.set(
            [*_active_user_vars.get(), *(v for v in unwind_vars if v not in _active_user_vars.get())]
        )
    distinct = proj.DISTINCT() is not None
    order_ctx = proj.oC_Order()

    skip_value, limit_value = _parse_skip_limit(proj, bind_vars)

    items_ctx = proj.oC_ProjectionItems()
    items = items_ctx.oC_ProjectionItem()
    is_star = _is_star_projection(items_ctx)
    if not items and not is_star:
        raise CoreError("RETURN items required", code="UNSUPPORTED")

    # `RETURN *` / `RETURN *, extra` expands to every user variable in scope.
    star_items = _expand_star_projection(var_env) if is_star else []

    # --- Aggregation detection ---
    has_agg = False
    for it in items:
        expr_txt = it.oC_Expression().getText().strip()
        if var_env:
            expr_txt = _rewrite_vars(expr_txt, var_env)
        if _aggregate_call(expr_txt) is not None or _parse_collect(expr_txt) is not None:
            has_agg = True
            break
        if nested_aggregates(it.oC_Expression()):
            has_agg = True
            break

    if has_agg:
        if is_star:
            raise CoreError(
                "RETURN * combined with aggregation is not supported",
                code="NOT_IMPLEMENTED",
            )
        _append_return_aggregation(
            items,
            lines=lines,
            bind_vars=bind_vars,
            var_env=var_env,
            order_ctx=order_ctx,
            skip_value=skip_value,
            limit_value=limit_value,
        )
        return

    # --- Non-aggregation path ---
    compiled_items: list[tuple[str | None, str]] = list(star_items)
    # Positions whose column name was supplied here rather than by an ``AS``:
    # a single such item still returns a scalar, as an unaliased one does.
    implicit_names: set[int] = set()
    # The static type of each aliased projection, for ORDER BY of the alias.
    item_types: dict[str, str | None] = {}
    # Position -> Cypher's column name, under ``cypher_column_names``.
    written_names: dict[int, str] = {}
    for it in items:
        expr_ctx = it.oC_Expression()
        alias = it.oC_Variable().getText().strip() if it.oC_Variable() is not None else None
        written = written_column(it)
        if written is not None:
            written_names[len(compiled_items)] = written
        if alias is not None:
            item_types[alias] = _string_kind(expr_ctx)
        expr_txt = expr_ctx.getText().strip()
        if rel_type_exprs and expr_txt.lower().startswith("type(") and expr_txt.endswith(")"):
            inner = expr_txt[5:-1].strip()
            if inner in rel_type_exprs:
                if alias is None:
                    implicit_names.add(len(compiled_items))
                compiled_items.append((alias or "type", rel_type_exprs[inner]))
                continue

        expr = _compile_expression(expr_ctx, bind_vars)
        if var_env:
            expr = _rewrite_vars(expr, var_env)
        if alias is None and expr != expr_txt and _BARE_IDENTIFIER_RE.fullmatch(expr_txt):
            # A variable bound under another AQL name (``r`` as ``r_1``) keeps
            # its Cypher name as the column.
            implicit_names.add(len(compiled_items))
            alias = expr_txt
        compiled_items.append((alias, expr))

    if distinct:
        # `RETURN *` always projects named columns, so it uses the object form
        # even for a single variable (a scalar COLLECT would drop the name).
        single = len(compiled_items) == 1 and not is_star and not _object_rows.get()
        if single and order_ctx is None and live_sort_terms(lines):
            # Rows sorted by an earlier WITH: AQL's RETURN DISTINCT keeps the
            # first of each value in order, where COLLECT would re-sort them.
            append_distinct_return(
                lines,
                compiled_items[0][1],
                skip_value=skip_value,
                limit_value=limit_value,
                append_skip_limit=_append_skip_limit,
            )
        elif single:
            alias, expr = compiled_items[0]
            col_var = alias or _infer_key(expr) or "value"
            # `RETURN DISTINCT p` infers the key `p`, and `COLLECT p = p`
            # re-declares the loop variable: ERR 1511 at execution, for every
            # bare-variable DISTINCT. An alias naming any in-scope variable
            # collides the same way. The column is scalar, so the collect
            # variable's name never reaches the result — rename on collision.
            declared = _declared_aql_vars(lines)
            if col_var in declared:
                col_var = _pick_fresh_var(_affix_name(col_var, suffix="_distinct"), forbidden_vars=declared)
            lines.append(f"  COLLECT {col_var} = {expr}")
            if order_ctx is not None:
                # After the COLLECT only the column is in scope: the sort item
                # (the alias or the projected expression) is the column.
                sort_line = _compile_order_by(order_ctx, bind_vars, var_env=var_env)
                column_env = {expr: col_var, **({alias: col_var} if alias else {})}
                lines.append("  " + _rewrite_vars(sort_line, column_env))
            _append_skip_limit(lines, skip_value, limit_value)
            lines.append(f"  RETURN {col_var}")
        else:
            if order_ctx is not None:
                lines.append(
                    "  "
                    + _compile_order_by(
                        order_ctx,
                        bind_vars,
                        var_env=_order_env(var_env, compiled_items),
                        aliases={a: e for a, e in compiled_items if a},
                        alias_types=item_types,
                    )
                )
            append_distinct_return(
                lines,
                _compile_return_object(compiled_items, written_names),
                skip_value=skip_value,
                limit_value=limit_value,
                append_skip_limit=_append_skip_limit,
            )
        return

    if order_ctx is not None:
        lines.append(
            "  "
            + _compile_order_by(
                order_ctx,
                bind_vars,
                var_env=_order_env(var_env, compiled_items),
                aliases={a: e for a, e in compiled_items if a},
                alias_types=item_types,
            )
        )
    _append_skip_limit(lines, skip_value, limit_value)

    if (
        len(compiled_items) == 1
        and (compiled_items[0][0] is None or 0 in implicit_names)
        and not _object_rows.get()
    ):
        lines.append(f"  RETURN {compiled_items[0][1]}")
        return

    lines.append("  RETURN " + _compile_return_object(compiled_items, written_names))


# Identifiers referenced as variables in a projection expression: a bare name or
# the leading part of a dotted access (``director`` in ``director.name``), but not
# a function call (``collect(``) or a property suffix (``.name``).
_REFERENCED_IDENT_RE = re.compile(r"(?<![\w.])([A-Za-z_]\w*)(?!\s*\()")


def _append_return_aggregation(
    items: list,
    *,
    lines: list[str],
    bind_vars: dict[str, Any],
    var_env: dict[str, str] | None = None,
    order_ctx: Any = None,
    skip_value: str | None = None,
    limit_value: str | None = None,
) -> None:
    # Each entry is (projection_key, aql_var, expr). The projection key is the
    # Cypher-visible name (RETURN {key: ...}); the AQL var is what the COLLECT
    # actually binds. They differ when the natural name collides with a live FOR
    # variable — ``COLLECT director = director.name`` is rejected by AQL
    # ("variable 'director' is assigned multiple times"), so the group var is
    # reallocated to a fresh name while the projection key stays ``director``.
    compiled_nonagg: list[tuple[str, str, str]] = []
    compiled_agg: list[tuple[str, str, str]] = []
    # Each collect() is lowered to an AGGREGATE PUSH/UNIQUE part; a list (not a
    # single slot) so multiple collect() in one projection are supported —
    # AQL allows many AGGREGATE parts but only one INTO.
    compiled_collects: list[tuple[str, str, dict[str, Any]]] = []
    # Expressions over nested aggregates (``head(collect(x))``), evaluated
    # after grouping, and the COLLECT variables of those aggregates.
    compiled_post: list[tuple[str, str, str]] = []
    hidden: set[str] = set()
    # Grouping expression (with renamed variables) → its form in Cypher names:
    # ``ORDER BY person.id`` must find ``person_3.id``'s group variable.
    cypher_level: dict[str, str] = {}

    # Names live *before* the COLLECT: the enclosing FOR variables. A COLLECT
    # group/aggregate/INTO variable must not reuse any of them. ``var_env`` carries
    # them after a WITH, but a plain ``MATCH … RETURN <agg>`` passes an empty env,
    # so also harvest the variables referenced by the projection expressions
    # themselves (``director.name`` references the live ``director``) — a group var
    # matching one of those would collide.
    live_names: set[str] = set(var_env.values()) if var_env else set()
    for _it in items:
        _t = _it.oC_Expression().getText()
        if var_env:
            _t = _rewrite_vars(_t, var_env)
        live_names.update(_REFERENCED_IDENT_RE.findall(_t))

    used_keys: set[str] = set()
    # Column keys in projection order: the RETURN object keeps it, as Cypher's
    # columns do (collect() columns were emitted after the other aggregates).
    column_order: list[str] = []
    # Projection key -> Cypher's column name, under ``cypher_column_names``;
    # the key itself still names the AQL variable.
    written_names: dict[str, str] = {}
    current_written: list[str | None] = [None]

    def claim_key(key: str, expr_text: str, used: set[str]) -> str:
        claimed = _unique_projection_key(key, expr_text, used)
        column_order.append(claimed)
        if current_written[0] is not None:
            written_names[claimed] = current_written[0]
        return claimed

    def _alloc(preferred: str) -> str:
        name = preferred
        i = 1
        while name in live_names:
            name = _affix_name(preferred, suffix=f"_g{i}")
            i += 1
        live_names.add(name)
        return name

    # Each whole-item aggregate's Cypher text -> its variable, for ORDER BY.
    return_aggregates: dict[str, str] = {}
    for it in items:
        expr_ctx = it.oC_Expression()
        alias = it.oC_Variable().getText().strip() if it.oC_Variable() is not None else None
        current_written[0] = written_column(it)
        expr_txt = expr_ctx.getText().strip()
        # The column of an unaliased item is named in Cypher: ``person``, not
        # the ``person_1`` it is bound to after a rename.
        cypher_key = _infer_key(expr_txt)
        if var_env:
            expr_txt = _rewrite_vars(expr_txt, var_env)

        collect_spec = _parse_collect(expr_txt, expr_ctx)
        if collect_spec is not None:
            collect_spec = _compile_collect_inner(collect_spec, bind_vars)
            idx = len(compiled_nonagg) + len(compiled_agg) + len(compiled_collects) + 1
            key = claim_key(alias or f"expr{idx}", expr_txt, used_keys)
            compiled_collects.append((key, _alloc(key), collect_spec))
            return_aggregates[aggregate_key(expr_ctx.getText())] = compiled_collects[-1][1]
            continue

        agg = _compile_agg_expr(expr_txt, bind_vars)
        if agg is not None:
            _kind, agg_expr = agg
            fn_match = re.match(r"([A-Za-z_][A-Za-z0-9_]*)\(", expr_txt)
            fn_key = fn_match.group(1).lower() if fn_match else None
            idx = len(compiled_nonagg) + len(compiled_agg) + len(compiled_collects) + 1
            key = claim_key(alias or fn_key or f"expr{idx}", expr_txt, used_keys)
            compiled_agg.append((key, _alloc(key), agg_expr))
            return_aggregates[aggregate_key(expr_ctx.getText())] = compiled_agg[-1][1]
            continue

        nested = _hoist_nested_aggregates(
            expr_ctx,
            incoming_env=var_env,
            bind_vars=bind_vars,
            reserve=_alloc,
            compiled_agg=compiled_agg,
            compiled_collects=compiled_collects,
        )
        if nested:
            hidden.update(nested.values())
            with aggregate_variables(nested):
                post = _compile_expression(expr_ctx, bind_vars)
            if var_env:
                post = _rewrite_vars(post, var_env)
            idx = len(compiled_nonagg) + len(compiled_post) + 1
            key = claim_key(alias or cypher_key or f"expr{idx}", expr_txt, used_keys)
            compiled_post.append((key, _alloc(key), post))
            continue

        expr = _compile_expression(expr_ctx, bind_vars)
        cypher_level[expr] = expr
        if var_env:
            renamed = _rewrite_vars(expr, var_env)
            cypher_level[renamed] = cypher_level.pop(expr)
            expr = renamed
        # Infer the COLLECT group key from the original Cypher expression
        # (e.g. ``type(r)`` -> ``type``) rather than the compiled AQL, which
        # for non-trivial expressions like the ``type()`` runtime discriminator
        # would yield a malformed identifier (``...collection)``).
        key = claim_key(
            alias
            or cypher_key
            or _infer_key(expr_txt)
            or _infer_key(expr)
            or f"expr{len(compiled_nonagg) + len(compiled_agg) + 1}",
            expr_txt,
            used_keys,
        )
        compiled_nonagg.append((key, _alloc(key), expr))

    group_parts = ", ".join(f"{aql} = {e}" for _k, aql, e in compiled_nonagg)

    if compiled_collects:
        # PUSH is not a valid AGGREGATE expression on ArangoDB 3.11 (ERR 1574),
        # so a projection with a collect() groups INTO rows.
        collect_line, collect_lets = _collect_into_rows(
            group_parts, compiled_agg, compiled_collects, _alloc("_collect_rows")
        )
        lines.append(collect_line)
        for aql, finish in collect_lets:
            lines.append(f"  LET {aql} = {finish}")
    else:
        agg_pieces, post_lets = _aggregate_clause(compiled_agg, _alloc)
        agg_parts = ", ".join(agg_pieces)

        collect_line = "  COLLECT"
        if group_parts:
            collect_line += f" {group_parts}"
        if agg_parts:
            collect_line += f" AGGREGATE {agg_parts}"

        lines.append(collect_line)
        for pk, pe in post_lets:
            lines.append(f"  LET {pk} = {pe}")
    _append_post_aggregate_lets(lines, compiled_post, compiled_nonagg)

    # ORDER BY / RETURN env: map both the projection key and the original grouped
    # expression to the (possibly reallocated) AQL variable. After
    # ``COLLECT name = p.name`` the Cypher variable ``p`` is out of scope, so an
    # ORDER BY referring to ``p.name`` must resolve to the group var.
    agg_env: dict[str, str] = {}
    for key, aql, _e in compiled_nonagg + compiled_agg + compiled_post:
        if key not in hidden:
            agg_env[key] = aql
    for key, aql, _s in compiled_collects:
        if key not in hidden:
            agg_env[key] = aql
    for _key, aql, expr in compiled_nonagg:
        if expr not in agg_env:
            agg_env[expr] = aql
        if cypher_level.get(expr, expr) not in agg_env:
            agg_env[cypher_level.get(expr, expr)] = aql

    if order_ctx is not None:
        lines.append(
            "  "
            + _compile_order_by(
                order_ctx,
                bind_vars,
                var_env=agg_env,
                grouped=_grouped_expression_vars(compiled_nonagg),
                aggregates=return_aggregates,
            )
        )
    _append_skip_limit(lines, skip_value, limit_value)

    columns = {k: aql for k, aql, _ in compiled_nonagg + compiled_agg + compiled_post}
    columns.update({k: aql for k, aql, _ in compiled_collects})
    all_items: list[tuple[str, str]] = [(k, columns[k]) for k in column_order if k not in hidden]
    if len(all_items) == 1 and not _object_rows.get():
        lines.append(f"  RETURN {all_items[0][1]}")
    else:
        parts = ", ".join(
            f"{object_key(written_names[k]) if k in written_names else k}: {aql}" for k, aql in all_items
        )
        lines.append(f"  RETURN {{{parts}}}")


def _infer_key(expr: str) -> str | None:
    # Best-effort key inference for `n.prop.subprop` → `subprop`; only a plain
    # property path (a dotted AQL expression such as a ternary is not one).
    if re.fullmatch(r"[A-Za-z_]\w*(?:\s*\.\s*[A-Za-z_]\w*)+", expr):
        return expr.split(".")[-1].strip()
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", expr):
        # A folded constant compiles to ``true`` / ``false`` / ``null``: a
        # reserved word is not a usable bare object key (ERR 1501).
        return None if expr.lower() in _AQL_RESERVED else expr
    m = re.match(r"([A-Za-z_][A-Za-z0-9_]*)\(", expr)
    if m:
        return m.group(1)
    return None


def _unique_projection_key(key: str, expr: str, used: set[str]) -> str:
    """*key*, or a variant no earlier projection column uses, recorded in *used*.

    Two columns inferred from the same property (``a.name, b.name``) would
    both be ``name``, and an AQL object keeps only the last of two equal keys.
    Prefer ``a.name`` → ``a_name``, then a numeric suffix.
    """
    if key in used:
        m = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)", expr)
        if m:
            key = f"{m.group(1)}_{m.group(2)}"
    if key in used:
        i = 2
        while _affix_name(key, suffix=f"_{i}") in used:
            i += 1
        key = _affix_name(key, suffix=f"_{i}")
    used.add(key)
    return key


def _compile_return_object(
    compiled_items: list[tuple[str | None, str]], written_names: dict[int, str] | None = None
) -> str:
    """``{key: expr, …}``; *written_names* (position -> Cypher's column name,
    under ``cypher_column_names``) overrides the inferred key."""
    obj_parts: list[str] = []
    used: set[str] = set()
    for i, (alias, expr) in enumerate(compiled_items):
        if written_names and i in written_names:
            name = written_names[i]
            used.add(name)
            obj_parts.append(f"{object_key(name)}: {expr}")
            continue
        key = _unique_projection_key(alias or _infer_key(expr) or f"expr{len(obj_parts) + 1}", expr, used)
        obj_parts.append(f"{key}: {expr}")
    return "{" + ", ".join(obj_parts) + "}"


def _parse_skip_limit(
    proj: CypherParser.OC_ProjectionBodyContext,
    bind_vars: dict[str, Any],
) -> tuple[str | None, str | None]:
    skip_expr_ctx = proj.oC_Skip().oC_Expression() if proj.oC_Skip() is not None else None
    limit_expr_ctx = proj.oC_Limit().oC_Expression() if proj.oC_Limit() is not None else None

    skip_value: str | None = None
    if skip_expr_ctx is not None:
        skip_value = _compile_expression(skip_expr_ctx, bind_vars)

    limit_value: str | None = None
    if limit_expr_ctx is not None:
        limit_value = _compile_expression(limit_expr_ctx, bind_vars)

    for clause, value in (("SKIP", skip_value), ("LIMIT", limit_value)):
        if value is not None and re.search(r"\b(?:RAND|UUID|DATE_NOW)\(", value):
            # AQL's LIMIT takes a value fixed when the query is planned, and
            # a LET does not make one (ERR 1504).
            raise CoreError(
                f"{clause} with a value computed at run time ({value[:60]}) is not supported: "
                "AQL needs a constant or a parameter",
                code="UNSUPPORTED",
            )
        # A parameter's value is known now: SKIP/LIMIT take a non-negative
        # integer (openCypher InvalidArgumentType / NegativeIntegerArgument).
        m = re.fullmatch(r"@(\w+)", value or "")
        if m and m.group(1) in bind_vars:
            given = bind_vars[m.group(1)]
            if isinstance(given, bool) or not isinstance(given, int) or given < 0:
                raise CoreError(
                    f"{clause} expects a non-negative integer, got parameter ${m.group(1)} = {given!r}",
                    code="UNSUPPORTED",
                )

    return skip_value, limit_value


def _append_skip_limit(lines: list[str], skip_value: str | None, limit_value: str | None) -> None:
    if skip_value is None and limit_value is None:
        return
    if skip_value is not None and limit_value is not None:
        lines.append(f"  LIMIT {skip_value}, {limit_value}")
        return
    if skip_value is not None:
        # SKIP without LIMIT: use a large count.
        lines.append(f"  LIMIT {skip_value}, 1000000000")
        return
    lines.append(f"  LIMIT {limit_value}")


def _query_variable_names(ctx: Any) -> set[str]:
    """Every variable name written anywhere in the query that holds *ctx*."""
    root = ctx
    while getattr(root, "parentCtx", None) is not None:
        root = root.parentCtx
    names: set[str] = set()
    stack = [root]
    while stack:
        node = stack.pop()
        if isinstance(node, CypherParser.OC_VariableContext):
            names.add(node.getText().strip())
            continue
        stack.extend(getattr(node, "children", None) or [])
    return names


def _anonymous_var(default: str, ctx: Any, taken: set[str]) -> str:
    """The AQL variable for an unnamed node or relationship.

    *default* (``u``, ``v``, ``r``) unless an earlier part of the pattern
    (*taken*) or any variable the query names uses it. Reusing a taken name
    re-declared it (ERR 1511 — two anonymous nodes in LDBC BI3), and sharing
    a user's name made a later ``(v)`` a back-reference to the unnamed node,
    which silently joined the two and returned no rows.
    """
    named = _query_variable_names(ctx)
    if default not in taken and default not in named:
        return default
    return _pick_fresh_var(default, forbidden_vars=taken | named)


def _extract_node_var_and_labels(
    node: CypherParser.OC_NodePatternContext, *, default_var: str
) -> tuple[str, list[str]]:
    var = (node.oC_Variable().getText() if node.oC_Variable() is not None else default_var).strip()
    labels_ctx = node.oC_NodeLabels()
    if labels_ctx is None:
        return var, []
    node_labels = labels_ctx.oC_NodeLabel() or []
    labels: list[str] = []
    for nl in node_labels:
        label = nl.oC_LabelName().getText().strip()
        if not label:
            raise CoreError("Invalid node pattern", code="UNSUPPORTED")
        labels.append(label)
    if not var:
        raise CoreError("Invalid node pattern", code="UNSUPPORTED")
    return var, labels


def _pick_primary_entity_label(labels: list[str], resolver: MappingResolver) -> str:
    """For multi-label nodes, pick a primary label that exists in the mapping.

    When cardinality statistics are available, prefer the label with the
    smallest estimated count (most selective).  Otherwise fall back to
    right-to-left ordering to align with common Neo4j conventions.
    """
    valid: list[str] = []
    last_err: CoreError | None = None
    for lab in labels:
        try:
            resolver.resolve_entity(_strip_label_backticks(lab))
            valid.append(lab)
        except CoreError as e:
            last_err = e
            if e.code != "MAPPING_NOT_FOUND":
                raise
    if not valid:
        if last_err is not None:
            raise last_err
        raise CoreError("A single label is required in v0 subset", code="UNSUPPORTED")
    if len(valid) == 1:
        return valid[0]
    # A concrete label implies its supertypes (``(p:Post:Message)`` is a Post),
    # is more selective, and is the only one that can be created; the
    # supertype stays as an extra label filter.
    concrete = [lab for lab in valid if not resolver.is_supertype(_strip_label_backticks(lab))]
    if concrete:
        valid = concrete
        if len(valid) == 1:
            return valid[0]

    best: str | None = None
    best_count: int | None = None
    for lab in valid:
        cnt = resolver.estimated_count(lab)
        if cnt is not None:
            if best_count is None or cnt < best_count:
                best = lab
                best_count = cnt
    if best is not None:
        return best
    return valid[-1]


def _supertype_mapping(resolver: MappingResolver | None, label: str) -> dict[str, Any] | None:
    """*label*'s entity mapping when it is a supertype, else ``None`` (unmapped included)."""
    if resolver is None:
        return None
    try:
        emap = resolver.resolve_entity(label)
    except CoreError as e:
        if e.code == "MAPPING_NOT_FOUND":
            return None
        raise
    return emap if emap.get("style") in SUPERTYPE_STYLES else None


def _label_implies(resolver: MappingResolver, label: str, supertype: str) -> bool:
    """Whether every *label* node is a *supertype* node (``Post`` implies ``Message``)."""
    members = set(resolver.supertype_members(supertype))
    if resolver.is_supertype(label):
        return set(resolver.supertype_members(label)) <= members
    return label in members


def _collection_label_filters(
    var: str, labels: list[str], primary: str, bind_vars: dict[str, Any], *, bare: bool = False
) -> list[str]:
    """Extra-label filters for a node whose *primary* label is ``COLLECTION``-mapped.

    A document lives in one collection, so another concrete label cannot be
    checked and is only warned about. A supertype label can: it is implied by
    *primary* (no filter) or tested like any supertype — ``(x:Person:Message)``
    is no Person, not every Person.
    """
    resolver = _active_resolver.get()
    supers = [
        lab
        for lab in labels
        if lab != primary and _supertype_mapping(resolver, _strip_label_backticks(lab)) is not None
    ]
    unverifiable = [lab for lab in labels if lab not in supers]
    if len(unverifiable) > 1:
        _warn_multi_label_collection(unverifiable, primary)
    return _extra_label_filters(var, supers, primary, bind_vars, bare=bare)


def _extra_label_filters(
    var: str, labels: list[str], primary: str, bind_vars: dict[str, Any], *, bare: bool = False
) -> list[str]:
    """Filters for a node's labels other than *primary*, which its scan or collection check covers.

    A supertype that *primary* implies (``(p:Post:Message)``: every Post is a
    Message) adds nothing. Another supertype adds its own test: the labels
    array for ``MULTI_LABEL``, the member collections for ``UNION``. Any other
    label is tested against a ``labels`` array. *bare* drops the outer
    parentheses for callers that emit each filter as its own ``FILTER``.
    """
    resolver = _active_resolver.get()
    primary_name = _strip_label_backticks(primary)
    out: list[str] = []
    for lab in labels:
        if lab == primary:
            continue
        name = _strip_label_backticks(lab)
        emap = _supertype_mapping(resolver, name)
        if resolver is not None and emap is not None:
            if _label_implies(resolver, primary_name, name):
                continue
            if emap.get("style") == "UNION":
                out.append(union_membership_filter(var, name, resolver, bind_vars))
            else:
                out.append(_multi_label_filter(var, emap, bind_vars))
            continue
        test = f"{_aql_string_literal(lab)} IN {var}.labels"
        out.append(test if bare else f"({test})")
    return out


def _multi_label_filter(var: str, emap: dict[str, Any], bind_vars: dict[str, Any]) -> str:
    """The filter for a ``MULTI_LABEL`` supertype: ``@labelValue IN var[@labelsField]``.

    A supertype in a shared collection (LDBC's ``Message`` over ``nodes``) is
    every document whose labels array lists it (arango-query-core
    ``SUPERTYPE_STYLES``).
    """
    lf, lv = emap.get("labelsField"), emap.get("labelValue")
    if not (isinstance(lf, str) and lf and isinstance(lv, str) and lv):
        raise CoreError(
            "MULTI_LABEL entity mapping requires non-empty 'labelsField' and 'labelValue'",
            code="INVALID_MAPPING",
        )
    lf_key = _pick_bind_key("labelsField", bind_vars)
    lv_key = _pick_bind_key("labelValue", bind_vars)
    bind_vars[lf_key] = lf
    bind_vars[lv_key] = lv
    return f"@{lv_key} IN {var}[@{lf_key}]"


def _compile_label_predicate(
    var: str,
    labels_ctx: Any,
    bind_vars: dict[str, Any],
) -> str | None:
    """Compile a label predicate (``n:Label`` / ``n:A:B``) to an AQL boolean.

    Cypher allows a label test in expression position — most commonly a WHERE
    clause over a variable that carries no label in the MATCH pattern, e.g.
    ``MATCH (risk) WHERE risk:RISK_FACTOR``. Previously the ``:Label`` suffix
    was dropped and the predicate collapsed to a no-op (``risk``), silently
    returning every row. Each label is resolved against the active mapping to
    the same discriminator filter the MATCH binder emits:

    - ``LABEL`` / ``GENERIC_WITH_TYPE`` (LPG / type-discriminated collection)
      → ``var[@typeField] == @typeValue``.
    - ``COLLECTION`` (dedicated collection per label) → ``IS_SAME_COLLECTION``.

    Multiple labels (``n:A:B``) are ANDed, matching Cypher's "has all labels"
    semantics. Returns ``None`` when no resolver is bound so callers degrade to
    the prior (suffix-ignoring) behaviour rather than crash.
    """
    resolver = _active_resolver.get()
    if resolver is None:
        return None
    node_labels = labels_ctx.oC_NodeLabel() or []
    parts: list[str] = []
    for nl in node_labels:
        label = nl.oC_LabelName().getText().strip()
        if not label:
            raise CoreError("Invalid label in predicate", code="UNSUPPORTED")
        mapping = resolver.resolve_entity(_strip_label_backticks(label))
        style = mapping.get("style")
        if style == "MULTI_LABEL":
            parts.append(_multi_label_filter(var, mapping, bind_vars))
            continue
        if style == "UNION":
            parts.append(union_membership_filter(var, label, resolver, bind_vars))
            continue
        if style in ("LABEL", "GENERIC_WITH_TYPE"):
            tf = mapping.get("typeField")
            tv = mapping.get("typeValue")
            if not (isinstance(tf, str) and tf):
                raise CoreError(
                    f"Label predicate '{label}' has no type field in mapping",
                    code="INVALID_MAPPING",
                )
            tf_key = _pick_bind_key(f"{var}LabelField", bind_vars)
            tv_key = _pick_bind_key(f"{var}LabelValue", bind_vars)
            bind_vars[tf_key] = tf
            bind_vars[tv_key] = tv
            parts.append(label_test(var, tf_key, tv_key, mapping, bind_vars))
        elif style == "COLLECTION":
            coll = mapping.get("collectionName")
            if not (isinstance(coll, str) and coll):
                raise CoreError(
                    f"Label predicate '{label}' has no collection in mapping",
                    code="INVALID_MAPPING",
                )
            coll_key = _pick_bind_key(f"{var}LabelColl", bind_vars)
            bind_vars[coll_key] = coll
            parts.append(f"IS_SAME_COLLECTION(@{coll_key}, {var})")
        else:
            raise CoreError(
                f"Unsupported entity mapping style for label predicate: {style}",
                code="INVALID_MAPPING",
            )
    if not parts:
        return None
    if len(parts) == 1:
        return f"({parts[0]})"
    return "(" + " AND ".join(parts) + ")"


def _shared_type_field(resolver: MappingResolver | None, kind: str) -> str | None:
    """Return the common type-discriminator field across a mapping section.

    ``kind`` is ``"entities"`` or ``"relationships"``. When every
    type-discriminated definition in that section shares a single
    ``typeField`` (the GraphRAG / naked-LPG shape — one physical
    collection whose rows are distinguished by, e.g., ``type``), return
    that field name so ``labels()`` / ``type()`` can read the real
    discriminator instead of collapsing to the collection name. Returns
    ``None`` when there is no discriminator or the field is ambiguous
    (mixed discriminators), in which case callers fall back to the
    collection-name behaviour.
    """
    if resolver is None:
        return None
    section = resolver.bundle.physical_mapping.get(kind)
    if not isinstance(section, dict) or not section:
        return None
    discriminated_styles = {"LABEL", "GENERIC_WITH_TYPE"}
    fields = {
        d["typeField"]
        for d in section.values()
        if isinstance(d, dict) and d.get("style") in discriminated_styles and d.get("typeField")
    }
    return next(iter(fields)) if len(fields) == 1 else None


def _unlabeled_start_collection(chain: Any, resolver: MappingResolver) -> str:
    """The collection to scan for an unlabeled, unbound start node.

    The mapping's one node collection when it has one
    (``_infer_unlabeled_collection``); otherwise the collection the first
    relationship's endpoint guarantees: ``(p)<-[:HAS_CREATOR]-(m)`` starts at
    a Person, since HAS_CREATOR's range is Person (LDBC IC5 on PG). Refuses
    when neither settles it.
    """
    try:
        return _infer_unlabeled_collection(resolver)
    except CoreError as e:
        if e.code != "UNSUPPORTED" or chain is None:
            raise
        refusal = e
    rel_pat = chain.oC_RelationshipPattern()
    rel_type, _var, _range = _extract_relationship_type_and_var(rel_pat, default_var="_r")
    direction = _relationship_direction(rel_pat)
    if rel_type is None or direction not in ("OUTBOUND", "INBOUND"):
        raise refusal
    # Seen from the far end, the edge leads back to the start node.
    back = "INBOUND" if direction == "OUTBOUND" else "OUTBOUND"
    collections = {
        resolver.resolve_entity(label).get("collectionName")
        for label in resolver.all_entity_labels()
        if resolver.edge_constrains_target(rel_type, label, back)
    }
    collections.discard(None)
    if len(collections) != 1:
        raise refusal
    return str(next(iter(collections)))


def _unlabeled_target_filter(
    var: str, resolver: MappingResolver, bind_vars: dict[str, Any], key_base: str
) -> str | None:
    """The collection check for an unlabeled traversal target, or ``None``.

    An unlabeled node matches any node, and a traversal only reaches what its
    edges lead to, so the check only narrows to the collection an unlabeled
    node means when the mapping has one (``_infer_unlabeled_collection``:
    the single node collection, or the one domain collection beside side
    stores). With several node collections there is none to narrow to; the
    target was refused ("A single label is required", LDBC IS2/IC3/IC5 on
    PG) and now goes unchecked.
    """
    try:
        coll = _infer_unlabeled_collection(resolver)
    except CoreError as e:
        if e.code != "UNSUPPORTED":
            raise
        return None
    key = _pick_bind_key(key_base, bind_vars)
    bind_vars[key] = coll
    return f"IS_SAME_COLLECTION(@{key}, {var})"


def _infer_unlabeled_collection(resolver: MappingResolver) -> str:
    pm = resolver.bundle.physical_mapping
    entities = pm.get("entities") if isinstance(pm.get("entities"), dict) else {}
    if not isinstance(entities, dict) or not entities:
        raise CoreError("A single label is required in v0 subset", code="UNSUPPORTED")

    def _collections(styles: set[str] | None) -> set[str]:
        out: set[str] = set()
        for m in entities.values():
            if not isinstance(m, dict):
                continue
            c = m.get("collectionName")
            if not (isinstance(c, str) and c):
                continue
            if styles is None or m.get("style") in styles:
                out.add(c)
        return out

    all_colls = _collections(None)
    if len(all_colls) == 1:
        return next(iter(all_colls))

    # Multi-collection schema. The common GraphRAG / naked-LPG shape is one
    # type-discriminated "domain" vertex collection (e.g. ``Node``, whose rows
    # carry a ``type`` discriminator) plus side document stores (chunk text,
    # benchmark/ops collections) that are modelled COLLECTION-style. An
    # unlabeled ``MATCH (n)`` means the domain graph, so resolve to the single
    # type-discriminated collection when there is exactly one — the side
    # stores are intentionally excluded.
    core_colls = _collections({"LABEL", "GENERIC_WITH_TYPE", "MULTI_LABEL"})
    if len(core_colls) == 1:
        primary = next(iter(core_colls))
        excluded = sorted(all_colls - core_colls)
        if excluded:
            warnings = _active_warnings.get()
            # Cap the inline list so the banner doesn't become a wall of
            # text on databases with many operational / RAG / benchmark
            # side stores (some real schemas exclude 25+). Show a short
            # preview plus a count, and point to the two ways to reach the
            # excluded data: label it, or scope to a named graph.
            preview_n = 5
            shown = ", ".join(excluded[:preview_n])
            more = len(excluded) - preview_n
            listed = shown + (f", and {more} more" if more > 0 else "")
            msg = (
                f"Unlabeled MATCH resolved to the domain collection '{primary}'. "
                f"{len(excluded)} side collection(s) excluded from label-less "
                f"matches ({listed}). Reference one by label "
                f"(e.g. MATCH (n:Chunk)) to query it directly, or scope the "
                f"session to a named graph to drop side collections entirely."
            )
            if msg not in warnings:
                warnings.append(msg)
        return primary

    raise CoreError("A single label is required in v0 subset", code="UNSUPPORTED")


def _infer_unlabeled_edge_collection(resolver: MappingResolver) -> str:
    """Resolve the single edge collection backing an untyped relationship.

    An untyped relationship (``-[r]->`` / ``-[]->`` / ``-->``) does not name a
    type, so we traverse a single edge collection with no type-discriminator
    filter (i.e. all relationship types in that collection are returned). This
    mirrors :func:`_infer_unlabeled_collection` for label-less nodes.

    Resolution order:

    1. If the mapping has exactly one edge collection, use it.
    2. Otherwise, narrow to type-discriminated edge collections
       (``GENERIC_WITH_TYPE`` / ``DEDICATED_COLLECTION``). When exactly one
       remains, use it and warn that other edge collections are excluded.
    3. Otherwise raise ``UNSUPPORTED`` — spanning multiple edge collections
       requires naming a type (a multi-collection ``ANY`` traversal is a
       larger change deferred for now).
    """
    pm = resolver.bundle.physical_mapping
    rels = pm.get("relationships") if isinstance(pm.get("relationships"), dict) else {}
    if not isinstance(rels, dict) or not rels:
        raise CoreError(
            "Untyped relationships require at least one relationship in the mapping",
            code="UNSUPPORTED",
        )

    def _edge_collections(styles: set[str] | None) -> set[str]:
        out: set[str] = set()
        for m in rels.values():
            if not isinstance(m, dict):
                continue
            c = m.get("edgeCollectionName") or m.get("collectionName")
            if not (isinstance(c, str) and c):
                continue
            if styles is None or m.get("style") in styles:
                out.add(c)
        return out

    all_edges = _edge_collections(None)
    if len(all_edges) == 1:
        return next(iter(all_edges))
    if not all_edges:
        raise CoreError(
            "Untyped relationships require at least one edge collection in the mapping",
            code="UNSUPPORTED",
        )

    core_edges = _edge_collections({"GENERIC_WITH_TYPE", "DEDICATED_COLLECTION"})
    if len(core_edges) == 1:
        primary = next(iter(core_edges))
        excluded = sorted(all_edges - core_edges)
        if excluded:
            warnings = _active_warnings.get()
            msg = (
                f"Untyped relationship resolved to edge collection '{primary}'. "
                f"Other edge collection(s) {excluded} are excluded from type-less "
                f"matches; specify a relationship type (e.g. -[r:TYPE]->) to query them."
            )
            if msg not in warnings:
                warnings.append(msg)
        return primary

    raise CoreError(
        "Untyped relationships spanning multiple edge collections are not yet "
        "supported; specify a relationship type, e.g. -[r:TYPE]->",
        code="UNSUPPORTED",
    )


def _resolve_relationship_for_pattern(resolver: MappingResolver, rel_type: str | None) -> dict[str, Any]:
    """Resolve a relationship pattern to a physical mapping.

    For a typed relationship this delegates to
    :meth:`MappingResolver.resolve_relationship`. For an untyped relationship
    (``rel_type is None``) it synthesises a ``DEDICATED_COLLECTION``-style map
    over the inferred single edge collection, so every existing style branch
    emits a plain traversal with no type filter. ``type(r)`` is special-cased
    separately (see :func:`_untyped_rel_type_expr`) because the literal-type
    shortcut used for dedicated collections does not apply when no type is named.
    """
    if rel_type is None:
        return {
            "style": "DEDICATED_COLLECTION",
            "edgeCollectionName": _infer_unlabeled_edge_collection(resolver),
        }
    if MULTI_TYPE_SEPARATOR in rel_type:
        return resolve_multi_type(rel_type, resolver)
    return resolver.resolve_relationship(_strip_label_backticks(rel_type))


def _dedicated_rel_type_expr(rel_type: str | None, ref: str, resolver: MappingResolver | None) -> str:
    """``type(r)`` for a relationship in a dedicated edge collection.

    The type itself when there is one; for several (``[r:A|B]``), the type
    whose edge collection *ref* lives in; for none, the edge's runtime type.
    """
    if rel_type is None:
        return _untyped_rel_type_expr(ref, resolver)
    if MULTI_TYPE_SEPARATOR not in rel_type or resolver is None:
        return _aql_string_literal(rel_type)
    return multi_type_runtime_expr(ref, rel_type, resolver)


def _untyped_rel_type_expr(ref: str, resolver: MappingResolver | None) -> str:
    """AQL expression yielding the runtime type of an untyped edge.

    Prefers the shared type-discriminator field (e.g. ``r.type``) when the
    mapping defines a single one across relationships, falling back to the
    edge's physical collection name. Mirrors the runtime ``type()`` compilation
    used elsewhere so ``RETURN type(r)`` is correct for untyped traversals.
    """
    type_field = _shared_type_field(resolver, "relationships") if resolver else None
    if type_field:
        return f"({ref}.{type_field} != null ? {ref}.{type_field} : PARSE_IDENTIFIER({ref}._id).collection)"
    return f"PARSE_IDENTIFIER({ref}._id).collection"


def _compile_node_pattern_properties(
    node_pat: CypherParser.OC_NodePatternContext,
    *,
    var: str,
    bind_vars: dict[str, Any],
    var_env: dict[str, str] | None = None,
) -> list[str]:
    """
    Compile inline node properties in patterns, e.g. (n:User {id: "u1"}).

    v0 subset: only supports map literals with simple expressions for values.
    Parameterized maps (e.g. {..} via $param) are NOT_IMPLEMENTED. In a MATCH
    after WITH, *var_env* renames the variables a value refers to
    (``{id: knownTagId}`` once WITH has bound it as ``knownTagId_1``).
    """
    props_ctx = node_pat.oC_Properties()
    if props_ctx is None:
        return []
    if props_ctx.oC_Parameter() is not None:
        raise CoreError("Parameterized node properties are not supported in v0", code="NOT_IMPLEMENTED")
    m = props_ctx.oC_MapLiteral()
    if m is None:
        return []
    keys = m.oC_PropertyKeyName() or []
    vals = m.oC_Expression() or []
    if len(keys) != len(vals):
        raise CoreError("Invalid node properties map", code="UNSUPPORTED")
    out: list[str] = []
    for k_ctx, v_ctx in zip(keys, vals, strict=False):
        key = k_ctx.getText().strip()
        if not key:
            raise CoreError("Invalid node properties map", code="UNSUPPORTED")
        expr = _compile_expression(v_ctx, bind_vars)
        if var_env:
            expr = _rewrite_vars(expr, var_env)
        if _active_case_insensitive_inline.get() and (expr.startswith('"') or expr.startswith("'")):
            # Opt-in (TranslateOptions.case_insensitive_inline_strings).
            out.append(f"(LOWER({var}.{key}) == LOWER({expr}))")
        else:
            # Cypher's ``=``: exact, and a null value matches nothing.
            out.append(_compile_comparison(f"{var}.{key}", "=", expr, "true"))
    return out


def _compile_relationship_pattern_properties(
    rel_pat: CypherParser.OC_RelationshipPatternContext,
    *,
    var: str,
    bind_vars: dict[str, Any],
    var_env: dict[str, str] | None = None,
) -> list[str]:
    """
    Compile inline relationship properties in patterns, e.g. -[:ACTED_IN {role: "Forrest"}]->

    v0 subset: only supports map literals with simple expressions for values.
    Parameterized properties maps are NOT_IMPLEMENTED.
    """
    detail = rel_pat.oC_RelationshipDetail()
    if detail is None:
        return []
    props_ctx = detail.oC_Properties()
    if props_ctx is None:
        return []
    if props_ctx.oC_Parameter() is not None:
        raise CoreError(
            "Parameterized relationship properties are not supported in v0", code="NOT_IMPLEMENTED"
        )
    m = props_ctx.oC_MapLiteral()
    if m is None:
        return []
    keys = m.oC_PropertyKeyName() or []
    vals = m.oC_Expression() or []
    if len(keys) != len(vals):
        raise CoreError("Invalid relationship properties map", code="UNSUPPORTED")
    out: list[str] = []
    for k_ctx, v_ctx in zip(keys, vals, strict=False):
        key = k_ctx.getText().strip()
        if not key:
            raise CoreError("Invalid relationship properties map", code="UNSUPPORTED")
        expr = _compile_expression(v_ctx, bind_vars)
        if var_env:
            expr = _rewrite_vars(expr, var_env)
        out.append(_compile_comparison(f"{var}.{key}", "=", expr, "true"))
    return out


def _relationship_direction(rel_pat: CypherParser.OC_RelationshipPatternContext) -> str:
    left = rel_pat.oC_LeftArrowHead() is not None
    right = rel_pat.oC_RightArrowHead() is not None
    if left and not right:
        return "INBOUND"
    if right and not left:
        return "OUTBOUND"
    return "ANY"


def _extract_relationship_type_and_var(
    rel_pat: CypherParser.OC_RelationshipPatternContext, *, default_var: str
) -> tuple[str | None, str, tuple[int, int]]:
    """Returns (rel_type, rel_var, (min_hops, max_hops)).

    ``rel_type`` is ``None`` for an *untyped* relationship — ``-->``,
    ``-[]->`` or ``-[r]->`` with no ``:TYPE``. Callers resolve untyped
    relationships via :func:`_resolve_relationship_for_pattern`, which traverses
    the single inferred edge collection with no type filter. Several types
    (``-[:A|B]->``) come back joined by ``|``.
    """
    detail = rel_pat.oC_RelationshipDetail()
    if detail is None:
        # Bare ``-->`` / ``<--`` / ``--``: untyped, no variable, single hop.
        return None, default_var, (1, 1)

    min_hops, max_hops = 1, 1
    range_ctx = detail.oC_RangeLiteral()
    if range_ctx is not None:
        min_hops, max_hops = _parse_range_literal(range_ctx)

    rel_var = (detail.oC_Variable().getText() if detail.oC_Variable() is not None else default_var).strip()
    if not rel_var:
        raise CoreError("Invalid relationship pattern", code="UNSUPPORTED")

    types_ctx = detail.oC_RelationshipTypes()
    if types_ctx is None:
        # ``-[]->`` or ``-[r]->``: untyped relationship.
        return None, rel_var, (min_hops, max_hops)
    types = types_ctx.oC_RelTypeName()
    if not types:
        return None, rel_var, (min_hops, max_hops)
    names = [t.getText().strip() for t in types]
    if not all(names):
        raise CoreError("Invalid relationship pattern", code="UNSUPPORTED")
    # Several types (``[:A|B]``) travel as one ``A|B`` string, which
    # :func:`_resolve_relationship_for_pattern` resolves (PRD §8.2).
    return MULTI_TYPE_SEPARATOR.join(names), rel_var, (min_hops, max_hops)


_MAX_VLP_DEPTH = 10


def _capped_depth(range_ctx: Any) -> int:
    """The hop cap for an open-ended range (``*``, ``*2..``), announced as a warning.

    AQL traversals need a maximum depth, so an unbounded Cypher range is cut
    at :data:`_MAX_VLP_DEPTH` hops: a path longer than that is not matched.
    The warning makes the difference visible to the caller instead of silent.
    """
    msg = (
        f"Variable-length relationship '{range_ctx.getText()}' has no upper bound; "
        f"it is limited to {_MAX_VLP_DEPTH} hops (longer paths are not matched)."
    )
    warnings = _active_warnings.get()
    if msg not in warnings:
        warnings.append(msg)
    return _MAX_VLP_DEPTH


def _parse_range_literal(
    range_ctx: CypherParser.OC_RangeLiteralContext,
) -> tuple[int, int]:
    """Parse ``*``, ``*2``, ``*1..3``, ``*..5`` etc. into (min, max)."""
    ints = range_ctx.oC_IntegerLiteral() or []
    has_dots = ".." in range_ctx.getText()

    if not ints and not has_dots:
        # bare ``*`` → 1..max
        return 1, _capped_depth(range_ctx)

    if len(ints) == 1 and not has_dots:
        # ``*3`` → exactly 3 hops
        n = int(ints[0].getText())
        return n, n

    if len(ints) == 2 and has_dots:
        lo = int(ints[0].getText())
        hi = int(ints[1].getText())
        # ``*2..1`` is an empty interval: it matches nothing
        # (formatting.empty_varlength_ranges), it is not an error.
        return lo, hi

    if len(ints) == 1 and has_dots:
        raw = range_ctx.getText().strip()
        n = int(ints[0].getText())
        if raw.startswith("*") and ".." in raw:
            before_dots = raw[1 : raw.index("..")]
            if before_dots.strip().isdigit():
                return n, _capped_depth(range_ctx)
            return 1, n
        return n, _capped_depth(range_ctx)

    return 1, _capped_depth(range_ctx)


def _extract_interleaved_op(ctx: Any, term_index: int, valid_ops: set[str]) -> str:
    """
    In ANTLR's openCypher grammar, binary operators are interleaved between
    sub-rule children and SP tokens.  Walk the raw children to find the
    operator token that sits between term (term_index-1) and term (term_index).
    """
    term_count = 0
    for i in range(ctx.getChildCount()):
        child = ctx.getChild(i)
        if hasattr(child, "getRuleIndex"):
            term_count += 1
            if term_count > term_index:
                break
            continue
        t = child.getText().strip()
        if t in valid_ops and term_count == term_index:
            return t
    return next(iter(valid_ops))


def _compile_where(expr_ctx: CypherParser.OC_ExpressionContext, bind_vars: dict[str, Any]) -> str:
    """Compile a predicate whose value only a FILTER observes (true vs not true)."""
    return _compile_in_mode(expr_ctx, bind_vars, "true")


def _compile_value(ctx: Any, bind_vars: dict[str, Any]) -> str:
    """Compile *ctx* as a value, whatever predicate mode encloses it."""
    return _compile_in_mode(ctx, bind_vars, None)


def _compile_in_mode(ctx: Any, bind_vars: dict[str, Any], mode: str | None) -> str:
    token = _PREDICATE_MODE.set(mode)
    try:
        return _compile_expression(ctx, bind_vars)
    finally:
        _PREDICATE_MODE.reset(token)


def _compile_single_node_subquery_body(
    *,
    start_node: CypherParser.OC_NodePatternContext,
    match_ctx: CypherParser.OC_MatchContext,
    resolver: MappingResolver,
    bind_vars: dict[str, Any],
) -> str:
    """Compile a *single-node* EXISTS{}/COUNT{} body (no relationship chain).

    Two shapes, distinguished by whether the node re-uses an outer binding:

    * **Correlated** — a bare named node with no label / properties
      (``exists { (n) WHERE n.prop = 1 }`` where ``n`` is bound in the
      enclosing query). Existence collapses to the WHERE predicate on the
      outer node, so we probe a single-element list ``[n]``; the WHERE
      references the outer variable directly (valid, since the subquery is
      inline within the outer ``FOR n`` scope).
    * **Fresh scan** — a labelled node (``exists { (m:Movie) WHERE m.x>1 }``
      / ``count { (m:Movie) }``). We scan the label's collection with the
      usual discriminator + inline-property + WHERE filters.

    An anonymous, unlabelled, uncorrelated node (``exists { () }``) has no
    collection to scan and is refused. Collision-safe bind keys keep the
    subquery's collection / type binds from clobbering the outer query's.
    """
    var, labels = _extract_node_var_and_labels(start_node, default_var="_sq_v")
    named = var != "_sq_v"
    has_props = start_node.oC_Properties() is not None
    where_ctx = match_ctx.oC_Where()

    # Correlated single outer node: existence == the WHERE predicate on it.
    if named and not labels and not has_props and var in outer_bound_names(match_ctx):
        sub = f"FOR _sq_probe_{var} IN [{var}]"
        if where_ctx is not None:
            sub += f" FILTER {_compile_where(where_ctx.oC_Expression(), bind_vars)}"
        return sub + " RETURN 1"

    if not labels:
        # A node of the subquery's own: scan the one collection unlabeled
        # nodes live in (failing closed when that is ambiguous).
        coll_key = _pick_bind_key("@sqNodes", bind_vars)
        bind_vars[coll_key] = _infer_unlabeled_collection(resolver)
        sub = f"FOR {var} IN {_aql_collection_ref(coll_key)}"
        for f in _compile_node_pattern_properties(start_node, var=var, bind_vars=bind_vars):
            sub += f" FILTER {f}"
        if where_ctx is not None:
            sub += f" FILTER {_compile_where(where_ctx.oC_Expression(), bind_vars)}"
        return sub + " RETURN 1"

    primary = _pick_primary_entity_label(labels, resolver)
    emap = resolver.resolve_entity(_strip_label_backticks(primary))
    style = emap.get("style")
    coll_key = _pick_bind_key("@sqNodeColl", bind_vars)
    # A UNION supertype applies the node's inline properties inside its member branches.
    pushed = (
        _compile_node_pattern_properties(start_node, var=var, bind_vars=bind_vars) if style == "UNION" else []
    )
    source, _ = node_scan_source(var, primary, emap, resolver, bind_vars, coll_key=coll_key, filters=pushed)

    filters: list[str] = []
    if style == "LABEL":
        tf = _pick_bind_key("sqNodeTF", bind_vars)
        tv = _pick_bind_key("sqNodeTV", bind_vars)
        bind_vars[tf] = emap.get("typeField")
        bind_vars[tv] = emap.get("typeValue")
        filters.append(label_test(var, tf, tv, emap, bind_vars))
        filters.extend(_extra_label_filters(var, labels, primary, bind_vars, bare=True))
    elif style == "MULTI_LABEL":
        filters.append(_multi_label_filter(var, emap, bind_vars))
        filters.extend(_extra_label_filters(var, labels, primary, bind_vars, bare=True))
    elif style == "UNION":
        filters.extend(_extra_label_filters(var, labels, primary, bind_vars, bare=True))
    elif style != "COLLECTION":
        raise CoreError(f"Unsupported entity mapping style: {style}", code="INVALID_MAPPING")
    else:
        filters.extend(_collection_label_filters(var, labels, primary, bind_vars, bare=True))

    if style != "UNION":
        filters.extend(_compile_node_pattern_properties(start_node, var=var, bind_vars=bind_vars))
    if where_ctx is not None:
        filters.append(_compile_where(where_ctx.oC_Expression(), bind_vars))

    sub = f"FOR {var} IN {source}"
    for f in filters:
        sub += f" FILTER {f}"
    return sub + " RETURN 1"


def _compile_subquery_with_aggregation(
    *,
    with_clauses: list[CypherParser.OC_WithContext],
    sub_prefix: str,
    start_var: str,
    bind_vars: dict[str, Any],
) -> str:
    """Lower a ``MATCH … WITH … count(*) … [WHERE having]`` subquery body.

    Handles the common counting-existential shape (openCypher TCK
    ``ExistentialSubquery2`` scenario 2, and typical LLM output):

        exists { MATCH (n)-->(m) WITH n, count(*) AS c WHERE c > 3 RETURN true }

    The traversal ``sub_prefix`` (``FOR _sq_v, _sq_e IN … RETURN``-less) is
    followed by ``COLLECT WITH COUNT INTO <alias>``, an optional post-COLLECT
    ``FILTER`` (the WITH's ``WHERE``, i.e. a HAVING), and ``RETURN 1``. Because
    the subquery is already scoped to the single correlated ``start_var``,
    grouping by that variable is one group, so a keyless ``COLLECT WITH COUNT``
    is exactly the per-``start_var`` count; the alias is used verbatim as the
    AQL COLLECT variable so the HAVING predicate needs no rewriting.

    Only this shape is supported. Anything else — multiple WITHs, ``DISTINCT``,
    ``ORDER BY``/``SKIP``/``LIMIT``, non-``count(*)`` aggregates, grouping keys
    other than the correlated ``start_var`` — is refused (rather than emitting
    silently-wrong AQL) so a fuller subquery pipeline can be added later.
    """
    if len(with_clauses) != 1:
        raise CoreError(
            "EXISTS/COUNT subquery supports a single WITH clause",
            code="UNSUPPORTED",
        )
    pb = with_clauses[0].oC_ProjectionBody()
    if pb is None:
        raise CoreError("Malformed WITH in subquery", code="UNSUPPORTED")
    if pb.DISTINCT() is not None:
        raise CoreError("DISTINCT in a subquery WITH is not supported", code="UNSUPPORTED")
    if pb.oC_Order() is not None or pb.oC_Skip() is not None or pb.oC_Limit() is not None:
        raise CoreError("ORDER BY/SKIP/LIMIT in a subquery WITH is not supported", code="UNSUPPORTED")

    items_ctx = pb.oC_ProjectionItems()
    items = (items_ctx.oC_ProjectionItem() if items_ctx is not None else None) or []
    if not items:
        # `WITH *` has no oC_ProjectionItem children.
        raise CoreError("Subquery WITH must project count(*)", code="UNSUPPORTED")

    count_alias: str | None = None
    for it in items:
        expr_ctx = it.oC_Expression()
        alias_ctx = it.oC_Variable()
        expr_text = expr_ctx.getText() if expr_ctx is not None else ""
        norm = expr_text.replace(" ", "").lower()
        if norm == "count(*)":
            if alias_ctx is None:
                raise CoreError("count(*) in a subquery WITH requires an alias", code="UNSUPPORTED")
            count_alias = alias_ctx.getText().strip()
        elif expr_text.strip() == start_var and alias_ctx is None:
            # Grouping by the correlated outer node — one group; no-op.
            continue
        else:
            raise CoreError(
                f"Unsupported WITH item in subquery: {expr_text!r} "
                "(only count(*) and grouping by the correlated node are supported)",
                code="UNSUPPORTED",
            )

    if count_alias is None:
        raise CoreError("Subquery WITH must aggregate count(*)", code="UNSUPPORTED")

    aql = f"{sub_prefix} COLLECT WITH COUNT INTO {count_alias}"
    having_ctx = with_clauses[0].oC_Where()
    if having_ctx is not None:
        aql += f" FILTER {_compile_where(having_ctx.oC_Expression(), bind_vars)}"
    return aql + " RETURN 1"


def _compile_subquery_body(
    sq_ctx: CypherParser.OC_SubqueryBodyContext,
    bind_vars: dict[str, Any],
) -> str:
    """Compile the inner SubqueryBody of an EXISTS{} or COUNT{} subquery to AQL.

    The inner query contains a MATCH clause; a relationship pattern compiles
    to a traversal subquery, and a bare single-node pattern (delegated to
    :func:`_compile_single_node_subquery_body`) compiles to a collection scan
    or a correlated single-node probe. A ``MATCH … WITH count(*) …`` body is
    lowered by :func:`_compile_subquery_with_aggregation`. Either way the
    result is the ``FOR … RETURN 1`` body, without the outer ``LENGTH`` wrapper.
    """
    resolver = _active_resolver.get()
    if resolver is None:
        raise CoreError("Subquery requires a mapping resolver", code="UNSUPPORTED")

    reading_clauses = sq_ctx.oC_ReadingClause() or []
    if not reading_clauses:
        raise CoreError("EXISTS/COUNT subquery must contain a MATCH clause", code="UNSUPPORTED")

    match_clauses: list[CypherParser.OC_MatchContext] = []
    for rc in reading_clauses:
        m = rc.oC_Match()
        if m is not None:
            match_clauses.append(m)

    if not match_clauses:
        raise CoreError("EXISTS/COUNT subquery must contain a MATCH clause", code="UNSUPPORTED")

    match_ctx = match_clauses[0]
    pattern = match_ctx.oC_Pattern()
    parts = pattern.oC_PatternPart() or []
    if not parts:
        raise CoreError("Empty MATCH pattern in subquery", code="UNSUPPORTED")

    pp = parts[0]
    anon = pp.oC_AnonymousPatternPart()
    if anon is None:
        raise CoreError("Named patterns not supported in subquery", code="NOT_IMPLEMENTED")
    refuse_unbound_shortest_path(anon)
    pe = anon.oC_PatternElement()

    with_clauses = sq_ctx.oC_With() or []
    has_with = bool(with_clauses)

    start_node = pe.oC_NodePattern()
    chains = pe.oC_PatternElementChain() or []
    if not chains:
        if has_with:
            raise CoreError(
                "WITH aggregation in a single-node subquery is not supported",
                code="UNSUPPORTED",
            )
        return _compile_single_node_subquery_body(
            start_node=start_node,
            match_ctx=match_ctx,
            resolver=resolver,
            bind_vars=bind_vars,
        )

    start_var, start_labels = _extract_node_var_and_labels(start_node, default_var="_sq_start")
    if len(chains) > 1 and not has_with and len(parts) == 1:
        # A multi-hop pattern: the bound-variable MATCH compiler, with the
        # names the query binds before the subquery as its bound variables.
        outer = outer_bound_names(sq_ctx)
        sub_lines: list[str] = []
        _compile_match_from_bound(
            match_ctx,
            resolver=resolver,
            bind_vars=bind_vars,
            forbidden_vars=set(outer),
            var_env={name: name for name in outer},
            lines=sub_lines,
        )
        return " ".join(ln.strip() for ln in sub_lines) + " RETURN 1"
    if len(chains) > 1:
        # Only one hop is compiled; dropping the others would test a weaker
        # pattern than the query's.
        raise CoreError(
            "EXISTS/COUNT subquery over a multi-hop pattern is not supported", code="NOT_IMPLEMENTED"
        )
    chain = chains[0]
    rel_pat = chain.oC_RelationshipPattern()
    target_node = chain.oC_NodePattern()
    if rel_pat is None:
        raise CoreError("Invalid subquery pattern", code="UNSUPPORTED")

    rel_type, rel_name, rel_range = _extract_relationship_type_and_var(rel_pat, default_var="_sq_r")
    direction = _relationship_direction(rel_pat)

    r_map = _resolve_relationship_for_pattern(resolver, rel_type)
    edge_key = _pick_bind_key("@sqEdge", bind_vars)
    bind_vars[edge_key] = r_map.get("edgeCollectionName") or r_map.get("collectionName")

    # A relationship the subquery names is its own edge variable.
    sq_e = rel_name if rel_name != "_sq_r" and rel_name not in outer_bound_names(sq_ctx) else "_sq_e"
    target_var, t_labels = _extract_node_var_and_labels(target_node, default_var="_sq_t")
    target_named = target_var != "_sq_t"
    target_has_props = target_node.oC_Properties() is not None
    # A *bare* named target (no label, no properties) references an outer binding
    # — e.g. ``exists{(p)-[:R]->(m)}`` where ``m`` is bound in the enclosing
    # query — so traverse to a fresh internal variable and constrain it by
    # ``_id``; reusing the outer name as the loop variable makes AQL reject the
    # query ("variable 'm' is assigned multiple times"). A *labeled/propertied*
    # named target (``(m:User)``) is a fresh binding local to the subquery, so it
    # is used as the loop variable directly.
    #
    # In an *aggregation* subquery (``… WITH count(*) …``) the target is being
    # enumerated/counted, not correlated — so it is always a fresh loop
    # variable regardless of naming.
    correlate_target = (
        (not has_with)
        and target_named
        and not t_labels
        and not target_has_props
        and target_var in outer_bound_names(sq_ctx)
    )
    sq_v = target_var if (target_named and not correlate_target) else "_sq_v"

    filters: list[str] = []
    if correlate_target:
        filters.append(f"{sq_v}._id == {target_var}._id")

    r_style = r_map.get("style")
    if r_style == "GENERIC_WITH_TYPE":
        rtf = _pick_bind_key("sqRelTF", bind_vars)
        rtv = _pick_bind_key("sqRelTV", bind_vars)
        bind_vars[rtf] = r_map.get("typeField")
        bind_vars[rtv] = r_map.get("typeValue")
        filters.append(f"{sq_e}[@{rtf}] == @{rtv}")

    if t_labels:
        t_primary = _pick_primary_entity_label(t_labels, resolver)
        try:
            t_map = resolver.resolve_entity(_strip_label_backticks(t_primary))
            if t_map.get("style") == "LABEL":
                ttf = _pick_bind_key("sqTTF", bind_vars)
                ttv = _pick_bind_key("sqTTV", bind_vars)
                bind_vars[ttf] = t_map.get("typeField")
                bind_vars[ttv] = t_map.get("typeValue")
                filters.append(label_test(sq_v, ttf, ttv, t_map, bind_vars))
        except CoreError:
            pass

    where_ctx = match_ctx.oC_Where()
    if where_ctx is not None:
        where_cond = _compile_where(where_ctx.oC_Expression(), bind_vars)
        filters.append(where_cond)

    rmin, rmax = rel_range
    edge_ref = _aql_collection_ref(edge_key)
    sub = f"FOR {sq_v}, {sq_e} IN {rmin}..{rmax} {direction} {start_var} {edge_ref}"
    for f in filters:
        sub += f" FILTER {f}"

    if has_with:
        return _compile_subquery_with_aggregation(
            with_clauses=with_clauses,
            sub_prefix=sub,
            start_var=start_var,
            bind_vars=bind_vars,
        )

    sub += " RETURN 1"
    return sub


def _compile_exists_subquery(
    ctx: CypherParser.OC_ExistsSubqueryContext,
    bind_vars: dict[str, Any],
) -> str:
    """Compile ``EXISTS { MATCH ... }`` to ``LENGTH(FOR ... RETURN 1) > 0``."""
    sq = ctx.oC_SubqueryBody()
    sub = _compile_subquery_body(sq, bind_vars)
    return f"(LENGTH({sub}) > 0)"


def _compile_count_subquery(
    ctx: CypherParser.OC_CountSubqueryContext,
    bind_vars: dict[str, Any],
) -> str:
    """Compile ``COUNT { MATCH ... }`` to ``LENGTH(FOR ... RETURN 1)``."""
    sq = ctx.oC_SubqueryBody()
    sub = _compile_subquery_body(sq, bind_vars)
    return f"LENGTH({sub})"


def _compile_pattern_hops(
    ctx: CypherParser.OC_RelationshipsPatternContext,
    bind_vars: dict[str, Any],
    *,
    prefix: str,
    introduces: Callable[[str], bool],
    path_var: str | None = None,
) -> tuple[list[str], list[str]]:
    """Compile a relationship pattern that starts at a bound variable to nested
    traversal loops, for a pattern predicate or a pattern comprehension.

    Returns the filters on the start node (its labels and properties, checked
    on the bound variable) and one ``FOR … FILTER …`` loop per relationship,
    honouring each hop's type, direction, range and properties and its end
    node's labels and properties. A named end node or relationship that
    *introduces* says is new becomes the loop variable (a named
    variable-length relationship is its list of edges); any other named one is
    already bound and is matched by ``_id``. One relationship never matches
    twice within the pattern. A *path_var* is bound after the last loop to
    ``{nodes, edges}``, as a named path in MATCH is.
    """
    start_node = ctx.oC_NodePattern()
    chains = ctx.oC_PatternElementChain() or []
    if not chains:
        raise CoreError("A pattern needs at least one relationship here", code="UNSUPPORTED")

    resolver = _active_resolver.get()
    if resolver is None:
        raise CoreError(
            "Pattern predicates and comprehensions require a mapping resolver", code="UNSUPPORTED"
        )

    start_var, _ = _extract_node_var_and_labels(start_node, default_var=f"_{prefix}_start")
    if start_var == f"_{prefix}_start":
        raise CoreError(
            "A pattern predicate or comprehension must start at a variable bound earlier in the query",
            code="UNSUPPORTED",
        )
    head_filters: list[str] = []
    if start_node.oC_NodeLabels() is not None:
        pred = _compile_label_predicate(start_var, start_node.oC_NodeLabels(), bind_vars)
        if pred is not None:
            head_filters.append(pred)
    head_filters += _compile_node_pattern_properties(start_node, var=start_var, bind_vars=bind_vars)

    loops: list[str] = []
    single_hop_edges: list[str] = []
    hop_paths: list[str] = []
    prev = start_var
    for i, chain in enumerate(chains):
        rel_pat = chain.oC_RelationshipPattern()
        target_node = chain.oC_NodePattern()
        if rel_pat is None or target_node is None:
            raise CoreError("Invalid relationship pattern", code="UNSUPPORTED")
        suffix = "" if i == 0 else f"_{i}"

        rel_type, rel_var, rel_range = _extract_relationship_type_and_var(
            rel_pat, default_var=f"_{prefix}_r{suffix}"
        )
        target_var, _ = _extract_node_var_and_labels(target_node, default_var=f"_{prefix}_t{suffix}")
        rel_named = rel_var != f"_{prefix}_r{suffix}"
        target_named = target_var != f"_{prefix}_t{suffix}"
        rmin, rmax = rel_range
        single = (rmin, rmax) == (1, 1)
        introduced_rel = rel_named and introduces(rel_var)
        loop_e = rel_var if introduced_rel and single else f"_{prefix}_e{suffix}"
        loop_v = target_var if target_named and introduces(target_var) else f"_{prefix}_v{suffix}"
        direction = _relationship_direction(rel_pat)
        r_map = _resolve_relationship_for_pattern(resolver, rel_type)
        typed_edges = r_map.get("style") == "GENERIC_WITH_TYPE"
        needs_path = path_var is not None or (not single and introduced_rel)
        hop_path = f"_{prefix}_p{suffix}" if needs_path else None

        edge_key = _pick_bind_key(f"@{prefix}Edge", bind_vars)
        bind_vars[edge_key] = r_map.get("edgeCollectionName") or r_map.get("collectionName")

        filters: list[str] = []
        if typed_edges:
            rtf = _pick_bind_key(f"{prefix}RelTF", bind_vars)
            rtv = _pick_bind_key(f"{prefix}RelTV", bind_vars)
            bind_vars[rtf] = r_map.get("typeField")
            bind_vars[rtv] = r_map.get("typeValue")
            # ``_guard_varlength_edge_types`` extends this last-edge test to
            # every edge of a variable-length hop with a PRUNE.
            filters.append(f"{loop_e}[@{rtf}] == @{rtv}")
        filters += _compile_relationship_pattern_properties(rel_pat, var=loop_e, bind_vars=bind_vars)

        if single:
            # Cypher never matches one relationship twice within a pattern.
            filters += [f"{loop_e}._id != {other}._id" for other in single_hop_edges]
            single_hop_edges.append(loop_e)

        if target_named and loop_v != target_var:
            filters.append(f"{loop_v}._id == {target_var}._id")
        labels_ctx = target_node.oC_NodeLabels()
        if labels_ctx is not None:
            labels = [
                _strip_label_backticks(nl.oC_LabelName().getText().strip())
                for nl in labels_ctx.oC_NodeLabel()
            ]
            # As in MATCH: a collection-style label needs no IS_SAME_COLLECTION
            # check when the relationship's domain/range already guarantees it.
            # (A label-style label is a type-field test that domain/range never
            # replaces: every member of a union shares the generic collection.)
            guaranteed = (
                len(labels) == 1
                and rel_type is not None
                and resolver.resolve_entity(labels[0]).get("style") == "COLLECTION"
                and resolver.edge_constrains_target(rel_type, labels[0], direction)
            )
            pred = None if guaranteed else _compile_label_predicate(loop_v, labels_ctx, bind_vars)
            if pred is not None:
                filters.append(pred)
        filters += _compile_node_pattern_properties(target_node, var=loop_v, bind_vars=bind_vars)

        loop_vars = f"{loop_v}, {loop_e}" + (f", {hop_path}" if hop_path else "")
        loop = f"FOR {loop_vars} IN {rmin}..{rmax} {direction} {prev} {_aql_collection_ref(edge_key)}"
        loop += "".join(f" FILTER {f}" for f in filters)
        if introduced_rel and not single:
            loop += f" LET {rel_var} = {hop_path}.edges"
        loops.append(loop)
        if hop_path:
            hop_paths.append(hop_path)
        prev = loop_v
    if path_var is not None:
        nodes = _concat_lists([f"[{start_var}]", *(f"SLICE({hp}.vertices, 1)" for hp in hop_paths)])
        edges = _concat_lists([f"{hp}.edges" for hp in hop_paths])
        loops[-1] += f" LET {path_var} = {{nodes: {nodes}, edges: {edges}}}"
    return head_filters, loops


def _concat_lists(parts: list[str]) -> str:
    """AQL concatenation of list expressions (``UNION`` keeps duplicates)."""
    return parts[0] if len(parts) == 1 else f"UNION({', '.join(parts)})"


def _compile_pattern_predicate(
    ctx: CypherParser.OC_RelationshipsPatternContext,
    bind_vars: dict[str, Any],
) -> str:
    """Compile a pattern predicate like ``(n)-[:KNOWS]->(m)`` to an AQL
    existence subquery::

        (LENGTH(FOR _pp_v, _pp_e IN 1..1 OUTBOUND n @@ppEdge
                  FILTER _pp_v._id == m._id … LIMIT 1 RETURN 1) > 0)

    The whole pattern is honoured (:func:`_compile_pattern_hops`). A pattern
    predicate cannot introduce a variable, so every named node is the one
    bound outside.

    Before this, only the first hop was compiled and the end node's variable,
    a collection-style label and any properties were dropped, so
    ``NOT (a)-[:KNOWS]-(b)`` meant "a knows nobody" (LDBC BI18 returned no
    rows; docs/ldbc_benchmark_plan.md §3.5).
    """
    head_filters, loops = _compile_pattern_hops(ctx, bind_vars, prefix="pp", introduces=lambda _name: False)
    exists = f"(LENGTH({' '.join(loops)} LIMIT 1 RETURN 1) > 0)"
    if head_filters:
        return "(" + " AND ".join([*head_filters, exists]) + ")"
    return exists


def _compile_quantifier(
    kind: str,
    filt_expr: CypherParser.OC_FilterExpressionContext,
    bind_vars: dict[str, Any],
) -> str:
    """Compile a list quantifier — ``any``/``all``/``none``/``single`` —
    ``<kind>(x IN <list> WHERE <pred>)`` with Cypher's three-valued result:
    the predicate is evaluated per element as a value and the true / false /
    null counts decide (``local_scopes.quantifier_aql``). A missing ``WHERE``
    defaults the predicate to ``true``.
    """
    owner = filt_expr.parentCtx
    id_in_coll = filt_expr.oC_IdInColl()
    var_name = id_in_coll.oC_Variable().getText().strip()
    aql_var = local_aql_name(var_name, owner)
    list_expr = _compile_expression(id_in_coll.oC_Expression(), bind_vars)
    where = filt_expr.oC_Where()
    predicate = _compile_value(where.oC_Expression(), bind_vars) if where is not None else "true"
    return quantifier_aql(
        kind,
        owner=owner,
        var=aql_var,
        list_aql=list_expr,
        predicate=rename_local(predicate, var_name, aql_var),
    )


def _reduce_additive_terms(body_ctx: CypherParser.OC_ExpressionContext) -> Any | None:
    """If *body_ctx* is a pure additive expression (no OR/XOR/AND/NOT/comparison
    at the top), return its ``OC_AddOrSubtractExpressionContext``; else ``None``."""
    or_expr = body_ctx.oC_OrExpression()
    xs = or_expr.oC_XorExpression()
    if len(xs) != 1:
        return None
    ands = xs[0].oC_AndExpression()
    if len(ands) != 1:
        return None
    nots = ands[0].oC_NotExpression()
    if len(nots) != 1:
        return None
    if nots[0].NOT():
        return None
    cmp = nots[0].oC_ComparisonExpression()
    if cmp.oC_PartialComparisonExpression():
        return None
    terms = cmp.oC_InExpression().oC_AddOrSubtractExpression()
    return terms[0] if len(terms) == 1 else None


def _compile_reduce(
    ctx: CypherParser.OC_ReduceExpressionContext,
    bind_vars: dict[str, Any],
) -> str:
    """Compile ``reduce(acc = init, x IN list | body)``.

    AQL has no general fold, so only the dominant **sum-fold** shape is
    supported: a body that is ``acc + f(x)`` (any additive arrangement where the
    accumulator appears once and every top-level operator is ``+``). That lowers
    to ``(init + SUM((FOR x IN list RETURN f(x))))``. Any other accumulation
    (``*``, string build, multi-reference, conditional) raises a clear
    capability error rather than mis-translating.
    """
    acc_var = ctx.oC_Variable().getText().strip()
    exprs = ctx.oC_Expression()  # [init, body]
    id_in_coll = ctx.oC_IdInColl()
    cypher_x_var = id_in_coll.oC_Variable().getText().strip()
    x_var = local_aql_name(cypher_x_var, ctx)
    init_aql = _compile_expression(exprs[0], bind_vars)
    list_aql = _compile_expression(id_in_coll.oC_Expression(), bind_vars)

    unsupported = CoreError(
        "reduce() is only supported for sum-style accumulation "
        "(reduce(acc = init, x IN list | acc + f(x))); AQL has no general fold",
        code="NOT_IMPLEMENTED",
    )

    add = _reduce_additive_terms(exprs[1])
    if add is None:
        raise unsupported
    terms = add.oC_MultiplyDivideModuloExpression()
    if len(terms) < 2:
        raise unsupported
    # Every top-level operator must be '+'.
    for i in range(1, len(terms)):
        if _extract_interleaved_op(add, i, {"+", "-"}) != "+":
            raise unsupported

    acc_pattern = re.compile(rf"(?<![\w.])({re.escape(acc_var)})(?![\w])")
    acc_indices = [i for i, t in enumerate(terms) if t.getText().strip() == acc_var]
    if len(acc_indices) != 1:
        raise unsupported  # accumulator must appear exactly once, as a bare term
    rest = [t for i, t in enumerate(terms) if i != acc_indices[0]]
    rest_aql_parts = [rename_local(_compile_expression(t, bind_vars), cypher_x_var, x_var) for t in rest]
    # The per-element value must not itself reference the accumulator.
    if any(acc_pattern.search(part) for part in rest_aql_parts):
        raise unsupported
    # Refuse string folds: Cypher ``+`` on strings means concatenation, but this
    # lowering uses AQL ``SUM`` (numeric only). A string literal in the init or a
    # fold term is the tell — reduce(s = "", x IN xs | s + x + "!") is a concat,
    # not a sum, so bail rather than emit a wrong numeric aggregate.
    if any('"' in part or "'" in part for part in [init_aql, *rest_aql_parts]):
        raise unsupported
    fold_value = " + ".join(rest_aql_parts)
    return f"({init_aql} + SUM((FOR {x_var} IN {list_aql} RETURN {fold_value})))"


def _compile_list_comprehension(
    ctx: CypherParser.OC_ListComprehensionContext,
    bind_vars: dict[str, Any],
) -> str:
    """Compile ``[x IN list WHERE cond | expr]`` to AQL subquery.

    Generates::
        (FOR x IN list FILTER cond RETURN expr)
    """
    filt_expr = ctx.oC_FilterExpression()
    id_in_coll = filt_expr.oC_IdInColl()
    cypher_var = id_in_coll.oC_Variable().getText().strip()
    var_name = local_aql_name(cypher_var, ctx)
    list_expr = _compile_expression(id_in_coll.oC_Expression(), bind_vars)

    where = filt_expr.oC_Where()
    filter_clause = ""
    if where is not None:
        cond = rename_local(_compile_where(where.oC_Expression(), bind_vars), cypher_var, var_name)
        filter_clause = f" FILTER {cond}"

    map_expr = ctx.oC_Expression()
    if map_expr is not None:
        projection = rename_local(_compile_expression(map_expr, bind_vars), cypher_var, var_name)
    else:
        projection = var_name

    return f"(FOR {var_name} IN {list_expr}{filter_clause} RETURN {projection})"


def _compile_pattern_comprehension(
    ctx: CypherParser.OC_PatternComprehensionContext,
    bind_vars: dict[str, Any],
) -> str:
    """Compile ``[(a)-[:REL]->(b) WHERE cond | expr]`` to an AQL subquery::

        (FOR b, _pc_e IN 1..1 OUTBOUND a @@pcEdge FILTER … FILTER cond RETURN expr)

    The whole pattern is honoured (:func:`_compile_pattern_hops`). A named
    node or relationship that is not bound outside is introduced by the
    comprehension; one that is bound (``[(tag)<-[i:HAS_INTEREST]-(person) | i]``
    with ``person`` in scope) is matched, not rebound.

    Before this, only the first hop was compiled, a bound end node was
    declared again (ERR 1511) and a collection-style label on the end node
    was ignored, so ``[(t)<-[:HAS_TAG]-(m:Message)-[:HAS_CREATOR]->(p) | m]``
    listed every tagged forum, post and comment (LDBC BI8).
    """
    path_var = ctx.oC_Variable().getText().strip() if ctx.oC_Variable() is not None else None
    in_scope = set(_active_user_vars.get()) | _enclosing_local_vars(ctx)
    head_filters, loops = _compile_pattern_hops(
        ctx.oC_RelationshipsPattern(),
        bind_vars,
        prefix="pc",
        introduces=lambda name: name not in in_scope,
        path_var=path_var,
    )
    # Grammar: ( WHERE oC_Expression )? '|' oC_Expression
    exprs = ctx.oC_Expression()
    filters = list(loops)
    if ctx.WHERE() is not None:
        filters.append(f"FILTER {_compile_where(exprs[0], bind_vars)}")
    projection = _compile_expression(exprs[-1], bind_vars)
    sub = f"({' '.join(filters)} RETURN {projection})"
    if head_filters:
        return f"(({' AND '.join(head_filters)}) ? {sub} : [])"
    return sub


def _enclosing_local_vars(ctx: Any) -> set[str]:
    """Variables bound around *ctx* by an enclosing list comprehension,
    quantifier, ``reduce()`` or pattern comprehension (``[x IN xs | …]``)."""
    names: set[str] = set()
    node = ctx.parentCtx
    while node is not None:
        if isinstance(node, CypherParser.OC_ListComprehensionContext):
            names.add(node.oC_FilterExpression().oC_IdInColl().oC_Variable().getText().strip())
        elif isinstance(node, CypherParser.OC_AtomContext) and node.oC_FilterExpression() is not None:
            names.add(node.oC_FilterExpression().oC_IdInColl().oC_Variable().getText().strip())
        elif isinstance(node, CypherParser.OC_ReduceExpressionContext):
            names.add(node.oC_Variable().getText().strip())
            names.add(node.oC_IdInColl().oC_Variable().getText().strip())
        elif isinstance(node, CypherParser.OC_PatternComprehensionContext):
            names.update(_pattern_comprehension_names(node))
        node = node.parentCtx
    return names


def _pattern_comprehension_names(ctx: CypherParser.OC_PatternComprehensionContext) -> set[str]:
    """Node and relationship variables named in a pattern comprehension's pattern."""
    names: set[str] = set()
    rel_pattern = ctx.oC_RelationshipsPattern()
    nodes = [rel_pattern.oC_NodePattern()]
    for chain in rel_pattern.oC_PatternElementChain() or []:
        nodes.append(chain.oC_NodePattern())
        detail = chain.oC_RelationshipPattern().oC_RelationshipDetail()
        if detail is not None and detail.oC_Variable() is not None:
            names.add(detail.oC_Variable().getText().strip())
    names.update(n.oC_Variable().getText().strip() for n in nodes if n.oC_Variable() is not None)
    return names


def _compile_case(
    ctx: CypherParser.OC_CaseExpressionContext,
    bind_vars: dict[str, Any],
) -> str:
    """Compile a Cypher CASE expression to nested AQL ternaries.

    AQL has no CASE expression (ArangoDB 3.11 parses ``CASE WHEN`` as a
    syntax error, ERR 1501), so ``CASE WHEN c1 THEN v1 WHEN c2 THEN v2 ELSE e
    END`` becomes ``(c1 ? v1 : (c2 ? v2 : e))``. The first true condition
    wins and a null condition counts as false, as in Cypher; a missing ELSE
    is ``null``. The simple form (``CASE x WHEN v THEN r``) compares the test
    expression to each WHEN value; a null test value matches no WHEN, since
    ``null = v`` is null in Cypher (AQL's ``null == null`` is true).
    """
    alternatives = ctx.oC_CaseAlternatives() or []
    all_exprs = ctx.oC_Expression() or []
    has_else = ctx.ELSE() is not None

    # Direct oC_Expression children of the CASE node:
    #   Generic form: 0 (no ELSE) or 1 (ELSE value)
    #   Simple form:  1 (test, no ELSE) or 2 (test + ELSE value)
    is_simple = len(all_exprs) > (1 if has_else else 0)

    if is_simple:
        test_expr = _compile_expression(all_exprs[0], bind_vars)
        else_expr_idx = 1
    else:
        test_expr = None
        else_expr_idx = 0

    branches: list[tuple[str, str]] = []
    for alt in alternatives:
        alt_exprs = alt.oC_Expression()
        when_e = _compile_expression(alt_exprs[0], bind_vars)
        then_e = _compile_expression(alt_exprs[1], bind_vars)
        if test_expr is not None:
            cond = _compile_comparison(test_expr, "=", when_e, "true")
        else:
            # A ternary's condition is a test: a null one takes the next branch.
            cond = _compile_where(alt_exprs[0], bind_vars)
        branches.append((cond, then_e))

    out = "null"
    if has_else and else_expr_idx < len(all_exprs):
        out = _compile_expression(all_exprs[else_expr_idx], bind_vars)
    for cond, then_e in reversed(branches):
        out = f"({cond} ? {then_e} : {out})"
    return out


def _compile_expression(ctx: Any, bind_vars: dict[str, Any]) -> str:
    # We only implement what the v0.1 basic corpus needs.
    # For everything else, fail fast with a structured error.

    if collecting_null_operands() and not is_arithmetic_transparent(ctx):
        # An operand of arithmetic being compiled: see ``null_propagating``.
        return compile_null_operand(lambda: _compile_expression(ctx, bind_vars))

    mode = _PREDICATE_MODE.get()
    if mode is not None and not _is_predicate_transparent(ctx):
        # An operand in a predicate position (``WHERE n.active``): compile it
        # as a value, then test it. See ``three_valued`` for the modes.
        return _test_truth(_compile_value(ctx, bind_vars), mode, two_valued=_is_two_valued(ctx))

    aggregate_var = aggregate_variable(ctx)
    if aggregate_var is not None:
        # An aggregate nested in a projection: see ``_hoist_nested_aggregates``.
        return aggregate_var

    folded = try_fold(ctx, bind_vars, shadowed=_enclosing_local_vars(ctx))
    if folded is not None:
        # A constant expression, evaluated with Cypher's semantics: see ``const_eval``.
        aql, value = folded
        two_valued = isinstance(value, bool)
        return _test_truth(aql, mode, two_valued=two_valued) if mode is not None else aql

    if isinstance(ctx, CypherParser.OC_ExpressionContext):
        return _compile_expression(ctx.oC_OrExpression(), bind_vars)

    if isinstance(ctx, CypherParser.OC_OrExpressionContext):
        parts = ctx.oC_XorExpression()
        if len(parts) == 1:
            return _compile_expression(parts[0], bind_vars)
        _reject_obvious_non_boolean_literals(parts, "OR")
        if mode is not None:
            # "a OR b" is true when either is; it is false when both are.
            joiner = " OR " if mode == "true" else " AND "
            return "(" + joiner.join(_compile_expression(part, bind_vars) for part in parts) + ")"
        return _compile_cypher_boolean_fold([_compile_expression(part, bind_vars) for part in parts], "OR")

    if isinstance(ctx, CypherParser.OC_XorExpressionContext):
        parts = ctx.oC_AndExpression()
        if len(parts) == 1:
            return _compile_expression(parts[0], bind_vars)
        # AQL has no XOR operator.  Preserve Cypher's three-valued semantics:
        # an XOR with a null operand is null; otherwise it is boolean inequality.
        _reject_obvious_non_boolean_literals(parts, "XOR")
        compiled = [_compile_value(part, bind_vars) for part in parts]
        result = compiled[0]
        for right in compiled[1:]:
            result = f"((({result}) == null OR ({right}) == null) ? null : (({result}) != ({right})))"
        return _test_truth(result, mode, two_valued=False)
    if isinstance(ctx, CypherParser.OC_AndExpressionContext):
        parts = ctx.oC_NotExpression()
        if len(parts) == 1:
            return _compile_expression(parts[0], bind_vars)
        _reject_obvious_non_boolean_literals(parts, "AND")
        if mode is not None:
            # "a AND b" is true when both are; it is false when either is.
            joiner = " AND " if mode == "true" else " OR "
            return "(" + joiner.join(_compile_expression(part, bind_vars) for part in parts) + ")"
        return _compile_cypher_boolean_fold([_compile_expression(part, bind_vars) for part in parts], "AND")

    if isinstance(ctx, CypherParser.OC_NotExpressionContext):
        # Grammar: (NOT SP?)* comparison
        cmp_ctx = ctx.oC_ComparisonExpression()
        negated = len(ctx.NOT()) % 2 == 1
        if mode is not None:
            # "NOT x" is true exactly when x is false: flip the test.
            return _compile_in_mode(cmp_ctx, bind_vars, _negate_mode(mode) if negated else mode)
        inner = _compile_expression(cmp_ctx, bind_vars)
        if negated:
            return _negate_value(inner, two_valued=_is_two_valued(cmp_ctx))
        return inner

    if isinstance(ctx, CypherParser.OC_InExpressionContext):
        terms = ctx.oC_AddOrSubtractExpression()
        if len(terms) == 1:
            return _compile_expression(terms[0], bind_vars)
        # ``a IN b IN c`` is ``(a IN b) IN c``; only the last test takes the
        # predicate mode.
        left = _compile_value(terms[0], bind_vars)
        for i, term in enumerate(terms[1:]):
            right = _compile_value(term, bind_vars)
            left = compile_in(left, right, mode if i == len(terms) - 2 else None)
        return left

    if isinstance(ctx, CypherParser.OC_ComparisonExpressionContext):
        partials = ctx.oC_PartialComparisonExpression()
        if not partials:
            return _compile_expression(ctx.oC_InExpression(), bind_vars)
        # A chained comparison ``a < b < c`` means ``a < b AND b < c``
        # (openCypher §Comparison): compile each adjacent pair and AND them.
        operand_ctxs = [ctx.oC_InExpression(), *(p.oC_InExpression() for p in partials)]
        operands = [_compile_value(c, bind_vars) for c in operand_ctxs]
        types = [_string_kind(c) for c in operand_ctxs]
        conjuncts = []
        for i, p in enumerate(partials):
            op = p.getChild(0).getText()
            # A computed temporal value and a stored one compare as epoch milliseconds.
            left, right = comparison_operands(
                operand_ctxs[i], operands[i], operand_ctxs[i + 1], operands[i + 1]
            )
            compared = None
            if op in ("<", "<=", ">", ">=") and (left, right) == (operands[i], operands[i + 1]):
                # A known string orders by code point, as in ORDER BY.
                compared = _code_point_operands(left, types[i], right, types[i + 1])
            conjunct = _compile_comparison(left, op, right, mode, compared=compared)
            if (
                temporal_strings.enabled()
                and op in ("<", "<=", ">", ">=")
                and (left, right) == (operands[i], operands[i + 1])
                and _static_type(operand_ctxs[i]) in (None, STRING)
                and _static_type(operand_ctxs[i + 1]) in (None, STRING)
            ):
                # Two Times or two DateTimes held as strings compare by instant.
                conjunct = temporal_strings.ordering(left, op, right, conjunct, negated=mode == "false")
            conjuncts.append(conjunct)
        if len(conjuncts) == 1:
            return conjuncts[0]
        if mode is None:
            return _compile_cypher_boolean_fold(conjuncts, "AND")
        return "(" + (" AND " if mode == "true" else " OR ").join(conjuncts) + ")"

    if isinstance(ctx, CypherParser.OC_AddOrSubtractExpressionContext):
        terms = ctx.oC_MultiplyDivideModuloExpression()
        if len(terms) == 1:
            return _compile_expression(terms[0], bind_vars)
        ops = [_extract_interleaved_op(ctx, i, {"+", "-"}) for i in range(1, len(terms))]
        if temporal_strings.enabled() and _maybe_temporal_pair(terms):
            # A temporal string and a duration, or two durations, read at
            # run time (``temporal_strings``); else Cypher's usual + / -.
            # A computed temporal or duration with anything else is a type
            # error in Cypher; the run-time read covers every valid pair.
            fallback = (
                "null" if has_temporal_operand(list(terms)) else _compile_additive(terms, ops, bind_vars)
            )
            return temporal_strings.additive(
                _compile_expression(terms[0], bind_vars),
                ops[0],
                _compile_expression(terms[1], bind_vars),
                fallback,
            )
        if any(is_duration(t) for t in terms):
            return compile_temporal_arithmetic(
                list(terms), ops, compile_expr=lambda e: _compile_expression(e, bind_vars)
            )
        return _compile_additive(terms, ops, bind_vars)

    if isinstance(ctx, CypherParser.OC_MultiplyDivideModuloExpressionContext):
        terms = ctx.oC_PowerOfExpression()
        if len(terms) == 1:
            return _compile_expression(terms[0], bind_vars)

        def build_product() -> str:
            result = _compile_expression(terms[0], bind_vars)
            result_type = _static_type(terms[0])
            for i in range(1, len(terms)):
                op = _extract_interleaved_op(ctx, i, {"*", "/", "%"})
                right = _compile_expression(terms[i], bind_vars)
                declared = (
                    declared_static_type(terms[i - 1]) if i == 1 else None,
                    declared_static_type(terms[i]),
                )
                result = raw_arithmetic(
                    result, op, right, result_type, _static_type(terms[i]), declared=declared
                )
                result_type = _numeric_type(result_type, terms[i])
            return result

        if temporal_strings.enabled() and len(terms) == 2:
            op = _extract_interleaved_op(ctx, 1, {"*", "/", "%"})
            if op in ("*", "/") and _maybe_duration_product(terms):
                # A duration string scaled by a number, read at run time.
                return temporal_strings.multiplicative(
                    _compile_expression(terms[0], bind_vars),
                    op,
                    _compile_expression(terms[1], bind_vars),
                    null_propagating(build_product),
                )
        return null_propagating(build_product)

    if isinstance(ctx, CypherParser.OC_PowerOfExpressionContext):
        terms = ctx.oC_UnaryAddOrSubtractExpression()
        if len(terms) == 1:
            return _compile_expression(terms[0], bind_vars)

        def build_power() -> str:
            result = _compile_expression(terms[0], bind_vars)
            for term in terms[1:]:
                result = f"POW({result}, {_compile_expression(term, bind_vars)})"
            return result

        return null_propagating(build_power)

    if isinstance(ctx, CypherParser.OC_UnaryAddOrSubtractExpressionContext):
        inner_ctx = ctx.oC_StringListNullOperatorExpression()
        has_minus = False
        for i in range(ctx.getChildCount()):
            if ctx.getChild(i).getText() == "-":
                has_minus = not has_minus
        if not has_minus:
            return _compile_expression(inner_ctx, bind_vars)
        return null_propagating(lambda: f"(-{_compile_expression(inner_ctx, bind_vars)})")

    if isinstance(ctx, CypherParser.OC_StringListNullOperatorExpressionContext):
        null_ops = ctx.oC_NullOperatorExpression()
        list_ops = ctx.oC_ListOperatorExpression()
        string_ops = ctx.oC_StringOperatorExpression()
        if not (null_ops or list_ops or string_ops):
            return _compile_expression(ctx.oC_PropertyOrLabelsExpression(), bind_vars)
        if mode is not None and list_ops:
            # A subscript or slice is a value, not a predicate: test it.
            return _test_truth(_compile_value(ctx, bind_vars), mode, two_valued=False)
        base = _compile_value(ctx.oC_PropertyOrLabelsExpression(), bind_vars)
        # Handle null operator: IS NULL / IS NOT NULL
        if null_ops:
            if len(null_ops) != 1:
                raise CoreError("Multiple null operators not supported in v0", code="UNSUPPORTED")
            t = null_ops[0].getText().upper().replace(" ", "")
            if t not in {"ISNULL", "ISNOTNULL"}:
                raise CoreError(f"Unsupported null operator: {t}", code="UNSUPPORTED")
            is_null = (t == "ISNULL") != (mode == "false")
            return f"({base} == null)" if is_null else f"({base} != null)"

        if list_ops:
            if len(list_ops) != 1:
                raise CoreError("Multiple list operators not supported in v0", code="UNSUPPORTED")
            lop = list_ops[0]
            # List subscript "expr[i]" and slice "expr[i..j]" / "[i..]" / "[..j]".
            # The grammar's list operator covers both; distinguish by the ".."
            # token in the operator's own text (it never includes the base).
            lop_text = lop.getText()
            exprs = lop.oC_Expression() or []
            if ".." in lop_text:
                # Half-open slice, Cypher semantics: [start, end). Map to AQL
                # SLICE(array, start, length). Bounds are optional on either side.
                inner = lop_text.strip()
                if inner.startswith("[") and inner.endswith("]"):
                    inner = inner[1:-1]
                left_text, _, right_text = inner.partition("..")
                has_start = left_text.strip() != ""
                has_end = right_text.strip() != ""
                start_ctx = end_ctx = None
                if has_start and has_end:
                    if len(exprs) >= 2:
                        start_ctx, end_ctx = exprs[0], exprs[1]
                elif has_start:
                    if exprs:
                        start_ctx = exprs[0]
                elif has_end:
                    if exprs:
                        end_ctx = exprs[0]
                start = _compile_expression(start_ctx, bind_vars) if start_ctx is not None else None
                end = _compile_expression(end_ctx, bind_vars) if end_ctx is not None else None
                if start is not None and end is not None:
                    # length = end - start (valid for non-negative bounds, the
                    # common case; negative-bound slices are a known limitation).
                    return f"SLICE({base}, {start}, ({end}) - ({start}))"
                if start is not None:
                    return f"SLICE({base}, {start})"
                if end is not None:
                    return f"SLICE({base}, 0, {end})"
                # "[..]" — whole list.
                return f"{base}"
            # Single subscript: AQL array access supports negative indices.
            if len(exprs) != 1:
                raise CoreError("list index expects 1 expression", code="UNSUPPORTED")
            idx = _compile_expression(exprs[0], bind_vars)
            return compile_subscript(
                base, idx, _static_type(ctx.oC_PropertyOrLabelsExpression()), _static_type(exprs[0])
            )

        if string_ops:
            if len(string_ops) != 1:
                raise CoreError(
                    "Chained string operators not supported in v0",
                    code="UNSUPPORTED",
                )
            sop = string_ops[0]
            rhs_ctx = sop.oC_PropertyOrLabelsExpression()
            rhs = _compile_value(rhs_ctx, bind_vars)
            if sop.STARTS() is not None:
                kind = "starts"
            elif sop.ENDS() is not None:
                kind = "ends"
            elif sop.CONTAINS() is not None:
                kind = "contains"
            elif "=~" in sop.getText():
                kind = "regex"
            else:
                raise CoreError("Unknown string operator", code="UNSUPPORTED")
            # Null unless both operands are strings (string_predicates).
            return compile_string_predicate(
                kind,
                base,
                rhs,
                base_is_string=_static_type(ctx.oC_PropertyOrLabelsExpression()) == STRING,
                rhs_is_string=_static_type(rhs_ctx) == STRING,
                mode=mode,
            )

        return base

    if isinstance(ctx, CypherParser.OC_PropertyOrLabelsExpressionContext):
        atom = _compile_expression(ctx.oC_Atom(), bind_vars)
        lookups = ctx.oC_PropertyLookup()
        keys = [lk.oC_PropertyKeyName().getText().strip() for lk in lookups or []]
        atom = compile_property_chain(ctx.oC_Atom(), atom, keys, _active_resolver.get())
        labels_ctx = ctx.oC_NodeLabels()
        if labels_ctx is not None and not lookups:
            # Label predicate on a bare variable (`n:Label`, `n:A:B`), e.g.
            # `WHERE risk:RISK_FACTOR`. Translate to the mapping's discriminator
            # filter instead of dropping it (which made the predicate a no-op).
            # Only the pure-variable case is handled — `n.prop:Label` is
            # degenerate Cypher and keeps the legacy suffix-ignoring behaviour.
            pred = _compile_label_predicate(atom, labels_ctx, bind_vars)
            if pred is not None:
                return pred
        # No resolver bound (or property+label combo): ignore the labels suffix.
        return atom

    if isinstance(ctx, CypherParser.OC_AtomContext):
        if ctx.oC_Literal() is not None:
            return _compile_expression(ctx.oC_Literal(), bind_vars)
        if ctx.oC_Parameter() is not None:
            return _compile_expression(ctx.oC_Parameter(), bind_vars)
        if ctx.oC_FunctionInvocation() is not None:
            return _compile_expression(ctx.oC_FunctionInvocation(), bind_vars)
        if ctx.oC_Variable() is not None:
            return ctx.oC_Variable().getText().strip()
        if ctx.oC_ParenthesizedExpression() is not None:
            return _compile_expression(ctx.oC_ParenthesizedExpression().oC_Expression(), bind_vars)
        if ctx.oC_CaseExpression() is not None:
            return _compile_case(ctx.oC_CaseExpression(), bind_vars)
        if ctx.oC_ExistsSubquery() is not None:
            return _compile_exists_subquery(ctx.oC_ExistsSubquery(), bind_vars)
        if ctx.oC_CountSubquery() is not None:
            return _compile_count_subquery(ctx.oC_CountSubquery(), bind_vars)
        if ctx.oC_RelationshipsPattern() is not None:
            return _compile_pattern_predicate(ctx.oC_RelationshipsPattern(), bind_vars)
        if ctx.oC_ListComprehension() is not None:
            return _compile_list_comprehension(ctx.oC_ListComprehension(), bind_vars)
        if ctx.oC_PatternComprehension() is not None:
            return _compile_pattern_comprehension(ctx.oC_PatternComprehension(), bind_vars)
        if ctx.oC_ReduceExpression() is not None:
            return _compile_reduce(ctx.oC_ReduceExpression(), bind_vars)
        # List quantifiers: any/all/none/single(x IN list WHERE pred)
        if ctx.oC_FilterExpression() is not None:
            if ctx.ANY() is not None:
                kind = "any"
            elif ctx.ALL() is not None:
                kind = "all"
            elif ctx.NONE() is not None:
                kind = "none"
            elif ctx.SINGLE() is not None:
                kind = "single"
            else:
                raise CoreError("Unsupported quantifier in v0", code="UNSUPPORTED")
            return _compile_quantifier(kind, ctx.oC_FilterExpression(), bind_vars)
        # COUNT(*) aggregate
        if ctx.COUNT() is not None:
            raise CoreError("COUNT(*) subquery not supported in v0", code="NOT_IMPLEMENTED")
        raise CoreError("Unsupported atom in v0", code="UNSUPPORTED")

    if isinstance(ctx, CypherParser.OC_LiteralContext):
        if ctx.oC_BooleanLiteral() is not None:
            t = ctx.oC_BooleanLiteral().getText().lower()
            return "true" if t == "true" else "false"
        if ctx.oC_NumberLiteral() is not None:
            return aql_number_literal(ctx.oC_NumberLiteral())
        if ctx.StringLiteral() is not None:
            return _aql_string_from_cypher(ctx.StringLiteral().getText())
        if ctx.NULL() is not None:
            return "null"
        if ctx.oC_ListLiteral() is not None:
            inner = ctx.oC_ListLiteral()
            exprs = inner.oC_Expression() or []
            return "[" + ",".join(_compile_expression(e, bind_vars) for e in exprs) + "]"
        if ctx.oC_MapLiteral() is not None:
            ml = ctx.oC_MapLiteral()
            keys = ml.oC_PropertyKeyName() or []
            vals = ml.oC_Expression() or []
            pairs = [
                f"{k.getText().strip()}: {_compile_expression(v, bind_vars)}"
                for k, v in zip(keys, vals, strict=False)
            ]
            return "{" + ", ".join(pairs) + "}"
        raise CoreError("Unsupported literal in v0", code="UNSUPPORTED")

    if isinstance(ctx, CypherParser.OC_ParameterContext):
        txt = ctx.getText()
        if not txt.startswith("$"):
            raise CoreError("Invalid parameter syntax", code="UNSUPPORTED")
        # A positional parameter ($0, $1) is the AQL bind parameter of that
        # name: AQL bind names may start with a digit.
        # Bind var value (if provided) is already in bind_vars; leave as-is.
        return _aql_bind_name(txt)

    if isinstance(ctx, CypherParser.OC_FunctionInvocationContext):
        fn = ctx.oC_FunctionName().getText()
        fn_norm = fn.lower()
        args = ctx.oC_Expression() or []
        compiled_args = [_compile_expression(a, bind_vars) for a in args]

        # minimal function mapping for corpus
        if fn_norm == "size":
            if len(compiled_args) != 1:
                raise CoreError("size expects 1 arg", code="UNSUPPORTED")
            return f"LENGTH({compiled_args[0]})"
        # ``toLower``/``toUpper`` are canonical openCypher; ``lower``/``upper``
        # are common non-canonical aliases LLMs (and some dialects) emit. Accept
        # both so generated queries don't fail on a casing-helper name mismatch.
        if fn_norm in ("tolower", "lower"):
            if len(compiled_args) != 1:
                raise CoreError("toLower expects 1 arg", code="UNSUPPORTED")
            return f"LOWER({compiled_args[0]})"
        if fn_norm in ("toupper", "upper"):
            if len(compiled_args) != 1:
                raise CoreError("toUpper expects 1 arg", code="UNSUPPORTED")
            return f"UPPER({compiled_args[0]})"
        if fn_norm == "coalesce":
            if not compiled_args:
                raise CoreError("coalesce expects at least 1 arg", code="UNSUPPORTED")
            # AQL's NOT_NULL() returns its first non-null argument: Cypher's
            # coalesce(). (The previous FIRST(REMOVE_VALUES([…], null)) removed
            # nothing — REMOVE_VALUES takes an *array* of values — so it always
            # returned the first argument, null or not; LDBC IS2.)
            return f"NOT_NULL({', '.join(compiled_args)})"
        if fn_norm == "id":
            if len(compiled_args) != 1:
                raise CoreError("id expects 1 arg", code="UNSUPPORTED")
            return f"{compiled_args[0]}._id"
        if fn_norm == "keys":
            if len(compiled_args) != 1:
                raise CoreError("keys expects 1 arg", code="UNSUPPORTED")
            return compile_keys(compiled_args[0], _active_resolver.get())
        if fn_norm == "properties":
            if len(compiled_args) != 1:
                raise CoreError("properties expects 1 arg", code="UNSUPPORTED")
            return compile_properties(compiled_args[0], _active_resolver.get())
        if fn_norm == "tostring":
            if len(compiled_args) != 1:
                raise CoreError("toString expects 1 arg", code="UNSUPPORTED")
            return compile_to_string(compiled_args[0], _static_type(args[0]))
        if fn_norm in ("tointeger", "toint"):
            if len(compiled_args) != 1:
                raise CoreError("toInteger expects 1 arg", code="UNSUPPORTED")
            return compile_to_integer(compiled_args[0], _static_type(args[0]))
        if fn_norm == "tofloat":
            if len(compiled_args) != 1:
                raise CoreError("toFloat expects 1 arg", code="UNSUPPORTED")
            return compile_to_float(compiled_args[0], _static_type(args[0]))
        if fn_norm in ("toboolean", "tobool"):
            if len(compiled_args) != 1:
                raise CoreError("toBoolean expects 1 arg", code="UNSUPPORTED")
            return compile_to_boolean(compiled_args[0], _static_type(args[0]))

        if fn_norm == "exists":
            if len(compiled_args) != 1:
                raise CoreError("exists expects 1 arg", code="UNSUPPORTED")
            if _is_two_valued(args[0]):
                # exists((a)-->(b)) is the pattern predicate itself; ``!= null``
                # of a boolean was always true.
                return compiled_args[0]
            return f"({compiled_args[0]} != null)"
        if fn_norm == "abs":
            if len(compiled_args) != 1:
                raise CoreError("abs expects 1 arg", code="UNSUPPORTED")
            return f"ABS({compiled_args[0]})"
        if fn_norm == "ceil":
            if len(compiled_args) != 1:
                raise CoreError("ceil expects 1 arg", code="UNSUPPORTED")
            return f"CEIL({compiled_args[0]})"
        if fn_norm == "floor":
            if len(compiled_args) != 1:
                raise CoreError("floor expects 1 arg", code="UNSUPPORTED")
            return f"FLOOR({compiled_args[0]})"
        if fn_norm == "round":
            if len(compiled_args) != 1:
                raise CoreError("round expects 1 arg", code="UNSUPPORTED")
            return f"ROUND({compiled_args[0]})"
        if fn_norm == "sign":
            if len(compiled_args) != 1:
                raise CoreError("sign expects 1 arg", code="UNSUPPORTED")
            return f"(({compiled_args[0]}) > 0 ? 1 : (({compiled_args[0]}) < 0 ? -1 : 0))"
        if fn_norm == "rand":
            return "RAND()"
        if fn_norm == "length":
            if len(compiled_args) != 1:
                raise CoreError("length expects 1 arg", code="UNSUPPORTED")
            pvars = _active_path_vars.get()
            path_arg = args[0].getText().strip() if args else compiled_args[0]
            if path_arg in pvars:
                return f"LENGTH({path_arg}.edges)"
            return f"LENGTH({compiled_args[0]})"
        if fn_norm == "left":
            if len(compiled_args) != 2:
                raise CoreError("left expects 2 args", code="UNSUPPORTED")
            return f"LEFT({compiled_args[0]}, {compiled_args[1]})"
        if fn_norm == "right":
            if len(compiled_args) != 2:
                raise CoreError("right expects 2 args", code="UNSUPPORTED")
            return f"RIGHT({compiled_args[0]}, {compiled_args[1]})"
        if fn_norm == "ltrim":
            if len(compiled_args) != 1:
                raise CoreError("lTrim expects 1 arg", code="UNSUPPORTED")
            return f"LTRIM({compiled_args[0]})"
        if fn_norm == "rtrim":
            if len(compiled_args) != 1:
                raise CoreError("rTrim expects 1 arg", code="UNSUPPORTED")
            return f"RTRIM({compiled_args[0]})"
        if fn_norm == "trim":
            if len(compiled_args) != 1:
                raise CoreError("trim expects 1 arg", code="UNSUPPORTED")
            return f"TRIM({compiled_args[0]})"
        if fn_norm == "replace":
            if len(compiled_args) != 3:
                raise CoreError("replace expects 3 args", code="UNSUPPORTED")
            return f"SUBSTITUTE({compiled_args[0]}, {compiled_args[1]}, {compiled_args[2]})"
        if fn_norm == "substring":
            if len(compiled_args) == 2:
                return f"SUBSTRING({compiled_args[0]}, {compiled_args[1]})"
            if len(compiled_args) == 3:
                return f"SUBSTRING({compiled_args[0]}, {compiled_args[1]}, {compiled_args[2]})"
            raise CoreError("substring expects 2-3 args", code="UNSUPPORTED")
        if fn_norm == "reverse":
            if len(compiled_args) != 1:
                raise CoreError("reverse expects 1 arg", code="UNSUPPORTED")
            return f"REVERSE({compiled_args[0]})"
        if fn_norm == "split":
            if len(compiled_args) not in (1, 2):
                raise CoreError("split expects 1-2 args", code="UNSUPPORTED")
            return f"SPLIT({', '.join(compiled_args)})"
        if fn_norm == "nodes":
            if len(compiled_args) != 1:
                raise CoreError("nodes expects 1 arg", code="UNSUPPORTED")
            pvars = _active_path_vars.get()
            path_arg = args[0].getText().strip() if args else compiled_args[0]
            if path_arg in pvars:
                return f"{path_arg}.nodes"
            return f"{compiled_args[0]}.nodes"
        if fn_norm == "relationships" or fn_norm == "rels":
            if len(compiled_args) != 1:
                raise CoreError("relationships expects 1 arg", code="UNSUPPORTED")
            pvars = _active_path_vars.get()
            path_arg = args[0].getText().strip() if args else compiled_args[0]
            if path_arg in pvars:
                return f"{path_arg}.edges"
            return f"{compiled_args[0]}.edges"
        if fn_norm == "head":
            if len(compiled_args) != 1:
                raise CoreError("head expects 1 arg", code="UNSUPPORTED")
            return f"FIRST({compiled_args[0]})"
        if fn_norm == "last":
            if len(compiled_args) != 1:
                raise CoreError("last expects 1 arg", code="UNSUPPORTED")
            return f"LAST({compiled_args[0]})"
        if fn_norm == "tail":
            if len(compiled_args) != 1:
                raise CoreError("tail expects 1 arg", code="UNSUPPORTED")
            return f"SLICE({compiled_args[0]}, 1)"
        if fn_norm == "range":
            if len(compiled_args) == 2:
                return f"RANGE({compiled_args[0]}, {compiled_args[1]})"
            if len(compiled_args) == 3:
                return f"RANGE({compiled_args[0]}, {compiled_args[1]}, {compiled_args[2]})"
            raise CoreError("range expects 2-3 args", code="UNSUPPORTED")
        if fn_norm == "type":
            if len(compiled_args) != 1:
                raise CoreError("type expects 1 arg", code="UNSUPPORTED")
            r_var = compiled_args[0]
            if r_var.strip() == "null":
                return "null"
            resolver = _active_resolver.get()
            type_field = _shared_type_field(resolver, "relationships") if resolver else None
            if type_field:
                # Generic-edge shape (e.g. GraphRAG/LPG: all edges in one
                # collection discriminated by ``type``): the Cypher
                # relationship type is the discriminator value. Fall back
                # to the collection name for dedicated edges that lack the
                # field, so mixed schemas stay correct.
                return (
                    f"({r_var}.{type_field} != null ? {r_var}.{type_field} "
                    f": PARSE_IDENTIFIER({r_var}._id).collection)"
                )
            # Dedicated-collection shape: the edge collection name encodes
            # the relationship type.
            return f"PARSE_IDENTIFIER({r_var}._id).collection"
        if fn_norm == "labels":
            if len(compiled_args) != 1:
                raise CoreError("labels expects 1 arg", code="UNSUPPORTED")
            n_var = compiled_args[0]
            if n_var.strip() == "null":
                return "null"
            if args and args[0].getText().strip() in _active_path_vars.get():
                # openCypher InvalidArgumentType: labels() takes a node.
                raise CoreError("labels() expects a node, not a path", code="UNSUPPORTED")
            resolver = _active_resolver.get()
            type_field = _shared_type_field(resolver, "entities") if resolver else None
            multi = compile_labels(n_var, resolver, type_field) if type_field else None
            if multi is not None:
                return multi
            if type_field:
                # Type-discriminated nodes (LABEL / GENERIC_WITH_TYPE) carry
                # their label in the discriminator field; dedicated /
                # side-store collections (e.g. chunks) fall back to the
                # collection name.
                return (
                    f"[{n_var}.{type_field} != null ? {n_var}.{type_field} "
                    f": PARSE_IDENTIFIER({n_var}._id).collection]"
                )
            return f"[PARSE_IDENTIFIER({n_var}._id).collection]"
        if fn_norm == "timestamp":
            return "DATE_NOW()"
        if fn_norm == "duration":
            return compile_duration(list(args), compile_expr=lambda e: _compile_expression(e, bind_vars))
        if fn_norm in ("date", "datetime", "localdatetime"):
            return compile_temporal_function(
                fn_norm, list(args), compiled_args, compile_expr=lambda e: _compile_expression(e, bind_vars)
            )
        if fn_norm == "e":
            return "2.718281828459045"
        if fn_norm == "pi":
            return "PI()"
        if fn_norm == "log":
            if len(compiled_args) != 1:
                raise CoreError("log expects 1 arg", code="UNSUPPORTED")
            return f"LOG({compiled_args[0]})"
        if fn_norm == "log10":
            if len(compiled_args) != 1:
                raise CoreError("log10 expects 1 arg", code="UNSUPPORTED")
            return f"LOG10({compiled_args[0]})"
        if fn_norm == "sqrt":
            if len(compiled_args) != 1:
                raise CoreError("sqrt expects 1 arg", code="UNSUPPORTED")
            return f"SQRT({compiled_args[0]})"

        if fn_norm.startswith("arango."):
            registry = _active_registry.get()
            if registry is None:
                raise CoreError(
                    f"arango.* extension '{fn}' requires a registry "
                    f"(pass TranslateOptions(registry=...) to translate)",
                    code="EXTENSIONS_DISABLED",
                )
            return registry.compile_function(fn_norm, compiled_args, bind_vars)

        raise CoreError(f"Unsupported function in v0: {fn}", code="UNSUPPORTED")

    raise CoreError(f"Unsupported expression node: {type(ctx).__name__}", code="UNSUPPORTED")


def _is_obvious_non_boolean_literal(expr: str) -> bool:
    """Return whether *expr* is a literal Cypher cannot use with ``XOR``.

    Variables and compound expressions are deliberately left to runtime because
    their type depends on graph data. Literal arguments are statically known,
    and rejecting them matches the TCK's invalid-argument contract.
    """
    text = expr.strip()
    if text.lower() in {"true", "false", "null"}:
        return False
    return bool(
        re.fullmatch(r"[+-]?\d+(?:\.\d+)?", text)
        or (len(text) >= 2 and text[0] in {"'", '"'} and text[-1] == text[0])
        or (text.startswith("[") and text.endswith("]"))
        or (text.startswith("{") and text.endswith("}"))
    )


def _reject_obvious_non_boolean_literals(parts: list[Any], operator: str) -> None:
    """Reject literal operands that Cypher type-checks before evaluation."""
    for part in parts:
        if _is_obvious_non_boolean_literal(part.getText().strip()):
            raise CoreError(f"{operator} requires boolean operands", code="UNSUPPORTED")


def _compile_cypher_boolean_fold(parts: list[str], operator: str) -> str:
    """Fold boolean operands while preserving Cypher's three-valued logic."""
    result = parts[0]
    for right in parts[1:]:
        if operator == "AND":
            result = (
                f"((({result}) == false OR ({right}) == false) ? false : "
                f"((({result}) == true AND ({right}) == true) ? true : null))"
            )
        elif operator == "OR":
            result = (
                f"((({result}) == true OR ({right}) == true) ? true : "
                f"((({result}) == false AND ({right}) == false) ? false : null))"
            )
        else:  # pragma: no cover - internal fixed call sites
            raise ValueError(f"Unsupported boolean operator: {operator}")
    return result
