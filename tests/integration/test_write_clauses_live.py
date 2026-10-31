"""SET / REMOVE / DELETE / CREATE write shapes, executed on a real ArangoDB.

Each of these translated before but failed on a server: a second write to a
collection (ERR 1579), a relationship written in its start node's collection
(ERR 1202), a trailing LET after CREATE (ERR 1501), or an edge replaced without
its endpoints (ERR 1233). Under LPG, ``SET n = {…}`` dropped the type field.
The unit counterpart is ``tests/test_translate_property_writes.py``.
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

_DB = "arango_cypher_write_clauses_it"


@pytest.fixture
def db() -> Iterator[Any]:
    user = os.environ.get("ARANGO_USER", "root")
    password = os.environ.get("ARANGO_PASS", "openSesame")
    client = ArangoClient(hosts=os.environ.get("ARANGO_URL", "http://localhost:8529"))
    sys_db = client.db("_system", username=user, password=password)
    if sys_db.has_database(_DB):
        sys_db.delete_database(_DB)
    sys_db.create_database(_DB)
    try:
        database = client.db(_DB, username=user, password=password)
        for name in ("users", "docs", "persons", "places", "vertices"):
            database.create_collection(name)
        for name in ("follows", "edges"):
            database.create_collection(name, edge=True)
        database.collection("users").insert_many(
            [{"_key": "a", "name": "a", "x": 1, "y": 2}, {"_key": "b", "name": "b", "x": 1}]
        )
        database.collection("follows").insert({"_key": "e", "_from": "users/a", "_to": "users/b", "w": 5})
        database.collection("vertices").insert({"_key": "v", "type": "User", "name": "a", "x": 1})
        yield database
    finally:
        if sys_db.has_database(_DB):
            sys_db.delete_database(_DB)


def _run(db: Any, cypher: str, mapping: str = "pg") -> list[Any]:
    q = translate(cypher, mapping=mapping_bundle_for(mapping))
    return list(db.aql.execute(q.aql, bind_vars=q.bind_vars))


def _doc(db: Any, collection: str, key: str) -> dict[str, Any]:
    return {k: v for k, v in db.collection(collection).get(key).items() if not k.startswith("_")}


@pytest.mark.parametrize(
    ("cypher", "expected"),
    [
        ("MATCH (n:User) WHERE n.name = 'a' REMOVE n.x, n.y", {"name": "a"}),
        ("MATCH (n:User) WHERE n.name = 'a' SET n.z = 1 REMOVE n.x", {"name": "a", "y": 2, "z": 1}),
        ("MATCH (n:User) WHERE n.name = 'a' SET n.z = 1 SET n.y = 9", {"name": "a", "x": 1, "y": 9, "z": 1}),
        ("MATCH (n:User) WHERE n.name = 'a' REMOVE n.x SET n.x = 7", {"name": "a", "x": 7, "y": 2}),
        (
            "MATCH (n:User) WHERE n.name = 'a' SET n.x = n.x + 10, n += {k: 1} REMOVE n.y",
            {"name": "a", "x": 11, "k": 1},
        ),
        ("MATCH (n:User) WHERE n.name = 'a' WITH n SET n.z = 1 REMOVE n.x", {"name": "a", "y": 2, "z": 1}),
        ("MATCH (n:User) WHERE n.name = 'a' SET (n).z = 4", {"name": "a", "x": 1, "y": 2, "z": 4}),
    ],
)
def test_several_items_on_one_node_are_one_write(db: Any, cypher: str, expected: dict[str, Any]) -> None:
    _run(db, cypher)
    assert _doc(db, "users", "a") == expected


@pytest.mark.parametrize(
    ("cypher", "edge"),
    [
        ("MATCH (n:User)-[r:FOLLOWS]->(m:User) WHERE n.name = 'a' SET r.w = 6", {"w": 6}),
        ("MATCH (n:User)-[r:FOLLOWS]->(m:User) WHERE n.name = 'a' REMOVE r.w", {}),
        ("MATCH (n:User)-[r:FOLLOWS]->(m:User) WHERE n.name = 'a' SET r = {w: 8}", {"w": 8}),
    ],
)
def test_a_relationship_is_written_in_its_edge_collection(db: Any, cypher: str, edge: dict[str, Any]) -> None:
    _run(db, cypher)
    assert _doc(db, "follows", "e") == edge
    stored = db.collection("follows").get("e")
    assert (stored["_from"], stored["_to"]) == ("users/a", "users/b")


def test_deleting_a_relationship_removes_the_edge(db: Any) -> None:
    _run(db, "MATCH (n:User)-[r:FOLLOWS]->(m:User) WHERE n.name = 'a' DELETE r")
    assert db.collection("follows").count() == 0
    assert db.collection("users").count() == 2


def test_a_node_and_its_relationship_are_written_together(db: Any) -> None:
    _run(db, "MATCH (n:User)-[r:FOLLOWS]->(m:User) WHERE n.name = 'a' SET r.w = 6, n.x = 9")
    assert _doc(db, "follows", "e") == {"w": 6}
    assert _doc(db, "users", "a")["x"] == 9


def test_replacing_a_labelled_node_keeps_its_label(db: Any) -> None:
    _run(db, "MATCH (n:User) WHERE n.name = 'a' SET n = {name: 'a', q: 1}", mapping="lpg")
    assert _doc(db, "vertices", "v") == {"name": "a", "q": 1, "type": "User"}


@pytest.mark.parametrize(
    ("cypher", "expected", "rows"),
    [
        ("CREATE (n:User {name: 'c', x: 1}) SET n.y = 2", {"name": "c", "x": 1, "y": 2}, []),
        ("CREATE (n:User {name: 'c', x: 1}) REMOVE n.x", {"name": "c"}, []),
        (
            "CREATE (n:User {name: 'c', x: 1}) SET n.y = n.x + 1 RETURN n.y AS y",
            {"name": "c", "x": 1, "y": 2},
            [{"y": 2}],
        ),
    ],
)
def test_create_then_set_or_remove_is_folded_into_the_insert(
    db: Any, cypher: str, expected: dict[str, Any], rows: list[Any]
) -> None:
    assert _run(db, cypher) == rows
    created = [d for d in db.collection("users").find({"name": "c"})]
    assert len(created) == 1
    assert {k: v for k, v in created[0].items() if not k.startswith("_")} == expected


@pytest.mark.parametrize(
    ("literal", "expected"),
    [
        ("'\\U0001F600'", "\U0001f600"),
        ("'\\U00000022'", '"'),
        ("'\\N'", "\n"),
        ("'a\\Tb'", "a\tb"),
        ("'\\\\U0001F600'", "\\U0001F600"),
    ],
)
def test_cypher_string_escapes_mean_the_same_in_aql(db: Any, literal: str, expected: str) -> None:
    rows = _run(db, f"RETURN {literal} AS x")
    assert rows == [{"x": expected}]


def test_set_after_merge_writes_on_match_and_on_create(db: Any) -> None:
    """SET after a node MERGE was dropped from the AQL; it is now written
    whether the MERGE matched (users/a) or created (name 'c') the node."""
    _run(db, "MERGE (n:User {name: 'a'}) SET n.seen = true")
    assert _doc(db, "users", "a") == {"name": "a", "x": 1, "y": 2, "seen": True}
    _run(db, "MERGE (n:User {name: 'c'}) SET n.seen = true")
    created = list(
        db.aql.execute("FOR u IN users FILTER u.name == 'c' RETURN UNSET(u, '_id', '_key', '_rev')")
    )
    assert created == [{"name": "c", "seen": True}]


def _edge(db: Any, key: str) -> dict[str, Any]:
    return {k: v for k, v in db.collection("follows").get(key).items() if k not in ("_id", "_key", "_rev")}


def test_on_match_set_a_whole_node_replaces_the_relationship_properties(db: Any) -> None:
    _run(db, "MATCH (a:User {name: 'a'}), (b:User {name: 'b'}) MERGE (a)-[r:FOLLOWS]->(b) ON MATCH SET r = b")
    assert _edge(db, "e") == {"_from": "users/a", "_to": "users/b", "name": "b", "x": 1}


def test_on_match_merging_a_map_with_a_null_removes_that_key(db: Any) -> None:
    _run(
        db,
        "MATCH (a:User {name: 'a'}), (b:User {name: 'b'}) MERGE (a)-[r:FOLLOWS]->(b) "
        "ON MATCH SET r += {w: null, z: 3}",
    )
    assert _edge(db, "e") == {"_from": "users/a", "_to": "users/b", "z": 3}


def test_on_create_set_a_map_on_a_new_relationship(db: Any) -> None:
    _run(
        db,
        "MATCH (a:User {name: 'b'}), (b:User {name: 'a'}) MERGE (a)-[r:FOLLOWS]->(b) "
        "ON CREATE SET r = {since: 2020, gone: null}",
    )
    created = list(db.aql.execute("FOR e IN follows FILTER e._from == 'users/b' RETURN e.since"))
    assert created == [2020]
    assert list(db.aql.execute("FOR e IN follows FILTER e._from == 'users/b' RETURN HAS(e, 'gone')")) == [
        False
    ]


def test_on_create_and_on_match_set_on_a_merge_endpoint(db: Any) -> None:
    """ON CREATE / ON MATCH SET of an endpoint writes that node, per what the MERGE did."""
    query = (
        "MATCH (a:User {name: 'b'}), (b:User {name: 'a'}) MERGE (a)-[:FOLLOWS]->(b) "
        "ON CREATE SET b.created = 1 ON MATCH SET b.matched = 1, a.matched = 2"
    )
    assert _run(db, query) == []
    assert _doc(db, "users", "a") == {"name": "a", "x": 1, "y": 2, "created": 1}
    assert _doc(db, "users", "b") == {"name": "b", "x": 1}
    _run(db, query)
    assert _doc(db, "users", "a") == {"name": "a", "x": 1, "y": 2, "created": 1, "matched": 1}
    assert _doc(db, "users", "b") == {"name": "b", "x": 1, "matched": 2}
    assert db.collection("follows").count() == 2


def test_endpoint_sets_in_one_lpg_collection_are_one_write(db: Any) -> None:
    """Both endpoints live in ``vertices``: one UPDATE writes both (a second would be ERR 1579)."""
    db.collection("vertices").insert({"_key": "w", "type": "User", "name": "w"})
    rows = _run(
        db,
        "MATCH (a:User {name: 'a'}), (b:User {name: 'w'}) MERGE (a)-[r:FOLLOWS]->(b) "
        "ON CREATE SET a.out = 1, b.in = 1 RETURN count(r) AS c",
        mapping="lpg",
    )
    assert rows == [1]  # a single count is returned bare by the write RETURN
    assert _doc(db, "vertices", "v")["out"] == 1
    assert _doc(db, "vertices", "w")["in"] == 1


def test_merge_binds_a_named_path(db: Any) -> None:
    rows = _run(db, "MERGE p = (n:User {name: 'z'}) RETURN length(p) AS l, [m IN nodes(p) | m.name] AS names")
    assert rows == [{"l": 0, "names": ["z"]}]
    rows = _run(
        db,
        "MATCH (a:User {name: 'a'}), (b:User {name: 'b'}) MERGE p = (a)-[:FOLLOWS]->(b) "
        "RETURN length(p) AS l, [n IN nodes(p) | n.name] AS names",
    )
    assert rows == [{"l": 1, "names": ["a", "b"]}]
    assert db.collection("follows").count() == 1


def test_a_relationship_merge_with_other_properties_creates_a_new_relationship(db: Any) -> None:
    """The pattern's properties are matched: {w: 6} does not match the w: 5 edge."""
    query = (
        "MATCH (a:User {name: 'a'}), (b:User {name: 'b'}) MERGE (a)-[r:FOLLOWS {w: 6}]->(b) RETURN r.w AS w"
    )
    assert _run(db, query) == [{"w": 6}]
    assert sorted(e["w"] for e in db.collection("follows").all()) == [5, 6]
    _run(db, query)  # now it matches
    assert db.collection("follows").count() == 2


def _names(db: Any, collection: str) -> list[str]:
    return sorted(d["name"] for d in db.collection(collection).all())


def test_a_pattern_merge_with_unbound_endpoints_creates_then_matches_the_whole_pattern(db: Any) -> None:
    query = (
        "MERGE (a:User {name: 'x'})-[r:FOLLOWS {w: 1}]->(b:User {name: 'y'}) RETURN a.name AS a, b.name AS b"
    )
    assert _run(db, query) == [{"a": "x", "b": "y"}]
    assert _names(db, "users") == ["a", "b", "x", "y"]
    edge = next(e for e in db.collection("follows").all() if e.get("w") == 1)
    assert (edge["_from"], edge["_to"]) == (
        next(db.collection("users").find({"name": "x"}))["_id"],
        next(db.collection("users").find({"name": "y"}))["_id"],
    )
    assert _run(db, query) == [{"a": "x", "b": "y"}]  # matched: nothing new
    assert db.collection("users").count() == 4 and db.collection("follows").count() == 2


def test_a_pattern_merge_creates_every_node_when_only_the_relationship_is_missing(db: Any) -> None:
    """openCypher: users 'a' and 'b' exist, but no a-[:FOLLOWS {w: 9}]->b, so all of it is created."""
    _run(db, "MERGE (a:User {name: 'a'})-[:FOLLOWS {w: 9}]->(b:User {name: 'b'})")
    assert _names(db, "users") == ["a", "a", "b", "b"]
    assert db.collection("follows").count() == 2


def test_a_pattern_merge_writes_lpg_endpoints_in_one_insert(db: Any) -> None:
    rows = _run(
        db, "MERGE (a:User {name: 'p'})<-[r:FOLLOWS]-(b:User {name: 'q'}) RETURN type(r) AS t", mapping="lpg"
    )
    assert rows == [{"t": "FOLLOWS"}]
    new = {d["name"]: d for d in db.collection("vertices").all() if d["name"] in ("p", "q")}
    assert new["p"]["type"] == new["q"]["type"] == "User"
    (edge,) = list(db.collection("edges").all())
    assert (edge["_from"], edge["_to"], edge["type"]) == (new["q"]["_id"], new["p"]["_id"], "FOLLOWS")


def test_remove_after_optional_match_ignores_the_rows_it_did_not_match(db: Any) -> None:
    """TCK Remove1 [6]: REMOVE of null does nothing; the edge of 'a' loses w."""
    rows = _run(db, "MATCH (n:User) OPTIONAL MATCH (n)-[r:FOLLOWS]->() REMOVE r.w RETURN n.name AS name")
    assert sorted(r["name"] for r in rows) == ["a", "b"]
    assert _edge(db, "e") == {"_from": "users/a", "_to": "users/b"}
