"""A hop that closes at a bound node gives the same rows with its neighbour-list filter (WP-44).

With statistics that show a person having more interests than a tag has
followers, LDBC BI18's last hop, ``(person2)-[:HAS_INTEREST]->(tag)``, first
checks ``person2`` against the tag's followers and walks only from those that
are on the list. Here every person follows ten filler tags, so walks from the
others would read ten edges each. The statistics are computed from this
database, as a service computes them.
"""

from __future__ import annotations

import dataclasses
import os
from collections.abc import Iterator
from typing import Any

import pytest
from arango import ArangoClient
from arango_query_core import MappingBundle

from arango_cypher import translate
from arango_cypher.schema_acquire import compute_statistics
from tests.helpers.mapping_fixtures import mapping_bundle_for

pytestmark = pytest.mark.integration

_DB = "arango_cypher_closing_hop_prefilter_it"
_PERSONS = [1, 2, 3, 4, 5, 6]
_KNOWS = [(1, 2), (2, 3), (3, 4), (1, 5), (5, 3), (4, 6), (2, 6)]
_FOLLOWERS = [1, 3, 4, 6]  # of the tag the queries name
_FILLER_TAGS = 10

_BI18 = (
    "MATCH (tag:Tag {name: 'Frank'})<-[:HAS_INTEREST]-(person1:Person)-[:KNOWS]-(mutualFriend:Person)"
    "-[:KNOWS]-(person2:Person)-[:HAS_INTEREST]->(tag) "
    "WHERE person1 <> person2 AND NOT (person1)-[:KNOWS]-(person2) "
    "RETURN person1.id AS p1, person2.id AS p2, count(DISTINCT mutualFriend) AS n ORDER BY p1, p2"
)

_QUERIES = {
    "bi18": _BI18,
    "several pattern parts": (
        "MATCH (tag:Tag {name: 'Frank'})<-[:HAS_INTEREST]-(p:Person), (p)-[:KNOWS]-(q:Person)-[:HAS_INTEREST]->(tag) "
        "RETURN p.id AS p, q.id AS q ORDER BY p, q"
    ),
    "leading match": (
        "MATCH (tag:Tag {name: 'Frank'})<-[:HAS_INTEREST]-(p:Person)-[:KNOWS]-(q:Person)-[:HAS_INTEREST]->(tag) "
        "WITH p, q RETURN p.id AS p, q.id AS q ORDER BY p, q"
    ),
    "optional match keeps unmatched rows": (
        "MATCH (tag:Tag) WHERE tag.name IN ['Frank', 'Nobody'] "
        "OPTIONAL MATCH (tag)<-[:HAS_INTEREST]-(p:Person)-[:KNOWS]-(q:Person)-[:HAS_INTEREST]->(tag) "
        "RETURN tag.name AS tag, count(q) AS n ORDER BY tag"
    ),
    "undirected cycle": (
        "MATCH (a:Person)-[:KNOWS]-(b:Person)-[:KNOWS]-(c:Person)-[:KNOWS]-(d:Person)-[:KNOWS]-(a) "
        "RETURN a.id AS a, b.id AS b, c.id AS c, d.id AS d ORDER BY a, b, c, d"
    ),
}


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
        # An unlabeled endpoint declares every vertex collection (``WITH``),
        # so the mapping's other collections exist, empty.
        mapping = mapping_bundle_for("ldbc_pg").physical_mapping
        for ent in mapping["entities"].values():
            name = ent.get("collectionName")
            if name and not database.has_collection(name):
                database.create_collection(name)
        for rel in mapping["relationships"].values():
            name = rel.get("edgeCollectionName")
            if name and not database.has_collection(name):
                database.create_collection(name, edge=True)
        database.collection("person").import_bulk([{"_key": f"p{i}", "id": i} for i in _PERSONS])
        fillers = [f"f{i}" for i in range(_FILLER_TAGS)]
        database.collection("tag").import_bulk(
            [{"_key": "frank", "name": "Frank"}, {"_key": "nobody", "name": "Nobody"}]
            + [{"_key": f, "name": f} for f in fillers]
        )
        database.collection("knows").import_bulk(
            [{"_from": f"person/p{a}", "_to": f"person/p{b}"} for a, b in _KNOWS]
        )
        database.collection("has_interest").import_bulk(
            [{"_from": f"person/p{p}", "_to": "tag/frank"} for p in _FOLLOWERS]
            + [{"_from": f"person/p{p}", "_to": f"tag/{f}"} for p in _PERSONS for f in fillers]
        )
        yield database
    finally:
        if sys_db.has_database(_DB):
            sys_db.delete_database(_DB)


def _with_statistics(db: Any) -> MappingBundle:
    bundle = mapping_bundle_for("ldbc_pg")
    return dataclasses.replace(
        bundle, metadata={**bundle.metadata, "statistics": compute_statistics(db, bundle)}
    )


def _run(db: Any, cypher: str, mapping: MappingBundle) -> tuple[list[Any], int, str]:
    q = translate(cypher, mapping=mapping)
    cursor = db.aql.execute(q.aql, bind_vars=q.bind_vars)
    return list(cursor), cursor.statistics()["scanned_index"], q.aql


@pytest.mark.parametrize("name", list(_QUERIES))
def test_rows_are_the_same_with_the_list(db: Any, name: str) -> None:
    cypher = _QUERIES[name]
    without, _, plain = _run(db, cypher, mapping_bundle_for("ldbc_pg"))
    with_list, _, listed = _run(db, cypher, _with_statistics(db))
    assert "_cl_e" not in plain, plain
    assert "_cl_e" in listed, listed
    assert with_list == without
    assert without, "the query should match something"


def test_bi18_rows(db: Any) -> None:
    rows, _, _ = _run(db, _BI18, _with_statistics(db))
    # Followers of Frank two KNOWS hops apart, not knowing each other.
    assert rows == [
        {"p1": 1, "p2": 3, "n": 2},
        {"p1": 1, "p2": 6, "n": 1},
        {"p1": 3, "p2": 1, "n": 2},
        {"p1": 3, "p2": 6, "n": 2},
        {"p1": 6, "p2": 1, "n": 1},
        {"p1": 6, "p2": 3, "n": 2},
    ]


def test_bi18_walks_only_from_followers(db: Any) -> None:
    _, scanned_without, _ = _run(db, _BI18, mapping_bundle_for("ldbc_pg"))
    _, scanned_with, aql = _run(db, _BI18, _with_statistics(db))
    assert scanned_with < scanned_without, (scanned_with, scanned_without, aql)
