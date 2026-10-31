"""What a MERGE binds besides the entity it writes.

- A named path: ``MERGE p = (a {num: 1})`` or ``MERGE p = (a)-[r:T]->(b)``
  binds ``p`` to the merged node, or to the two endpoints and the merged
  relationship (:func:`merge_path_line`).
- Actions on a bound endpoint of a relationship MERGE:
  ``MERGE (a)-[:KNOWS]->(b) ON CREATE SET b.created = 1`` writes node ``b``
  when the UPSERT created the relationship, not the relationship
  (:func:`endpoint_action_lines`).
"""

from __future__ import annotations

import re
from typing import Any

from arango_query_core import CoreError

from .._antlr.CypherParser import CypherParser
from .naming import _aql_collection_ref, _strip_label_backticks
from .state import _active_path_vars


def merge_path_line(merge_ctx: CypherParser.OC_MergeContext, nodes: list[str], edges: list[str]) -> list[str]:
    """``LET p = {nodes, edges}`` for a MERGE's named path (none if it has no name)."""
    name = merge_ctx.oC_PatternPart().oC_Variable()
    if name is None:
        return []
    path = _strip_label_backticks(name.getText().strip())
    _active_path_vars.get()[path] = ([], [])
    return [f"LET {path} = {{nodes: [{', '.join(nodes)}], edges: [{', '.join(edges)}]}}"]


def endpoint_action_lines(
    endpoint_actions: dict[tuple[str, bool], list[str]],
    var_collections: dict[str, str],
    bind_vars: dict[str, Any],
    *,
    final: bool,
) -> list[str]:
    """Write ON CREATE / ON MATCH SET items on bound endpoints after the UPSERT.

    *endpoint_actions* maps ``(variable, on_create)`` to ``key: value`` fields;
    *var_collections* maps each pipeline variable to its collection's bind key.
    The UPSERT's ``OLD`` is null when it created the relationship. Each
    endpoint collection is written by one UPDATE over the patches that apply:
    the MATCH that bound the endpoints read that collection before it. When
    nothing follows (*final*), the last UPDATE ends the query; otherwise each
    is a subquery, so the row survives for the RETURN.
    """
    # Keyed by collection name: two bind keys can name one collection (LPG's
    # ``vertices``), and a second UPDATE of it would be ERR 1579.
    by_collection: dict[str, tuple[str, list[str]]] = {}
    for (var, on_create), fields in endpoint_actions.items():
        key = var_collections.get(var)
        if key is None or key not in bind_vars:
            raise CoreError(
                f"MERGE ... SET of {var!r}: the collection of {var!r} is not known here, so it would not be written",
                code="UNSUPPORTED",
            )
        condition = "_merge_old == null" if on_create else "_merge_old != null"
        patch = "{" + ", ".join(fields) + "}"
        _, items = by_collection.setdefault(str(bind_vars[key]), (key, []))
        items.append(f"{{d: {var}, p: ({condition} ? {patch} : null)}}")
    out = ["LET _merge_old = OLD"]
    groups = list(by_collection.values())
    for i, (key, items) in enumerate(groups):
        update = f"FOR _w IN [{', '.join(items)}] FILTER _w.p != null UPDATE _w.d WITH _w.p IN {_aql_collection_ref(key)}"
        out.append(update if final and i == len(groups) - 1 else f"LET _endpoint_sets{i} = ({update})")
    return out


def refuse_stale_endpoint_return(
    ret: CypherParser.OC_ReturnContext, endpoint_actions: dict[tuple[str, bool], list[str]]
) -> None:
    """Refuse a RETURN of an endpoint the MERGE actions wrote.

    The endpoint's variable still holds the document the MATCH read, so the
    RETURN would show it without the SET.
    """
    text = ret.getText()
    for var, _ in endpoint_actions:
        if re.search(rf"(?<![\w`]){re.escape(var)}(?![\w`])", text):
            raise CoreError(
                f"RETURN of {var!r} after MERGE ... SET of {var!r} is not supported: "
                "it would return the node as read, without the SET",
                code="NOT_IMPLEMENTED",
            )
