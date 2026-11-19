"""Relationships in different parts of one MATCH are distinct, named or not.

``MATCH (u)-[:FOLLOWS]->(v), (w)-[:FOLLOWS]->(v)`` compiles to two separate
traversals, which ArangoDB does not keep apart; the uniqueness filter was
emitted only for named relationships, so one edge matched both parts and the
query returned ``u = w``. The chain ``(u)-[:FOLLOWS]->(v)<-[:FOLLOWS]-(w)`` and
the named form returned nothing, as Cypher does.
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

_DB = "arango_cypher_relationship_uniqueness_it"
_SHAPES = {
    "comma, unnamed": "MATCH (u:User)-[:FOLLOWS]->(v:User), (w:User)-[:FOLLOWS]->(v)",
    "comma, named": "MATCH (u:User)-[a:FOLLOWS]->(v:User), (w:User)-[b:FOLLOWS]->(v)",
    "chain": "MATCH (u:User)-[:FOLLOWS]->(v:User)<-[:FOLLOWS]-(w:User)",
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
        # Every collection the pg fixture names, so each query's WITH resolves.
        for cypher in _SHAPES.values():
            q = translate(f"{cypher} RETURN u.id", mapping=mapping_bundle_for("pg"))
            for key, name in q.bind_vars.items():
                if key.startswith("@") and not database.has_collection(name):
                    database.create_collection(name, edge=key.startswith("@edgeCollection"))
        database.collection("users").import_bulk([{"_key": k, "id": k} for k in ("a", "b", "c", "d")])
        # a -> b alone; c -> d and a -> d, so d has two distinct followers.
        database.collection("follows").import_bulk(
            [
                {"_from": "users/a", "_to": "users/b"},
                {"_from": "users/c", "_to": "users/d"},
                {"_from": "users/a", "_to": "users/d"},
            ]
        )
        yield database
    finally:
        if sys_db.has_database(_DB):
            sys_db.delete_database(_DB)


@pytest.mark.parametrize("shape", list(_SHAPES))
def test_one_edge_never_matches_two_relationships(db: Any, shape: str) -> None:
    q = translate(
        f"{_SHAPES[shape]} RETURN u.id AS u, w.id AS w ORDER BY u, w", mapping=mapping_bundle_for("pg")
    )
    rows = list(db.aql.execute(q.aql, bind_vars=q.bind_vars))
    # Only d has two followers: the two orderings of a and c, never a follower with itself.
    assert rows == [{"u": "a", "w": "c"}, {"u": "c", "w": "a"}], q.aql


def test_relationships_in_different_match_clauses_may_be_the_same_edge(db: Any) -> None:
    # Uniqueness holds within one MATCH clause: here a -> b matches both.
    q = translate(
        "MATCH (u:User)-[a:FOLLOWS]->(v:User) MATCH (w:User)-[b:FOLLOWS]->(v) "
        "RETURN u.id AS u, w.id AS w ORDER BY u, w",
        mapping=mapping_bundle_for("pg"),
    )
    rows = list(db.aql.execute(q.aql, bind_vars=q.bind_vars))
    assert rows == [
        {"u": "a", "w": "a"},
        {"u": "a", "w": "a"},
        {"u": "a", "w": "c"},
        {"u": "c", "w": "a"},
        {"u": "c", "w": "c"},
    ], q.aql
