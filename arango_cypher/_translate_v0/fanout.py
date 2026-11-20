"""Whether an LDBC-style LPG variable-length hop should let the type index in (WP-44).

On a type-discriminated edge collection, a variable-length hop is guarded by
``PRUNE e != null AND e[@tf] != @tv``, which keeps ArangoDB from using a
vertex-centric ``(_from, type)`` index: every edge of every vertex reached is
read, then pruned. Testing the path (``p.edges[*][@tf] ALL == @tv``) lets the
index hand over only edges of the type, but that index is a persistent index
with no in-memory cache, while the edge index the PRUNE uses has one. So it
pays where vertices have many edges of other types, and costs where they have
few.

The mapping's cardinality statistics (``metadata.statistics``, from the schema
analyzer or :func:`arango_cypher.schema_acquire.compute_statistics`) give the
average degree of every relationship type, which is enough to tell the two
apart. Without statistics the answer is no: the guard stays as it was.
"""

from __future__ import annotations

from typing import Any

from arango_query_core import CoreError, MappingResolver

#: Use the type index when edges of other types outnumber the hop's own, at the
#: vertices it expands from, by at least this factor. Chosen from LDBC SF0.1 on
#: LPG (timings stay internal, benchmark plan decision 11): KNOWS at a person
#: (about 26 other edges per KNOWS edge) was faster with the index; REPLY_OF
#: outbound at a message (about 3) and IS_SUBCLASS_OF outbound at a tag class
#: (none: its HAS_TYPE edges point inwards) were slower.
VCI_FANOUT_RATIO = 10.0


def _labels(endpoint: Any) -> set[str]:
    if isinstance(endpoint, str):
        return {endpoint} if endpoint else set()
    if isinstance(endpoint, list):
        return {e for e in endpoint if isinstance(e, str) and e}
    return set()


def _degree(resolver: MappingResolver, rel_type: str, direction: str) -> float | None:
    """*rel_type*'s average degree in *direction* (``ANY``: both ways), or None without statistics."""
    stats = resolver.relationship_stats(rel_type)
    if stats is None:
        return None
    if direction == "OUTBOUND":
        return stats.avg_out_degree
    if direction == "INBOUND":
        return stats.avg_in_degree
    return stats.avg_out_degree + stats.avg_in_degree


def other_type_fanout_ratio(
    resolver: MappingResolver, type_values: list[str], collection: str, direction: str
) -> float | None:
    """Edges of other types per edge of the hop's own types, at the vertices the hop
    expands from (its types' domain and range), in *direction*; None without
    statistics for the hop's own types."""
    own: list[tuple[str, dict[str, Any]]] = []
    others: list[tuple[str, dict[str, Any]]] = []
    for name in resolver.all_relationship_types():
        try:
            rmap = resolver.resolve_relationship(name)
        except CoreError:
            continue
        if rmap.get("style") != "GENERIC_WITH_TYPE" or rmap.get("edgeCollectionName") != collection:
            continue
        (own if rmap.get("typeValue") in type_values else others).append((name, rmap))
    if not own:
        return None
    own_degree = 0.0
    vertices: set[str] = set()
    for name, rmap in own:
        degree = _degree(resolver, name, direction)
        if degree is None:
            return None
        own_degree += degree
        vertices |= _labels(rmap.get("domain")) | _labels(rmap.get("range"))
    other = 0.0
    for name, rmap in others:
        stats = resolver.relationship_stats(name)
        if stats is None:
            continue
        if direction in ("OUTBOUND", "ANY") and _labels(rmap.get("domain")) & vertices:
            other += stats.avg_out_degree
        if direction in ("INBOUND", "ANY") and _labels(rmap.get("range")) & vertices:
            other += stats.avg_in_degree
    if own_degree <= 0:
        return None
    return other / own_degree


def prefers_type_index(
    resolver: MappingResolver | None, type_value: Any, collection: Any, direction: str
) -> bool:
    """Whether the hop should test its path so the type index can serve it (module docstring)."""
    if resolver is None or not isinstance(collection, str):
        return False
    values = type_value if isinstance(type_value, list) else [type_value]
    if not values or not all(isinstance(v, str) for v in values):
        return False
    ratio = other_type_fanout_ratio(resolver, values, collection, direction)
    return ratio is not None and ratio >= VCI_FANOUT_RATIO
