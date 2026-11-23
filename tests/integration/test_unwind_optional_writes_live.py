"""UNWIND, OPTIONAL MATCH and write translations, run on ArangoDB.

Every case here compiled before, and on a server either failed (ERR 1203,
1501, 1563) or returned or wrote the wrong thing. The expected rows and
documents are what Cypher gives. The unit counterpart is
``tests/test_translate_unwind_optional_writes.py``.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import pytest
from arango import ArangoClient
from arango_query_core import ExtensionPolicy, ExtensionRegistry

from arango_cypher import translate
from arango_cypher.procedures import parse_signature, register_table_procedure
from tests.helpers.mapping_fixtures import mapping_bundle_for

pytestmark = pytest.mark.integration

_DB = "arango_cypher_unwind_optional_writes_it"

#: alice follows bob. Person p1 follows Place l1 since 2019.
_USERS = [{"_key": "alice", "name": "alice"}, {"_key": "bob", "name": "bob"}]


@pytest.fixture
def db() -> Iterator[Any]:
    """A fresh database per test: several tests write."""
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
        database.create_collection("docs")
        database.create_collection("users").import_bulk(_USERS)
        database.create_collection("persons").insert({"_key": "p1", "id": 1})
        database.create_collection("places").insert({"_key": "l1", "id": 2})
        database.create_collection("follows", edge=True).import_bulk(
            [
                {"_from": "users/alice", "_to": "users/bob"},
                {"_from": "persons/p1", "_to": "places/l1", "since": 2019},
            ]
        )
        yield database
    finally:
        if sys_db.has_database(_DB):
            sys_db.delete_database(_DB)


def _rows(db: Any, cypher: str, **kwargs: Any) -> list[Any]:
    q = translate(cypher, mapping=mapping_bundle_for("pg"), **kwargs)
    return list(db.aql.execute(q.aql, bind_vars=q.bind_vars))


def _users(db: Any) -> dict[str, dict[str, Any]]:
    return {d["name"]: d for d in db.collection("users").all() if "name" in d}


# --- UNWIND -------------------------------------------------------------------


def test_unwind_after_the_match_runs_when_one_comes_before_it(db: Any) -> None:
    rows = _rows(db, "UNWIND [1, 2] AS x MATCH (u:User) UNWIND [3, 4] AS y RETURN count(*) AS n")
    assert rows == [8]  # 2 × 2 users × 2 (one column: scalar rows)


def test_unwind_of_a_scalar_before_a_match_is_one_row(db: Any) -> None:
    rows = _rows(db, "UNWIND 5 AS x MATCH (u:User) RETURN x, u.name AS u ORDER BY u")
    assert rows == [{"x": 5, "u": "alice"}, {"x": 5, "u": "bob"}]


def test_unwind_of_null_before_a_match_is_no_rows(db: Any) -> None:
    assert _rows(db, "UNWIND null AS x MATCH (u:User) RETURN count(*) AS n") == [0]


def test_unwind_of_null_before_create_creates_nothing(db: Any) -> None:
    _rows(db, "UNWIND null AS x CREATE (:User {name: x})")
    assert db.collection("users").count() == 2


def test_unwind_of_a_scalar_before_merge_merges_once(db: Any) -> None:
    _rows(db, "UNWIND 7 AS x MERGE (:User {name: 'u' + toString(x)})")
    assert "u7" in _users(db)
    assert db.collection("users").count() == 3


# --- OPTIONAL MATCH with no MATCH --------------------------------------------


def test_unwind_then_optional_match_keeps_the_rows_it_does_not_match(db: Any) -> None:
    rows = _rows(
        db,
        "UNWIND ['alice', 'zed'] AS name OPTIONAL MATCH (u:User {name: name}) "
        "RETURN name, u.name AS found ORDER BY name",
    )
    assert rows == [{"name": "alice", "found": "alice"}, {"name": "zed", "found": None}]


def test_unwind_then_optional_match_with_no_match_at_all(db: Any) -> None:
    rows = _rows(db, "UNWIND [1, 2] AS x OPTIONAL MATCH (u:User {name: 'nobody'}) RETURN x, u ORDER BY x")
    assert rows == [{"x": 1, "u": None}, {"x": 2, "u": None}]


def test_call_then_optional_match_keeps_the_rows_it_does_not_match(db: Any) -> None:
    registry = ExtensionRegistry(policy=ExtensionPolicy(enabled=True))
    register_table_procedure(
        registry, parse_signature("test.names() :: (name :: STRING?)"), [{"name": "alice"}, {"name": "zed"}]
    )
    rows = _rows(
        db,
        "CALL test.names() YIELD name OPTIONAL MATCH (u:User {name: name}) RETURN name, u.name AS found ORDER BY name",
        registry=registry,
    )
    assert rows == [{"name": "alice", "found": "alice"}, {"name": "zed", "found": None}]


def test_optional_match_unwind_optional_match_in_source_order(db: Any) -> None:
    rows = _rows(
        db,
        "OPTIONAL MATCH (u:User {name: 'alice'}) UNWIND [1, 2] AS y "
        "OPTIONAL MATCH (u)-[:FOLLOWS]->(f:User) RETURN y, u.name AS u, f.name AS f ORDER BY y",
    )
    assert rows == [{"y": 1, "u": "alice", "f": "bob"}, {"y": 2, "u": "alice", "f": "bob"}]


# --- OPTIONAL MATCH … SET / DELETE with no RETURN -------------------------------


def test_optional_match_set_with_no_return_writes_the_match(db: Any) -> None:
    assert _rows(db, "OPTIONAL MATCH (u:User {name: 'bob'}) SET u.flag = true") == []
    assert _users(db)["bob"]["flag"] is True


def test_optional_match_set_with_no_return_and_no_match_writes_nothing(db: Any) -> None:
    assert _rows(db, "OPTIONAL MATCH (u:User {name: 'nobody'}) SET u.flag = true, u.x = 1 REMOVE u.y") == []
    assert db.collection("users").count() == 2
    assert all("flag" not in d for d in _users(db).values())


# --- MERGE ----------------------------------------------------------------------


def test_relationship_merge_after_node_merges_creates_an_edge_with_other_properties(db: Any) -> None:
    cypher = "MERGE (a:Person {id: 1}) MERGE (b:Place {id: 2}) MERGE (a)-[r:FOLLOWS {since: 2020}]->(b)"
    _rows(db, cypher)
    since = sorted(e["since"] for e in db.collection("follows").all() if e["_from"] == "persons/p1")
    assert since == [2019, 2020]
    _rows(db, cypher)  # now it matches
    assert db.collection("follows").count() == 3


def test_node_merge_stores_no_null_property(db: Any) -> None:
    _rows(db, "MERGE (u:User {name: 'nina'}) ON CREATE SET u.nick = null, u.age = 3")
    nina = _users(db)["nina"]
    assert "nick" not in nina
    assert nina["age"] == 3


def test_node_merge_on_match_set_null_removes_the_property(db: Any) -> None:
    db.collection("users").update({"_key": "bob", "age": 40})
    _rows(db, "MERGE (u:User {name: 'bob'}) ON MATCH SET u.age = null")
    assert "age" not in _users(db)["bob"]
