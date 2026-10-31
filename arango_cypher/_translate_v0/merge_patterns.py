"""MERGE of a relationship whose endpoints no earlier clause binds.

``MERGE (a:A {k: 1})-[r:R]->(b:B)`` merges the whole pattern (openCypher): if
some ``(a:A {k: 1})-[r:R]->(b:B)`` exists it is matched, else every part of
it is created — both nodes, even when an ``A {k: 1}`` node already exists
without such a relationship, and the relationship between them.

An edge UPSERT cannot express this: it needs the endpoints' ids before it
runs. The pattern is instead read first, then written (each collection once,
which AQL requires — ERR 1579):

1. ``_merge_found``: the first match of the whole pattern, or null;
2. one INSERT per node collection of the endpoints the pattern needs created
   (none when it matched; both endpoints in one statement when they share a
   collection, as in LPG's ``vertices``);
3. the endpoints bound to the match or to the new nodes;
4. one INSERT of the relationship when the pattern was created.

It applies only when the MERGE is the query's first clause, so it runs once.
Over several rows openCypher has each row see the nodes earlier rows created;
an AQL read inside the loop is not guaranteed to see the same query's earlier
writes, so the caller refuses that shape (:func:`refuse_unbound_endpoints`).
"""

from __future__ import annotations

from dataclasses import dataclass

from arango_query_core import CoreError

from .._antlr.CypherParser import CypherParser
from .naming import _aql_collection_ref


@dataclass(frozen=True)
class MergeEndpoint:
    """A node of the merged pattern: its AQL name, collection and documents."""

    var: str
    coll_key: str
    search_doc: str
    insert_doc: str


@dataclass(frozen=True)
class MergeEdge:
    """The relationship of the merged pattern.

    *search_fields* are its type and property fields (not ``_from``/``_to``);
    *insert_fields* the same plus its properties for the INSERT. *direction*
    is ``OUTBOUND``, ``INBOUND`` or ``ANY`` (matched either way, created
    outbound, as openCypher does).
    """

    var: str
    coll_key: str
    direction: str
    search_fields: list[str]
    insert_fields: list[str]


def unbound_endpoints(merge_ctx: CypherParser.OC_MergeContext, bound: set[str]) -> list[str]:
    """The endpoint node patterns of *merge_ctx* that no earlier clause binds (by text)."""
    elem = merge_ctx.oC_PatternPart().oC_AnonymousPatternPart().oC_PatternElement()
    nodes = [elem.oC_NodePattern(), *(c.oC_NodePattern() for c in elem.oC_PatternElementChain() or [])]
    out: list[str] = []
    for node in nodes:
        var = node.oC_Variable()
        if var is None or var.getText().strip() not in bound:
            out.append(node.getText())
    return out


def refuse_unbound_endpoints(
    merge_ctx: CypherParser.OC_MergeContext, unbound: list[str], *, first_clause: bool, hops: int
) -> None:
    """Refuse the unbound-endpoint relationship MERGEs :func:`pattern_merge_lines` does not cover."""
    if not unbound:
        return
    names = ("endpoints " if len(unbound) > 1 else "endpoint ") + ", ".join(unbound)
    if hops != 1:
        raise CoreError(
            f"MERGE of a multi-hop pattern whose {names} no earlier clause binds is not "
            "supported: bind them with MATCH or MERGE first, or MERGE one relationship at a time",
            code="NOT_IMPLEMENTED",
        )
    if not first_clause:
        raise CoreError(
            f"MERGE of a relationship whose {names} no earlier clause binds is supported "
            "only as the query's first clause: over several rows each row would have to see the "
            "nodes earlier rows created. Bind the endpoints with MATCH or MERGE first.",
            code="NOT_IMPLEMENTED",
        )
    if merge_ctx.oC_MergeAction():
        raise CoreError(
            f"ON CREATE / ON MATCH SET on a MERGE whose {names} no earlier clause binds "
            "is not supported: bind the endpoints with MATCH or MERGE first",
            code="NOT_IMPLEMENTED",
        )


def pattern_merge_lines(
    start: MergeEndpoint, end: MergeEndpoint, edge: MergeEdge, *, final: bool
) -> list[str]:
    """AQL that merges ``(start)-[edge]-(end)`` as a whole; *start* is end for a self-loop.

    Binds ``start.var``, ``end.var`` and ``edge.var``. When *final* (nothing
    follows), the relationship INSERT ends the query.
    """
    self_loop = start.var == end.var
    if edge.direction == "INBOUND":
        attach, other = "_mr._to == _ms._id", "_mr._from"
    elif edge.direction == "OUTBOUND":
        attach, other = "_mr._from == _ms._id", "_mr._to"
    else:
        attach, other = (
            "(_mr._from == _ms._id OR _mr._to == _ms._id)",
            "(_mr._from == _ms._id ? _mr._to : _mr._from)",
        )
    edge_search = "{" + ", ".join(edge.search_fields) + "}"
    find = [
        f"LET _merge_found = FIRST(FOR _ms IN {_aql_collection_ref(start.coll_key)} FILTER MATCHES(_ms, {start.search_doc})",
        f"  FOR _mr IN {_aql_collection_ref(edge.coll_key)} FILTER {attach} FILTER MATCHES(_mr, {edge_search})",
    ]
    if self_loop:
        find.append(f"  FILTER {other} == _ms._id LIMIT 1 RETURN {{s: _ms, r: _mr, e: _ms}})")
    else:
        find.append(
            f"  FOR _me IN {_aql_collection_ref(end.coll_key)} FILTER _me._id == {other} "
            f"FILTER MATCHES(_me, {end.search_doc})"
        )
        find.append("  LIMIT 1 RETURN {s: _ms, r: _mr, e: _me})")
    lines = find

    # One INSERT per collection: LPG keeps both endpoints in one.
    endpoints = [("s", start)] if self_loop else [("s", start), ("e", end)]
    groups: dict[str, list[tuple[str, MergeEndpoint]]] = {}
    for role, node in endpoints:
        groups.setdefault(node.coll_key, []).append((role, node))
    for i, (key, members) in enumerate(groups.items()):
        docs = ", ".join(node.insert_doc for _, node in members)
        lines.append(
            f"LET _merge_new{i} = (FOR _d IN (_merge_found == null ? [{docs}] : []) "
            f"INSERT _d IN {_aql_collection_ref(key)} RETURN NEW)"
        )
        for j, (role, node) in enumerate(members):
            lines.append(f"LET {node.var} = _merge_found != null ? _merge_found.{role} : _merge_new{i}[{j}]")

    from_var, to_var = (end.var, start.var) if edge.direction == "INBOUND" else (start.var, end.var)
    edge_doc = "{" + ", ".join([f"_from: {from_var}._id", f"_to: {to_var}._id", *edge.insert_fields]) + "}"
    insert = f"FOR _d IN (_merge_found == null ? [{edge_doc}] : []) INSERT _d IN {_aql_collection_ref(edge.coll_key)}"
    if final:
        lines.append(insert)
    else:
        lines.append(f"LET _merge_new_rel = ({insert} RETURN NEW)")
        lines.append(f"LET {edge.var} = _merge_found != null ? _merge_found.r : _merge_new_rel[0]")
    return lines
