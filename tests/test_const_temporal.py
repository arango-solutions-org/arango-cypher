"""Temporal values in compile-time evaluation (``const_temporal``): constant
temporal expressions are folded with ``arango_cypher.temporal`` and emitted as
their Neo4j string form; anything that is not constant still compiles through
the runtime lowering in ``_translate_v0/temporal.py``."""

from __future__ import annotations

import pytest

from arango_cypher import temporal as T
from arango_cypher import translate
from arango_cypher._translate_v0 import cypher_functions as fns
from arango_cypher._translate_v0 import cypher_values as cv
from arango_cypher._translate_v0.const_eval import Evaluator
from arango_cypher._translate_v0.const_temporal import install, to_eval_error
from arango_cypher._translate_v0.cypher_values import CypherEvalError, NotConstant
from arango_cypher.parser import parse_cypher
from tests.helpers.mapping_fixtures import mapping_bundle_for

MAPPING = mapping_bundle_for("lpg")


def _eval(expr: str, env: dict | None = None):
    tree = parse_cypher(f"RETURN {expr} AS v").tree  # type: ignore[attr-defined]
    stack = [tree]
    while stack:
        node = stack.pop()
        if type(node).__name__ == "OC_ExpressionContext":
            return Evaluator(env or {}).eval(node)
        stack.extend(reversed(getattr(node, "children", None) or []))
    raise AssertionError("no expression")


def _returned(cypher: str) -> object:
    """The folded bind value a one-column ``RETURN {col: @cvN}`` returns."""
    out = translate(cypher, mapping=MAPPING)
    returned = out.aql.rsplit("RETURN", 1)[1]
    names = [k for k in out.bind_vars if k.startswith("cv") and f"@{k}" in returned]
    assert len(names) == 1, (out.aql, out.bind_vars)
    return out.bind_vars[names[0]]


# --- evaluation -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("expr", "expected"),
    [
        ("toString(date({year: 1816, week: 1}))", "1816-01-01"),
        (
            "toString(datetime('2015-07-21T21:40:32.142[Europe/London]'))",
            "2015-07-21T21:40:32.142+01:00[Europe/London]",
        ),
        (
            "toString(localdatetime.truncate('millennium', localdatetime('2017-10-11T12:31'), {day: 2}))",
            "2000-01-02T00:00",
        ),
        (
            "toString(duration.inMonths(date('1984-10-11'), datetime('2015-07-21T21:40:32.142+0100')))",
            "P30Y9M",
        ),
        ("toString(datetime.fromepoch(416779, 999999999))", "1970-01-05T19:46:19.999999999Z"),
        ("toString(datetime.fromepochmillis(237821673987))", "1977-07-15T13:34:33.987Z"),
        ("toString(date('2015-01-31') + duration('P1M'))", "2015-02-28"),
        ("toString(duration('P1D') * 2)", "P2D"),
        ("toString(2 * duration('P1D'))", "P2D"),
        ("toString(duration('P1D') / 2)", "PT12H"),
        ("toString(-duration('P1D'))", "P-1D"),
        ("toString(duration('P2D') - duration('P1D'))", "P1D"),
        ("date('2015-01-01') < date('2015-01-02')", True),
        ("date('2015-01-01') = date('2015-01-01')", True),
        ("date('2015-01-01') = localdatetime('2015-01-01T00:00')", False),
        ("date('2015-01-01') < localdatetime('2015-01-01T00:00')", None),
        ("date('2015-01-01') = '2015-01-01'", False),
        ("date('2015-01-01') IN [1, date('2015-01-01')]", True),
        ("duration('PT70S') = duration('PT1M10S')", True),
        ("date('1984-10-11').week", 41),
        ("datetime('2015-07-21T21:40:32.142+01:00').offsetMinutes", 60),
        ("duration('P1Y4M').monthsOfYear", 4),
        ("'d: ' + date('2015-01-01')", "d: 2015-01-01"),
        ("date(null)", None),
        ("date.statement(null)", None),
        ("duration.between(null, date('2015-01-01'))", None),
        ("date.truncate('day', null)", None),
        ("datetime.fromepoch(null, 1)", None),
    ],
)
def test_temporal_expressions_evaluate(expr: str, expected: object) -> None:
    assert _eval(expr) == expected


def test_with_constants_are_temporal_values() -> None:
    other = T.construct("localtime", "12:31:14.645876123")
    assert (
        str(_eval("time({time: other, timezone: '+05:00'})", {"other": other})) == "12:31:14.645876123+05:00"
    )


@pytest.mark.parametrize(
    ("expr", "category", "cypher_class"),
    [
        ("date({year: 2015, month: 13})", "ArgumentError", "InvalidArgumentValue"),
        ("date('not a date')", "ArgumentError", "InvalidArgumentValue"),
        ("datetime({year: 2015, timezone: 'Mars/Olympus'})", "ArgumentError", "InvalidArgumentValue"),
        ("date({year: 2015, day: 3})", "ArgumentError", "InvalidArgumentValue"),
        ("date(1)", "TypeError", "InvalidArgumentType"),
        ("date('2015-01-01') + date('2015-01-01')", "TypeError", "InvalidArgumentType"),
        ("date('2015-01-01') + 1", "TypeError", "InvalidArgumentType"),
        ("date('2015-01-01') * 2", "TypeError", "InvalidArgumentType"),
        ("date('2015-01-01').hour", "ArgumentError", "InvalidArgumentValue"),
        ("date.truncate('day', date('2015-01-01'), 1)", "TypeError", "InvalidArgumentType"),
        ("date('2015-01-01', '2016-01-01')", "SyntaxError", "InvalidNumberOfArguments"),
        ("duration.between(date('2015-01-01'))", "SyntaxError", "InvalidNumberOfArguments"),
    ],
)
def test_temporal_errors(expr: str, category: str, cypher_class: str) -> None:
    with pytest.raises(CypherEvalError) as raised:
        _eval(expr)
    assert (raised.value.category, raised.value.cypher_class) == (category, cypher_class)


@pytest.mark.parametrize(
    "expr",
    [
        "date()",
        "datetime.statement()",
        "localtime.realtime('Europe/Berlin')",
        "time.transaction({timezone: '+01:00'})",
        "localdatetime({timezone: 'Europe/Berlin'})",
        # different readings of the clock
        "duration.between(date.transaction(), date())",
        "duration.between(date.realtime(), date.realtime())",
        "duration.between(date(), localdatetime())",
        "duration.inSeconds(datetime.statement('Europe/Berlin'), datetime.statement('Europe/Berlin'))",
    ],
)
def test_clock_readings_are_not_constant(expr: str) -> None:
    with pytest.raises(NotConstant):
        _eval(expr)


@pytest.mark.parametrize(
    "expr",
    [
        "duration.inSeconds(localtime(), localtime())",
        "duration.between(date(), date.statement())",
        "duration.inMonths(datetime.transaction(), datetime.transaction())",
        "duration.inDays(localdatetime( ), localdatetime())",
        "duration.between(time(), time())",
    ],
)
def test_one_clock_reading_twice_is_a_zero_duration(expr: str) -> None:
    """Neo4j reads the statement (or transaction) clock once: TCK Temporal10 [12]."""
    assert str(_eval(expr)) == "PT0S"


def test_error_mapping() -> None:
    assert to_eval_error(T.InvalidTemporalType("x")).category == "TypeError"
    unit = to_eval_error(T.UnsupportedTemporalUnit("x"))
    assert (unit.category, unit.cypher_class) == ("ArgumentError", "InvalidArgumentValue")
    overflow = to_eval_error(T.TemporalOverflow("x"))
    assert (overflow.category, overflow.cypher_class) == ("ArgumentError", "NumberOutOfRange")


def test_install_is_idempotent() -> None:
    before = len(cv.VALUE_KINDS)
    install()
    assert len(cv.VALUE_KINDS) == before
    for name in (
        "date",
        "duration",
        "date.truncate",
        "datetime.fromepochmillis",
        "duration.inseconds",
        "time.realtime",
    ):
        assert name in fns.FUNCTIONS


# --- translation ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("cypher", "expected"),
    [
        ("RETURN datetime({year: 1816, week: 1}) AS d", "1816-01-01T00:00Z"),
        ("RETURN duration.between(date('1984-10-11'), date('2015-06-24')) AS d", "P30Y8M13D"),
        (
            "WITH localtime({hour: 12, minute: 31}) AS other RETURN time({time: other, timezone: '+05:00'}) AS t",
            "12:31+05:00",
        ),
        ("WITH date({year: 1984, month: 10, day: 11}) AS d RETURN toString(d) AS ts", "1984-10-11"),
        (
            "UNWIND [1] AS x RETURN [date('2015-01-01'), date('2015-01-02')] AS l",
            ["2015-01-01", "2015-01-02"],
        ),
        # forms the runtime lowering refuses (tests/test_translate_temporal*.py)
        # are folded when their inputs are constant
        ("RETURN date({date: date('2020-01-01'), day: 3}) AS d", "2020-01-03"),
        (
            "RETURN datetime({year: 2020, timezone: 'Europe/Paris'}) AS d",
            "2020-01-01T00:00+01:00[Europe/Paris]",
        ),
        ("RETURN duration({days: 1}) + duration({days: 2}) AS t", "P3D"),
        ("RETURN time('12:00+01:00') + duration({hours: 1}) AS t", "13:00+01:00"),
    ],
)
def test_constant_temporal_values_are_bound_as_neo4j_strings(cypher: str, expected: object) -> None:
    assert _returned(cypher) == expected


def test_folded_scalars_are_inlined() -> None:
    out = translate(
        "WITH date('1984-10-11') AS d RETURN d.week AS w, d < date('2000-01-01') AS b", mapping=MAPPING
    )
    assert "41" in out.aql and "true" in out.aql


def test_invalid_constant_fails_at_run_time() -> None:
    out = translate("RETURN date({year: 2015, month: 13}) AS d", mapping=MAPPING)
    assert "FAIL(@cv0)" in out.aql
    assert "InvalidArgumentValue" in out.bind_vars["cv0"]


def test_data_dependent_temporal_values_still_lower_at_run_time() -> None:
    out = translate("MATCH (n:User) RETURN date(n.born) AS d, datetime() AS now", mapping=MAPPING)
    assert "DATE_ISO8601" in out.aql and "DATE_NOW()" in out.aql
    assert not any(k.startswith("cv") for k in out.bind_vars)


def test_comparison_with_a_property_reads_the_folded_value_on_the_time_line() -> None:
    """The folded string is one AQL's DATE_* functions parse (no named zone)."""
    out = translate(
        "MATCH (n:User) WHERE n.born > datetime('2010-01-01T12:00+01:00') RETURN n", mapping=MAPPING
    )
    assert out.bind_vars["cv0"] == "2010-01-01T12:00+01:00"


def test_a_folded_zoned_constant_is_compared_without_its_zone_name():
    # AQL's DATE_TIMESTAMP reads '…+02:00[Europe/Berlin]' as invalid (null).
    out = translate(
        "MATCH (p:Post) WHERE p.creationDate >= "
        "datetime({year: 2012, month: 9, day: 9, hour: 2, timezone: 'Europe/Berlin'}) RETURN p.id",
        mapping=mapping_bundle_for("ldbc_pg"),
    )
    assert out.bind_vars["cv0"] == "2012-09-09T02:00+02:00[Europe/Berlin]"
    assert 'DATE_TIMESTAMP(IS_STRING(@cv0) ? FIRST(SPLIT(@cv0, "[")) : @cv0)' in out.aql
    assert "DATE_TIMESTAMP(p.creationDate)" in out.aql


def test_run_time_temporal_operands_keep_the_plain_form():
    out = translate(
        "MATCH (m:Message) WHERE $date <= m.creationDate RETURN m.id",
        mapping=mapping_bundle_for("ldbc_pg"),
        params={"date": "2012-01-01T00:00:00Z"},
    )
    assert "SPLIT" not in out.aql
