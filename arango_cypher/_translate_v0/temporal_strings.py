"""Temporal values held as strings, read at run time in AQL.

Folded temporal values (``const_temporal``) and values stored by ``CREATE``
are strings in Neo4j's form: ``'1984-10-11'``, ``'12:31:14.645876123'``,
``'12:31:14.645876123+01:00'``, ``'1984-10-11T12:31:14.645876123'``,
``'1984-10-11T12:31:14.645876123+01:00[Europe/Stockholm]'``,
``'P1Y4M10DT1H1M1.111111111S'``. With ``TranslateOptions.temporal_strings``
the translator treats a string in one of these forms as the temporal value it
spells wherever its type is only known at run time:

* accessors: ``d.year``, ``d.week``, ``d.offsetMinutes``, ``d.nanosecond``,
  ``dur.minutesOfHour`` (:func:`accessor`);
* ``+`` / ``-`` of a temporal and a duration, or of two durations, and
  ``*`` / ``/`` of a duration and a number (:func:`additive`,
  :func:`multiplicative`), with java.time's rules: month arithmetic clamps the
  day of month, fractions spill into smaller units;
* ``ORDER BY``: a Time orders by its instant on the day, then its local time;
  a DateTime by its instant (:func:`sort_key`). The other kinds already order
  correctly as text.

Everything is parsed in AQL from the string, at nanosecond precision (AQL's
``DATE_*`` functions stop at milliseconds, so the fraction is read
separately); calendar fields use ``DATE_*`` on the date part, so years 0000 to
9999. A named zone keeps the offset the value was stored with: arithmetic does
not re-resolve it across a daylight-saving change.

The option is off by default: a string that merely looks like a time is then
a string, and nothing here is emitted.

The snippets are AQL subqueries over ``LET`` bindings named ``_tv<n>``,
fresh per translation (AQL refuses a name an enclosing scope declares). AQL
evaluates both arms of a ternary, so no arm raises: a value of the wrong kind
gives null.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from ..temporal.parsing import AVG_DAYS_PER_MONTH

#: ``TranslateOptions.temporal_strings`` of the running translation.
_active_temporal_strings: ContextVar[bool] = ContextVar("_active_temporal_strings", default=False)
#: Next fresh ``_tv<n>`` binding of the running translation.
_next_binding: ContextVar[list[int] | None] = ContextVar("_next_binding", default=None)

TEMPORAL_KINDS = ("date", "localtime", "time", "localdatetime", "datetime")

_DATE_KINDS: tuple[str, ...] = ("date", "localdatetime", "datetime")
_TIME_KINDS: tuple[str, ...] = ("localtime", "time", "localdatetime", "datetime")
_ZONED_KINDS: tuple[str, ...] = ("time", "datetime")

# Neo4j's string forms of the date and time kinds. Groups: 1 year, 2 month,
# 3 day, 4 "T", 5 hour, 6 minute, 7 second, 8 fraction, 9 offset, 10 offset
# sign, 11-13 offset hours / minutes / seconds, 14 "[zone]".
_DATE_TIME_RE = (
    r"^(?:([+-]?[0-9]{4,9})-([0-9]{2})-([0-9]{2}))?(T)?"
    r"(?:([0-9]{2}):([0-9]{2})(?::([0-9]{2})(?:[.]([0-9]{1,9}))?)?)?"
    r"(Z|([+-])([0-9]{2}):([0-9]{2})(?::([0-9]{2}))?)?(\[[^\]]+\])?$"
)
# Neo4j's string form of a duration (``format_duration``). Groups: 1 years,
# 2 months, 3 weeks, 4 days, 5 hours, 6 minutes, 7 seconds' sign, 8 whole
# seconds, 9 fraction.
_DURATION_RE = (
    r"^P(?:(-?[0-9]+)Y)?(?:(-?[0-9]+)M)?(?:(-?[0-9]+)W)?(?:(-?[0-9]+)D)?"
    r"(?:T(?:(-?[0-9]+)H)?(?:(-?[0-9]+)M)?(?:(-?)([0-9]+)(?:[.]([0-9]{1,9}))?S)?)?$"
)
# A string ending in an offset or a zone: a Time or a DateTime (sort keys).
_ZONED_SUFFIX_RE = r"(Z|[+-][0-9]{2}:[0-9]{2}(:[0-9]{2})?)(\[[^\]]+\])?$"

_NS_PER_S = 1_000_000_000
_NS_PER_DAY = 86_400 * _NS_PER_S
_MS_PER_DAY = 86_400_000


def enabled() -> bool:
    return _active_temporal_strings.get()


@contextmanager
def translation_scope(enabled: bool) -> Iterator[None]:
    """The option and a fresh binding counter for one translation."""
    option = _active_temporal_strings.set(enabled)
    counter = _next_binding.set([0])
    try:
        yield
    finally:
        _next_binding.reset(counter)
        _active_temporal_strings.reset(option)


def _lit(text: str) -> str:
    """A constant (never user input) as an AQL string literal."""
    return json.dumps(text)


def _fresh() -> str:
    counter = _next_binding.get()
    if counter is None:  # outside a translation (unit tests of a snippet)
        counter = [0]
        _next_binding.set(counter)
    n = counter[0]
    counter[0] += 1
    return f"_tv{n}"


def _let(bindings: list[tuple[str, str]], result: str) -> str:
    """``FIRST(LET a = … LET b = … RETURN result)``: names for subexpressions."""
    lets = " ".join(f"LET {name} = {expr}" for name, expr in bindings)
    return f"FIRST({lets} RETURN {result})"


def _bind(expr: str, body: Callable[[str], str]) -> str:
    name = _fresh()
    return _let([(name, expr)], body(name))


def _trunc_div(a: str, b: str) -> str:
    """Integer division toward zero (Java's ``/``)."""
    return f"(({a}) / ({b}) < 0 ? CEIL(({a}) / ({b})) : FLOOR(({a}) / ({b})))"


def _trunc(x: str) -> str:
    return f"(({x}) < 0 ? CEIL({x}) : FLOOR({x}))"


def _group(n: str, scale: int) -> str:
    """Three digits of the whole number *n* (0 <= n < 1e15) from 10^scale."""
    value = f"(({n}) % 1000)" if scale == 0 else f"(FLOOR(({n}) / {10**scale}) % 1000)"
    # A number below 1000 is always written in full; a larger double may be
    # written in exponent form (TO_STRING(9e8) is "9e+8").
    return f"RIGHT(CONCAT('000', TO_STRING({value})), 3)"


def _digits(n: str) -> str:
    """The 15 digits of the whole number *n* (0 <= n < 1e15), zero-padded."""
    return f"CONCAT({', '.join(_group(n, scale) for scale in (12, 9, 6, 3, 0))})"


def _pad(n: str, width: int) -> str:
    """The whole number *n* (0 <= n < 10^width) zero-padded to *width* digits."""
    if width <= 3:
        return f"RIGHT(CONCAT('000', TO_STRING({n})), {width})"
    return f"RIGHT({_digits(n)}, {width})"


def _int_str(n: str) -> str:
    """The whole number *n* (|n| < 1e15) as text, without exponent form."""
    return _bind(
        n,
        lambda v: f"CONCAT({v} < 0 ? '-' : '', {v} == 0 ? '0' : LTRIM({_digits(f'ABS({v})')}, '0'))",
    )


# --- parsing ---------------------------------------------------------------------------


def parse(value: str) -> str:
    """AQL for the parsed form of the string *value*, or null.

    Date and time kinds: ``{k, y, mo, d, ds, ns, off, os, z}`` (kind; year,
    month, day and ``YYYY-MM-DD``; nanoseconds of the day; offset in seconds
    and as written; ``[zone]``). Durations: ``{k: 'duration', mo, dd, s, n}``
    with ``n`` in [0, 1e9).
    """
    return _bind(value, _parse_bound)


def _parse_bound(v: str) -> str:
    m, u = _fresh(), _fresh()
    g = [f"{m}[{i}]" for i in range(15)]
    kind = (
        f"({g[1]} != '' ? (({g[4]} != '' && {g[5]} != '') ? ({g[9]} != '' ? 'datetime' : "
        f"({g[14]} == '' ? 'localdatetime' : null)) : (({g[4]} == '' && {g[5]} == '' && {g[9]} == '' "
        f"&& {g[14]} == '') ? 'date' : null)) : (({g[4]} == '' && {g[5]} != '' && {g[14]} == '') ? "
        f"({g[9]} != '' ? 'time' : 'localtime') : null))"
    )
    fraction = f"TO_NUMBER(LEFT(CONCAT({g[8]}, '000000000'), 9))"
    seconds_of_day = f"(TO_NUMBER({g[5]}) * 3600 + TO_NUMBER({g[6]}) * 60 + TO_NUMBER({g[7]}))"
    offset = (
        f"({g[9]} == '' ? null : ({g[9]} == 'Z' ? 0 : ({g[10]} == '-' ? -1 : 1) * "
        f"(TO_NUMBER({g[11]}) * 3600 + TO_NUMBER({g[12]}) * 60 + TO_NUMBER({g[13]}))))"
    )
    date_time = (
        f"{{k: {kind}, y: TO_NUMBER({g[1]}), mo: TO_NUMBER({g[2]}), d: TO_NUMBER({g[3]}), "
        f"ds: CONCAT({g[1]}, '-', {g[2]}, '-', {g[3]}), ns: {seconds_of_day} * {_NS_PER_S} + {fraction}, "
        f"off: {offset}, os: {g[9]}, z: {g[14]}}}"
    )
    d = [f"{u}[{i}]" for i in range(10)]
    sign = f"({d[7]} == '-' ? -1 : 1)"
    raw_nanos = f"{sign} * TO_NUMBER(LEFT(CONCAT({d[9]}, '000000000'), 9))"
    raw_seconds = f"(TO_NUMBER({d[5]}) * 3600 + TO_NUMBER({d[6]}) * 60 + {sign} * TO_NUMBER({d[8]}))"
    carry = f"FLOOR(({raw_nanos}) / {_NS_PER_S})"
    duration = (
        f"{{k: 'duration', mo: TO_NUMBER({d[1]}) * 12 + TO_NUMBER({d[2]}), "
        f"dd: TO_NUMBER({d[3]}) * 7 + TO_NUMBER({d[4]}), s: {raw_seconds} + {carry}, "
        f"n: {raw_nanos} - {carry} * {_NS_PER_S}}}"
    )
    is_duration = f"(IS_STRING({v}) && LEFT({v}, 1) == 'P')"
    return _let(
        [
            (m, f"({is_duration} || !IS_STRING({v})) ? null : REGEX_MATCHES({v}, {_lit(_DATE_TIME_RE)})"),
            (u, f"{is_duration} ? REGEX_MATCHES({v}, {_lit(_DURATION_RE)}) : null"),
        ],
        f"({m} != null ? {date_time} : (({u} != null && {u}[0] != 'P' && {u}[0] != 'PT') ? {duration} : null))",
    )


# --- formatting ------------------------------------------------------------------------


def format_time_of_day(ns: str) -> str:
    """java.time's time of day: ``12:31``, ``12:31:14``, ``12:31:14.645``,
    ``12:31:14.645876``, ``12:31:14.645876123``."""
    h, mi, sec, fr = _fresh(), _fresh(), _fresh(), _fresh()
    fraction = (
        f"({fr} == 0 ? '' : ({fr} % 1000000 == 0 ? CONCAT('.', {_pad(f'{fr} / 1000000', 3)}) : "
        f"({fr} % 1000 == 0 ? CONCAT('.', {_pad(f'{fr} / 1000', 6)}) : CONCAT('.', {_pad(fr, 9)}))))"
    )
    return _let(
        [
            (h, f"FLOOR(({ns}) / {3600 * _NS_PER_S})"),
            (mi, f"FLOOR(({ns}) / {60 * _NS_PER_S}) % 60"),
            (sec, f"FLOOR(({ns}) / {_NS_PER_S}) % 60"),
            (fr, f"({ns}) % {_NS_PER_S}"),
        ],
        f"CONCAT({_pad(h, 2)}, ':', {_pad(mi, 2)}, ({sec} == 0 && {fr} == 0) ? '' : "
        f"CONCAT(':', {_pad(sec, 2)}, {fraction}))",
    )


def format_duration(mo: str, dd: str, s: str, n: str) -> str:
    """Neo4j's duration string of (months, days, seconds, nanos in [0, 1e9))."""
    s1, n1, rest, sec = _fresh(), _fresh(), _fresh(), _fresh()

    def unit(q: str, suffix: str) -> str:
        return f"({q} != 0 ? CONCAT({_int_str(q)}, {_lit(suffix)}) : '')"

    seconds_part = (
        f"(({sec} != 0 || {n1} != 0) ? CONCAT(({sec} == 0 && {n1} < 0) ? '-' : '', {sec}, "
        f"{n1} != 0 ? CONCAT('.', RTRIM({_pad(f'ABS({n1})', 9)}, '0')) : '', 'S') : '')"
    )
    time_part = (
        f"(({s1} != 0 || {n1} != 0) ? CONCAT('T', {unit(_trunc_div(s1, '3600'), 'H')}, "
        f"{unit(_trunc_div(rest, '60'), 'M')}, {seconds_part}) : '')"
    )
    body = (
        f"(({mo}) == 0 && ({dd}) == 0 && ({s}) == 0 && ({n}) == 0) ? 'PT0S' : "
        f"CONCAT('P', {unit(_trunc_div(mo, '12'), 'Y')}, {unit(f'(({mo}) % 12)', 'M')}, "
        f"{unit(f'({dd})', 'D')}, {time_part})"
    )
    return _let(
        [
            (s1, f"(({s}) < 0 && ({n}) != 0) ? ({s}) + 1 : ({s})"),
            (n1, f"(({s}) < 0 && ({n}) != 0) ? ({n}) - {_NS_PER_S} : ({n})"),
            (rest, f"{s1} % 3600"),
            (sec, f"{rest} % 60"),
        ],
        body,
    )


def _normalized_duration(mo: str, dd: str, s: str, n: str) -> str:
    """The string of a duration whose nanos may lie outside [0, 1e9)."""
    carry = _fresh()
    return _let(
        [(carry, f"FLOOR(({n}) / {_NS_PER_S})")],
        format_duration(mo, dd, f"(({s}) + {carry})", f"(({n}) - {carry} * {_NS_PER_S})"),
    )


def _approximate(months: str, days: str, seconds: str, nanos: str) -> str:
    """``parsing.approximate``: fractions cascade down, each step truncated
    toward zero, in the same floating-point order as Python (and Neo4j)."""
    m, d2, d, s2, s, n2 = (_fresh() for _ in range(6))
    return _let(
        [
            (m, _trunc(months)),
            (d2, f"({days}) + {AVG_DAYS_PER_MONTH!r} * (({months}) - {m})"),
            (d, _trunc(d2)),
            (s2, f"({seconds}) + 86400 * ({d2} - {d})"),
            (s, _trunc(s2)),
            (n2, f"({nanos}) + 1000000000.0 * ({s2} - {s})"),
        ],
        _normalized_duration(m, d, s, _trunc(n2)),
    )


# --- accessors ---------------------------------------------------------------------------

DATE_ACCESSORS = (
    "year",
    "quarter",
    "month",
    "week",
    "weekYear",
    "day",
    "ordinalDay",
    "weekDay",
    "dayOfWeek",
    "dayOfQuarter",
)
TIME_ACCESSORS = ("hour", "minute", "second", "millisecond", "microsecond", "nanosecond")
ZONE_ACCESSORS = ("timezone", "offset", "offsetMinutes", "offsetSeconds")
INSTANT_ACCESSORS = ("epochSeconds", "epochMillis")
DURATION_ACCESSORS = (
    "years",
    "quarters",
    "months",
    "weeks",
    "days",
    "hours",
    "minutes",
    "seconds",
    "milliseconds",
    "microseconds",
    "nanoseconds",
    "quartersOfYear",
    "monthsOfQuarter",
    "monthsOfYear",
    "daysOfWeek",
    "minutesOfHour",
    "secondsOfMinute",
    "millisecondsOfSecond",
    "microsecondsOfSecond",
    "nanosecondsOfSecond",
)
ACCESSORS = frozenset(
    DATE_ACCESSORS + TIME_ACCESSORS + ZONE_ACCESSORS + INSTANT_ACCESSORS + DURATION_ACCESSORS
)


def _kind_in(p: str, kinds: tuple[str, ...]) -> str:
    return f"{p}.k IN {json.dumps(list(kinds))}"


def _date_field(p: str, name: str) -> str:
    quarter = f"(FLOOR(({p}.mo - 1) / 3) + 1)"
    fields = {
        "year": f"{p}.y",
        "month": f"{p}.mo",
        "day": f"{p}.d",
        "quarter": quarter,
        "week": f"DATE_ISOWEEK({p}.ds)",
        "weekYear": f"DATE_ISOWEEKYEAR({p}.ds).year",
        "ordinalDay": f"DATE_DAYOFYEAR({p}.ds)",
        "weekDay": f"(((DATE_DAYOFWEEK({p}.ds) + 6) % 7) + 1)",
        "dayOfWeek": f"(((DATE_DAYOFWEEK({p}.ds) + 6) % 7) + 1)",
        "dayOfQuarter": (
            f"(DATE_DIFF(CONCAT(LEFT({p}.ds, LENGTH({p}.ds) - 5), {_pad(f'({quarter} - 1) * 3 + 1', 2)}, "
            f"'-01'), {p}.ds, 'd') + 1)"
        ),
    }
    return fields[name]


def _time_field(p: str, name: str) -> str:
    fraction = f"({p}.ns % {_NS_PER_S})"
    fields = {
        "hour": f"FLOOR({p}.ns / {3600 * _NS_PER_S})",
        "minute": f"(FLOOR({p}.ns / {60 * _NS_PER_S}) % 60)",
        "second": f"(FLOOR({p}.ns / {_NS_PER_S}) % 60)",
        "millisecond": f"FLOOR({fraction} / 1000000)",
        "microsecond": f"FLOOR({fraction} / 1000)",
        "nanosecond": fraction,
    }
    return fields[name]


def _epoch_seconds(p: str) -> str:
    return f"(DATE_TIMESTAMP({p}.ds) / 1000 + FLOOR({p}.ns / {_NS_PER_S}) - {p}.off)"


def _zone_field(p: str, name: str) -> str:
    fields = {
        "timezone": f"({p}.z != '' ? SUBSTRING({p}.z, 1, LENGTH({p}.z) - 2) : {p}.os)",
        "offset": f"{p}.os",
        "offsetMinutes": _trunc_div(f"{p}.off", "60"),
        "offsetSeconds": f"{p}.off",
        "epochSeconds": _epoch_seconds(p),
        "epochMillis": f"({_epoch_seconds(p)} * 1000 + FLOOR(({p}.ns % {_NS_PER_S}) / 1000000))",
    }
    return fields[name]


def _duration_field(p: str, name: str) -> str:
    mo, dd, s, n = f"{p}.mo", f"{p}.dd", f"{p}.s", f"{p}.n"
    fields = {
        "years": _trunc_div(mo, "12"),
        "quarters": _trunc_div(mo, "3"),
        "months": mo,
        "weeks": _trunc_div(dd, "7"),
        "days": dd,
        "hours": _trunc_div(s, "3600"),
        "minutes": _trunc_div(s, "60"),
        "seconds": s,
        "milliseconds": f"({s} * 1000 + FLOOR({n} / 1000000))",
        "microseconds": f"({s} * 1000000 + FLOOR({n} / 1000))",
        "nanoseconds": f"({s} * {_NS_PER_S} + {n})",
        "quartersOfYear": _trunc_div(f"({mo} % 12)", "3"),
        "monthsOfQuarter": f"({mo} % 3)",
        "monthsOfYear": f"({mo} % 12)",
        "daysOfWeek": f"({dd} % 7)",
        "minutesOfHour": f"({_trunc_div(s, '60')} % 60)",
        "secondsOfMinute": f"({s} % 60)",
        "millisecondsOfSecond": f"FLOOR({n} / 1000000)",
        "microsecondsOfSecond": f"FLOOR({n} / 1000)",
        "nanosecondsOfSecond": n,
    }
    return fields[name]


def accessor(value: str, name: str) -> str:
    """``value.name`` where *value* may be a temporal string or a map / node.

    A string is parsed; an accessor its kind lacks gives null. Anything else
    reads the attribute, as before.
    """
    if name not in ACCESSORS:
        raise ValueError(f"not a temporal accessor: {name}")

    def on(v: str) -> str:
        p = _fresh()
        if name in DATE_ACCESSORS:
            field, kinds = _date_field(p, name), _DATE_KINDS
        elif name in TIME_ACCESSORS:
            field, kinds = _time_field(p, name), _TIME_KINDS
        elif name in ZONE_ACCESSORS:
            field, kinds = _zone_field(p, name), _ZONED_KINDS
        elif name in INSTANT_ACCESSORS:
            field, kinds = _zone_field(p, name), ("datetime",)
        else:
            field, kinds = _duration_field(p, name), ("duration",)
        read = _let([(p, _parse_bound(v))], f"({p} != null && {_kind_in(p, kinds)}) ? {field} : null")
        return f"(IS_STRING({v}) ? {read} : {v}.{name})"

    return _bind(value, on)


# --- arithmetic ---------------------------------------------------------------------------


def _shift(p: str, mo: str, dd: str, s: str, n: str) -> str:
    """The string of the parsed temporal *p* plus a duration's components
    (already signed): months then days on the wall clock, the day of month
    clamped (java.time ``plusMonths``), then seconds and nanoseconds."""
    total, ny, nm, nd, day_ms, q, t, extra, ns2, ds2 = (_fresh() for _ in range(10))
    date_days = f"(({dd}) + {_trunc_div(s, '86400')})"
    time_days = f"(({dd}) + {q} + {extra})"
    is_date = f"{p}.k == 'date'"
    date_string = (
        f"(({ny}) < 0 || ({ny}) > 9999) ? null : LEFT(DATE_ISO8601({day_ms} + "
        f"({is_date} ? {date_days} : {time_days}) * {_MS_PER_DAY}), 10)"
    )
    time_string = format_time_of_day(ns2)
    zone = f"CONCAT({p}.os, {p}.z)"
    result = (
        f"{is_date} ? {ds2} : ({p}.k == 'localtime' ? {time_string} : ({p}.k == 'time' ? "
        f"CONCAT({time_string}, {zone}) : ({p}.k == 'localdatetime' ? CONCAT({ds2}, 'T', {time_string}) : "
        f"CONCAT({ds2}, 'T', {time_string}, {zone}))))"
    )
    first_of_month = f"CONCAT({_pad(ny, 4)}, '-', {_pad(nm, 2)}, '-01')"
    return _let(
        [
            (total, f"{p}.y * 12 + {p}.mo - 1 + ({mo})"),
            (ny, f"FLOOR({total} / 12)"),
            (nm, f"{total} - {ny} * 12 + 1"),
            (nd, f"MIN([{p}.d, DATE_DAYS_IN_MONTH({first_of_month})])"),
            (day_ms, f"DATE_TIMESTAMP(CONCAT({_pad(ny, 4)}, '-', {_pad(nm, 2)}, '-', {_pad(nd, 2)}))"),
            (q, f"FLOOR(({s}) / 86400)"),
            (t, f"{p}.ns + (({s}) - {q} * 86400) * {_NS_PER_S} + ({n})"),
            (extra, f"FLOOR({t} / {_NS_PER_DAY})"),
            (ns2, f"{t} - {extra} * {_NS_PER_DAY}"),
            (ds2, date_string),
        ],
        result,
    )


def _shift_time_of_day(p: str, s: str, n: str) -> str:
    """A LocalTime / Time plus seconds and nanos: the day wraps around."""
    ns2 = _fresh()
    time_string = format_time_of_day(ns2)
    return _let(
        [
            (
                ns2,
                f"(({p}.ns + ((({s}) % 86400 + 86400) % 86400) * {_NS_PER_S} + ({n})) % {_NS_PER_DAY} + {_NS_PER_DAY}) % {_NS_PER_DAY}",
            )
        ],
        f"{p}.k == 'localtime' ? {time_string} : CONCAT({time_string}, {p}.os, {p}.z)",
    )


def _plus(p: str, du: str, sign: int) -> str:
    """Temporal *p* plus (sign 1) or minus (-1) the parsed duration *du*."""
    signed = (lambda c: f"{du}.{c}") if sign > 0 else (lambda c: f"(-{du}.{c})")
    on_day = _shift_time_of_day(p, signed("s"), signed("n"))
    on_calendar = _shift(p, signed("mo"), signed("dd"), signed("s"), signed("n"))
    return f"({p}.k IN ['localtime', 'time'] ? {on_day} : {on_calendar})"


def additive(left: str, op: str, right: str, fallback: str) -> str:
    """``left + right`` / ``left - right`` with temporal strings read as
    temporal values; *fallback* (Cypher's ordinary ``+`` / ``-``) otherwise."""
    a, b, pa, pb = _fresh(), _fresh(), _fresh(), _fresh()
    sign = 1 if op == "+" else -1
    both_durations = f"{pa}.k == 'duration' && {pb}.k == 'duration'"
    durations = _normalized_duration(
        f"{pa}.mo {op} {pb}.mo", f"{pa}.dd {op} {pb}.dd", f"{pa}.s {op} {pb}.s", f"{pa}.n {op} {pb}.n"
    )
    temporal = json.dumps(list(TEMPORAL_KINDS))
    cases = [
        (both_durations, durations),
        (f"{pa}.k IN {temporal} && {pb}.k == 'duration'", _plus(pa, pb, sign)),
    ]
    if op == "+":
        cases.append((f"{pa}.k == 'duration' && {pb}.k IN {temporal}", _plus(pb, pa, 1)))
    result = "null"
    for test, value in reversed(cases):
        result = f"(({test}) ? {value} : {result})"
    temporal_case = " || ".join(f"({test})" for test, _ in cases)
    return _let(
        [
            (a, left),
            (b, right),
            (pa, f"IS_STRING({a}) ? {_parse_bound(a)} : null"),
            (pb, f"IS_STRING({b}) ? {_parse_bound(b)} : null"),
        ],
        f"({pa} != null && {pb} != null && ({temporal_case})) ? {result} : {fallback}",
    )


def multiplicative(left: str, op: str, right: str, fallback: str) -> str:
    """``duration * number``, ``number * duration`` and ``duration / number``
    with a duration string read as a duration; *fallback* otherwise. Division
    by zero gives null (an AQL ternary cannot raise in one arm only)."""
    a, b, pa, pb = _fresh(), _fresh(), _fresh(), _fresh()

    def scaled(du: str, factor: str) -> str:
        return _approximate(
            f"{du}.mo {op} {factor}",
            f"{du}.dd {op} {factor}",
            f"{du}.s {op} {factor}",
            f"{du}.n {op} {factor}",
        )

    left_duration = f"{pa} != null && {pa}.k == 'duration' && IS_NUMBER({b})"
    if op == "/":
        left_duration += f" && {b} != 0"
    cases = [(left_duration, scaled(pa, b))]
    if op == "*":
        cases.append((f"{pb} != null && {pb}.k == 'duration' && IS_NUMBER({a})", scaled(pb, a)))
    result = fallback
    for test, value in reversed(cases):
        result = f"(({test}) ? {value} : {result})"
    return _let(
        [
            (a, left),
            (b, right),
            (pa, f"IS_STRING({a}) ? {_parse_bound(a)} : null"),
            (pb, f"IS_STRING({b}) ? {_parse_bound(b)} : null"),
        ],
        result,
    )


# --- ordering ------------------------------------------------------------------------------


def _zoned_key(v: str) -> str:
    """``[0, instant on the day, local time]`` for a Time string,
    ``[1, epoch second, nanosecond]`` for a DateTime string, else null."""
    p = _fresh()
    time_key = f"[0, {p}.ns - {p}.off * {_NS_PER_S}, {p}.ns]"
    datetime_key = f"[1, {_epoch_seconds(p)}, {p}.ns % {_NS_PER_S}]"
    keyed = _let(
        [(p, _parse_bound(v))],
        f"({p} != null && {p}.k == 'time') ? {time_key} : (({p} != null && {p}.k == 'datetime') ? "
        f"{datetime_key} : null)",
    )
    return f"((IS_STRING({v}) && REGEX_TEST({v}, {_lit(_ZONED_SUFFIX_RE)})) ? {keyed} : null)"


def sort_key(value: str, fallback: str) -> str:
    """The sort key of *value*: a Time orders by its instant on the day, then
    its local time; a DateTime by its instant; anything else by *fallback*
    (its usual key). The other kinds already order correctly as text."""
    v, k = _fresh(), _fresh()
    return _let([(v, value), (k, _zoned_key(v))], f"{k} != null ? {k} : {fallback}")


def ordering(left: str, op: str, right: str, fallback: str, *, negated: bool = False) -> str:
    """``left <op> right`` (``<``, ``<=``, ``>``, ``>=``): two Times compare by
    instant on the day then local time, two DateTimes by instant; any other
    pair as *fallback* (the comparison compiled as usual). *negated* when
    *fallback* tests that the comparison is false (``WHERE NOT a < b``)."""
    a, b, ka, kb = _fresh(), _fresh(), _fresh(), _fresh()
    compared = f"({ka} {op} {kb})"
    if negated:
        compared = f"!{compared}"
    return _let(
        [(a, left), (b, right), (ka, _zoned_key(a)), (kb, _zoned_key(b))],
        f"({ka} != null && {kb} != null && {ka}[0] == {kb}[0]) ? {compared} : {fallback}",
    )
