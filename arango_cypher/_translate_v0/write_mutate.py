"""``MATCH … SET / DELETE / REMOVE`` compilation for the v0 translator."""

from __future__ import annotations

import re
from typing import Any

from arango_query_core import AqlQuery, CoreError, MappingResolver

from .._antlr.CypherParser import CypherParser
from .label_tests import label_test
from .naming import _aql_collection_ref, _pick_bind_key, _pick_fresh_var, _strip_label_backticks
from .property_writes import keep_fields_for
from .supertypes import union_scan_source
from .write_create import _find_or_create_collection_bind_key
from .writes import (
    _append_return,
    _compile_expression,
    _compile_label_predicate,
    _compile_node_pattern_properties,
    _compile_relationship_pattern_properties,
    _compile_where,
    _extract_node_var_and_labels,
    _extract_relationship_type_and_var,
    _infer_unlabeled_collection,
    _multi_label_filter,
    _pick_primary_entity_label,
    _property_writes,
    _refuse_repeated_collections,
    _relationship_direction,
    _resolve_relationship_for_pattern,
    _stored_write_line,
)


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
