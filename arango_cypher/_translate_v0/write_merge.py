"""MERGE compilation for the v0 translator: node, relationship, multi-hop and multi-MERGE UPSERTs."""

from __future__ import annotations

import json
from typing import Any

from arango_query_core import AqlQuery, CoreError, MappingResolver

from .._antlr.CypherParser import CypherParser
from .label_tests import label_write_entries, shared_label_fields
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
from .naming import _aql_collection_ref, _pick_bind_key, _strip_label_backticks
from .scopes import outer_bound_names
from .state import _active_resolver
from .write_create import (
    _build_insert_doc,
    _compile_create_props,
    _compile_create_rel_props,
    _compile_return_for_create,
    _find_or_create_collection_bind_key,
)
from .write_guards import (
    trailing_set_fields,
)
from .writes import (
    _atom_variable,
    _compile_cypher_fragment,
    _compile_expression,
    _compile_write_reading_clauses,
    _extract_node_var_and_labels,
    _infer_unlabeled_collection,
    _infer_var_collections_from_pipeline,
    _pick_primary_entity_label,
    _relationship_direction,
    _supertype_label_fields,
)


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


def _refuse_unmergeable_relationships(chains: list, bind_vars: dict[str, Any]) -> None:
    """Refuse a relationship no MERGE can write: variable-length, or with a null property.

    openCypher rejects both (CreatingVarLength at compile time,
    MergeReadOwnWrites at run time); the UPSERT ignored the range and searched
    for the null.
    """
    for chain in chains:
        detail = chain.oC_RelationshipPattern().oC_RelationshipDetail()
        if detail is None:
            continue
        if detail.oC_RangeLiteral() is not None:
            raise CoreError("MERGE cannot merge a variable-length relationship", code="UNSUPPORTED")
        props = _compile_create_props(detail.oC_Properties(), dict(bind_vars))
        if any(v.strip() == "null" for _, v in props):
            raise CoreError("MERGE cannot match a null property value", code="UNSUPPORTED")


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
    _refuse_unmergeable_relationships(chains, bind_vars)
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
    _refuse_unmergeable_relationships([chain], bind_vars)

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
