"""String escapes on ArangoDB, folded at translate time and left to AQL.

A literal in an expression with constants only is evaluated at translate
time (``const_eval``) and bound as a value; otherwise it reaches AQL as text.
Both read the same literal the same way, and refuse the same ones: the folded
path used to crash on ``\\UFFFFFFFF`` (OverflowError) and send ArangoDB an
unpaired surrogate (ERR 600), and the AQL path read ``\\U00e9`` as ``U00e9``.
The unit counterpart is ``tests/test_translate_string_literals.py``.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import pytest
from arango import ArangoClient
from arango_query_core import CoreError

from arango_cypher import translate
from arango_cypher._translate_v0.const_eval import constant_folding
from arango_cypher.api import clear_translate_cache
from tests.helpers.mapping_fixtures import mapping_bundle_for

pytestmark = pytest.mark.integration

B = "\\"


@pytest.fixture(scope="module")
def db() -> Any:
    if os.environ.get("RUN_INTEGRATION") != "1":
        pytest.skip("RUN_INTEGRATION=1 required")
    client = ArangoClient(hosts=os.environ.get("ARANGO_URL", "http://localhost:8529"))
    return client.db(
        "_system",
        username=os.environ.get("ARANGO_USER", "root"),
        password=os.environ.get("ARANGO_PASS", "openSesame"),
    )


@pytest.fixture(params=[True, False], ids=["folded", "aql"])
def folding(request: pytest.FixtureRequest) -> Iterator[bool]:
    clear_translate_cache()
    with constant_folding(request.param):
        yield request.param
    clear_translate_cache()


def _code_points(db: Any, literal: str) -> list[int]:
    q = translate(f"RETURN {literal} + '' AS s", mapping=mapping_bundle_for("pg"))
    (row,) = list(db.aql.execute(q.aql, bind_vars=q.bind_vars))
    return [ord(c) for c in row["s"]]


@pytest.mark.parametrize(
    ("literal", "expected"),
    [
        (f"'{B}u00e9'", [0xE9]),
        (f"'{B}U00e9x'", [0xE9, ord("x")]),
        (f"'{B}U000000e9'", [0xE9]),
        (f"'{B}u00412'", [0x41, ord("2")]),
        (f"'{B}U0001F600'", [0x1F600]),
        (f"'{B}uD83D{B}uDE00'", [0x1F600]),
        (f"'{B}N{B}T'", [10, 9]),
    ],
)
def test_a_literal_reads_the_same_both_ways(
    db: Any, folding: bool, literal: str, expected: list[int]
) -> None:
    assert _code_points(db, literal) == expected


@pytest.mark.parametrize(
    "literal",
    [f"'{B}UFFFFFFFF'", f"'{B}U00110000'", f"'{B}uD800'", f"'{B}U0000DC00'"],
)
def test_what_is_not_a_character_is_refused_both_ways(db: Any, folding: bool, literal: str) -> None:
    with pytest.raises(CoreError) as exc:
        _code_points(db, literal)
    assert exc.value.code == "UNSUPPORTED"
