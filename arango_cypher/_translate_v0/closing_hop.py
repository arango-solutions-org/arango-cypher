"""A hop that closes a pattern at a bound node checks the bound node's neighbours first (WP-44).

In ``(tag)<-[:HAS_INTEREST]-(p1)-[:KNOWS]-()-[:KNOWS]-(p2)-[:HAS_INTEREST]->(tag)``
the last hop ends at ``tag``, which the pattern bound at its start. It compiles
to a walk from every ``p2`` row over all of ``p2``'s HAS_INTEREST edges, kept
only where the walk reaches ``tag``::

    FOR tag_m, r IN 1..1 OUTBOUND p2 @@has_interest
      FILTER tag_m._id == tag._id

When ``tag`` has fewer such edges than ``p2`` (a person has many interests, a
tag few people), a list of ``tag``'s neighbours is cheaper, and ArangoDB builds
it once per ``tag`` because it depends on nothing else (the optimizer moves the
subquery up to where ``tag`` is bound). Rows whose ``p2`` is not on it are
dropped before the walk, which still binds the edge for the survivors::

    FILTER p2._id IN (FOR _cl_e IN @@has_interest FILTER _cl_e._to == tag._id RETURN _cl_e._from)

The filter is only a necessary condition, so the rows are the same either way.
The mapping's cardinality statistics decide whether to add it: the list is
the relationship's average degree at the bound end, the walk its average degree
at the open end. Without statistics nothing changes.
"""

from __future__ import annotations

from typing import Any

from arango_query_core import MappingResolver

from .fanout import _degree
from .multi_type import MULTI_TYPE_SEPARATOR
from .naming import _aql_collection_ref, _pick_bind_key

#: Add the neighbour list when it is no longer, on average, than the walk it
#: saves. Built once per bound node, as ArangoDB does, it is then cheaper
#: whenever more than one row reaches the hop per bound node. Chosen from LDBC
#: SF0.1 (timings stay internal, benchmark plan decision 11): BI18 (HAS_INTEREST,
#: a list far shorter than the walk) and BI11 and BI14 (undirected KNOWS, equal
#: lengths, many rows per bound person) were faster; IS7 (KNOWS, one row per
#: bound person) was slightly slower, which equal lengths accept.
CLOSING_LIST_RATIO = 1.0

#: The edge variable inside the list subquery (an ``_``-prefixed internal
#: name, as ``_pp_e`` is for pattern predicates).
_EDGE = "_cl_e"

_REVERSE = {"OUTBOUND": "INBOUND", "INBOUND": "OUTBOUND", "ANY": "ANY"}


def prefers_neighbour_list(resolver: MappingResolver, rel_type: str, direction: str) -> bool:
    """Whether the bound end's neighbour list is, on average, no longer than the walk.

    *direction* is the hop's, from the open end to the bound end.
    """
    walk = _degree(resolver, rel_type, direction)
    neighbours = _degree(resolver, rel_type, _REVERSE.get(direction, "ANY"))
    if walk is None or neighbours is None or walk <= 0:
        return False
    return neighbours <= walk * CLOSING_LIST_RATIO


def closing_hop_prefilter(
    *,
    resolver: MappingResolver,
    open_var: str,
    bound_var: str,
    rel_type: str | None,
    r_map: dict[str, Any],
    edge_key: str,
    direction: str,
    rel_range: tuple[int, int],
    bind_vars: dict[str, Any],
) -> str | None:
    """The filter on *open_var* to emit before a one-hop walk that closes at *bound_var*, or None.

    None unless the hop is a single typed relationship between two different
    variables, in a dedicated or type-discriminated edge collection, and the
    statistics favour the list (:func:`prefers_neighbour_list`).
    """
    if rel_range != (1, 1) or open_var == bound_var:
        return None
    if not rel_type or MULTI_TYPE_SEPARATOR in rel_type:
        return None
    if not isinstance(bind_vars.get(edge_key), str):
        return None
    style = r_map.get("style")
    if style not in ("DEDICATED_COLLECTION", "GENERIC_WITH_TYPE"):
        return None
    type_field, type_value = r_map.get("typeField"), r_map.get("typeValue")
    if style == "GENERIC_WITH_TYPE" and (not type_field or type_value is None):
        return None
    if not prefers_neighbour_list(resolver, rel_type, direction):
        return None
    type_test = ""
    if style == "GENERIC_WITH_TYPE":
        tf_key = _pick_bind_key("clTypeField", bind_vars)
        bind_vars[tf_key] = type_field
        tv_key = _pick_bind_key("clTypeValue", bind_vars)
        bind_vars[tv_key] = type_value
        type_test = f" FILTER {_EDGE}[@{tf_key}] == @{tv_key}"

    coll = _aql_collection_ref(edge_key)

    def neighbours(at: str, other: str) -> str:
        return f"(FOR {_EDGE} IN {coll} FILTER {_EDGE}.{at} == {bound_var}._id{type_test} RETURN {_EDGE}.{other})"

    if direction == "OUTBOUND":
        listed = neighbours("_to", "_from")
    elif direction == "INBOUND":
        listed = neighbours("_from", "_to")
    else:
        listed = f"APPEND({neighbours('_to', '_from')}, {neighbours('_from', '_to')})"
    return f"{open_var}._id IN {listed}"
