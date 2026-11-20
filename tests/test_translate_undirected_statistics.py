"""Statistics never give an undirected hop a direction.

An undirected relationship matches either orientation; choosing ``OUTBOUND``
or ``INBOUND`` from the average fan-out and fan-in drops the other half of the
matches. The live test (``tests/integration/test_undirected_hop_statistics_live.py``)
shows the rows; these check every compiler path keeps ``ANY`` on both layouts.
"""

from __future__ import annotations

import dataclasses

import pytest
from arango_query_core import MappingBundle, MappingResolver

from arango_cypher import translate
from tests.helpers.mapping_fixtures import mapping_bundle_for

#: HAS_CREATOR in the proportions LDBC SF0.1 has: one creator per message,
#: about two hundred messages per creator.
_STATS = {
    "relationships": {
        "HAS_CREATOR": {
            "edge_count": 324221,
            "source_count": 324221,
            "target_count": 1639,
            "avg_out_degree": 1.0,
            "avg_in_degree": 197.82,
        }
    }
}


def _with_stats(layout: str) -> MappingBundle:
    bundle = mapping_bundle_for(f"ldbc_{layout}")
    return dataclasses.replace(bundle, metadata={**bundle.metadata, "statistics": _STATS})


def _hops(cypher: str, bundle: MappingBundle) -> list[str]:
    aql = translate(cypher, mapping=bundle).aql
    return [line.strip() for line in aql.splitlines() if " IN 1..1 " in line]


@pytest.mark.parametrize("layout", ["pg", "lpg"])
def test_the_statistics_would_pick_a_direction(layout: str) -> None:
    assert MappingResolver(_with_stats(layout)).preferred_traversal_direction("HAS_CREATOR") == "OUTBOUND"


@pytest.mark.parametrize("layout", ["pg", "lpg"])
@pytest.mark.parametrize(
    "cypher",
    [
        # single-part MATCH
        "MATCH (p:Person {id: 1})-[:HAS_CREATOR]-(m) RETURN count(m) AS n",
        "MATCH (m:Post)-[:HAS_CREATOR]-(p:Person {id: 1}) RETURN count(m) AS n",
        # the leading MATCH of a multi-part query
        "MATCH (p:Person {id: 1})-[:HAS_CREATOR]-(m) WITH m RETURN count(m) AS n",
    ],
)
def test_an_undirected_hop_stays_undirected(layout: str, cypher: str) -> None:
    with_stats = _hops(cypher, _with_stats(layout))
    assert with_stats, cypher
    assert all(" ANY " in hop for hop in with_stats), with_stats
    assert with_stats == _hops(cypher, mapping_bundle_for(f"ldbc_{layout}"))
