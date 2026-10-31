"""Temporal values, functions and accessors (LDBC plan WP-41).

A stored temporal property is an epoch-ms number or an ISO-8601 string (the
mapping may say which, ``PropertyInfo.encoding``). A temporal value the query
computes is an ISO-8601 string in UTC, which AQL's ``DATE_*`` functions read
like either stored form:

* ``datetime(x)``, ``localdatetime(x)`` → ``DATE_ISO8601(x)``: ``x`` is a string
  or epoch-ms number; ``datetime({epochMillis: e})``, ``{epochSeconds: s}`` and
  ``{year: …, month: …, day: …, hour: …, minute: …, second: …,
  millisecond: …}`` are supported; no argument is now.
* ``date(x)`` → ``LEFT(DATE_ISO8601(x), 10)``, the ``YYYY-MM-DD`` of ``x``
  (its UTC date), and ``date({year:, month:, day:})``.
* ``v.year`` / ``.month`` / ``.day`` / … → ``DATE_YEAR(v)`` / … when ``v`` is a
  temporal value: a property the mapping declares temporal
  (``n.creationDate.year``), a temporal function, or a variable a ``WITH``
  bound to one (``WITH datetime({epochMillis: x}) AS b … b.month``). On a
  parameter, which may be a map, the accessor is decided at run time:
  ``IS_OBJECT(@p) ? @p.year : DATE_YEAR(@p)``. Elsewhere ``.year`` stays the
  attribute it always was (``m.year`` of a movie).

Components are UTC: an ISO value with an offset is converted, where Cypher
would keep its local fields. Each lowered call is null for a null value (AQL
returns null with a warning).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from arango_query_core import CoreError, MappingResolver

from .._antlr.CypherParser import CypherParser
from . import temporal_strings
from .arithmetic import BOOL, FLOAT, INT, NUMBER, STRING, declared_property_type
from .state import _active_resolver, _temporal_vars
from .temporal_guard import TEMPORAL_ACCESSORS, temporal_properties
from .three_valued import _three_valued

TEMPORAL_FUNCTIONS = frozenset({"datetime", "localdatetime", "date"})


def _fn(name: str) -> Callable[[str], str]:
    return lambda v: f"{name}({v})"


#: Accessor → its AQL over a value AQL's ``DATE_*`` functions accept.
INSTANT_ACCESSORS: dict[str, Callable[[str], str]] = {
    "year": _fn("DATE_YEAR"),
    "quarter": _fn("DATE_QUARTER"),
    "month": _fn("DATE_MONTH"),
    "week": _fn("DATE_ISOWEEK"),
    "day": _fn("DATE_DAY"),
    "ordinalDay": _fn("DATE_DAYOFYEAR"),
    # AQL counts from Sunday = 0; Cypher from Monday = 1 to Sunday = 7.
    "dayOfWeek": lambda v: f"(((DATE_DAYOFWEEK({v}) + 6) % 7) + 1)",
    "weekDay": lambda v: f"(((DATE_DAYOFWEEK({v}) + 6) % 7) + 1)",
    "hour": _fn("DATE_HOUR"),
    "minute": _fn("DATE_MINUTE"),
    "second": _fn("DATE_SECOND"),
    "millisecond": _fn("DATE_MILLISECOND"),
    # Stored values carry milliseconds, so the finer fields are multiples.
    "microsecond": lambda v: f"(DATE_MILLISECOND({v}) * 1000)",
    "nanosecond": lambda v: f"(DATE_MILLISECOND({v}) * 1000000)",
    "epochMillis": _fn("DATE_TIMESTAMP"),
    "epochSeconds": lambda v: f"FLOOR(DATE_TIMESTAMP({v}) / 1000)",
}


def _null_safe(expr: str, value: str) -> str:
    return _three_valued(expr, expr, (value,), None)


def lower_accessor(value: str, accessor: str) -> str:
    """``value.accessor`` for a value known to be temporal."""
    build = INSTANT_ACCESSORS.get(accessor)
    if build is None:
        raise CoreError(
            f"Temporal accessor '.{accessor}' is not supported yet (supported: "
            f"{', '.join(sorted(INSTANT_ACCESSORS))}; docs/ldbc_benchmark_plan.md WP-41)",
            code="UNSUPPORTED",
        )
    return _null_safe(build(value), value)


def dispatch_accessor(value: str, accessor: str) -> str:
    """``value.accessor`` for a value that may be a map or a temporal (a parameter)."""
    return f"(IS_OBJECT({value}) ? {value}.{accessor} : {lower_accessor(value, accessor)})"


def compile_property_chain(
    atom_ctx: CypherParser.OC_AtomContext, atom: str, keys: list[str], resolver: MappingResolver | None
) -> str:
    """``atom.k1.k2…`` with temporal accessors lowered (module docstring)."""
    state = _atom_state(atom_ctx)
    for i, key in enumerate(keys):
        if state == "temporal" and key in TEMPORAL_ACCESSORS:
            atom, state = lower_accessor(atom, key), None
        elif state == "maybe" and key in INSTANT_ACCESSORS:
            atom, state = dispatch_accessor(atom, key), None
        elif state in ("variable", None) and key in temporal_strings.ACCESSORS and temporal_strings.enabled():
            # A value of unknown type: a temporal string is read at run time.
            atom, state = temporal_strings.accessor(atom, key), None
        else:
            atom = f"{atom}.{key}"
            # ``n.creationDate`` of a variable: the mapping says it is temporal.
            declared = i == 0 and state == "variable" and len(keys) > 1 and resolver is not None
            if declared and keys[1] in TEMPORAL_ACCESSORS:
                _refuse_scalar_accessor(key, keys[1], resolver)
            state = (
                "temporal"
                if declared and resolver is not None and key in temporal_properties(resolver)
                else None
            )
    return atom


def _refuse_scalar_accessor(prop: str, accessor: str, resolver: MappingResolver | None) -> None:
    """``n.p.year`` where the mapping types ``p`` as a string, number or boolean.

    Such a value has no ``.year``: in Cypher it is an error, and it usually
    means a timestamp the mapping types by its storage (the analyzer types
    epoch-ms data as a number). AQL would read a missing attribute and return
    null for every row.
    """
    declared = declared_property_type(prop, resolver)
    if declared in (STRING, INT, FLOAT, NUMBER, BOOL):
        raise CoreError(
            f"'.{accessor}' of property '{prop}', which the mapping types as {declared}: if it holds "
            "timestamps, declare it date or datetime (with its encoding) in the mapping",
            code="UNSUPPORTED",
        )


def _atom_state(atom_ctx: CypherParser.OC_AtomContext) -> str | None:
    """``temporal``, ``maybe`` (a parameter), ``variable`` or None."""
    if atom_ctx.oC_Parameter() is not None:
        return "maybe"
    if atom_ctx.oC_Variable() is not None:
        name = atom_ctx.oC_Variable().getText().strip()
        return "temporal" if _temporal_vars.get().get(name) == "computed" else "variable"
    if is_temporal_value(atom_ctx):
        return "temporal"
    return None


def is_temporal_value(ctx: Any) -> bool:
    """Whether the expression *ctx* is a temporal value: a temporal function, a
    declared temporal property of a variable, a variable bound to one, or one
    of these plus or minus a duration."""
    operand = temporal_operand(ctx)
    return operand is not None and operand[0] in ("property", "computed")


def temporal_operand(ctx: Any) -> tuple[str, str] | None:
    """What kind of temporal operand *ctx* is: ``("property", encoding)`` for a
    declared temporal property of a variable (encoding ``""`` when the mapping
    does not say), ``("computed", "")`` for a value the query computes,
    ``("duration", "")`` for ``duration(…)``, else None."""
    while True:
        if isinstance(ctx, CypherParser.OC_AddOrSubtractExpressionContext):
            terms = ctx.oC_MultiplyDivideModuloExpression()
            if len(terms) > 1:
                kinds = [temporal_operand(t) for t in terms]
                if not any(k is not None and k[0] == "duration" for k in kinds):
                    return None
                if all(k is not None and k[0] == "duration" for k in kinds):
                    return ("duration", "")
                return ("computed", "")
        if isinstance(ctx, CypherParser.OC_PropertyOrLabelsExpressionContext):
            lookups = ctx.oC_PropertyLookup() or []
            if ctx.oC_NodeLabels() is not None or len(lookups) > 1:
                return None
            if lookups:
                atom = ctx.oC_Atom()
                key = lookups[0].oC_PropertyKeyName().getText().strip()
                resolver = _active_resolver.get()
                if atom.oC_Variable() is None or resolver is None or key not in temporal_properties(resolver):
                    return None
                return ("property", declared_encoding(key, resolver))
            ctx = ctx.oC_Atom()
            continue
        if isinstance(ctx, CypherParser.OC_AtomContext):
            if ctx.oC_FunctionInvocation() is not None:
                name = ctx.oC_FunctionInvocation().oC_FunctionName().getText().strip().lower()
                if name == "duration":
                    return ("duration", "")
                return ("computed", "") if name in TEMPORAL_FUNCTIONS else None
            if ctx.oC_Variable() is not None:
                kind = _temporal_vars.get().get(ctx.oC_Variable().getText().strip())
                return (kind, "") if kind is not None else None
            if ctx.oC_ParenthesizedExpression() is not None:
                ctx = ctx.oC_ParenthesizedExpression().oC_Expression()
                continue
            return None
        child = _single_child(ctx)
        if child is None:
            return None
        ctx = child


def has_temporal_operand(terms: list[Any]) -> bool:
    """Whether a term is a temporal value or duration the query computes."""
    return any(
        (operand := temporal_operand(term)) is not None and operand[0] in ("computed", "duration")
        for term in terms
    )


def refuse_temporal_operands(terms: list[Any]) -> None:
    """``+`` / ``-`` with a temporal value the query computes and no duration.

    The value is an ISO string, so AQL would concatenate or subtract text
    (``datetime() - datetime()`` is ``duration.between`` in Cypher).
    """
    for term in terms:
        operand = temporal_operand(term)
        if operand is not None and operand[0] == "computed":
            raise CoreError(
                "Arithmetic on a temporal value is supported with a duration({…}) operand only "
                "(datetime() + duration({days: 1})); duration.between() is not supported yet",
                code="NOT_IMPLEMENTED",
            )


def is_duration(ctx: Any) -> bool:
    operand = temporal_operand(ctx)
    return operand is not None and operand[0] == "duration"


def declared_encoding(prop: str, resolver: MappingResolver) -> str:
    """The encoding every declaration of *prop* agrees on, else ``""``."""
    found = {
        info.encoding
        for name in [*resolver.all_entity_labels(), *resolver.all_relationship_types()]
        if (info := resolver.resolve_properties(name).get(prop)) is not None
    }
    return found.pop() if len(found) == 1 else ""


# --- Durations and temporal arithmetic ------------------------------------

#: duration({…}) fields → (months, milliseconds) per unit. Months and years
#: are calendar units; days and finer are fixed lengths, exact in UTC.
_DURATION_FIELDS: dict[str, tuple[int, float]] = {
    "years": (12, 0),
    "quarters": (3, 0),
    "months": (1, 0),
    "weeks": (0, 7 * 86_400_000),
    "days": (0, 86_400_000),
    "hours": (0, 3_600_000),
    "minutes": (0, 60_000),
    "seconds": (0, 1_000),
    "milliseconds": (0, 1),
    "microseconds": (0, 0.001),
    "nanoseconds": (0, 0.000001),
}


def duration_fields(
    args: list[Any], *, compile_expr: Callable[[Any], str]
) -> tuple[str | None, str | None, list[str]]:
    """``duration({…})`` as ``(months, milliseconds)`` AQL amounts (None when
    absent) and the field values (null makes the result null), or refuse a
    form that is not a map literal."""
    fields = map_literal(args[0]) if len(args) == 1 else None
    if fields is None:
        raise CoreError(
            "duration() of a map literal is supported (duration({days: 1, hours: 2})); a duration "
            "string or a duration held in a variable is not yet",
            code="NOT_IMPLEMENTED",
        )
    unknown = sorted(set(fields) - set(_DURATION_FIELDS))
    if unknown:
        raise CoreError(f"duration({{…}}) with {', '.join(unknown)} is not supported", code="UNSUPPORTED")
    months: list[str] = []
    millis: list[str] = []
    values: list[str] = []
    for name, value in fields.items():
        per_month, per_ms = _DURATION_FIELDS[name]
        compiled = compile_expr(value)
        values.append(compiled)
        amount = f"({compiled})"
        if per_month:
            months.append(amount if per_month == 1 else f"{amount} * {per_month}")
        else:
            factor = int(per_ms) if per_ms == int(per_ms) else per_ms
            millis.append(amount if factor == 1 else f"{amount} * {factor}")
    return (" + ".join(months) or None), (" + ".join(millis) or None), values


def compile_duration(args: list[Any], *, compile_expr: Callable[[Any], str]) -> str:
    """``duration({…})`` as a value: its ISO-8601 duration (``P3M``, ``PT36H``)."""
    months, millis, values = duration_fields(args, compile_expr=compile_expr)
    parts = ['"P"']
    if months is not None:
        parts += [f"({months})", '"M"']
    if millis is not None:
        parts += ['"T"', f"(({millis}) / 1000)", '"S"']
    elif months is None:
        parts += ['"T0S"']
    iso = f"CONCAT({', '.join(parts)})"
    return _three_valued(iso, iso, tuple(values), None) if values else iso


def add_duration(value: str, op: str, args: list[Any], *, compile_expr: Callable[[Any], str]) -> str:
    """``value ± duration({…})``: months first (the day clamped to the target
    month, as Neo4j does), then the fixed-length part in milliseconds."""
    months, millis, values = duration_fields(args, compile_expr=compile_expr)
    sign = "" if op == "+" else "-"
    result = value
    if months is not None:
        result = _add_months(result, f"{sign}({months})")
    if millis is not None:
        result = f'DATE_ADD({result}, {sign}({millis}), "millisecond")'
    return _three_valued(result, result, (value, *values), None)


def _add_months(value: str, months: str) -> str:
    first = f'DATE_ADD(DATE_ISO8601(DATE_YEAR({value}), DATE_MONTH({value}), 1), {months}, "month")'
    day = f"MIN([DATE_DAY({value}), DATE_DAYS_IN_MONTH({first})])"
    time = ", ".join(f"DATE_{u}({value})" for u in ("HOUR", "MINUTE", "SECOND", "MILLISECOND"))
    return f"DATE_ISO8601(DATE_YEAR({first}), DATE_MONTH({first}), {day}, {time})"


def compile_temporal_arithmetic(
    terms: list[Any], ops: list[str], *, compile_expr: Callable[[Any], str]
) -> str:
    """``t ± duration({…}) ± …`` and ``duration({…}) + t`` (module docstring)."""
    kinds = [temporal_operand(t) for t in terms]
    if all(k is not None and k[0] == "duration" for k in kinds):
        raise CoreError("Adding or subtracting durations is not supported yet", code="NOT_IMPLEMENTED")
    first_kind = kinds[0]
    if first_kind is not None and first_kind[0] == "duration":
        if not ops or ops[0] != "+" or len(terms) < 2:
            raise CoreError("A duration can only be added to a temporal value", code="UNSUPPORTED")
        terms = [terms[1], terms[0], *terms[2:]]
    result = compile_expr(terms[0])
    for op, term in zip(ops, terms[1:], strict=True):
        kind = temporal_operand(term)
        if kind is None or kind[0] != "duration":
            raise CoreError(
                "Temporal arithmetic other than value ± duration({…}) is not supported yet",
                code="NOT_IMPLEMENTED",
            )
        result = add_duration(result, op, _duration_args(term), compile_expr=compile_expr)
    return result


def _duration_args(ctx: Any) -> list[Any]:
    """The argument list of the ``duration(…)`` call *ctx* is."""
    while not isinstance(ctx, CypherParser.OC_AtomContext):
        nxt = (
            ctx.oC_Atom()
            if isinstance(ctx, CypherParser.OC_PropertyOrLabelsExpressionContext)
            else _single_child(ctx)
        )
        if nxt is None:
            raise CoreError(
                "Only a duration({…}) call can be added to a temporal value", code="NOT_IMPLEMENTED"
            )
        ctx = nxt
    if ctx.oC_ParenthesizedExpression() is not None:
        return _duration_args(ctx.oC_ParenthesizedExpression().oC_Expression())
    fn = ctx.oC_FunctionInvocation()
    if fn is None:
        raise CoreError("Only a duration({…}) call can be added to a temporal value", code="NOT_IMPLEMENTED")
    return list(fn.oC_Expression() or [])


# --- Comparisons ---------------------------------------------------------


def comparison_operands(left_ctx: Any, left: str, right_ctx: Any, right: str) -> tuple[str, str]:
    """Operands of a comparison, read on one time line when one side is a
    temporal value the query computes (an ISO string in UTC) and the other is not.

    A stored ISO string's offset form, or an epoch number, does not compare
    with a computed ISO string as text, so both sides become epoch
    milliseconds. A property the mapping declares ``epoch_ms`` already is one,
    and keeps its plain form, which an index can serve.
    """
    lk, rk = temporal_operand(left_ctx), temporal_operand(right_ctx)
    lc = lk is not None and lk[0] == "computed"
    rc = rk is not None and rk[0] == "computed"
    if lc == rc:
        return left, right
    return _on_time_line(left, lk), _on_time_line(right, rk)


#: A value ``const_eval`` folded (``@cv0``) — the only computed temporal that
#: can print with a zone name (the run-time lowering emits AQL ISO strings).
_FOLDED_BIND = re.compile(r"@cv\d+")


def _on_time_line(operand: str, kind: tuple[str, str] | None) -> str:
    """*operand* as epoch milliseconds.

    A folded DateTime in a named zone prints as ``…+01:00[Europe/Berlin]``
    (Neo4j's form), and AQL's ``DATE_TIMESTAMP`` reads that suffix as
    invalid: null, and a filter that silently matches nothing. The offset
    already fixes the instant, so a folded value's zone name is dropped first
    (once per query: it is a bind variable). Other operands keep the plain
    form; a stored value carries a zone name only if a query wrote one — a
    known gap (PRD §8, temporal values).
    """
    if kind is not None and kind == ("property", "epoch_ms"):
        return operand
    if _FOLDED_BIND.fullmatch(operand.strip()):
        # Only a string can carry a zone name; an epoch number passes through.
        return f'DATE_TIMESTAMP(IS_STRING({operand}) ? FIRST(SPLIT({operand}, "[")) : {operand})'
    return f"DATE_TIMESTAMP({operand})"


def _single_child(ctx: Any) -> Any | None:
    """The one operand of an expression level that applies no operator, else None."""
    for cls, getter in (
        (CypherParser.OC_ExpressionContext, "oC_OrExpression"),
        (CypherParser.OC_OrExpressionContext, "oC_XorExpression"),
        (CypherParser.OC_XorExpressionContext, "oC_AndExpression"),
        (CypherParser.OC_AndExpressionContext, "oC_NotExpression"),
        (CypherParser.OC_AddOrSubtractExpressionContext, "oC_MultiplyDivideModuloExpression"),
        (CypherParser.OC_MultiplyDivideModuloExpressionContext, "oC_PowerOfExpression"),
        (CypherParser.OC_PowerOfExpressionContext, "oC_UnaryAddOrSubtractExpression"),
    ):
        if isinstance(ctx, cls):
            parts = getattr(ctx, getter)()
            if not isinstance(parts, list):  # a rule with exactly one operand
                return parts
            return parts[0] if len(parts) == 1 else None
    if isinstance(ctx, CypherParser.OC_NotExpressionContext):
        return None if ctx.NOT() else ctx.oC_ComparisonExpression()
    if isinstance(ctx, CypherParser.OC_ComparisonExpressionContext):
        return None if ctx.oC_PartialComparisonExpression() else ctx.oC_AddOrSubtractExpression()
    if isinstance(ctx, CypherParser.OC_UnaryAddOrSubtractExpressionContext):
        if any(ctx.getChild(i).getText() == "-" for i in range(ctx.getChildCount())):
            return None
        return ctx.oC_StringListNullOperatorExpression()
    if isinstance(ctx, CypherParser.OC_StringListNullOperatorExpressionContext):
        if (
            ctx.oC_NullOperatorExpression()
            or ctx.oC_StringOperatorExpression()
            or ctx.oC_ListOperatorExpression()
        ):
            return None
        return ctx.oC_PropertyOrLabelsExpression()
    return None


def bind_with(with_ctx: CypherParser.OC_WithContext) -> tuple[dict[str, str], dict[str, str]]:
    """The temporal variables (name → ``computed`` / ``duration``) while a WITH
    compiles, and after it.

    While it compiles (its WHERE reads the new names) both the incoming and the
    projected ones count; after it only the projected ones.
    """
    incoming = dict(_temporal_vars.get())
    items_ctx = with_ctx.oC_ProjectionBody().oC_ProjectionItems()
    star = items_ctx.getText().startswith("*")
    projected: dict[str, str] = dict(incoming) if star else {}
    for item in items_ctx.oC_ProjectionItem() or []:
        expr = item.oC_Expression()
        alias = item.oC_Variable()
        name = alias.getText().strip() if alias is not None else expr.getText().strip()
        operand = temporal_operand(expr)
        if operand is not None and operand[0] in ("property", "computed"):
            projected[name] = "computed"
        elif operand is not None and operand[0] == "duration":
            projected[name] = "duration"
        else:
            projected.pop(name, None)
    return {**incoming, **projected}, projected


def compile_temporal_function(
    name: str,
    args: list[Any],
    compiled_args: list[str],
    *,
    compile_expr: Callable[[Any], str],
) -> str:
    """``datetime()``, ``localdatetime()`` and ``date()`` (module docstring)."""
    if len(args) > 1:
        raise CoreError(f"{name}() takes at most one argument", code="UNSUPPORTED")
    if not args:
        iso = "DATE_ISO8601(DATE_NOW())"
    else:
        fields = map_literal(args[0])
        if fields is None:
            iso = f"DATE_ISO8601({compiled_args[0]})"
            if name != "date":
                return _null_safe(iso, compiled_args[0])
        else:
            iso = _from_fields(name, {k: compile_expr(v) for k, v in fields.items()})
    # LEFT(null, 10) is "": a null or unreadable value stays null.
    return _null_safe(f"LEFT({iso}, 10)", iso) if name == "date" else iso


_TIME = ("hour", "minute", "second")
#: Sub-second fields, as milliseconds (values are kept to the millisecond).
_SUBSECOND = {"millisecond": "{}", "microsecond": "FLOOR({} / 1000)", "nanosecond": "FLOOR({} / 1000000)"}
#: The calendar forms of a date: which fields each takes besides ``year``.
_CALENDARS: tuple[frozenset[str], ...] = (
    frozenset({"month", "day"}),
    frozenset({"ordinalDay"}),
    frozenset({"quarter", "dayOfQuarter"}),
    frozenset({"week", "dayOfWeek"}),
)


def _from_fields(name: str, fields: dict[str, str]) -> str:
    """A temporal value from ``{epochMillis}``, ``{epochSeconds}`` or a calendar
    date (year with month/day, ordinalDay, quarter/dayOfQuarter, or ISO
    week/dayOfWeek) plus the time of day."""
    if set(fields) == {"epochMillis"}:
        e = fields["epochMillis"]
        return _null_safe(f"DATE_ISO8601({e})", e)
    if set(fields) == {"epochSeconds"}:
        s = fields["epochSeconds"]
        return _null_safe(f"DATE_ISO8601({s} * 1000)", s)
    date_fields = set(fields) - {"year", *_TIME, *_SUBSECOND}
    calendar = next((c for c in _CALENDARS if date_fields <= c), None)
    if calendar is None or "year" not in fields:
        raise CoreError(
            f"{name}({{{', '.join(fields)}}}) is not supported yet: give a year with month/day, ordinalDay, "
            "quarter/dayOfQuarter or week/dayOfWeek, and hour, minute, second, millisecond, microsecond, "
            "nanosecond; or epochMillis / epochSeconds alone",
            code="UNSUPPORTED",
        )
    if name == "date" and set(fields) & {*_TIME, *_SUBSECOND}:
        raise CoreError("date({…}) takes no time of day", code="UNSUPPORTED")
    subsecond = [template.format(fields[f]) for f, template in _SUBSECOND.items() if f in fields]
    time = [fields.get(f, "0") for f in _TIME] + [" + ".join(subsecond) if subsecond else "0"]
    year = fields["year"]
    if not date_fields - {"month", "day"}:
        return f"DATE_ISO8601({', '.join([year, fields.get('month', '1'), fields.get('day', '1'), *time])})"
    base = _calendar_date(year, fields)
    return f"DATE_ISO8601({', '.join([f'DATE_YEAR({base})', f'DATE_MONTH({base})', f'DATE_DAY({base})', *time])})"


def _calendar_date(year: str, fields: dict[str, str]) -> str:
    """The date of an ordinal, quarter or ISO-week calendar form."""
    if "ordinalDay" in fields:
        return f'DATE_ADD(DATE_ISO8601({year}, 1, 1), ({fields["ordinalDay"]}) - 1, "day")'
    if "quarter" in fields:
        first = f"DATE_ISO8601({year}, (({fields['quarter']}) - 1) * 3 + 1, 1)"
        return f'DATE_ADD({first}, ({fields.get("dayOfQuarter", "1")}) - 1, "day")'
    # ISO week 1 is the week holding 4 January; weeks start on Monday.
    jan4 = f"DATE_ISO8601({year}, 1, 4)"
    offset = (
        f"(({fields.get('week', '1')}) - 1) * 7 + (({fields.get('dayOfWeek', '1')}) - 1)"
        f" - ((DATE_DAYOFWEEK({jan4}) + 6) % 7)"
    )
    return f'DATE_ADD({jan4}, {offset}, "day")'


def map_literal(expr_ctx: Any) -> dict[str, Any] | None:
    """The fields of an expression that is a map literal, else None."""
    ctx = expr_ctx
    while ctx is not None and not isinstance(ctx, CypherParser.OC_PropertyOrLabelsExpressionContext):
        ctx = _single_child(ctx)
    if ctx is None or ctx.oC_PropertyLookup() or ctx.oC_NodeLabels() is not None:
        return None
    literal = ctx.oC_Atom().oC_Literal()
    if literal is None or literal.oC_MapLiteral() is None:
        return None
    ml = literal.oC_MapLiteral()
    keys = [k.getText().strip() for k in ml.oC_PropertyKeyName() or []]
    return dict(zip(keys, ml.oC_Expression() or [], strict=True))
