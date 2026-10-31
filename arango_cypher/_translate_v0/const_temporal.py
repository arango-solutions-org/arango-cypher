"""Temporal values in compile-time evaluation (``const_eval``).

Connects :mod:`arango_cypher.temporal` (Cypher's six temporal types with
Neo4j's java.time semantics) to the constant evaluator:

* :class:`TemporalKind`, a ``cypher_values.ValueKind``: equality, ordering,
  ``+`` / ``-`` / ``*`` / ``/`` with durations, accessors (``d.week``,
  ``dur.minutesOfHour``), ``toString`` and the JSON form;
* the functions ``date`` … ``duration``, ``<kind>.truncate``,
  ``datetime.fromepoch`` / ``fromepochmillis``, ``duration.between`` /
  ``inMonths`` / ``inDays`` / ``inSeconds``, and the clock functions
  (``<kind>.statement`` / ``transaction`` / ``realtime``), which are never
  constant except on an explicit null.

A folded temporal value is emitted as its Neo4j string form
(``'2015-07-21T21:40:32.142+01:00'``, ``'P30Y8M'``), the form a Cypher client
reads and the form AQL's ``DATE_*`` functions read for every value without a
named zone; the runtime lowering in ``temporal.py`` also carries temporal
values as ISO strings. Only inputs known at translate time are folded; any
other argument raises ``NotConstant`` before these functions run, and the
expression compiles through ``temporal.py`` as before.

Library errors become :class:`cypher_values.CypherEvalError` with the Neo4j
error detail, so an invalid construction compiles to ``FAIL()``.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from .. import temporal as T
from . import cypher_functions as fns
from . import cypher_values as cv

_KINDS = ("date", "localtime", "time", "localdatetime", "datetime", "duration")
_BETWEEN_UNITS: dict[str, str | None] = {
    "between": None,
    "inmonths": "months",
    "indays": "days",
    "inseconds": "seconds",
}


def to_eval_error(error: T.TemporalError) -> cv.CypherEvalError:
    """The constant evaluator's error for a temporal library error: a wrong
    argument type is a ``TypeError``, every other one an ``ArgumentError``."""
    category = "TypeError" if error.cypher_class == "InvalidArgumentType" else "ArgumentError"
    return cv.CypherEvalError(category, error.tck_detail, error.message)


def _call(fn: Callable[..., Any], *args: Any) -> Any:
    """Run a library function, translating its errors and its not-constant
    signal to the evaluator's."""
    try:
        return fn(*args)
    except T.NotConstant as signal:
        raise cv.NotConstant(str(signal)) from signal
    except T.TemporalError as error:
        raise to_eval_error(error) from error


class TemporalKind(cv.ValueKind):
    """How temporal values take part in Cypher's operators."""

    def owns(self, v: Any) -> bool:
        return T.is_temporal(v)

    def equal(self, a: Any, b: Any) -> bool | None:
        return _call(T.equals, a, b)

    def compare(self, a: Any, b: Any) -> int | None:
        return _call(T.compare, a, b)

    def add(self, a: Any, b: Any) -> Any:
        if not _temporal_or_duration_pair(a, b):
            return NotImplemented
        return _call(T.add, a, b)

    def subtract(self, a: Any, b: Any) -> Any:
        if not _temporal_or_duration_pair(a, b):
            return NotImplemented
        return _call(T.subtract, a, b)

    def multiply(self, a: Any, b: Any) -> Any:
        if not (isinstance(a, T.Duration) and cv.is_number(b)) and not (
            isinstance(b, T.Duration) and cv.is_number(a)
        ):
            return NotImplemented
        return _call(T.multiply, a, b)

    def divide(self, a: Any, b: Any) -> Any:
        if not (isinstance(a, T.Duration) and cv.is_number(b)):
            return NotImplemented
        return _call(T.divide, a, b)

    def property(self, v: Any, key: str) -> Any:
        if not T.is_temporal(v):
            return NotImplemented
        return _call(T.get_property, v, key)

    def to_string(self, v: Any) -> Any:
        return str(v) if T.is_temporal(v) else NotImplemented

    def to_json(self, v: Any) -> Any:
        return str(v) if T.is_temporal(v) else NotImplemented


def _temporal_or_duration_pair(a: Any, b: Any) -> bool:
    """Both operands temporal: the library decides (``date + date`` is its
    type error). A temporal and anything else is not this kind's operator
    (``date + 'x'`` concatenates, ``date + 1`` is the caller's type error)."""
    return T.is_temporal(a) and T.is_temporal(b)


# --- functions ------------------------------------------------------------------


def _arity(name: str, args: list[Any], lo: int, hi: int) -> None:
    if not lo <= len(args) <= hi:
        expected = lo if lo == hi else f"{lo} to {hi}"
        raise cv.CypherEvalError(
            "SyntaxError",
            "InvalidNumberOfArguments",
            f"{name}() takes {expected} argument(s), got {len(args)}",
        )


def _constructor(kind: str) -> cv.FunctionImpl:
    def impl(args: list[Any]) -> Any:
        _arity(kind, args, 0, 1)
        return _call(T.construct, kind, *args)

    return impl


def _truncate(kind: str) -> cv.FunctionImpl:
    def impl(args: list[Any]) -> Any:
        _arity(f"{kind}.truncate", args, 2, 3)
        unit, value = args[0], args[1]
        overrides = args[2] if len(args) > 2 else None
        if unit is None or value is None:
            return None
        if overrides is not None and not isinstance(overrides, dict):
            raise cv.type_error(f"{kind}.truncate() expects a MAP of fields, got {cv.type_name(overrides)}")
        return _call(T.truncate, kind, unit, value, overrides)

    return impl


def _clock(kind: str, source: str) -> cv.FunctionImpl:
    def impl(args: list[Any]) -> Any:
        _arity(f"{kind}.{source}", args, 0, 1)
        return _call(T.clock, kind, source, *args)

    return impl


def _between(unit: str | None, name: str) -> cv.FunctionImpl:
    def impl(args: list[Any]) -> Any:
        _arity(name, args, 2, 2)
        return _call(T.duration_between, args[0], args[1], unit)

    return impl


def _from_epoch(args: list[Any]) -> Any:
    _arity("datetime.fromepoch", args, 2, 2)
    if args[0] is None or args[1] is None:
        return None
    return _call(T.from_epoch, args[0], args[1])


def _from_epoch_millis(args: list[Any]) -> Any:
    _arity("datetime.fromepochmillis", args, 1, 1)
    if args[0] is None:
        return None
    return _call(T.from_epoch_millis, args[0])


_KIND = TemporalKind()

#: duration.between(x, x) and duration.inMonths/inDays/inSeconds(x, x).
ZERO_DURATION = T.Duration(0, 0, 0, 0)
_CLOCK_CALL = re.compile(r"^(date|localtime|time|localdatetime|datetime)(?:\.(statement|transaction))?\(\)$")


def is_clock_difference(name: str, args: list[str]) -> bool:
    """``duration.between(x(), x())`` (or ``inMonths`` / ``inDays`` /
    ``inSeconds``) where both arguments read the same clock: Neo4j reads the
    statement clock once per statement and the transaction clock once per
    transaction, so the two values are equal and the duration is zero.

    Only exactly the same reading counts: the same kind, no arguments, and the
    same source (``x()`` is ``x.statement()``); ``realtime()`` reads the clock
    at each call, and a time zone argument is left to the run-time path.
    """
    if not name.startswith("duration.") or name.removeprefix("duration.") not in _BETWEEN_UNITS or len(args) != 2:
        return False
    readings = [_CLOCK_CALL.match(re.sub(r"\s+", "", a).lower()) for a in args]
    if readings[0] is None or readings[1] is None:
        return False
    kinds = {(m.group(1), m.group(2) or "statement") for m in readings}
    return len(kinds) == 1


def install() -> None:
    """Register the temporal value kind and functions with the evaluator.
    Idempotent: ``const_eval`` calls it once at import."""
    if not any(isinstance(k, TemporalKind) for k in cv.VALUE_KINDS):
        cv.VALUE_KINDS.append(_KIND)
    for kind in _KINDS:
        fns.register(kind, _constructor(kind))
        if kind == "duration":
            continue
        fns.register(f"{kind}.truncate", _truncate(kind))
        for source in T.CLOCK_SOURCES:
            fns.register(f"{kind}.{source}", _clock(kind, source))
    for suffix, unit in _BETWEEN_UNITS.items():
        fns.register(f"duration.{suffix}", _between(unit, f"duration.{suffix}"))
    fns.register("datetime.fromepoch", _from_epoch)
    fns.register("datetime.fromepochmillis", _from_epoch_millis)
