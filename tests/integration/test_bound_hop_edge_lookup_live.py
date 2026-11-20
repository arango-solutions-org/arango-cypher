"""A single edge between two bound nodes is looked up, not traversed (WP-44).

``NOT (a)-[:KNOWS]-(b)``, with ``a`` and ``b`` bound, traversed every
neighbour of ``a`` and read each one's document to compare its ``_id``, once
per row (LDBC BI18). The edge is now looked up by its two ends in the edge
collection. The answers must not change: direction, both orientations of
``ANY``, and parallel edges counted by a pattern comprehension.

KNOWS: a->b, b->a (two edges), a->c. d knows nobody.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import pytest
from arango import ArangoClient

from arango_cypher import translate
from tests.helpers.mapping_fixtures import mapping_bundle_for

pytestmark = pytest.mark.integration

_DB = "arango_cypher_bound_hop_edge_lookup_it"
_PEOPLE = ["a", "b", "c", "d"]
_KNOWS = [("a", "b"), ("b", "a"), ("a", "c")]
_PROBE = "MATCH (x:Person {id: 'a'}), (y:Person) WHERE x <> y"


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
        # Every collection the LDBC PG mapping names, so each query's WITH resolves.
        q = translate(f"{_PROBE} AND (x)-[:KNOWS]-(y) RETURN y.id", mapping=mapping_bundle_for("ldbc_pg"))
        for key, name in q.bind_vars.items():
            if key.startswith("@") and not database.has_collection(name):
                database.create_collection(name, edge=key.startswith(("@edgeCollection", "@ppEdge")))
        if not database.has_collection("knows"):
            database.create_collection("knows", edge=True)
        database.collection("person").import_bulk([{"_key": k, "id": k} for k in _PEOPLE])
        database.collection("knows").import_bulk(
            [{"_from": f"person/{x}", "_to": f"person/{y}"} for x, y in _KNOWS]
        )
        database.create_collection("nodes").import_bulk(
            [{"_key": k, "id": k, "type": "Person"} for k in _PEOPLE]
        )
        database.create_collection("edges", edge=True).import_bulk(
            [{"_from": f"nodes/{x}", "_to": f"nodes/{y}", "type": "KNOWS"} for x, y in _KNOWS]
            + [{"_from": "nodes/a", "_to": "nodes/d", "type": "LIKES"}]
        )
        yield database
    finally:
        if sys_db.has_database(_DB):
            sys_db.delete_database(_DB)


@pytest.fixture(params=["ldbc_pg", "ldbc_lpg"])
def mapping(request: pytest.FixtureRequest) -> Any:
    return mapping_bundle_for(request.param)


def _rows(db: Any, mapping: Any, cypher: str) -> list[Any]:
    q = translate(cypher, mapping=mapping)
    return list(db.aql.execute(q.aql, bind_vars=q.bind_vars))


def test_not_knows_either_way(db: Any, mapping: Any) -> None:
    # d is liked by a on LPG, but that is no KNOWS edge.
    rows = _rows(db, mapping, f"{_PROBE} AND NOT (x)-[:KNOWS]-(y) RETURN y.id AS y")
    assert rows == [{"y": "d"}]


def test_a_directed_predicate_keeps_its_direction(db: Any, mapping: Any) -> None:
    rows = _rows(db, mapping, f"{_PROBE} AND (y)-[:KNOWS]->(x) RETURN y.id AS y")
    assert rows == [{"y": "b"}]


def test_a_comprehension_counts_parallel_edges(db: Any, mapping: Any) -> None:
    rows = _rows(db, mapping, f"{_PROBE} RETURN y.id AS y, size([(x)-[k:KNOWS]-(y) | k]) AS n ORDER BY y")
    assert rows == [{"y": "b", "n": 2}, {"y": "c", "n": 1}, {"y": "d", "n": 0}]


def test_the_predicate_reads_no_neighbour_documents(db: Any) -> None:
    q = translate(f"{_PROBE} AND NOT (x)-[:KNOWS]-(y) RETURN y.id", mapping=mapping_bundle_for("ldbc_pg"))
    nodes = [n["type"] for n in db.aql.explain(q.aql, bind_vars=q.bind_vars)["nodes"]]
    assert "TraversalNode" not in nodes, q.aql
