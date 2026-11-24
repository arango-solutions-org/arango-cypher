"""EXISTS { } and COUNT { } on ArangoDB: the body means what the same MATCH means.

Every query here returned a wrong answer or failed before the body went
through the shared MATCH compiler. Each expected value is what Cypher returns
for this graph, on the PG mapping (a collection per label, ``follows`` edges
reaching ``users`` and ``docs``) and the LPG one (one vertex and one edge
collection with type fields). The unit counterpart is
``tests/test_translate_exists_count_subqueries.py``.
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

_DB = "arango_cypher_exists_count_subqueries_it"

#: a → b (w 1), b → c (w 2), a → d1 (w 1, a Doc), c → a (w 2); LPG also has b -LIKES-> d1.
_FOLLOWS = [("a", "b", 1), ("b", "c", 2), ("a", "d1", 1), ("c", "a", 2)]


def _pg_id(key: str) -> str:
    return f"docs/{key}" if key.startswith("d") else f"users/{key}"


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
            + [{"_from": "vertices/b", "_to": "vertices/d1", "w": 1, "type": "LIKES"}]
        )
        yield database
    finally:
        if sys_db.has_database(_DB):
            sys_db.delete_database(_DB)


def _rows(db: Any, cypher: str, mapping: str) -> list[Any]:
    q = translate(cypher, mapping=mapping_bundle_for(mapping))
    return list(db.aql.execute(q.aql, bind_vars=q.bind_vars))


def _names(db: Any, where: str, mapping: str) -> list[str]:
    rows = _rows(db, f"MATCH (u:User) WHERE {where} RETURN u.name AS n ORDER BY n", mapping)
    return [r["n"] for r in rows]


_MAPPINGS = pytest.mark.parametrize("mapping", ["pg", "lpg"])


@_MAPPINGS
@pytest.mark.parametrize(
    ("where", "expected"),
    [
        ("EXISTS { (u)-[:FOLLOWS]->(:Doc) }", ["a"]),
        ("NOT EXISTS { (u)-[:FOLLOWS]->(:Doc) }", ["b", "c"]),
        ("EXISTS { (u)-[:FOLLOWS]-(:Doc) }", ["a"]),
        ("EXISTS { (u)-[:FOLLOWS]->(:User {name: 'c'}) }", ["b"]),
        ("EXISTS { (u)-[:FOLLOWS]->(m {name: 'c'}) }", ["b"]),
        ("EXISTS { (u)-[:FOLLOWS {w: 2}]->() }", ["b", "c"]),
        ("EXISTS { (u)<-[:FOLLOWS {w: 1}]-() }", ["b"]),
        ("EXISTS { (u {name: 'a'})-[:FOLLOWS]->() }", ["a"]),
        ("EXISTS { (x:User {name: 'b'})-[:FOLLOWS]->(u) }", ["c"]),
        ("EXISTS { (u)-[:FOLLOWS*2 {w: 1}]->() }", []),
        ("EXISTS { MATCH (u)-[:FOLLOWS]->(x) MATCH (x)-[:FOLLOWS]->(:Doc) }", ["c"]),
        ("EXISTS { (u)-[:FOLLOWS]->(:User), (u)-[:FOLLOWS]->(:Doc) }", ["a"]),
        ("EXISTS { (u {name: 'b'}) }", ["b"]),
        ("EXISTS { MATCH (u)-[:FOLLOWS]->(x) WHERE EXISTS { (x)-[:FOLLOWS]->(:Doc) } }", ["c"]),
    ],
)
def test_exists_tests_the_whole_pattern(db: Any, mapping: str, where: str, expected: list[str]) -> None:
    assert _names(db, where, mapping) == expected


@_MAPPINGS
@pytest.mark.parametrize(
    ("subquery", "expected"),
    [
        ("COUNT { (u)-[:FOLLOWS]->(:User) }", {"a": 1, "b": 1, "c": 1}),
        ("COUNT { (u)-[:FOLLOWS*1..2]->(:Doc) }", {"a": 1, "b": 0, "c": 1}),
        ("COUNT { (u)-[:FOLLOWS]->(x)-[:FOLLOWS]->() }", {"a": 1, "b": 1, "c": 2}),
    ],
)
def test_count_counts_the_whole_pattern(
    db: Any, mapping: str, subquery: str, expected: dict[str, int]
) -> None:
    rows = _rows(db, f"MATCH (u:User) RETURN u.name AS n, {subquery} AS k", mapping)
    assert {r["n"]: r["k"] for r in rows} == expected


@_MAPPINGS
def test_a_bound_target_with_a_label_is_correlated(db: Any, mapping: str) -> None:
    cypher = "MATCH (u:User), (v:User) WHERE EXISTS { (u)-[:FOLLOWS]->(v:User) } RETURN count(*) AS k"
    assert _rows(db, cypher, mapping) == [3]


@_MAPPINGS
@pytest.mark.parametrize(
    "cypher",
    [
        "MATCH (p:User)-[:FOLLOWS]->(m) WHERE EXISTS { (p)-[:FOLLOWS]->(m) } RETURN count(*) AS k",
        "MATCH (p:User)-[:FOLLOWS]->() WHERE EXISTS { (p)-[:FOLLOWS]->()-[:FOLLOWS]->() } RETURN count(*) AS k",
    ],
)
def test_unnamed_variables_inside_and_outside_do_not_clash(db: Any, mapping: str, cypher: str) -> None:
    assert _rows(db, cypher, mapping) == [4]


@_MAPPINGS
@pytest.mark.parametrize(
    ("body", "expected"),
    [
        # Grouped by the outer u: no group, so no row, when u follows no Doc.
        ("MATCH (u)-[:FOLLOWS]->(:Doc) WITH u, count(*) AS c RETURN true", ["a"]),
        # No grouping key: one row (a count of 0) even when nothing matched.
        ("MATCH (u)-[:FOLLOWS]->(:Doc) WITH count(*) AS c RETURN c", ["a", "b", "c"]),
        ("MATCH (u)-[:FOLLOWS]->(m:Doc) WITH u, count(*) AS c WHERE c > 0 RETURN true", ["a"]),
        ("MATCH (u)-[:FOLLOWS]->()-[:FOLLOWS]->(m) WITH u, count(*) AS c WHERE c > 1 RETURN true", ["c"]),
    ],
)
def test_a_counting_subquery_groups_as_cypher_does(
    db: Any, mapping: str, body: str, expected: list[str]
) -> None:
    assert _names(db, f"EXISTS {{ {body} }}", mapping) == expected
