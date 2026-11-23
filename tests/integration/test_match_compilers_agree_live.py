"""The MATCH compilers agree, run on ArangoDB.

Every query here ran before and returned a wrong count from one of the MATCH
compilers that lacked a fix the single-pattern compiler has. Each expected
value is what Cypher returns for this graph, and where the same question can
be asked as one pattern, it is asked both ways. The unit counterpart is
``tests/test_translate_match_compilers_agree.py``.
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

_DB = "arango_cypher_match_compilers_agree_it"

#: a → b (w 1), b → c (w 2), a → a (w 1, a self-loop), c → a (w 2).
_USERS = [{"_key": k, "name": k} for k in ("a", "b", "c")]
_FOLLOWS = [("a", "b", 1), ("b", "c", 2), ("a", "a", 1), ("c", "a", 2)]


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
        for name in ("docs", "persons", "places"):
            database.create_collection(name)
        database.create_collection("users").import_bulk(_USERS)
        database.create_collection("follows", edge=True).import_bulk(
            [{"_from": f"users/{f}", "_to": f"users/{t}", "w": w} for f, t, w in _FOLLOWS]
        )
        yield database
    finally:
        if sys_db.has_database(_DB):
            sys_db.delete_database(_DB)


def _rows(db: Any, cypher: str) -> list[Any]:
    q = translate(cypher, mapping=mapping_bundle_for("pg"))
    return list(db.aql.execute(q.aql, bind_vars=q.bind_vars))


def test_an_unnamed_node_is_not_joined_with_a_named_one(db: Any) -> None:
    assert _rows(db, "MATCH (:User), (n1:User) RETURN count(*) AS n") == [9]


def test_a_self_loop_is_matched_once_across_comma_parts(db: Any) -> None:
    one = _rows(db, "MATCH (a:User)-[r:FOLLOWS]-(a) RETURN count(r) AS n")
    two = _rows(db, "MATCH (a:User), (a)-[r:FOLLOWS]-(a) RETURN count(r) AS n")
    assert one == two == [1]


@pytest.mark.parametrize(
    "cypher",
    [
        "MATCH (a:User), (a)-[r:FOLLOWS*1..2 {w: 1}]->(b) RETURN count(*) AS n",
        "MATCH (a:User)-[r:FOLLOWS*1..2 {w: 1}]->(b) WITH b RETURN count(*) AS n",
        "MATCH (a:User) WITH a MATCH (a)-[r:FOLLOWS*1..2 {w: 1}]->(b) RETURN count(*) AS n",
    ],
)
def test_variable_length_properties_hold_on_every_edge(db: Any, cypher: str) -> None:
    # Paths whose every edge has w = 1: a→b, a→a, a→a→b.
    assert _rows(db, "MATCH (a:User)-[r:FOLLOWS*1..2 {w: 1}]->(b) RETURN count(*) AS n") == [3]
    assert _rows(db, cypher) == [3]


def test_a_pattern_predicate_checks_every_edge(db: Any) -> None:
    # Only a starts a path of w = 1 edges; c → a → b ends on one but starts on w = 2.
    assert _rows(db, "MATCH (a:User) WHERE (a)-[:FOLLOWS*1..2 {w: 1}]->() RETURN a.name AS n") == [{"n": "a"}]


def test_a_variable_length_relationship_before_with_is_its_list_of_edges(db: Any) -> None:
    rows = _rows(db, "MATCH (a:User)-[r:FOLLOWS*1..2 {w: 1}]->(b) WITH r RETURN size(r) AS n ORDER BY n")
    assert [r["n"] for r in rows] == [1, 1, 2]


def test_relationship_uniqueness_before_with(db: Any) -> None:
    pattern = "MATCH (a:User)-[r1:FOLLOWS]-(b)-[r2:FOLLOWS]-(c)"
    assert _rows(db, f"{pattern} WITH c RETURN count(*) AS n") == _rows(db, f"{pattern} RETURN count(*) AS n")


def test_a_variable_length_hop_shares_no_edge_with_another_comma_part(db: Any) -> None:
    together = _rows(db, "MATCH (a:User)-[:FOLLOWS*1..2]->(b)-[s:FOLLOWS]->(c) RETURN count(*) AS n")
    apart = _rows(db, "MATCH (a:User)-[:FOLLOWS*1..2]->(b), (b)-[s:FOLLOWS]->(c) RETURN count(*) AS n")
    assert apart == together
