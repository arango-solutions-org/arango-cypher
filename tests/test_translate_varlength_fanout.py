"""An LPG variable-length hop tests its path only where the statistics say it pays (WP-44).

The guard's PRUNE keeps a ``(_from, type)`` index out. A path condition lets
it in, which helps where the vertices the hop expands from have many edges of
other types (KNOWS at a person) and hurts where they have few (REPLY_OF at a
message): the index has no in-memory cache, the edge index has. The mapping's
cardinality statistics decide (``_translate_v0/fanout.py``).
"""

from __future__ import annotations

import dataclasses

import pytest
from arango_query_core import MappingBundle, MappingResolver

from arango_cypher import translate
from arango_cypher._translate_v0.fanout import VCI_FANOUT_RATIO, other_type_fanout_ratio
from tests.helpers.mapping_fixtures import mapping_bundle_for


def _rel(out: float, in_: float) -> dict[str, float | int | str]:
    return {
        "edge_count": 1,
        "source_count": 1,
        "target_count": 1,
        "avg_out_degree": out,
        "avg_in_degree": in_,
    }


#: Degrees in the proportions LDBC SF0.1 has (rounded).
_STATS = {
    "relationships": {
        "KNOWS": _rel(9, 9),
        "LIKES": _rel(82, 0.4),
        "HAS_CREATOR": _rel(1, 198),
        "HAS_INTEREST": _rel(23, 2),
        "HAS_MEMBER": _rel(15, 147),
        "REPLY_OF": _rel(1, 0.5),
        "HAS_TAG": _rel(1, 19),
        "IS_LOCATED_IN": _rel(1, 230),
        "IS_SUBCLASS_OF": _rel(1, 1),
        "HAS_TYPE": _rel(1, 226),
    }
}


def _lpg(stats: dict | None = _STATS) -> MappingBundle:
    bundle = mapping_bundle_for("ldbc_lpg")
    if stats is None:
        return bundle
    return dataclasses.replace(bundle, metadata={**bundle.metadata, "statistics": stats})


def _aql(cypher: str, bundle: MappingBundle | None = None) -> str:
    return translate(cypher, mapping=bundle or _lpg()).aql


def test_knows_at_a_person_crosses_the_threshold() -> None:
    ratio = other_type_fanout_ratio(MappingResolver(_lpg()), ["KNOWS"], "edges", "ANY")
    assert ratio is not None and ratio >= VCI_FANOUT_RATIO


@pytest.mark.parametrize(
    ("rel_type", "direction"), [("REPLY_OF", "OUTBOUND"), ("IS_SUBCLASS_OF", "OUTBOUND")]
)
def test_a_hop_with_few_other_edges_stays_below(rel_type: str, direction: str) -> None:
    ratio = other_type_fanout_ratio(MappingResolver(_lpg()), [rel_type], "edges", direction)
    assert ratio is not None and ratio < VCI_FANOUT_RATIO


def test_a_knows_hop_tests_its_path() -> None:
    aql = _aql("MATCH (p:Person {id: 1})-[:KNOWS*1..2]-(f:Person) RETURN f.id")
    assert "FOR f, r, _vlp IN 1..2 ANY p" in aql, aql
    assert "FILTER _vlp.edges[*][@relTypeField] ALL == @relTypeValue" in aql, aql
    assert "PRUNE r != null" in aql  # the guard stays


def test_a_reply_of_hop_keeps_the_prune_alone() -> None:
    aql = _aql("MATCH (c:Comment {id: 1})-[:REPLY_OF*0..]->(p:Post) RETURN p.id")
    assert "ALL ==" not in aql and "PRUNE" in aql, aql


def test_direction_decides_is_subclass_of() -> None:
    # Outbound, a tag class has no other edges; inbound it has every tag's HAS_TYPE.
    out = _aql("MATCH (t:TagClass {id: 1})-[:IS_SUBCLASS_OF*0..]->(b:TagClass) RETURN b.id")
    in_ = _aql("MATCH (t:TagClass {id: 1})<-[:IS_SUBCLASS_OF*0..]-(b:TagClass) RETURN b.id")
    assert "ALL ==" not in out, out
    assert "ALL ==" in in_, in_


def test_without_statistics_the_guard_is_unchanged() -> None:
    aql = _aql("MATCH (p:Person {id: 1})-[:KNOWS*1..2]-(f:Person) RETURN f.id", _lpg(None))
    assert "ALL ==" not in aql and "_vlp" not in aql, aql


def test_a_dedicated_edge_collection_never_gets_it() -> None:
    bundle = dataclasses.replace(mapping_bundle_for("ldbc_pg"), metadata={"statistics": _STATS})
    aql = _aql("MATCH (p:Person {id: 1})-[:KNOWS*1..2]-(f:Person) RETURN f.id", bundle)
    assert "ALL ==" not in aql and "PRUNE" not in aql, aql


def test_the_translation_cache_keys_on_the_statistics() -> None:
    """A cached translation made without statistics was served with them, and the
    other way round: the key covered the mapping but not its statistics."""
    from arango_cypher.api import _cache_key

    cypher = "MATCH (p:Person {id: 1})-[:KNOWS*1..2]-(f:Person) RETURN f.id"
    assert "ALL ==" not in _aql(cypher, _lpg(None))
    assert "ALL ==" in _aql(cypher, _lpg())  # same query and mapping, now with statistics
    stamped = {**_STATS, "computed_at": "2026-01-01T00:00:00Z"}
    restamped = {**_STATS, "computed_at": "2026-02-02T00:00:00Z"}
    # Recomputing the same numbers keeps the key; other numbers change it.
    assert _cache_key(cypher, _lpg(stamped), None) == _cache_key(cypher, _lpg(restamped), None)
    assert _cache_key(cypher, _lpg(stamped), None) != _cache_key(cypher, _lpg(None), None)
