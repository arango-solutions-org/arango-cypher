"""Temporal values held as strings, read at run time on ArangoDB
(``TranslateOptions.temporal_strings``, ``_translate_v0/temporal_strings.py``).

The oracle is ``arango_cypher.temporal``, the Python implementation of
Neo4j's java.time semantics (itself checked against the TCK's tables): every
AQL snippet must give the value the library computes, at nanosecond
precision.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import pytest
from arango import ArangoClient

from arango_cypher import temporal as T
from arango_cypher import translate
from arango_cypher._translate_v0 import temporal_strings as ts
from tests.helpers.mapping_fixtures import mapping_bundle_for

pytestmark = pytest.mark.integration

_DB = "arango_cypher_temporal_strings_it"

VALUES = {
    "date": ["1984-10-11", "1984-01-01", "2016-02-29", "2015-12-31"],
    "localtime": ["12:31:14.645876123", "00:00", "23:59:59.5"],
    "time": ["12:31:14.645876123+01:00", "10:35-08:00", "00:00Z", "12:34:56-02:05:07"],
    "localdatetime": ["1984-11-11T12:31:14.645876123", "2015-01-31T23:00"],
    "datetime": [
        "1984-10-11T12:30:14.000000012+00:15",
        "1970-01-01T00:00Z",
        "1969-12-31T23:59:59.999999999Z",
        "1984-11-11T12:31:14.645876123+01:00[Europe/Stockholm]",
    ],
    "duration": [
        "P1Y4M10DT1H1M1.111111111S",
        "PT-23H-59M-59.9S",
        "PT-0.001S",
        "P-1Y-5M-10DT-25H",
        "PT0S",
        "P1M-14DT15H49M10S",
        "PT36H",
    ],
}


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


def _one(db: Any, aql: str, bind_vars: dict[str, Any]) -> Any:
    return next(iter(db.aql.execute(aql, bind_vars=bind_vars)))


def _expected(compute: Any) -> Any:
    try:
        return compute()
    except T.TemporalError:
        return None


@pytest.mark.parametrize("kind", sorted(VALUES))
def test_accessors_match_the_library(db: Any, kind: str) -> None:
    for text in VALUES[kind]:
        value = T.construct(kind, text)
        names = sorted(ts.ACCESSORS)
        aql = "RETURN [" + ", ".join(ts.accessor("@v", name) for name in names) + "]"
        got = _one(db, aql, {"v": text})
        expected = [_expected(lambda name=name, value=value: T.get_property(value, name)) for name in names]
        assert dict(zip(names, got, strict=True)) == dict(zip(names, expected, strict=True)), text


def test_non_temporal_values_keep_attribute_access(db: Any) -> None:
    aql = f"RETURN [{ts.accessor('@m', 'year')}, {ts.accessor('@s', 'year')}, {ts.accessor('@n', 'year')}]"
    assert _one(db, aql, {"m": {"year": 7}, "s": "not a date", "n": None}) == [7, None, None]


def _fixed_offset(value: T.Temporal) -> bool:
    """Arithmetic keeps a named zone's stored offset: compare fixed offsets only."""
    return not (isinstance(value, T.DateTime) and value.zone is not None)


def test_addition_and_subtraction_match_the_library(db: Any) -> None:
    durations = [T.construct("duration", d) for d in VALUES["duration"]]
    temporals = [
        T.construct(kind, text) for kind in sorted(VALUES) if kind != "duration" for text in VALUES[kind]
    ]
    cases: list[tuple[str, str, str, str]] = []
    for op in ("+", "-"):
        for a in [*temporals, *durations]:
            for b in durations:
                if _fixed_offset(a):
                    cases.append((str(a), op, str(b), str(T.add(a, b) if op == "+" else T.subtract(a, b))))
    for d in durations:
        for t in temporals:
            if _fixed_offset(t):
                cases.append((str(d), "+", str(t), str(T.add(d, t))))
    for op in ("+", "-"):
        selected = [(a, b, expected) for a, o, b, expected in cases if o == op]
        aql = f"FOR c IN @cases RETURN {ts.additive('c[0]', op, 'c[1]', 'null')}"
        got = list(db.aql.execute(aql, bind_vars={"cases": [[a, b] for a, b, _ in selected]}))
        mismatches = [(a, op, b, g, e) for (a, b, e), g in zip(selected, got, strict=True) if g != e]
        assert not mismatches, mismatches[:5]


def test_scaling_matches_the_library(db: Any) -> None:
    durations = [T.construct("duration", d) for d in VALUES["duration"]]
    for op in ("*", "/"):
        cases = [
            (str(d), f, str(T.multiply(d, f) if op == "*" else T.divide(d, f)))
            for d in durations
            for f in (1, 2, 0.5, -1, 0.3)
        ]
        aql = f"FOR c IN @cases RETURN {ts.multiplicative('c[0]', op, 'c[1]', 'null')}"
        got = list(db.aql.execute(aql, bind_vars={"cases": [[d, f] for d, f, _ in cases]}))
        assert got == [e for _, _, e in cases]


def test_ordinary_operands_fall_back(db: Any) -> None:
    aql = (
        f"RETURN [{ts.additive('@a', '+', '@b', 'CONCAT(@a, @b)')}, "
        f"{ts.multiplicative('@n', '*', '@m', '@n * @m')}, {ts.multiplicative('@d', '/', '@z', '@n')}]"
    )
    assert _one(db, aql, {"a": "x", "b": "y", "n": 3, "m": 4, "d": "P1D", "z": 0}) == ["xy", 12, 3]


def test_times_and_datetimes_order_by_instant(db: Any) -> None:
    times = [
        "10:35-08:00",
        "12:31:14.645876123+01:00",
        "12:31:14.645876124+01:00",
        "12:35:15+05:00",
        "12:30:14.645876123+01:01",
    ]
    datetimes = [
        "1984-10-11T12:30:14.000000012+00:15",
        "1984-10-11T12:31:14.645876123+00:17",
        "0001-01-01T01:01:01.000000001-11:59",
        "9999-09-09T09:59:59.999999999+11:59",
        "1980-12-11T12:31:14-11:59",
    ]
    for values, kind in ((times, "time"), (datetimes, "datetime")):
        parsed = [T.construct(kind, v) for v in values]
        expected = [str(v) for v in sorted(parsed, key=lambda v: _order(v))]
        aql = f"FOR v IN @values SORT {ts.sort_key('v', 'v')} RETURN v"
        assert list(db.aql.execute(aql, bind_vars={"values": values})) == expected
        pairs = [[a, b] for a in values for b in values]
        aql = f"FOR p IN @pairs RETURN {ts.ordering('p[0]', '<', 'p[1]', 'null')}"
        got = list(db.aql.execute(aql, bind_vars={"pairs": pairs}))
        assert got == [T.compare(T.construct(kind, a), T.construct(kind, b)) == -1 for a, b in pairs]


def _order(value: Any) -> Any:
    import functools

    return functools.cmp_to_key(lambda a, b: T.compare(a, b) or 0)(value)


def test_translated_queries_read_stored_temporals(db: Any) -> None:
    """End to end: values stored as strings, read back by translated Cypher."""
    mapping = mapping_bundle_for("lpg")
    col = db.create_collection("vertices") if not db.has_collection("vertices") else db.collection("vertices")
    col.truncate()
    col.insert_many(
        [
            {
                "type": "User",
                "name": "a",
                "date": "1984-10-11",
                "at": "10:35-08:00",
                "span": "P1M-14DT15H49M10S",
            },
            {"type": "User", "name": "b", "date": "1984-01-01", "at": "12:35:15+05:00", "span": "PT36H"},
        ]
    )
    query = (
        "MATCH (v:User) WITH v, v.date AS d, v.at AS t ORDER BY t "
        "RETURN v.name AS name, d.weekYear AS wy, d.week AS w, v.span * 2 AS twice, "
        "date(d) + v.span AS later"
    )
    out = translate(query, mapping=mapping, temporal_strings=True)
    bind = dict(out.bind_vars)
    if "@collection" in bind:
        bind["@collection"] = "vertices"
    rows = list(db.aql.execute(out.aql, bind_vars=bind))
    # 12:35:15+05:00 is 07:35 UTC, before 10:35-08:00 (18:35 UTC)
    assert [r["name"] for r in rows] == ["b", "a"]
    assert rows[0] == {"name": "b", "wy": 1983, "w": 52, "twice": "PT72H", "later": "1984-01-02"}
    assert rows[1] == {"name": "a", "wy": 1984, "w": 41, "twice": "P2M-28DT31H38M20S", "later": "1984-10-28"}
