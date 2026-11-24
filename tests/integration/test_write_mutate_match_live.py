"""MATCH … SET / REMOVE / DELETE on ArangoDB: the MATCH means what it means anywhere.

Every write here failed on the server (ERR 1203, ERR 1511), was refused, or
wrote rows Cypher does not match, while ``write_mutate`` compiled the MATCH
itself. Each case runs on a fresh graph, on the PG mapping (a collection per
label, ``follows`` reaching ``users`` and ``docs``) and the LPG one; the
written graph is then read back. The unit counterpart is
``tests/test_translate_write_mutate_match.py``.
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

_DB = "arango_cypher_write_mutate_match_it"

#: a → b (w 1), b → c (w 2), a → d1 (w 1, a Doc), c → a (w 2), a → a (w 3, a self-loop).
_FOLLOWS = [("a", "b", 1), ("b", "c", 2), ("a", "d1", 1), ("c", "a", 2), ("a", "a", 3)]


def _pg_id(key: str) -> str:
    return f"docs/{key}" if key.startswith("d") else f"users/{key}"


@pytest.fixture(scope="module")
def client() -> Iterator[tuple[Any, Any]]:
    if os.environ.get("RUN_INTEGRATION") != "1":
        pytest.skip("RUN_INTEGRATION=1 required")
    user = os.environ.get("ARANGO_USER", "root")
    password = os.environ.get("ARANGO_PASS", "openSesame")
    arango = ArangoClient(hosts=os.environ.get("ARANGO_URL", "http://localhost:8529"))
    sys_db = arango.db("_system", username=user, password=password)
    try:
        yield arango, sys_db
    finally:
        if sys_db.has_database(_DB):
            sys_db.delete_database(_DB)


@pytest.fixture
def db(client: tuple[Any, Any]) -> Any:
    """A fresh graph for each write."""
    arango, sys_db = client
    if sys_db.has_database(_DB):
        sys_db.delete_database(_DB)
    sys_db.create_database(_DB)
    database = arango.db(
        _DB,
        username=os.environ.get("ARANGO_USER", "root"),
        password=os.environ.get("ARANGO_PASS", "openSesame"),
    )
    for name in ("persons", "places"):
        database.create_collection(name)
    database.create_collection("users").import_bulk([{"_key": k, "name": k} for k in "abc"])
    database.create_collection("docs").import_bulk([{"_key": "d1", "name": "d1"}])
    database.create_collection("follows", edge=True).import_bulk(
        [{"_from": _pg_id(f), "_to": _pg_id(t), "w": w} for f, t, w in _FOLLOWS]
    )
    database.create_collection("vertices").import_bulk(
        [{"_key": k, "name": k, "type": "User"} for k in "abc"]
        + [{"_key": "d1", "name": "d1", "type": "Doc"}]
    )
    database.create_collection("edges", edge=True).import_bulk(
        [
            {"_from": f"vertices/{f}", "_to": f"vertices/{t}", "w": w, "type": "FOLLOWS"}
            for f, t, w in _FOLLOWS
        ]
    )
    return database


def _run(db: Any, cypher: str, mapping: str) -> list[Any]:
    q = translate(cypher, mapping=mapping_bundle_for(mapping))
    return list(db.aql.execute(q.aql, bind_vars=q.bind_vars))


def _hit(db: Any, mapping: str, label: str = "User") -> list[str]:
    rows = _run(db, f"MATCH (u:{label}) WHERE u.hit RETURN u.name AS n ORDER BY n", mapping)
    return [r["n"] for r in rows]


_MAPPINGS = pytest.mark.parametrize("mapping", ["pg", "lpg"])


@_MAPPINGS
def test_a_named_path_is_read_by_the_write(db: Any, mapping: str) -> None:
    _run(db, "MATCH p = (a:User {name: 'b'})-[:FOLLOWS]->(b:User) SET b.hops = length(p)", mapping)
    rows = _run(db, "MATCH (u:User) WHERE u.hops IS NOT NULL RETURN u.name AS n, u.hops AS h", mapping)
    assert rows == [{"n": "c", "h": 1}]


@_MAPPINGS
def test_a_named_path_is_returned_after_the_write(db: Any, mapping: str) -> None:
    cypher = (
        "MATCH p = (a:User {name: 'a'})-[:FOLLOWS]->(b:User {name: 'b'}) SET a.x = 1 "
        "RETURN length(p) AS l, size(nodes(p)) AS k"
    )
    assert _run(db, cypher, mapping) == [{"l": 1, "k": 2}]


@_MAPPINGS
@pytest.mark.parametrize(
    ("cypher", "expected"),
    [
        ("MATCH (:User {name: 'a'})-[:FOLLOWS]->(n:User) SET n.hit = true", ["a", "b"]),
        ("MATCH (v:User {name: 'b'})-[:FOLLOWS]->() SET v.hit = true", ["b"]),
        ("MATCH (n:User {name: 'b'}), (:User {name: 'c'}) SET n.hit = true", ["b"]),
        ("MATCH (a:User {name: 'a'}), (b:User {name: 'zz'}) SET a.hit = true", []),
        # Paths whose every edge has w = 2: none from a (a → b has w 1).
        ("MATCH (a:User {name: 'a'})-[:FOLLOWS*1..2 {w: 2}]->(b:User) SET b.hit = true", []),
        ("MATCH (a:User {name: 'b'})-[:FOLLOWS*1..2 {w: 2}]->(b:User) SET b.hit = true", ["a", "c"]),
        # b's only edges are a → b and b → c: no second edge leads back to b.
        (
            "MATCH (a:User {name: 'b'})-[r1:FOLLOWS]-(b:User)-[r2:FOLLOWS]-(c:User {name: 'b'}) SET b.hit = true",
            [],
        ),
        ("MATCH (a:User)-[r:FOLLOWS]->(b:User) WHERE r.w = 2 SET b.hit = true", ["a", "c"]),
    ],
)
def test_the_write_follows_the_whole_match(db: Any, mapping: str, cypher: str, expected: list[str]) -> None:
    _run(db, cypher, mapping)
    assert _hit(db, mapping) == expected


@_MAPPINGS
def test_a_self_loop_is_written_once(db: Any, mapping: str) -> None:
    assert _run(db, "MATCH (a:User)-[r:FOLLOWS]-(a) SET r.loop = true RETURN count(*) AS k", mapping) == [1]
    assert _run(db, "MATCH ()-[r:FOLLOWS]->() WHERE r.loop RETURN r.w AS w", mapping) == [{"w": 3}]


@_MAPPINGS
def test_a_target_of_another_collection_is_written_there(db: Any, mapping: str) -> None:
    _run(db, "MATCH (a:User {name: 'a'})-[:FOLLOWS]->(d:Doc) SET d.hit = true", mapping)
    assert _hit(db, mapping, "Doc") == ["d1"]
    assert _hit(db, mapping) == []


@_MAPPINGS
def test_comma_parts_read_each_other(db: Any, mapping: str) -> None:
    rows = _run(
        db, "MATCH (a:User {name: 'a'}), (b:User {name: 'b'}) SET a.x = b.name RETURN a.x AS x", mapping
    )
    assert rows == [{"x": "b"}]


@_MAPPINGS
def test_optional_match_with_a_named_path_writes_nothing_when_unmatched(db: Any, mapping: str) -> None:
    rows = _run(
        db, "OPTIONAL MATCH p = (a:User {name: 'zz'})-[:FOLLOWS]->(b:User) SET b.hit = true RETURN p", mapping
    )
    assert rows == [None]
    assert _hit(db, mapping) == []
