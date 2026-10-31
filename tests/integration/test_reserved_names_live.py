"""Cypher names that AQL reserves, and backtick-quoted names, on ArangoDB.

``like``, ``filter``, ``sort``, ``in`` ... are plain names in Cypher but
reserved words in AQL, and names reach the AQL text verbatim, so a
relationship called ``like`` (LDBC BI6) was an ERR 1501 syntax error. The
translator now quotes them, and builds its own names (``_x_distinct``,
``x_1``) inside a quoted name's backticks.

Users a -> b, a -> c, b -> c and c -> d follow each other.
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

_DB = "arango_cypher_reserved_names_it"


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
        database.create_collection("users").import_bulk(
            [{"_key": k, "name": k, "rank": i, "limit": i * 10} for i, k in enumerate("abcd")]
        )
        for coll in ("docs", "persons", "places"):  # named by a traversal's WITH clause
            database.create_collection(coll)
        database.create_collection("follows", edge=True).import_bulk(
            [{"_from": f"users/{a}", "_to": f"users/{b}"} for a, b in ("ab", "ac", "bc", "cd")]
        )
        yield database
    finally:
        if sys_db.has_database(_DB):
            sys_db.delete_database(_DB)


def _rows(db: Any, cypher: str) -> list[Any]:
    q = translate(cypher, mapping=mapping_bundle_for("pg"))
    return list(db.aql.execute(q.aql, bind_vars=q.bind_vars))


@pytest.mark.parametrize(
    ("cypher", "expected"),
    [
        pytest.param(
            "MATCH (a:User {name: 'a'})-[:FOLLOWS]->(b:User) OPTIONAL MATCH (b)<-[like:FOLLOWS]-(x:User) "
            "RETURN a.name AS a, count(DISTINCT like) AS n",
            [{"a": "a", "n": 3}],
            id="bi6-relationship-like",
        ),
        pytest.param(
            "MATCH (filter:User) WHERE filter.rank > 1 RETURN filter.name AS n ORDER BY n",
            [{"n": "c"}, {"n": "d"}],
            id="node-filter",
        ),
        pytest.param("MATCH (LIKE:User {name: 'b'}) RETURN LIKE.name AS n", [{"n": "b"}], id="upper-case"),
        pytest.param(
            "MATCH (a:User {name: 'a'})-[like:FOLLOWS]->(b:User) RETURN type(like) AS t, b.name AS b ORDER BY b",
            [{"t": "FOLLOWS", "b": "b"}, {"t": "FOLLOWS", "b": "c"}],
            id="type-of-like",
        ),
        pytest.param(
            "MATCH (n:User) WITH n.name AS sort RETURN sort ORDER BY sort DESC LIMIT 2",
            ["d", "c"],
            id="with-alias-sort",
        ),
        pytest.param(
            "MATCH (like:User)-[:FOLLOWS]->(x:User) WITH like, count(x) AS c "
            "MATCH (like)-[:FOLLOWS]->(y:User) RETURN like.name AS n, c ORDER BY n, c",
            [{"n": "a", "c": 2}, {"n": "a", "c": 2}, {"n": "b", "c": 1}, {"n": "c", "c": 1}],
            id="carried-through-with",
        ),
        pytest.param(
            "MATCH (a:User)-[like:FOLLOWS]->(b:User) WITH like, a RETURN a.name AS a, count(like) AS n ORDER BY a",
            [{"a": "a", "n": 2}, {"a": "b", "n": 1}, {"a": "c", "n": 1}],
            id="aggregate-over-like",
        ),
        pytest.param(
            "MATCH (n:User) WHERE n.limit >= 20 RETURN n.limit AS x ORDER BY x",
            [{"x": 20}, {"x": 30}],
            id="property-limit",
        ),
        pytest.param("RETURN {in: 1, for: 2} AS m", [{"m": {"in": 1, "for": 2}}], id="map-keys"),
    ],
)
def test_names_aql_reserves(db: Any, cypher: str, expected: list[Any]) -> None:
    assert _rows(db, cypher) == expected


@pytest.mark.parametrize(
    ("cypher", "expected"),
    [
        pytest.param(
            "MATCH (n:User) RETURN n.rank % 2 AS `a b`, count(*) AS `x y` ORDER BY `x y` DESC, `a b`",
            [{"a b": 0, "x y": 2}, {"a b": 1, "x y": 2}],
            id="grouped-quoted-aliases",
        ),
        pytest.param(
            "MATCH (n:User) RETURN count(n.rank) AS `a b`, sum(n.rank) AS `c d`",
            [{"a b": 4, "c d": 6}],
            id="aggregate-helpers-of-quoted-aliases",
        ),
        pytest.param(
            "MATCH (`a b`:User)-[:FOLLOWS]->(x:User) WITH `a b`, count(x) AS c "
            "MATCH (`a b`)-[:FOLLOWS]->(y:User) RETURN `a b`.name AS n, c ORDER BY n",
            [{"n": "a", "c": 2}, {"n": "a", "c": 2}, {"n": "b", "c": 1}, {"n": "c", "c": 1}],
            id="quoted-variable-carried-through-with",
        ),
    ],
)
def test_names_built_from_a_quoted_name_stay_quoted(db: Any, cypher: str, expected: list[Any]) -> None:
    """``_`a b`_distinct`` and a WITH that dropped ```a b``` from its scope."""
    assert _rows(db, cypher) == expected


@pytest.mark.parametrize(
    ("cypher", "expected"),
    [
        pytest.param(
            "MATCH (_1:User {name: 'a'})-[:FOLLOWS]->(_001:User) RETURN _001.name AS n ORDER BY n",
            ["b", "c"],
            id="underscore-digit-variables-are-renamed",
        ),
        pytest.param(
            "MATCH (ä:User {name: 'a'})-[:FOLLOWS]->(aä:User) RETURN aä.name AS n ORDER BY n",
            ["b", "c"],
            id="non-ascii-variables",
        ),
        pytest.param("RETURN {_001: 1}._001 AS x", [1], id="underscore-digit-property-key"),
        pytest.param(
            "MATCH (:User {name: 'c'})-[r:FOLLOWS]->() RETURN startNode(r).name AS s, endNode(r).name AS e",
            [{"s": "c", "e": "d"}],
            id="start-and-end-node",
        ),
        pytest.param(
            "OPTIONAL MATCH (:User {name: 'd'})-[r:FOLLOWS]->() RETURN startNode(r) AS s",
            [None],
            id="start-node-of-null",
        ),
    ],
)
def test_names_aql_cannot_spell_and_relationship_endpoints(db: Any, cypher: str, expected: list[Any]) -> None:
    """``_001`` / ``ä`` are not AQL variable names even quoted (ERR 1501), and
    ``startNode`` / ``endNode`` were unsupported functions."""
    rows = _rows(db, cypher)
    single = [next(iter(r.values())) if isinstance(r, dict) and len(r) == 1 else r for r in rows]
    assert single == expected
