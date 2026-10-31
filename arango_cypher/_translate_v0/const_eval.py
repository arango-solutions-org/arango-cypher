"""Compile-time evaluation of constant Cypher expressions.

An expression whose inputs are all known when the query is translated —
literals, ``WITH``-bound constants, deterministic functions of those — is
evaluated here with Cypher's own semantics (``cypher_values``) and emitted as
a literal, instead of being lowered to AQL whose semantics differ (nulls inside
lists, comparisons across types, integer division, Java's number formatting,
temporal values AQL has no type for).

Folding is an optimisation with a correctness argument, never a guess: any
construct the evaluator does not model raises :class:`NotConstant` and the
expression compiles to AQL exactly as before.  A Cypher error raised while
evaluating becomes an AQL ``FAIL()`` at the expression's position, so it fires
only if the expression is evaluated (``CASE WHEN false THEN 1/0 END`` is fine).
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from types import MappingProxyType
from typing import Any

from .._antlr.CypherParser import CypherParser
from . import const_temporal
from . import cypher_functions as fns
from . import cypher_values as cv
from .cypher_values import CypherEvalError, NotConstant
from .literals import parse_integer_literal

# Temporal values and functions (date(), duration.between(), d.week, …).
const_temporal.install()

#: Cypher variables bound to translate-time constants by a ``WITH`` (name →
#: value).  ``_apply_with`` maintains it like ``_temporal_vars``.
_const_env: ContextVar[Mapping[str, Any]] = ContextVar("_const_env", default=MappingProxyType({}))

#: Off only in tests that check how a construct lowers to AQL: they use
#: literal inputs, which folding would evaluate before the lowering runs.
#: Production never changes it.  ``translate()``'s cache is not keyed on it, so
#: whoever toggles it clears that cache (the ``without_constant_folding`` fixture).
_folding_enabled = True

_MISSING = object()


@contextmanager
def constant_folding(enabled: bool) -> Iterator[None]:
    """Enable or disable compile-time folding for the translations inside (tests)."""
    global _folding_enabled
    previous, _folding_enabled = _folding_enabled, enabled
    try:
        yield
    finally:
        _folding_enabled = previous


def folding_enabled() -> bool:
    """Whether translations fold constants (the translation cache keys on
    it: the AQL differs)."""
    return _folding_enabled


def const_env() -> Mapping[str, Any]:
    return _const_env.get()


@contextmanager
def const_env_scope(env: Mapping[str, Any]) -> Iterator[None]:
    token = _const_env.set(MappingProxyType(dict(env)))
    try:
        yield
    finally:
        _const_env.reset(token)


def set_const_env(env: Mapping[str, Any]) -> None:
    _const_env.set(MappingProxyType(dict(env)))


def bind_with_constants(with_ctx: CypherParser.OC_WithContext) -> tuple[dict[str, Any], dict[str, Any]]:
    """The constant variables while a ``WITH`` compiles, and after it.

    While it compiles (its ``WHERE`` / ``ORDER BY`` read the new names) the
    incoming and the projected constants are visible; after it only the
    projected ones.  A projection is constant when its expression evaluates
    with the incoming constants; an aggregate or anything data-dependent is
    not.
    """
    incoming = dict(const_env())
    items_ctx = with_ctx.oC_ProjectionBody().oC_ProjectionItems()
    star = items_ctx.getText().startswith("*")
    projected: dict[str, Any] = dict(incoming) if star else {}
    for item in items_ctx.oC_ProjectionItem() or []:
        expr = item.oC_Expression()
        alias = item.oC_Variable()
        name = (alias.getText() if alias is not None else expr.getText()).strip().strip("`")
        try:
            projected[name] = Evaluator(incoming).eval(expr)
        except (NotConstant, CypherEvalError, RecursionError):
            projected.pop(name, None)
    return {**incoming, **projected}, projected


# ---------------------------------------------------------------------------
# Folding hook used by the expression compiler
# ---------------------------------------------------------------------------

#: Node kinds that are already emitted exactly; folding them only churns AQL.
_TRIVIAL_LITERAL = re.compile(r"^(?:-?\d+(?:\.\d+)?|true|false|null|'[^'\\]*'|\"[^\"\\]*\")$", re.IGNORECASE)


def evaluate_or_not_constant(ctx: Any, *, shadowed: set[str] | frozenset[str] = frozenset()) -> Any:
    """Evaluate *ctx* with the current constant environment.

    *shadowed* names are bound by an enclosing comprehension / quantifier /
    ``reduce`` the translator is compiling at run time, so they are not the
    ``WITH`` constants of the same name.
    """
    env = {k: v for k, v in const_env().items() if k not in shadowed}
    return Evaluator(env).eval(ctx)


def is_trivial(ctx: Any) -> bool:
    """A bare literal (scalars, or lists / maps of them), variable or
    parameter: already emitted exactly, so folding would only churn AQL."""
    text = ctx.getText().strip()
    if _TRIVIAL_LITERAL.match(text):
        return True
    if re.fullmatch(r"\$?[A-Za-z_][A-Za-z_0-9]*", text):
        return True
    return _is_plain_literal(ctx)


def _is_plain_literal(ctx: Any) -> bool:
    node = ctx
    while not isinstance(node, CypherParser.OC_LiteralContext):
        kids = _children(node)
        if len(kids) != 1 or _is_token(kids[0]):
            return False
        node = kids[0]
    if node.oC_ListLiteral() is not None:
        return all(is_trivial(e) for e in node.oC_ListLiteral().oC_Expression() or [])
    if node.oC_MapLiteral() is not None:
        return all(is_trivial(e) for e in node.oC_MapLiteral().oC_Expression() or [])
    return (
        node.oC_NumberLiteral() is not None
        or node.StringLiteral() is not None
        or (node.oC_BooleanLiteral() is not None or node.NULL() is not None)
    )


def aql_literal(value: Any, bind_vars: dict[str, Any]) -> str:
    """AQL for a folded value: numbers, booleans and null inline; strings and
    composites through a bind variable (never interpolated)."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        if abs(value) <= 2**53:
            return str(value)
    elif isinstance(value, float):
        return repr(value)
    name = _fresh_bind_name(bind_vars)
    bind_vars[name] = cv.to_json(value)
    return f"@{name}"


def aql_fail(error: CypherEvalError, bind_vars: dict[str, Any]) -> str:
    """AQL that raises *error* when — and only when — it is evaluated."""
    name = _fresh_bind_name(bind_vars)
    bind_vars[name] = str(error)
    return f"FAIL(@{name})"


def _fresh_bind_name(bind_vars: dict[str, Any]) -> str:
    n = 0
    while f"cv{n}" in bind_vars:
        n += 1
    return f"cv{n}"


def try_fold(
    ctx: Any,
    bind_vars: dict[str, Any],
    *,
    shadowed: set[str] | frozenset[str] = frozenset(),
) -> tuple[str, Any] | None:
    """``(aql, value)`` when *ctx* is a non-trivial constant expression.

    ``value`` is the folded Python value (``_MISSING`` for an expression that
    fails, whose AQL is a ``FAIL()``); ``None`` overall means "not constant —
    compile it normally".
    """
    if not _folding_enabled or is_trivial(ctx):
        return None
    try:
        value = evaluate_or_not_constant(ctx, shadowed=shadowed)
    except NotConstant:
        return None
    except CypherEvalError as e:
        if e.category == "SyntaxError":
            # A compile-time error (wrong arity, …): the expression compiler
            # rejects it at translate time, as Neo4j does.
            return None
        return aql_fail(e, bind_vars), _MISSING
    except RecursionError:
        return None
    if not cv.representable(value):
        return None
    try:
        return aql_literal(value, bind_vars), value
    except NotConstant:
        return None


def is_failure(value: Any) -> bool:
    return value is _MISSING


# ---------------------------------------------------------------------------
# The evaluator
# ---------------------------------------------------------------------------

_AGGREGATES = frozenset(
    {"count", "sum", "avg", "min", "max", "collect", "stdev", "stdevp", "percentilecont", "percentiledisc"}
)


def _children(ctx: Any) -> list[Any]:
    """Children without whitespace tokens."""
    return [c for c in (ctx.children or []) if c.getText().strip() != ""]


def _is_token(node: Any) -> bool:
    return not hasattr(node, "getRuleIndex")


def unescape_string_literal(text: str) -> str:
    """The value of a Cypher string literal, quotes included in *text*."""
    body = text[1:-1]
    out: list[str] = []
    i = 0
    simple = {"\\": "\\", "'": "'", '"': '"', "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}
    while i < len(body):
        ch = body[i]
        if ch == "\\" and i + 1 < len(body):
            nxt = body[i + 1]
            if nxt.lower() == "u":
                width = 8 if nxt == "U" and re.fullmatch(r"[0-9a-fA-F]{8}", body[i + 2 : i + 10] or "") else 4
                out.append(chr(int(body[i + 2 : i + 2 + width], 16)))
                i += 2 + width
                continue
            out.append(simple.get(nxt.lower() if nxt in "BFNRT" else nxt, nxt))
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def aql_number_literal(ctx: CypherParser.OC_NumberLiteralContext) -> str:
    """AQL for a Cypher number literal.

    Hex and octal integers are written in decimal: AQL has no octal literal
    (``0o17``, ``017``) and its hex literals stop short of 64 bits
    (``0x7FFFFFFFFFFFFFFF`` is ERR 1501). Decimals pass through unchanged.
    """
    text = ctx.getText()
    if ctx.oC_IntegerLiteral() is None or text.isdigit() and not (len(text) > 1 and text[0] == "0"):
        return text
    return str(parse_integer_literal(text))


class Evaluator:
    """Evaluates one expression tree against an environment of constants."""

    def __init__(self, env: Mapping[str, Any]) -> None:
        self.env: dict[str, Any] = dict(env)

    # -- dispatch ----------------------------------------------------------

    def eval(self, ctx: Any) -> Any:
        method = _DISPATCH.get(type(ctx))
        if method is None:
            raise NotConstant(type(ctx).__name__)
        return method(self, ctx)

    def _with_binding(self, name: str, value: Any, fn: Callable[[], Any]) -> Any:
        saved = self.env.get(name, _MISSING)
        self.env[name] = value
        try:
            return fn()
        finally:
            if saved is _MISSING:
                self.env.pop(name, None)
            else:
                self.env[name] = saved

    # -- boolean connectives ----------------------------------------------

    def _expression(self, ctx: CypherParser.OC_ExpressionContext) -> Any:
        return self.eval(ctx.oC_OrExpression())

    def _or(self, ctx: CypherParser.OC_OrExpressionContext) -> Any:
        parts = ctx.oC_XorExpression()
        if len(parts) == 1:
            return self.eval(parts[0])
        return cv.or3([self.eval(p) for p in parts])

    def _xor(self, ctx: CypherParser.OC_XorExpressionContext) -> Any:
        parts = ctx.oC_AndExpression()
        result = self.eval(parts[0])
        if len(parts) == 1:
            return result
        for p in parts[1:]:
            result = cv.xor3(result, self.eval(p))
        return result

    def _and(self, ctx: CypherParser.OC_AndExpressionContext) -> Any:
        parts = ctx.oC_NotExpression()
        if len(parts) == 1:
            return self.eval(parts[0])
        return cv.and3([self.eval(p) for p in parts])

    def _not(self, ctx: CypherParser.OC_NotExpressionContext) -> Any:
        nots = len(ctx.NOT() or [])
        value = self.eval(ctx.oC_ComparisonExpression())
        for _ in range(nots):
            value = cv.not3(cv.as_boolean(value, "NOT"))
        return value

    # -- comparison --------------------------------------------------------

    def _comparison(self, ctx: CypherParser.OC_ComparisonExpressionContext) -> Any:
        left = self.eval(ctx.oC_InExpression())
        partials = ctx.oC_PartialComparisonExpression() or []
        if not partials:
            return left
        results: list[Any] = []
        for partial in partials:
            op = _children(partial)[0].getText()
            right = self.eval(partial.oC_InExpression())
            results.append(_compare(op, left, right))
            left = right
        return results[0] if len(results) == 1 else cv.and3(results)

    def _in(self, ctx: CypherParser.OC_InExpressionContext) -> Any:
        terms = ctx.oC_AddOrSubtractExpression()
        value = self.eval(terms[0])
        for term in terms[1:]:
            value = cv.in_list(value, self.eval(term))
        return value

    # -- arithmetic --------------------------------------------------------

    def _binary_chain(self, ctx: Any, operands: list[Any], ops: dict[str, Callable[[Any, Any], Any]]) -> Any:
        children = _children(ctx)
        value = self.eval(operands[0])
        operand_index = 1
        for child in children:
            if _is_token(child) and child.getText() in ops and operand_index < len(operands):
                value = ops[child.getText()](value, self.eval(operands[operand_index]))
                operand_index += 1
        return value

    def _add_sub(self, ctx: CypherParser.OC_AddOrSubtractExpressionContext) -> Any:
        return self._binary_chain(
            ctx, ctx.oC_MultiplyDivideModuloExpression(), {"+": cv.add, "-": cv.subtract}
        )

    def _mul_div(self, ctx: CypherParser.OC_MultiplyDivideModuloExpressionContext) -> Any:
        return self._binary_chain(
            ctx,
            ctx.oC_PowerOfExpression(),
            {"*": cv.multiply, "/": cv.divide, "%": cv.modulo},
        )

    def _power(self, ctx: CypherParser.OC_PowerOfExpressionContext) -> Any:
        # Cypher's ^ is left-associative: 2 ^ 3 ^ 2 = (2 ^ 3) ^ 2.
        return self._binary_chain(ctx, ctx.oC_UnaryAddOrSubtractExpression(), {"^": cv.power})

    def _unary(self, ctx: CypherParser.OC_UnaryAddOrSubtractExpressionContext) -> Any:
        signs = [c.getText() for c in _children(ctx) if _is_token(c) and c.getText() in "+-"]
        operand = ctx.oC_StringListNullOperatorExpression()
        negative = signs.count("-") % 2 == 1
        literal = _integer_literal_text(operand)
        if literal is not None:
            # -9223372036854775808 is a valid literal although its magnitude is not.
            value = parse_integer_literal(literal)
            return cv.check_int(-value if negative else value)
        value = self.eval(operand)
        if signs and value is not None and not cv.is_number(value) and cv._kind_of(value) is None:
            raise cv.type_error(f"unary {signs[-1]} expects a number, got {cv.type_name(value)}")
        return cv.negate(value) if negative else value

    # -- string / list / null operators -----------------------------------

    def _string_list_null(self, ctx: CypherParser.OC_StringListNullOperatorExpressionContext) -> Any:
        children = _children(ctx)
        value = self.eval(children[0])
        for op in children[1:]:
            value = self._apply_postfix(value, op)
        return value

    def _apply_postfix(self, value: Any, op: Any) -> Any:
        if isinstance(op, CypherParser.OC_NullOperatorExpressionContext):
            is_null = value is None
            return not is_null if op.NOT() is not None else is_null
        if isinstance(op, CypherParser.OC_StringOperatorExpressionContext):
            other = self.eval(op.oC_PropertyOrLabelsExpression())
            text = op.getText().upper()
            if op.STARTS() is not None:
                return fns.string_predicate(value, other, str.startswith)
            if op.ENDS() is not None:
                return fns.string_predicate(value, other, str.endswith)
            if op.CONTAINS() is not None:
                return fns.string_predicate(value, other, lambda a, b: b in a)
            if text.startswith("=~"):
                return fns.regex_match(value, other)
            raise NotConstant("string operator")
        if isinstance(op, CypherParser.OC_ListOperatorExpressionContext):
            parts = _children(op)
            if any(_is_token(p) and p.getText() == ".." for p in parts):
                start = end = None
                has_start = has_end = False
                seen_range = False
                for p in parts:
                    if _is_token(p) and p.getText() == "..":
                        seen_range = True
                    elif isinstance(p, CypherParser.OC_ExpressionContext):
                        if seen_range:
                            end, has_end = self.eval(p), True
                        else:
                            start, has_start = self.eval(p), True
                return cv.slice_list(value, start, end, has_start=has_start, has_end=has_end)
            return cv.index(value, self.eval(op.oC_Expression(0)))
        raise NotConstant(type(op).__name__)

    def _property_or_labels(self, ctx: CypherParser.OC_PropertyOrLabelsExpressionContext) -> Any:
        if ctx.oC_NodeLabels() is not None:
            raise NotConstant("label predicate")
        value = self.eval(ctx.oC_Atom())
        for lookup in ctx.oC_PropertyLookup() or []:
            value = cv.get_property(value, lookup.oC_PropertyKeyName().getText().strip().strip("`"))
        return value

    # -- atoms -------------------------------------------------------------

    def _atom(self, ctx: CypherParser.OC_AtomContext) -> Any:
        for getter in (
            ctx.oC_Literal,
            ctx.oC_CaseExpression,
            ctx.oC_ListComprehension,
            ctx.oC_ReduceExpression,
            ctx.oC_ParenthesizedExpression,
            ctx.oC_FunctionInvocation,
            ctx.oC_Variable,
        ):
            child = getter()
            if child is not None:
                return self.eval(child)
        if ctx.oC_FilterExpression() is not None:
            return self._quantifier(ctx)
        raise NotConstant("atom")

    def _parenthesized(self, ctx: CypherParser.OC_ParenthesizedExpressionContext) -> Any:
        return self.eval(ctx.oC_Expression())

    def _variable(self, ctx: CypherParser.OC_VariableContext) -> Any:
        name = ctx.getText().strip().strip("`")
        if name in self.env:
            return self.env[name]
        raise NotConstant(f"variable {name}")

    def _literal(self, ctx: CypherParser.OC_LiteralContext) -> Any:
        if ctx.oC_BooleanLiteral() is not None:
            return ctx.oC_BooleanLiteral().getText().lower() == "true"
        if ctx.NULL() is not None:
            return None
        if ctx.StringLiteral() is not None:
            return unescape_string_literal(ctx.StringLiteral().getText())
        if ctx.oC_NumberLiteral() is not None:
            num = ctx.oC_NumberLiteral()
            if num.oC_IntegerLiteral() is not None:
                return cv.check_int(parse_integer_literal(num.getText()))
            value = float(num.getText())
            if math.isinf(value):
                raise CypherEvalError(
                    "SyntaxError", "FloatingPointOverflow", f"float literal overflow: {num.getText()}"
                )
            return value
        if ctx.oC_ListLiteral() is not None:
            return [self.eval(e) for e in ctx.oC_ListLiteral().oC_Expression() or []]
        if ctx.oC_MapLiteral() is not None:
            ml = ctx.oC_MapLiteral()
            keys = [k.getText().strip().strip("`") for k in ml.oC_PropertyKeyName() or []]
            values = [self.eval(v) for v in ml.oC_Expression() or []]
            return dict(zip(keys, values, strict=True))
        raise NotConstant("literal")

    def _case(self, ctx: CypherParser.OC_CaseExpressionContext) -> Any:
        children = _children(ctx)
        alternatives = ctx.oC_CaseAlternatives() or []
        subject = _MISSING
        if len(children) > 1 and isinstance(children[1], CypherParser.OC_ExpressionContext):
            subject = self.eval(children[1])
        else_expr = None
        for i, child in enumerate(children):
            if _is_token(child) and child.getText().upper() == "ELSE":
                else_expr = children[i + 1]
        for alt in alternatives:
            when, then = alt.oC_Expression(0), alt.oC_Expression(1)
            if subject is _MISSING:
                if self.eval(when) is True:
                    return self.eval(then)
            elif cv.equal(subject, self.eval(when)) is True:
                return self.eval(then)
        return self.eval(else_expr) if else_expr is not None else None

    def _iterate(self, id_in_coll: CypherParser.OC_IdInCollContext) -> tuple[str, list[Any] | None]:
        name = id_in_coll.oC_Variable().getText().strip().strip("`")
        source = self.eval(id_in_coll.oC_Expression())
        if source is not None and not isinstance(source, list):
            raise cv.type_error(f"expected a list, got {cv.type_name(source)}")
        return name, source

    def _list_comprehension(self, ctx: CypherParser.OC_ListComprehensionContext) -> Any:
        filt = ctx.oC_FilterExpression()
        name, source = self._iterate(filt.oC_IdInColl())
        if source is None:
            return None
        where = filt.oC_Where()
        projection = ctx.oC_Expression()
        out: list[Any] = []
        for item in source:

            def step(item: Any = item) -> None:
                if where is not None and self.eval(where.oC_Expression()) is not True:
                    return
                out.append(self.eval(projection) if projection is not None else item)

            self._with_binding(name, item, step)
        return out

    def _quantifier(self, ctx: CypherParser.OC_AtomContext) -> Any:
        filt = ctx.oC_FilterExpression()
        name, source = self._iterate(filt.oC_IdInColl())
        if source is None:
            return None
        where = filt.oC_Where()
        results = [
            self._with_binding(name, item, lambda: self.eval(where.oC_Expression()))
            if where is not None
            else item
            for item in source
        ]
        for r in results:
            if r is not None and not isinstance(r, bool):
                raise cv.type_error(f"quantifier predicate must be boolean, got {cv.type_name(r)}")
        trues = sum(1 for r in results if r is True)
        nulls = sum(1 for r in results if r is None)
        if ctx.ALL() is not None:
            if any(r is False for r in results):
                return False
            return None if nulls else True
        if ctx.ANY() is not None:
            if trues:
                return True
            return None if nulls else False
        if ctx.NONE() is not None:
            if trues:
                return False
            return None if nulls else True
        if ctx.SINGLE() is not None:
            if trues > 1:
                return False
            if nulls:
                return None
            return trues == 1
        raise NotConstant("quantifier")

    def _reduce(self, ctx: CypherParser.OC_ReduceExpressionContext) -> Any:
        acc_name = ctx.oC_Variable().getText().strip().strip("`")
        acc = self.eval(ctx.oC_Expression(0))
        name, source = self._iterate(ctx.oC_IdInColl())
        if source is None:
            return None
        body = ctx.oC_Expression(1)

        def step(item: Any) -> Callable[[], Any]:
            def run() -> Any:
                return self._with_binding(name, item, lambda: self.eval(body))

            return run

        for item in source:
            acc = self._with_binding(acc_name, acc, step(item))
        return acc

    def _function(self, ctx: CypherParser.OC_FunctionInvocationContext) -> Any:
        name = ctx.oC_FunctionName().getText().strip().lower()
        if name in _AGGREGATES or ctx.DISTINCT() is not None:
            raise NotConstant(f"aggregate {name}")
        arg_ctxs = ctx.oC_Expression() or []
        if const_temporal.is_clock_difference(name, [a.getText() for a in arg_ctxs]):
            # duration.between(date(), date()): one clock per statement.
            return const_temporal.ZERO_DURATION
        impl = fns.FUNCTIONS.get(name)
        if impl is None:
            raise NotConstant(f"function {name}")
        args = [self.eval(a) for a in arg_ctxs]
        return impl(args)


def _compare(op: str, left: Any, right: Any) -> Any:
    if op == "=":
        return cv.equal(left, right)
    if op == "<>":
        return cv.not3(cv.equal(left, right))
    return cv.ordering(op, left, right)


def _integer_literal_text(ctx: Any) -> str | None:
    """The text of *ctx* when it is exactly an integer literal."""
    text = ctx.getText().strip()
    if re.fullmatch(r"0[xX][0-9a-fA-F]+|0[oO]?[0-7]+|\d+", text):
        return text
    return None


_DISPATCH: dict[type, Callable[[Evaluator, Any], Any]] = {
    CypherParser.OC_ExpressionContext: Evaluator._expression,
    CypherParser.OC_OrExpressionContext: Evaluator._or,
    CypherParser.OC_XorExpressionContext: Evaluator._xor,
    CypherParser.OC_AndExpressionContext: Evaluator._and,
    CypherParser.OC_NotExpressionContext: Evaluator._not,
    CypherParser.OC_ComparisonExpressionContext: Evaluator._comparison,
    CypherParser.OC_InExpressionContext: Evaluator._in,
    CypherParser.OC_AddOrSubtractExpressionContext: Evaluator._add_sub,
    CypherParser.OC_MultiplyDivideModuloExpressionContext: Evaluator._mul_div,
    CypherParser.OC_PowerOfExpressionContext: Evaluator._power,
    CypherParser.OC_UnaryAddOrSubtractExpressionContext: Evaluator._unary,
    CypherParser.OC_StringListNullOperatorExpressionContext: Evaluator._string_list_null,
    CypherParser.OC_PropertyOrLabelsExpressionContext: Evaluator._property_or_labels,
    CypherParser.OC_AtomContext: Evaluator._atom,
    CypherParser.OC_ParenthesizedExpressionContext: Evaluator._parenthesized,
    CypherParser.OC_VariableContext: Evaluator._variable,
    CypherParser.OC_LiteralContext: Evaluator._literal,
    CypherParser.OC_CaseExpressionContext: Evaluator._case,
    CypherParser.OC_ListComprehensionContext: Evaluator._list_comprehension,
    CypherParser.OC_ReduceExpressionContext: Evaluator._reduce,
    CypherParser.OC_FunctionInvocationContext: Evaluator._function,
}


def debug_value(value: Any) -> str:
    """A readable rendering of a folded value, for warnings and tests."""
    try:
        return json.dumps(cv.to_json(value))
    except (NotConstant, TypeError, ValueError):
        return repr(value)
