"""An undirected hop matches edges in both directions whatever the statistics say.

The translator used to turn ``-[:HAS_CREATOR]-`` into ``OUTBOUND`` or
``INBOUND`` when the relationship's average fan-out and fan-in differed by more
than five times (``MappingResolver.preferred_traversal_direction``). An
undirected pattern matches either orientation, so a person's messages, reached
from the person over ``-[:HAS_CREATOR]-``, came back empty once the mapping
carried statistics. Here the statistics are computed from this database as a
service computes them, and every count must match the count without them.
"""

from __future__ import annotations

import dataclasses
import os
from collections.abc import Iterator
from typing import Any

import pytest
from arango import ArangoClient
from arango_query_core import MappingBundle, MappingResolver

from arango_cypher import translate
from arango_cypher.schema_acquire import compute_statistics
from tests.helpers.mapping_fixtures import mapping_bundle_for

pytestmark = pytest.mark.integration

_DB = "arango_cypher_undirected_hop_statistics_it"
_POSTS = 6
_COMMENTS = 6

#: (Cypher, expected count). Person 1 created every message; person 2 none.
_CASES = [
    ("MATCH (p:Person {id: 1})-[:HAS_CREATOR]-(m) RETURN count(m) AS n", _POSTS + _COMMENTS),
    ("MATCH (m:Post)-[:HAS_CREATOR]-(p:Person {id: 1}) RETURN count(m) AS n", _POSTS),
    ("MATCH (p:Person {id: 1})-[:HAS_CREATOR]-(m) WITH m RETURN count(m) AS n", _POSTS + _COMMENTS),
    ("MATCH (m:Comment)-[:HAS_CREATOR]-(p:Person) WITH p RETURN count(p) AS n", _COMMENTS),
    ("MATCH (p:Person {id: 2})-[:HAS_CREATOR]-(m) RETURN count(m) AS n", 0),
]


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
        # An unlabeled endpoint declares every vertex collection (``WITH``),
        # so the mapping's other collections exist, empty.
        mapping = mapping_bundle_for("ldbc_pg").physical_mapping
        for ent in mapping["entities"].values():
            name = ent.get("collectionName")
            if name and name not in {"person", "post", "comment"} and not database.has_collection(name):
                database.create_collection(name)
        for rel in mapping["relationships"].values():
            name = rel.get("edgeCollectionName")
            if name and name != "has_creator" and not database.has_collection(name):
                database.create_collection(name, edge=True)
        database.create_collection("person").import_bulk([{"_key": "p1", "id": 1}, {"_key": "p2", "id": 2}])
        database.create_collection("post").import_bulk(
            [{"_key": f"m{i}", "id": 100 + i} for i in range(_POSTS)]
        )
        database.create_collection("comment").import_bulk(
            [{"_key": f"c{i}", "id": 200 + i} for i in range(_COMMENTS)]
        )
        database.create_collection("has_creator", edge=True).import_bulk(
            [{"_from": f"post/m{i}", "_to": "person/p1"} for i in range(_POSTS)]
            + [{"_from": f"comment/c{i}", "_to": "person/p1"} for i in range(_COMMENTS)]
        )
        yield database
    finally:
        if sys_db.has_database(_DB):
            sys_db.delete_database(_DB)


def _with_statistics(db: Any) -> MappingBundle:
    bundle = mapping_bundle_for("ldbc_pg")
    return dataclasses.replace(
        bundle, metadata={**bundle.metadata, "statistics": compute_statistics(db, bundle)}
    )


def _count(db: Any, cypher: str, mapping: MappingBundle) -> int:
    q = translate(cypher, mapping=mapping)
    # A single RETURN column comes back as bare values.
    (n,) = db.aql.execute(q.aql, bind_vars=q.bind_vars)
    return int(n)


def test_the_statistics_are_lopsided_enough_to_have_narrowed_the_hop(db: Any) -> None:
    # Every message has one creator and the one creator has twelve messages:
    # past the five-to-one ratio at which the old code picked a direction.
    assert MappingResolver(_with_statistics(db)).preferred_traversal_direction("HAS_CREATOR") == "OUTBOUND"


@pytest.mark.parametrize(("cypher", "expected"), _CASES)
def test_counts_match_with_and_without_statistics(db: Any, cypher: str, expected: int) -> None:
    assert _count(db, cypher, mapping_bundle_for("ldbc_pg")) == expected
    assert _count(db, cypher, _with_statistics(db)) == expected
