"""``… WITH … [DETACH] DELETE <expression>``: read every row, then remove once.

A DELETE after a WITH may name any expression that evaluates to a node, a
relationship or a path — an element of a collected list or map
(``WITH collect(n) AS friends DETACH DELETE friends[$i]``, TCK Delete5),
not only a variable the pipeline's FOR loop binds. Its target is known only
per row, so the REMOVE cannot sit inside the loop with a fixed collection.

As in :mod:`deletes` (the single-part form), the pipeline compiles into a
list of rows first, each row carrying the values its DELETE names; then the
ids are gathered — a path contributing its nodes and relationships, ``null``
skipped (Cypher deletes nothing for it), anything else failing as Neo4j does
— and each collection is written by one ``REMOVE``: edges first (with, for
DETACH, every edge touching a deleted node), then nodes. Every read happens
before the first write and no collection is written twice (ERR 1579).
"""

from __future__ import annotations

import re
from typing import Any

from arango_query_core import AqlQuery, CoreError, MappingResolver

from .._antlr.CypherParser import CypherParser
from .deletes import _node_collections, _removal
from .literals import _aql_string_literal
from .naming import _aql_collection_ref, _pick_bind_key

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_NOT_AN_ENTITY = _aql_string_literal(
    "DELETE: a value that is not a node, a relationship or a path can not be deleted"
)


def translate_pipeline_delete(
    delete_clauses: list[CypherParser.OC_DeleteContext],
    *,
    tail: CypherParser.OC_SinglePartQueryContext,
    lines: list[str],
    var_env: dict[str, str],
    bind_vars: dict[str, Any],
    resolver: MappingResolver,
) -> AqlQuery:
    """Compile DELETE clauses after a WITH pipeline already in *lines* (module docstring)."""
    from .writes import _append_return, _compile_expression, _rewrite_vars

    detach = any(dc.DETACH() is not None for dc in delete_clauses)
    targets: list[str] = []
    for dc in delete_clauses:
        for expr in dc.oC_Expression() or []:
            compiled = _compile_expression(expr, bind_vars)
            targets.append(_rewrite_vars(compiled, var_env) if var_env else compiled)

    # 1. The rows, each with the values its DELETE names.
    head: list[str] = []
    body = list(lines)
    while body and body[0].startswith("WITH "):
        head.append(body.pop(0))  # a WITH declaration must open the whole query
    row_vars = sorted({aql for aql in var_env.values() if _IDENTIFIER.fullmatch(aql)})
    fields = [f"{v}: {v}" for v in row_vars] + [f"_targets: [{', '.join(targets)}]"]
    out = [
        *head,
        "LET _matched = (",
        *(f"  {line}" for line in body),
        f"  RETURN {{{', '.join(fields)}}}",
        ")",
    ]

    # 2. The ids: a path gives its nodes and relationships; null deletes nothing.
    out.append(
        "LET _targets = (FOR _t IN FLATTEN(_matched[*]._targets) FILTER _t != null "
        f"RETURN IS_OBJECT(_t) ? _t : FAIL({_NOT_AN_ENTITY}))"
    )
    out.append(
        "LET _deleted_nodes = UNIQUE(FLATTEN((FOR _t IN _targets RETURN "
        "(HAS(_t, 'nodes') AND HAS(_t, 'edges')) ? _t.nodes[*]._id : HAS(_t, '_from') ? [] : [_t._id])))"
    )
    out.append(
        "LET _deleted_rels = UNIQUE(FLATTEN((FOR _t IN _targets RETURN "
        "(HAS(_t, 'nodes') AND HAS(_t, 'edges')) ? _t.edges[*]._id : HAS(_t, '_from') ? [_t._id] : [])))"
    )
    edge_parts = ["_deleted_rels"]
    edge_keys: list[tuple[str, str]] = []
    for i, ec in enumerate(resolver.all_edge_collections()):
        ec_key = _pick_bind_key("@deleteEdges", bind_vars)
        bind_vars[ec_key] = ec
        edge_keys.append((ec, ec_key))
        if detach:
            out.append(
                f"LET _detach{i} = (FOR _n IN _deleted_nodes FOR _x, _e IN 1..1 ANY _n "
                f"{_aql_collection_ref(ec_key)} RETURN _e._id)"
            )
            edge_parts.append(f"_detach{i}")
    joined = edge_parts[0] if len(edge_parts) == 1 else f"UNION({', '.join(edge_parts)})"
    out.append(f"LET _deleted_edges = UNIQUE({joined})")

    # 3. One REMOVE per collection, edges first.
    removals = [_removal("_deleted_edges", ec, ec_key, bind_vars) for ec, ec_key in edge_keys]
    for coll in _node_collections([], resolver):
        coll_key = _pick_bind_key("@deleteNodes", bind_vars)
        bind_vars[coll_key] = coll
        removals.append(_removal("_deleted_nodes", coll, coll_key, bind_vars))
    if not removals:
        raise CoreError("DELETE needs a node or edge collection in the mapping", code="INVALID_MAPPING")

    ret = tail.oC_Return()
    if ret is None:
        # A query cannot end on a LET: the last removal is the statement.
        out += [f"LET _removed{i} = ({r})" for i, r in enumerate(removals[:-1])]
        out.append(removals[-1])
        return AqlQuery(text="\n".join(out), bind_vars=bind_vars)

    # 4. RETURN over the rows read before the writes.
    out += [f"LET _removed{i} = ({r})" for i, r in enumerate(removals)]
    out.append("FOR _row IN _matched")
    out += [f"  LET {v} = _row.{v}" for v in row_vars]
    _append_return(ret.oC_ProjectionBody(), lines=out, bind_vars=bind_vars, var_env=var_env)
    return AqlQuery(text="\n".join(out), bind_vars=bind_vars)


def needs_pipeline_delete(delete_clauses: list[CypherParser.OC_DeleteContext]) -> bool:
    """Whether a DELETE after a WITH names something other than a bare variable."""
    from .semantic_expr import bare_variable

    return any(bare_variable(expr) is None for dc in delete_clauses for expr in dc.oC_Expression() or [])
