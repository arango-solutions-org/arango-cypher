"""A ``MULTI_LABEL`` supertype scan on LPG uses the type-led index (WP-44).

``(m:Message {id: $id})`` on LPG tested only the labels array, which only the
labels array index serves, so ArangoDB read every Message to find one (LDBC
IS4–IS7 start this way). The members' type field is now tested beside the
labels array, and the ``(type, id)`` index answers the lookup; the rows are
those the labels array names, as before.
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

_DB = "arango_cypher_supertype_lpg_index_it"


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
        nodes = database.create_collection("nodes")
        # The LDBC LPG layout's node indexes (tests/ldbc/loader.py lpg_indexes).
        nodes.add_index(
            {"type": "persistent", "fields": ["type", "id"], "unique": True, "name": "idx_nodes_type_id"}
        )
        nodes.add_index({"type": "persistent", "fields": ["labels[*]"], "name": "idx_nodes_labels"})
        nodes.import_bulk(
            [{"type": "Post", "labels": ["Message", "Post"], "id": i, "length": i} for i in range(40)]
            + [
                {"type": "Comment", "labels": ["Message", "Comment"], "id": 100 + i, "length": i}
                for i in range(10)
            ]
            # Same ids as messages, other types: a lookup must not return them.
            + [{"type": "Person", "id": i} for i in range(5)]
            + [{"type": "Forum", "id": 100 + i} for i in range(5)]
        )
        yield database
    finally:
        if sys_db.has_database(_DB):
            sys_db.delete_database(_DB)


def _t(cypher: str, **params: Any):
    return translate(cypher, mapping=mapping_bundle_for("ldbc_lpg"), params=params or None)


def _indexes(db: Any, aql: str, bind_vars: dict[str, Any]) -> list[str]:
    nodes = db.aql.explain(aql, bind_vars=bind_vars)["nodes"]
    return [idx["name"] for n in nodes if n["type"] == "IndexNode" for idx in n["indexes"]]


@pytest.mark.parametrize(("message_id", "length"), [(3, 3), (104, 4)])
def test_a_supertype_lookup_by_id_uses_the_type_id_index(db: Any, message_id: int, length: int) -> None:
    q = _t("MATCH (m:Message {id: $id}) RETURN m.id AS id, m.length AS length", id=message_id)
    assert list(db.aql.execute(q.aql, bind_vars=q.bind_vars)) == [{"id": message_id, "length": length}]
    assert _indexes(db, q.aql, q.bind_vars) == ["idx_nodes_type_id"], q.aql


def test_a_supertype_scan_returns_every_member_and_nothing_else(db: Any) -> None:
    q = _t("MATCH (m:Message) RETURN m.id AS id")
    ids = sorted(row["id"] for row in db.aql.execute(q.aql, bind_vars=q.bind_vars))
    assert ids == list(range(40)) + list(range(100, 110))


def test_a_node_of_another_type_is_not_a_message(db: Any) -> None:
    # The Person and Forum nodes have no labels array. Alone, the labels test
    # gave NOT n:Message = false for them: ArangoDB rewrites NOT (x IN a) to
    # x NOT IN a, which is false when a is not an array. Their type decides now.
    q = _t("MATCH (n) WHERE n.id IN [3, 104] RETURN n.id AS id, NOT n:Message AS other ORDER BY id, other")
    rows = list(db.aql.execute(q.aql, bind_vars=q.bind_vars))
    assert rows == [
        {"id": 3, "other": False},
        {"id": 3, "other": True},
        {"id": 104, "other": False},
        {"id": 104, "other": True},
    ], q.aql
