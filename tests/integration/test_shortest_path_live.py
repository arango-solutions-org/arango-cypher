"""``shortestPath()`` between bound nodes, run on ArangoDB, on PG and LPG (WP-39).

``shortestPath`` did not parse (LDBC IC1, IC13). It is now a breadth-first
traversal that keeps the first path reaching the target. On LPG it follows
only edges of the named type: a shorter route through a LIKES edge must not
count as a KNOWS path.

KNOWS: a-b, b-c, c-d. LIKES: a-d. e knows nobody.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import pytest
from arango import ArangoClient
from arango_query_core import CoreError, MappingBundle

from arango_cypher import translate

pytestmark = pytest.mark.integration

_DB = "arango_cypher_shortest_path_it"
_PEOPLE = ["a", "b", "c", "d", "e"]
_EDGES = [("a", "b", "KNOWS"), ("b", "c", "KNOWS"), ("c", "d", "KNOWS"), ("a", "d", "LIKES")]
_POSTS = 30


def _pg() -> MappingBundle:
    def rel(e: str) -> dict[str, Any]:
        return {
            "edgeCollectionName": e,
            "style": "DEDICATED_COLLECTION",
            "domain": "Person",
            "range": "Person",
        }

    return MappingBundle(
        conceptual_schema={"entities": [], "relationships": []},
        physical_mapping={
            "entities": {"Person": {"collectionName": "person", "style": "COLLECTION", "properties": {}}},
            "relationships": {"KNOWS": rel("knows"), "LIKES": rel("likes")},
        },
        metadata={},
    )


def _lpg() -> MappingBundle:
    def rel(t: str) -> dict[str, Any]:
        return {
            "edgeCollectionName": "edges",
            "style": "GENERIC_WITH_TYPE",
            "typeField": "type",
            "typeValue": t,
        }

    return MappingBundle(
        conceptual_schema={"entities": [], "relationships": []},
        physical_mapping={
            "entities": {
                "Person": {
                    "collectionName": "nodes",
                    "style": "LABEL",
                    "typeField": "type",
                    "typeValue": "Person",
                }
            },
            "relationships": {"KNOWS": rel("KNOWS"), "LIKES": rel("LIKES")},
        },
        metadata={},
    )


_MAPPINGS = {"pg": _pg(), "lpg": _lpg()}


@pytest.fixture(scope="module")
def db() -> Iterator[Any]:
    if os.environ.get("RUN_INTEGRATION") != "1":
        pytest.skip("RUN_INTEGRATION=1 required")
    user = os.environ.get("ARANGO_USER", "root")
    password = os.environ.get("ARANGO_PASS", "openSesame")
    client = ArangoClient(hosts=os.environ.get("ARANGO_URL", "http://localhost:8529"))
    sys_db = client.db("_system", username=user, password=password)
    if sys_db.has_database(_DB):
        sys_db.delete_database(_DB)
    sys_db.create_database(_DB)
    try:
        database = client.db(_DB, username=user, password=password)
        database.create_collection("person").import_bulk([{"_key": k, "id": k} for k in _PEOPLE])
        database.create_collection("nodes").import_bulk(
            [{"_key": k, "id": k, "type": "Person"} for k in _PEOPLE]
        )
        for t in ("knows", "likes"):
            database.create_collection(t, edge=True).import_bulk(
                [{"_from": f"person/{a}", "_to": f"person/{b}"} for a, b, tt in _EDGES if tt.lower() == t]
            )
        edges = database.create_collection("edges", edge=True)
        # The LDBC LPG layout's edge VCIs (tests/ldbc/loader.py lpg_indexes).
        edges.add_index({"type": "persistent", "fields": ["_from", "type"], "name": "idx_edges_from_type"})
        edges.add_index({"type": "persistent", "fields": ["_to", "type"], "name": "idx_edges_to_type"})
        edges.import_bulk([{"_from": f"nodes/{a}", "_to": f"nodes/{b}", "type": t} for a, b, t in _EDGES])
        # Every person also likes _POSTS posts on LPG: edges a KNOWS search must not read.
        database.collection("nodes").import_bulk([{"_key": f"m{i}", "type": "Post"} for i in range(_POSTS)])
        edges.import_bulk(
            [
                {"_from": f"nodes/{x}", "_to": f"nodes/m{i}", "type": "LIKES"}
                for x in _PEOPLE
                for i in range(_POSTS)
            ]
        )
        yield database
    finally:
        if sys_db.has_database(_DB):
            sys_db.delete_database(_DB)


@pytest.fixture(params=sorted(_MAPPINGS))
def mapping(request: pytest.FixtureRequest) -> MappingBundle:
    return _MAPPINGS[request.param]


def _rows(db: Any, mapping: MappingBundle, cypher: str, **params: Any) -> list[Any]:
    q = translate(cypher, mapping=mapping, params=params or None)
    return list(db.aql.execute(q.aql, bind_vars=q.bind_vars))


_QUERY = (
    "MATCH (x:Person {id: $from}), (y:Person {id: $to}), p = shortestPath((x)-[:KNOWS*]-(y)) "
    "RETURN length(p) AS n, [v IN nodes(p) | v.id] AS ids"
)


def test_the_shortest_path_follows_only_the_named_type(db: Any, mapping: MappingBundle) -> None:
    """a LIKES d directly, but the KNOWS path is a-b-c-d."""
    assert _rows(db, mapping, _QUERY, **{"from": "a", "to": "d"}) == [{"n": 3, "ids": ["a", "b", "c", "d"]}]


def test_no_path_means_no_row(db: Any, mapping: MappingBundle) -> None:
    assert _rows(db, mapping, _QUERY, **{"from": "a", "to": "e"}) == []


def test_a_zero_length_range_matches_a_node_to_itself(db: Any, mapping: MappingBundle) -> None:
    rows = _rows(
        db,
        mapping,
        "MATCH (x:Person {id: 'a'}), p = shortestPath((x)-[:KNOWS*0..3]-(x)) RETURN length(p) AS n",
    )
    assert rows == [{"n": 0}]


def test_a_shortest_path_after_with(db: Any, mapping: MappingBundle) -> None:
    """LDBC IC1's shape: endpoints bound by a WITH, the length bounded."""
    rows = _rows(
        db,
        mapping,
        "MATCH (x:Person {id: 'a'}), (y:Person) WHERE x <> y WITH x, y "
        "MATCH p = shortestPath((x)-[:KNOWS*1..2]-(y)) RETURN y.id AS y, length(p) AS n ORDER BY y",
    )
    assert rows == [{"y": "b", "n": 1}, {"y": "c", "n": 2}]


@pytest.mark.parametrize(
    ("cypher", "message"),
    [
        ("MATCH p = shortestPath((x:Person)-[:KNOWS*]-(y:Person)) RETURN p", "bound earlier"),
        (
            "MATCH (x:Person), (y:Person), p = allShortestPaths((x)-[:KNOWS*]-(y)) RETURN p",
            "allShortestPaths",
        ),
        ("MATCH (x:Person), (y:Person), p = shortestPath((x)-[:KNOWS*2..3]-(y)) RETURN p", "minimum length"),
        ("MATCH (x:Person), (y:Person), p = shortestPath((x)-[:KNOWS* {w: 1}]-(y)) RETURN p", "properties"),
        ("MATCH (x:Person), (y:Person) CREATE p = shortestPath((x)-[:KNOWS]->(y))", "created or merged"),
    ],
)
def test_refusals(mapping: MappingBundle, cypher: str, message: str) -> None:
    with pytest.raises(CoreError, match=message):
        translate(cypher, mapping=mapping)


def test_a_null_endpoint_means_no_row(db: Any, mapping: MappingBundle) -> None:
    """An endpoint an OPTIONAL MATCH left null has no path (AQL's SHORTEST_PATH refuses null)."""
    rows = _rows(
        db,
        mapping,
        "MATCH (x:Person {id: 'a'}) OPTIONAL MATCH (y:Person {id: 'nobody'}) WITH x, y "
        "MATCH p = shortestPath((x)-[:KNOWS*]-(y)) RETURN length(p) AS n",
    )
    assert rows == []


def test_several_types_take_the_shortest_route_over_any_of_them(db: Any, mapping: MappingBundle) -> None:
    """a LIKES d directly: over KNOWS or LIKES the path has one edge."""
    rows = _rows(
        db,
        mapping,
        "MATCH (x:Person {id: 'a'}), (y:Person {id: 'd'}), p = shortestPath((x)-[:KNOWS|LIKES*]-(y)) "
        "RETURN length(p) AS n",
    )
    assert rows == [{"n": 1}]


def test_one_start_with_several_targets(db: Any, mapping: MappingBundle) -> None:
    """Each target gets its own shortest path (LDBC IC1's many friends per person)."""
    rows = _rows(
        db,
        mapping,
        "MATCH (x:Person {id: 'a'}), (y:Person) WHERE x <> y WITH x, y "
        "MATCH p = shortestPath((x)-[:KNOWS*]-(y)) RETURN y.id AS y, length(p) AS n ORDER BY y",
    )
    assert rows == [{"y": "b", "n": 1}, {"y": "c", "n": 2}, {"y": "d", "n": 3}]


def test_dedicated_edge_collections_use_arangodb_shortest_path(db: Any) -> None:
    """On PG the search is AQL's SHORTEST_PATH, from both ends at once (WP-44): a
    traversal per pair re-explored the start's neighbourhood for every target."""
    q = translate(_QUERY, mapping=_pg(), params={"from": "a", "to": "d"})
    nodes = [n["type"] for n in db.aql.explain(q.aql, bind_vars=q.bind_vars)["nodes"]]
    assert "ShortestPathNode" in nodes, q.aql
    assert "TraversalNode" not in nodes, q.aql


def test_an_lpg_search_reads_only_edges_of_its_type(db: Any) -> None:
    """The guard's PRUNE alone kept the (_from, type) index out, so the search read
    every LIKES edge of every person it reached; the path condition lets it in (WP-44)."""
    q = translate(_QUERY, mapping=_lpg(), params={"from": "a", "to": "d"})
    cursor = db.aql.execute(q.aql, bind_vars=q.bind_vars)
    assert list(cursor) == [{"n": 3, "ids": ["a", "b", "c", "d"]}]
    assert cursor.statistics()["scanned_index"] < _POSTS, q.aql
