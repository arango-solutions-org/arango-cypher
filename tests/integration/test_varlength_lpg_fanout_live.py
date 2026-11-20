"""With statistics, an LPG KNOWS hop reads only KNOWS edges (WP-44).

The type guard's PRUNE keeps ArangoDB from using a vertex-centric
``(_from, type)`` index, so a ``KNOWS*1..2`` traversal read every edge of every
person it reached. Here every person also likes 30 posts. The mapping's
statistics, computed from this database as a service computes them, show the
other edges outnumbering KNOWS, so the hop tests its path and the index hands
over only KNOWS edges. Without statistics the guard is unchanged.
"""

from __future__ import annotations

import dataclasses
import os
from collections.abc import Iterator
from typing import Any

import pytest
from arango import ArangoClient
from arango_query_core import MappingBundle

from arango_cypher import translate
from arango_cypher.schema_acquire import compute_statistics
from tests.helpers.mapping_fixtures import mapping_bundle_for

pytestmark = pytest.mark.integration

_DB = "arango_cypher_varlength_lpg_fanout_it"
_PERSONS = ["a", "b", "c", "d"]
_KNOWS = [("a", "b"), ("b", "c"), ("c", "d")]
_LIKES_PER_PERSON = 30
_QUERY = "MATCH (a:Person {id: 'a'})-[:KNOWS*1..2]-(f:Person) RETURN f.id AS f ORDER BY f"


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
        edges = database.create_collection("edges", edge=True)
        # The LDBC LPG layout's edge VCIs (tests/ldbc/loader.py lpg_indexes).
        edges.add_index({"type": "persistent", "fields": ["_from", "type"], "name": "idx_edges_from_type"})
        edges.add_index({"type": "persistent", "fields": ["_to", "type"], "name": "idx_edges_to_type"})
        posts = [f"m{i}" for i in range(_LIKES_PER_PERSON)]
        nodes.import_bulk(
            [{"_key": k, "type": "Person", "id": k} for k in _PERSONS]
            + [{"_key": k, "type": "Post", "labels": ["Message", "Post"], "id": k} for k in posts]
        )
        edges.import_bulk(
            [{"_from": f"nodes/{x}", "_to": f"nodes/{y}", "type": "KNOWS"} for x, y in _KNOWS]
            + [{"_from": f"nodes/{p}", "_to": f"nodes/{m}", "type": "LIKES"} for p in _PERSONS for m in posts]
        )
        yield database
    finally:
        if sys_db.has_database(_DB):
            sys_db.delete_database(_DB)


def _with_statistics(db: Any) -> MappingBundle:
    bundle = mapping_bundle_for("ldbc_lpg")
    return dataclasses.replace(
        bundle, metadata={**bundle.metadata, "statistics": compute_statistics(db, bundle)}
    )


def _run(db: Any, mapping: MappingBundle) -> tuple[list[Any], int, str]:
    q = translate(_QUERY, mapping=mapping)
    cursor = db.aql.execute(q.aql, bind_vars=q.bind_vars)
    return list(cursor), cursor.statistics()["scanned_index"], q.aql


def test_with_statistics_the_hop_reads_no_likes_edges(db: Any) -> None:
    rows, scanned, aql = _run(db, _with_statistics(db))
    assert rows == [{"f": "b"}, {"f": "c"}]
    assert "ALL ==" in aql, aql
    # a, b and c each have 30 LIKES edges; reading any of them would pass 30.
    assert scanned < _LIKES_PER_PERSON, (scanned, aql)


def test_without_statistics_the_rows_are_the_same(db: Any) -> None:
    rows, _, aql = _run(db, mapping_bundle_for("ldbc_lpg"))
    assert rows == [{"f": "b"}, {"f": "c"}]
    assert "ALL ==" not in aql, aql
