"""A variable-length hop on LPG reads only edges of its type (WP-44).

The type guard ``PRUNE r != null AND r[@tf] != @tv`` keeps ArangoDB from using
a vertex-centric ``(_from, type)`` index, so a ``KNOWS*1..2`` traversal read
every edge of every person it reached, LIKES included, and pruned the
others. The guard's path condition ``p.edges[*][@tf] ALL == @tv`` lets the
index hand over only KNOWS edges. Here every person also likes 30 posts.
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

_DB = "arango_cypher_varlength_lpg_index_it"
_PERSONS = ["a", "b", "c", "d"]
_KNOWS = [("a", "b"), ("b", "c"), ("c", "d")]
_LIKES_PER_PERSON = 30


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
        nodes = database.create_collection("nodes")
        edges = database.create_collection("edges", edge=True)
        # The LDBC LPG layout's edge VCIs (tests/ldbc/loader.py lpg_indexes).
        edges.add_index({"type": "persistent", "fields": ["_from", "type"], "name": "idx_edges_from_type"})
        edges.add_index({"type": "persistent", "fields": ["_to", "type"], "name": "idx_edges_to_type"})
        posts = [f"m{i}" for i in range(_LIKES_PER_PERSON)]
        nodes.import_bulk(
            [{"_key": k, "type": "Person", "id": k} for k in _PERSONS]
            + [{"_key": k, "type": "Post", "labels": ["Message", "Post"], "id": k} for k in posts]
        )
        edges.import_bulk(
            [{"_from": f"nodes/{x}", "_to": f"nodes/{y}", "type": "KNOWS"} for x, y in _KNOWS]
            + [{"_from": f"nodes/{p}", "_to": f"nodes/{m}", "type": "LIKES"} for p in _PERSONS for m in posts]
        )
        yield database
    finally:
        if sys_db.has_database(_DB):
            sys_db.delete_database(_DB)


def _run(db: Any, cypher: str) -> tuple[list[Any], int]:
    q = translate(cypher, mapping=mapping_bundle_for("ldbc_lpg"))
    cursor = db.aql.execute(q.aql, bind_vars=q.bind_vars)
    return list(cursor), cursor.statistics()["scanned_index"]


def test_a_knows_traversal_reads_no_likes_edges(db: Any) -> None:
    rows, scanned = _run(
        db, "MATCH (a:Person {id: 'a'})-[:KNOWS*1..2]-(f:Person) RETURN f.id AS f ORDER BY f"
    )
    assert rows == [{"f": "b"}, {"f": "c"}]
    # a, b and c each have 30 LIKES edges; reading any of them would pass 30.
    assert scanned < _LIKES_PER_PERSON, scanned


def test_zero_hops_keep_the_start(db: Any) -> None:
    rows, _ = _run(db, "MATCH (a:Person {id: 'a'})-[:KNOWS*0..1]-(f:Person) RETURN f.id AS f ORDER BY f")
    assert rows == [{"f": "a"}, {"f": "b"}]


def test_several_types_follow_every_listed_type(db: Any) -> None:
    rows, _ = _run(db, "MATCH (a:Person {id: 'a'})-[:KNOWS|LIKES*1..2]->(x) RETURN count(DISTINCT x) AS n")
    # b (KNOWS); c (KNOWS twice); every post (a LIKES them, and b does too).
    assert rows == [2 + _LIKES_PER_PERSON], rows
