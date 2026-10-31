"""DELETE of an expression after a WITH, on ArangoDB: the side effects TCK Delete5
only states (its harness checks the result rows, not the graph)."""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import pytest
from arango import ArangoClient

from arango_cypher import translate
from tests.helpers.mapping_fixtures import mapping_bundle_for

pytestmark = pytest.mark.integration

_DB = "arango_cypher_pipeline_deletes_it"


@pytest.fixture
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
        # Every collection of the pg mapping: a DELETE whose target is known
        # only per row removes from each candidate collection.
        for name in ("users", "docs", "persons", "places"):
            database.create_collection(name)
        database.create_collection("follows", edge=True)
        database.collection("users").insert_many([{"_key": k, "name": k} for k in "abcd"])
        database.collection("follows").insert_many(
            [{"_key": f"a{k}", "_from": "users/a", "_to": f"users/{k}"} for k in "bcd"]
        )
        yield database
    finally:
        if sys_db.has_database(_DB):
            sys_db.delete_database(_DB)


def _run(db: Any, cypher: str, params: dict[str, Any] | None = None) -> list[Any]:
    q = translate(cypher, mapping=mapping_bundle_for("pg"), params=params)
    return list(db.aql.execute(q.aql, bind_vars=q.bind_vars))


def _keys(db: Any, collection: str) -> list[str]:
    return sorted(d["_key"] for d in db.collection(collection).all())


def test_detach_delete_one_element_of_a_collected_list(db: Any) -> None:
    _run(
        db,
        "MATCH (:User {name: 'a'})-[:FOLLOWS]->(n:User) WITH collect(n) AS fs ORDER BY 1 DETACH DELETE fs[0]",
    )
    assert len(_keys(db, "users")) == 3  # one node gone
    assert len(_keys(db, "follows")) == 2  # with its one relationship


def test_delete_relationships_from_a_map(db: Any) -> None:
    _run(db, "MATCH (:User)-[r:FOLLOWS]->(:User) WITH {key: r} AS rels DELETE rels.key")
    assert _keys(db, "follows") == []
    assert _keys(db, "users") == ["a", "b", "c", "d"]


def test_delete_a_path_removes_its_nodes_and_relationships(db: Any) -> None:
    rows = _run(
        db,
        "MATCH p = (:User {name: 'a'})-[:FOLLOWS]->(:User {name: 'b'}) WITH {key: collect(p)} AS m "
        "DETACH DELETE m.key[0] RETURN size(m.key) AS n",
    )
    assert rows == [{"n": 1}]
    assert _keys(db, "users") == ["c", "d"]
    assert _keys(db, "follows") == []  # a's other edges went with DETACH


def test_a_null_element_deletes_nothing(db: Any) -> None:
    _run(db, "MATCH (n:User) WITH collect(n) AS ns DELETE ns[10]")
    assert len(_keys(db, "users")) == 4
