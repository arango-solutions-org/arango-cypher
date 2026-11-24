"""``MATCH … SET / DELETE / REMOVE`` compilation for the v0 translator."""

from __future__ import annotations

import re
from typing import Any

from arango_query_core import AqlQuery, CoreError, MappingResolver

from .._antlr.CypherParser import CypherParser
from .naming import _aql_collection_ref, _pick_bind_key, _pick_fresh_var, _strip_label_backticks
from .pipeline_writes import end_on_write
from .property_writes import keep_fields_for
from .write_create import _find_or_create_collection_bind_key
from .writes import (
    _append_return,
    _compile_expression,
    _extract_node_var_and_labels,
    _extract_relationship_type_and_var,
    _infer_unlabeled_collection,
    _pick_primary_entity_label,
    _property_writes,
    _refuse_repeated_collections,
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
    parts = mc.oC_Pattern().oC_PatternPart() or []
    if not parts:
        raise CoreError("MATCH pattern is required", code="UNSUPPORTED")

    # The pattern compiles as any MATCH does (every part, a named path, the
    # WHERE); a named variable's AQL name is its Cypher name.
    from .core import _compile_match_from_nothing

    lines, forbidden = _compile_match_from_nothing(mc, resolver=resolver, bind_vars=bind_vars)
    write_targets, union_vars, row_vars = _write_targets(parts, resolver=resolver)
    # Bind key of the collection each written variable lives in.
    write_keys: dict[str, str] = {}

    # ``OPTIONAL MATCH … SET/REMOVE/DELETE``: with no match there is one row
    # whose variables are null, and a write to null does nothing.
    optional = mc.OPTIONAL() is not None
    if optional:
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
        if target_var in write_keys:
            return write_keys[target_var]
        if target_var not in write_targets:
            raise CoreError(
                f"{verb} targets {target_var!r}, which is not a node or relationship of the MATCH pattern",
                code="UNSUPPORTED",
            )
        target = write_targets[target_var]
        if target_var in union_vars:
            raise CoreError(
                f"{verb} on {target_var!r} is not supported: it is matched as the supertype "
                f"{union_vars[target_var]!r}, whose members live in several collections",
                code="UNSUPPORTED",
            )
        if target is None:
            raise CoreError(
                f"{verb} on {target_var!r}, a variable-length relationship (a list of edges), "
                "is not supported",
                code="UNSUPPORTED",
            )
        base, collection = target
        if collection is None:
            collection = _infer_unlabeled_collection(resolver)
        key = _find_or_create_collection_bind_key(base, collection, bind_vars)
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
            if ret is None:
                # Nothing reads the written document: a plain subquery, which
                # ``end_on_write`` turns into the last statement if it is last.
                done = _pick_fresh_var(f"_set_{target_var}", forbidden_vars=forbidden | set(row_vars))
                lines.append(f"  LET {done} = ({guarded(target_var, statement)})")
                continue
            updated = _pick_fresh_var(
                f"{target_var}_new", forbidden_vars=forbidden | set(row_vars) | set(return_env.values())
            )
            lines.append(f"  LET {updated} = FIRST({guarded(target_var, statement)} RETURN NEW)")
            return_env[target_var] = updated
            continue
        lines.append(_stored_write_line(target_var, _aql_collection_ref(key), write))
        if ret is not None:
            updated = _pick_fresh_var(
                f"{target_var}_new", forbidden_vars=forbidden | set(row_vars) | set(return_env.values())
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
        var_env = {v: return_env.get(v, v) for v in row_vars} if return_env else None
        _append_return(ret.oC_ProjectionBody(), lines=lines, bind_vars=bind_vars, var_env=var_env)
    else:
        # ``OPTIONAL MATCH … SET`` / ``DELETE`` with no RETURN ends on a
        # null-guarded ``LET``, and AQL ends a query with RETURN or a write
        # (ERR 1501).
        end_on_write(lines)

    return AqlQuery(text="\n".join(lines), bind_vars=bind_vars)


def _write_targets(
    parts: list[CypherParser.OC_PatternPartContext],
    *,
    resolver: MappingResolver,
) -> tuple[dict[str, tuple[str, str | None] | None], dict[str, str], list[str]]:
    """What each named variable of the MATCH *parts* can be written in.

    Returns ``(write_targets, union_vars, row_vars)``. *write_targets* maps a
    node or relationship to ``(bind key base, collection)``, the collection
    ``None`` for an unlabeled node (inferred when it is written, failing
    closed when that is ambiguous); the bind key is made only when the
    variable is written, as ArangoDB refuses an unused one (ERR 1552). It
    maps to ``None`` what cannot be written — a variable-length
    relationship (a list of edges) or a node matched by a UNION supertype,
    which *union_vars* names. *row_vars* lists every named variable, the path
    included, in pattern order.
    """
    targets: dict[str, tuple[str, str | None] | None] = {}
    union_vars: dict[str, str] = {}
    row_vars: list[str] = []

    def seen(name: str) -> None:
        if name not in row_vars:
            row_vars.append(name)

    for part in parts:
        if part.oC_Variable() is not None:
            seen(part.oC_Variable().getText().strip())
        elem = part.oC_AnonymousPatternPart().oC_PatternElement()
        chains = elem.oC_PatternElementChain() or []
        for node in [elem.oC_NodePattern(), *(chain.oC_NodePattern() for chain in chains)]:
            if node is None or node.oC_Variable() is None:
                continue
            var, labels = _extract_node_var_and_labels(node, default_var="_")
            seen(var)
            if not labels:
                targets.setdefault(var, ("@collection", None))
                continue
            known = targets.get(var)
            if known is not None and known[1] is not None:
                continue
            primary = _pick_primary_entity_label(labels, resolver)
            emap = resolver.resolve_entity(_strip_label_backticks(primary))
            if emap.get("style") == "UNION":
                union_vars[var] = _strip_label_backticks(primary)
                targets[var] = None
            else:
                targets[var] = ("@collection", emap["collectionName"])
        for chain in chains:
            rel_pat = chain.oC_RelationshipPattern()
            detail = rel_pat.oC_RelationshipDetail() if rel_pat is not None else None
            if detail is None or detail.oC_Variable() is None:
                continue
            rel_type, rel_var, _ = _extract_relationship_type_and_var(rel_pat, default_var="_")
            seen(rel_var)
            if rel_var in targets:
                continue
            if detail.oC_RangeLiteral() is not None:
                targets[rel_var] = None
                continue
            r_map = _resolve_relationship_for_pattern(resolver, rel_type)
            targets[rel_var] = (
                "@edgeCollection",
                r_map.get("edgeCollectionName") or r_map.get("collectionName"),
            )
    return targets, union_vars, row_vars
