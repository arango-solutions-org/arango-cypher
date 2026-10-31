"""Cypher's three-valued logic (true / false / null) on top of AQL.

AQL's comparison and string operators are two-valued, where Cypher's are not:
``null == null`` is true in AQL and null in Cypher, ``null != 'x'`` is true in
AQL and null in Cypher, and AQL's ``NOT null`` is true. Translating them one to
one silently changes results: ``WHERE n.x = null`` keeps rows whose ``x`` is
null, and ``WHERE NOT n.name = 'Bob'`` keeps nameless rows.

An expression is compiled in one of three *modes*:

* ``None`` (a value) — the result keeps Cypher's null, e.g.
  ``((n.x == null) ? null : (n.x > 3))``. Used wherever the value is observed:
  RETURN, WITH, SET, function arguments, operands.
* ``"true"`` — the two-valued test "is true", which is all a FILTER observes
  (it drops false and null alike). ``n.id = $id`` stays ``n.id == @id``, so
  an index still serves it.
* ``"false"`` — the test "is false", reached through NOT. ``NOT n.x = 1`` is
  ``(n.x != null AND n.x != 1)``: a null ``x`` makes the comparison null, and
  NOT of null is null, so the row is dropped.

AND and OR keep the mode for their operands, and De Morgan's laws swap them
in the "false" mode. Every other expression is compiled as a value and then
tested, so the mode never leaks into an operand.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from contextvars import ContextVar
from typing import Any

from arango_query_core import CoreError

from .._antlr.CypherParser import CypherParser

_PREDICATE_MODE: ContextVar[str | None] = ContextVar("_PREDICATE_MODE", default=None)

_OBVIOUS_NON_NULL_RE = re.compile(
    r"""
    ^\s*
    (?:
        -?\d+(?:\.\d+)?           # numeric literal
        | "(?:[^"\\]|\\.)*"       # double-quoted string literal
        | '(?:[^'\\]|\\.)*'       # single-quoted string literal
        | true | false            # booleans (non-null)
    )
    \s*$
    """,
    re.VERBOSE,
)

_BIND_PARAMETER_RE = re.compile(r"^@\w+$")
_BARE_VARIABLE_RE = re.compile(r"^(?:\w+|`[^`]+`)$")

_AQL_COMPARISON = {"=": "==", "<>": "!=", "<": "<", "<=": "<=", ">": ">", ">=": ">="}
_NEGATED_COMPARISON = {"==": "!=", "!=": "==", "<": ">=", "<=": ">", ">": "<=", ">=": "<"}


def _is_obvious_non_null(expr: str) -> bool:
    """True when *expr* is a textual form that AQL evaluates as non-null.

    Used to suppress redundant ``!= null`` guards on comparison operands
    that are clearly numeric/string/boolean literals, or list or map
    literals (``[1, x]``, ``{a: 1}``).
    """
    if _OBVIOUS_NON_NULL_RE.match(expr):
        return True
    text = expr.strip()
    if len(text) < 2 or (text[0], text[-1]) not in {("[", "]"), ("{", "}")}:
        return False
    return _closing_bracket(text) == len(text) - 1


def _unquoted_chars(text: str) -> Iterator[tuple[int, str, int]]:
    """Yield ``(index, char, bracket depth)`` for each character outside string literals.

    The depth is counted before the character, so an opening bracket is at
    the depth around it and a closing one at the depth inside it.
    """
    depth = 0
    quote = ""
    escaped = False
    for i, ch in enumerate(text):
        if quote:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                quote = ""
            continue
        if ch in "'\"`":
            quote = ch
            continue
        yield i, ch, depth
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1


def _closing_bracket(text: str) -> int:
    """Index of the bracket closing ``text[0]``; -1 if none."""
    for i, ch, depth in _unquoted_chars(text):
        if ch in ")]}" and depth == 1:
            return i
    return -1


def _guard_preference(operand: str) -> int:
    """Rank the operand an equality's single null guard goes on (lower first).

    A bind parameter folds away before ArangoDB plans the query, and a guard
    on a loop variable leaves the document attribute free for an index.
    """
    operand = operand.strip()
    if _BIND_PARAMETER_RE.match(operand):
        return 0
    if _BARE_VARIABLE_RE.match(operand):
        return 1
    return 2


def _negate_mode(mode: str | None) -> str | None:
    if mode is None:
        return None
    return "false" if mode == "true" else "true"


def _wrap(expr: str) -> str:
    """Parenthesise a bare operator expression; a call or atom stays as is.

    An expression is bare when it has a space outside brackets and string
    literals (``a == b``, not ``CONTAINS(a, ' ')``).
    """
    if any(ch == " " and depth == 0 for _, ch, depth in _unquoted_chars(expr)):
        return f"({expr})"
    return expr


def _three_valued(holds: str, fails: str, operands: Sequence[str], mode: str | None) -> str:
    """Compile a predicate that is null when any of *operands* is null.

    *holds* is the bare AQL test for "true" and *fails* for "false", both
    valid only when every operand is non-null; the operands that might be
    null are guarded. A literal ``null`` operand is guarded like any other
    rather than folded away, so a parameter beside it stays in the query
    (a bind variable the query does not use is ERR 1552).
    """
    nullable = [op for op in operands if not _is_obvious_non_null(op)]
    if mode is None:
        if not nullable:
            return _wrap(holds)
        test = " OR ".join(f"{op} == null" for op in nullable)
        return f"(({test}) ? null : {_wrap(holds)})"
    body = holds if mode == "true" else fails
    if not nullable:
        return _wrap(body)
    return "(" + " AND ".join([f"{op} != null" for op in nullable] + [body]) + ")"


def compile_in(left: str, right: str, mode: str | None) -> str:
    """Cypher's ``left IN right`` in *mode*.

    It is null when the list is null; false for an empty list; null for a
    null element (of a non-empty list); true when the element is found;
    otherwise null if the list holds a null (the element might equal it) and
    false if not. AQL's ``null IN [null]`` is true and its ``5 IN [1, null]``
    false. A "true" test needs only the element found and non-null.
    """
    if mode == "true":
        return _three_valued(f"{left} IN {right}", f"{left} NOT IN {right}", (left,), "true")
    if _is_obvious_non_null(right) and right.startswith("[") and "null" not in right:
        # A literal list without null: null only for a null element.
        empty = right.replace(" ", "") == "[]"
        if mode == "false":
            return (
                "true"
                if empty
                else _three_valued(f"{left} IN {right}", f"{left} NOT IN {right}", (left,), mode)
            )
        return (
            "false" if empty else _three_valued(f"{left} IN {right}", f"{left} NOT IN {right}", (left,), None)
        )
    simple = re.compile(r"@?\w+(?:\.\w+)*")
    if mode == "false" and simple.fullmatch(left.strip()) and simple.fullmatch(right.strip()):
        # Definitely not in the list: the list is empty, or the element is
        # non-null, absent, and the list holds no null it might equal.
        return (
            f"({right} != null AND (LENGTH({right}) == 0 OR "
            f"({left} != null AND {left} NOT IN {right} AND null NOT IN {right})))"
        )
    k = len(re.findall(r"FOR _inl\d+ IN", left + right))
    lhs, rhs = left, right
    if not (simple.fullmatch(left.strip()) and simple.fullmatch(right.strip())):
        # Evaluate computed operands once rather than repeat them.
        lhs, rhs = f"_inl{k}", f"_inr{k}"
    found_or_unknown = f"({lhs} IN {rhs} ? true : null IN {rhs} ? null : false)"
    value = f"({rhs} == null ? null : LENGTH({rhs}) == 0 ? false : {lhs} == null ? null : {found_or_unknown})"
    if lhs != left:
        value = f"FIRST(FOR {lhs} IN [{left}] FOR {rhs} IN [{right}] RETURN {value})"
    if mode == "false":
        return f"({value} == false)"
    return value


def _compile_comparison(
    left: str, op: str, right: str, mode: str | None, *, compared: tuple[str, str] | None = None
) -> str:
    """Compile ``left <op> right`` (Cypher operator *op*) in *mode*.

    Cypher compares with three-valued logic: any comparison with a null
    operand is null. AQL's ``null == null`` is true and it orders null below
    every value (``null < 1950``), so each operand that might be null is
    guarded. An equality that holds needs only one guard (``a == b`` with a
    non-null ``a`` already rules out a null ``b``), placed by
    ``_guard_preference``.

    *compared* replaces the operands in the comparison itself (code-point keys
    of strings, ``TO_HEX(x)``, which is ``""`` for null); the guards still read
    *left* and *right*.
    """
    aql_op = _AQL_COMPARISON.get(op)
    if aql_op is None:
        raise CoreError(f"Unsupported comparison op: {op}", code="UNSUPPORTED")
    cl, cr = compared or (left, right)
    holds = f"{cl} {aql_op} {cr}"
    fails = f"{cl} {_NEGATED_COMPARISON[aql_op]} {cr}"
    if mode == "true" and aql_op == "==":
        nullable = [s for s in (left, right) if not _is_obvious_non_null(s)]
        if len(nullable) < 2:
            return f"({holds})"
        guard = min((left, right), key=_guard_preference)
        return f"({guard} != null AND {left} {aql_op} {right})"
    return _three_valued(holds, fails, (left, right), mode)


def _test_truth(value: str, mode: str | None, *, two_valued: bool) -> str:
    """Turn the value *value* into the test *mode* asks for.

    A FILTER drops null and false alike, so "is true" is the value itself.
    "Is false" cannot be ``NOT value`` when the value may be null (AQL's
    ``NOT null`` is true), so it is ``value == false`` unless the value is
    known to be two-valued.
    """
    if mode is None or mode == "true":
        return value
    if two_valued:
        return f"(NOT {value})"
    return f"({value} == false)"


def _negate_value(value: str, *, two_valued: bool) -> str:
    """Cypher ``NOT value`` as a value: NOT of null is null."""
    if two_valued:
        return f"(NOT {value})"
    return f"({value} == null ? null : (NOT {value}))"


def _single_term(ctx: Any) -> Any | None:
    """The only operand of an arithmetic level, or ``None`` when it has more."""
    if isinstance(ctx, CypherParser.OC_AddOrSubtractExpressionContext):
        terms = ctx.oC_MultiplyDivideModuloExpression()
    elif isinstance(ctx, CypherParser.OC_MultiplyDivideModuloExpressionContext):
        terms = ctx.oC_PowerOfExpression()
    elif isinstance(ctx, CypherParser.OC_PowerOfExpressionContext):
        terms = ctx.oC_UnaryAddOrSubtractExpression()
    else:
        return None
    return terms[0] if len(terms) == 1 else None


def _has_unary_minus(ctx: CypherParser.OC_UnaryAddOrSubtractExpressionContext) -> bool:
    return any(ctx.getChild(i).getText() == "-" for i in range(ctx.getChildCount()))


def _is_predicate_transparent(ctx: Any) -> bool:
    """True when the node's compiler honours the predicate mode itself.

    These are the boolean connectives and the comparison, plus the
    single-operand levels and parentheses between them and an operand. Any
    other node is compiled as a value and then tested.
    """
    if isinstance(
        ctx,
        (
            CypherParser.OC_ExpressionContext,
            CypherParser.OC_OrExpressionContext,
            CypherParser.OC_XorExpressionContext,
            CypherParser.OC_AndExpressionContext,
            CypherParser.OC_NotExpressionContext,
            CypherParser.OC_ComparisonExpressionContext,
            CypherParser.OC_InExpressionContext,
            CypherParser.OC_StringListNullOperatorExpressionContext,
        ),
    ):
        return True
    if _single_term(ctx) is not None:
        return True
    if isinstance(ctx, CypherParser.OC_UnaryAddOrSubtractExpressionContext):
        return not _has_unary_minus(ctx)
    if isinstance(ctx, CypherParser.OC_PropertyOrLabelsExpressionContext):
        return not ctx.oC_PropertyLookup() and ctx.oC_NodeLabels() is None
    if isinstance(ctx, CypherParser.OC_AtomContext):
        return ctx.oC_ParenthesizedExpression() is not None
    return False


def _is_two_valued(ctx: Any) -> bool:
    """True when the expression can only be true or false, never null.

    Recognises label tests, IS [NOT] NULL, pattern predicates, EXISTS, the
    list quantifiers (compiled to count tests) and boolean literals; anything
    else is assumed nullable.
    """
    if isinstance(ctx, CypherParser.OC_ExpressionContext):
        return _is_two_valued(ctx.oC_OrExpression())
    for level, children in (
        (CypherParser.OC_OrExpressionContext, "oC_XorExpression"),
        (CypherParser.OC_XorExpressionContext, "oC_AndExpression"),
        (CypherParser.OC_AndExpressionContext, "oC_NotExpression"),
    ):
        if isinstance(ctx, level):
            parts = getattr(ctx, children)()
            return len(parts) == 1 and _is_two_valued(parts[0])
    if isinstance(ctx, CypherParser.OC_NotExpressionContext):
        return _is_two_valued(ctx.oC_ComparisonExpression())
    if isinstance(ctx, CypherParser.OC_ComparisonExpressionContext):
        if ctx.oC_PartialComparisonExpression():
            return False
        return _is_two_valued(ctx.oC_InExpression())
    if isinstance(ctx, CypherParser.OC_InExpressionContext):
        terms = ctx.oC_AddOrSubtractExpression()
        return len(terms) == 1 and _is_two_valued(terms[0])
    term = _single_term(ctx)
    if term is not None:
        return _is_two_valued(term)
    if isinstance(ctx, CypherParser.OC_UnaryAddOrSubtractExpressionContext):
        return not _has_unary_minus(ctx) and _is_two_valued(ctx.oC_StringListNullOperatorExpression())
    if isinstance(ctx, CypherParser.OC_StringListNullOperatorExpressionContext):
        if ctx.oC_NullOperatorExpression():
            return True
        if ctx.oC_ListOperatorExpression() or ctx.oC_StringOperatorExpression():
            return False
        return _is_two_valued(ctx.oC_PropertyOrLabelsExpression())
    if isinstance(ctx, CypherParser.OC_PropertyOrLabelsExpressionContext):
        if ctx.oC_PropertyLookup():
            return False
        if ctx.oC_NodeLabels() is not None:
            return True
        return _is_two_valued(ctx.oC_Atom())
    if isinstance(ctx, CypherParser.OC_AtomContext):
        if ctx.oC_RelationshipsPattern() is not None or ctx.oC_ExistsSubquery() is not None:
            return True
        if ctx.oC_FilterExpression() is not None:
            return True
        literal = ctx.oC_Literal()
        if literal is not None:
            return literal.oC_BooleanLiteral() is not None
        fn = ctx.oC_FunctionInvocation()
        if fn is not None:
            return fn.oC_FunctionName().getText().strip().lower() == "exists"
    return False
