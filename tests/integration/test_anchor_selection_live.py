"""A first MATCH starts at the end an index finds (``pattern_reversal``, WP-44).

``(p:Person)<-[:HAS_CREATOR]-(m:Message)-[:HAS_TAG]->(t:Tag {name: $name})``
scanned every Person, walked to all their messages and kept those tagged
``$name`` (LDBC BI16). It now starts at the Tag, which the ``name`` index finds,
and walks outwards; the rows are the same.
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

_DB = "arango_cypher_anchor_selection_it"
_CYPHER = (
    "MATCH (p:Person)<-[:HAS_CREATOR]-(m:Message)-[:HAS_TAG]->(t:Tag {name: $name}) "
    "RETURN p.id AS person, m.id AS message ORDER BY person, message"
)


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
        # Every collection the LDBC PG mapping names, so the query's WITH resolves.
        q = translate(_CYPHER, mapping=mapping_bundle_for("ldbc_pg"), params={"name": "x"})
        for key, name in q.bind_vars.items():
            if key.startswith("@") and not database.has_collection(name):
                database.create_collection(name, edge=key.startswith("@edgeCollection"))
        database.collection("tag").add_index(
            {"type": "persistent", "fields": ["name"], "name": "idx_tag_name"}
        )
        database.collection("person").import_bulk([{"_key": f"p{i}", "id": i} for i in range(4)])
        database.collection("tag").import_bulk(
            [{"_key": "jazz", "id": 1, "name": "Jazz"}, {"_key": "rock", "id": 2, "name": "Rock"}]
        )
        database.collection("post").import_bulk([{"_key": f"m{i}", "id": 10 + i} for i in range(6)])
        database.collection("comment").import_bulk([{"_key": "c0", "id": 20}])
        database.collection("has_creator").import_bulk(
            [{"_from": f"post/m{i}", "_to": f"person/p{i % 4}"} for i in range(6)]
            + [{"_from": "comment/c0", "_to": "person/p3"}]
        )
        database.collection("has_tag").import_bulk(
            [{"_from": f"post/m{i}", "_to": "tag/jazz" if i % 2 == 0 else "tag/rock"} for i in range(6)]
            + [{"_from": "comment/c0", "_to": "tag/jazz"}]
        )
        yield database
    finally:
        if sys_db.has_database(_DB):
            sys_db.delete_database(_DB)


def test_the_rows_are_those_of_the_pattern_as_written(db: Any) -> None:
    q = translate(_CYPHER, mapping=mapping_bundle_for("ldbc_pg"), params={"name": "Jazz"})
    rows = list(db.aql.execute(q.aql, bind_vars=q.bind_vars))
    # Posts m0, m2, m4 (persons 0, 2, 0) and comment c0 (person 3) carry Jazz.
    assert rows == [
        {"person": 0, "message": 10},
        {"person": 0, "message": 14},
        {"person": 2, "message": 12},
        {"person": 3, "message": 20},
    ], q.aql


def test_the_plan_starts_at_the_tag_name_index(db: Any) -> None:
    q = translate(_CYPHER, mapping=mapping_bundle_for("ldbc_pg"), params={"name": "Jazz"})
    nodes = db.aql.explain(q.aql, bind_vars=q.bind_vars)["nodes"]
    first = next(n for n in nodes if n["type"] in ("IndexNode", "EnumerateCollectionNode"))
    assert first["type"] == "IndexNode", q.aql
    assert first["collection"] == "tag"
    assert [i["name"] for i in first["indexes"]] == ["idx_tag_name"]
