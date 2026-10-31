"""Table-backed procedures on ArangoDB (``arango_cypher.procedures``): the
translated ``CALL`` returns exactly the rows Neo4j would."""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import pytest
from arango import ArangoClient
from arango_query_core import ExtensionPolicy, ExtensionRegistry

from arango_cypher import translate
from arango_cypher.procedures import register_table_procedure
from tests.helpers.mapping_fixtures import mapping_bundle_for

pytestmark = pytest.mark.integration

_DB = "arango_cypher_procedures_it"
MAPPING = mapping_bundle_for("lpg")


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
        yield client.db(_DB, username=user, password=password)
    finally:
        sys_db.delete_database(_DB)


@pytest.fixture()
def registry() -> ExtensionRegistry:
    r = ExtensionRegistry(policy=ExtensionPolicy(enabled=True))
    register_table_procedure(
        r,
        "test.my.proc(name :: STRING?, id :: INTEGER?) :: (city :: STRING?, country_code :: INTEGER?)",
        [
            {"name": "Stefan", "id": 1, "city": "Berlin", "country_code": 49},
            {"name": "Stefan", "id": 2, "city": "München", "country_code": 49},
            {"name": "Petra", "id": 1, "city": "London", "country_code": 44},
            {"name": None, "id": None, "city": "Nowhere", "country_code": 0},
        ],
    )
    register_table_procedure(
        r,
        "test.num(in :: NUMBER?) :: (out :: STRING?)",
        [{"in": 42, "out": "wisdom"}, {"in": 42.3, "out": "about right"}],
    )
    register_table_procedure(
        r, "test.labels() :: (label :: STRING?)", [{"label": "A"}, {"label": "B"}, {"label": "C"}]
    )
    register_table_procedure(r, "test.doNothing() :: ()", [])
    return r


def _rows(db: Any, query: str, registry: ExtensionRegistry, **kw: Any) -> list[Any]:
    out = translate(query, mapping=MAPPING, registry=registry, cypher_column_names=True, **kw)
    return list(db.aql.execute(out.aql, bind_vars=out.bind_vars))


def test_standalone_calls(db: Any, registry: ExtensionRegistry) -> None:
    assert _rows(db, "CALL test.my.proc('Stefan', 1)", registry) == [{"city": "Berlin", "country_code": 49}]
    assert _rows(db, "CALL test.my.proc(null, null) YIELD city", registry) == [{"city": "Nowhere"}]
    assert _rows(db, "CALL test.num(42.3)", registry) == [{"out": "about right"}]
    assert _rows(db, "CALL test.num(42)", registry) == [{"out": "wisdom"}]
    assert _rows(db, "CALL test.labels()", registry) == [{"label": "A"}, {"label": "B"}, {"label": "C"}]
    assert _rows(db, "CALL test.doNothing()", registry) == []


def test_implicit_arguments(db: Any, registry: ExtensionRegistry) -> None:
    rows = _rows(db, "CALL test.my.proc", registry, params={"name": "Stefan", "id": 2})
    assert rows == [{"city": "München", "country_code": 49}]


def test_in_query_calls(db: Any, registry: ExtensionRegistry) -> None:
    rows = _rows(
        db,
        "CALL test.my.proc('Stefan', 1) YIELD city AS town, country_code RETURN town, country_code",
        registry,
    )
    assert rows == [{"town": "Berlin", "country_code": 49}]
    rows = _rows(db, "CALL test.labels() YIELD label WHERE label <> 'B' RETURN label", registry)
    assert rows == ["A", "C"]  # a single RETURN column is returned bare
    rows = _rows(
        db,
        "CALL test.labels() YIELD label WITH count(*) AS c CALL test.labels() YIELD label RETURN c, label",
        registry,
    )
    assert rows == [{"c": 3, "label": "A"}, {"c": 3, "label": "B"}, {"c": 3, "label": "C"}]
