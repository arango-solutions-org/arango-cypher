"""Cypher arithmetic and ``+`` on top of AQL.

AQL's arithmetic operators are numeric only and turn every other operand into
a number: ``null + 1`` is 1, ``-null`` is 0, ``'a' + 'b'`` is 0 and
``1 + '2'`` is 3. In Cypher an operator with a null operand is null, ``+``
also concatenates strings and lists, and dividing two integers truncates.

The translator knows an operand's type only when the query shows it (a
literal, ``count()``, ``toString()``, …; see ``_static_type``). Then ``+``
compiles to the one operation that type allows. Otherwise it compiles to a
runtime dispatch on the operand types.

A type the mapping declares for a property is not one the query shows: it is
not checked against the data, and the analyzer declares numeric properties
``"string"``. Taken at its word it turned ``n.age + 1`` into ``CONCAT`` (and
``ORDER BY n.id`` into a text sort), so it never decides what an operator
computes. ``declared_static_type`` reads it, for callers whose AQL still
checks the value at run time.
"""

from __future__ import annotations

from collections.abc import Callable
from contextvars import ContextVar
from typing import Any

from arango_query_core import MappingResolver

from .._antlr.CypherParser import CypherParser
from .state import _active_resolver
from .three_valued import _is_obvious_non_null, _three_valued

# A static type is one of these, or None when the query does not show it.
INT = "int"
FLOAT = "float"
NUMBER = "number"  # a number the query does not show to be an integer
STRING = "string"
LIST = "list"
BOOL = "bool"
MAP = "map"
NULL = "null"
_NUMERIC = {INT, FLOAT, NUMBER}

#: Whether ``_static_type`` takes a property's mapping-declared type as its
#: type (``declared_static_type``); off otherwise (module docstring).
_TRUST_DECLARED: ContextVar[bool] = ContextVar("_TRUST_DECLARED", default=False)

# Mapping property types (``properties.<name>.type``) as static types.
_DECLARED_TYPES = {
    "string": STRING,
    "integer": INT,
    "int": INT,
    "long": INT,
    "number": NUMBER,
    "float": FLOAT,
    "double": FLOAT,
    "decimal": FLOAT,
    "boolean": BOOL,
    "array": LIST,
}

_FUNCTION_TYPES = {
    **dict.fromkeys(("count", "size", "length", "tointeger", "timestamp", "sign"), INT),
    **dict.fromkeys(
        (
            "tofloat",
            "avg",
            "sqrt",
            "rand",
            "log",
            "log10",
            "exp",
            "e",
            "pi",
            "sin",
            "cos",
            "tan",
            "asin",
            "acos",
            "atan",
            "atan2",
            "cot",
            "degrees",
            "radians",
            "haversin",
        ),
        FLOAT,
    ),
    **dict.fromkeys(
        (
            "tostring",
            "toupper",
            "tolower",
            "trim",
            "ltrim",
            "rtrim",
            "substring",
            "replace",
            "left",
            "right",
            "type",
        ),
        STRING,
    ),
    **dict.fromkeys(("collect", "keys", "labels", "range", "split", "nodes", "relationships", "tail"), LIST),
    **dict.fromkeys(("exists", "isempty", "startnode", "endnode"), None),
}


def _function_type(fn: CypherParser.OC_FunctionInvocationContext) -> str | None:
    name = fn.oC_FunctionName().getText().strip().lower()
    if name == "exists":
        return BOOL
    return _FUNCTION_TYPES.get(name)


def _static_type(ctx: Any) -> str | None:
    """The type of the Cypher expression *ctx* when the query shows it, else None."""
    if isinstance(ctx, CypherParser.OC_ExpressionContext):
        return _static_type(ctx.oC_OrExpression())
    for level, children in (
        (CypherParser.OC_OrExpressionContext, "oC_XorExpression"),
        (CypherParser.OC_XorExpressionContext, "oC_AndExpression"),
        (CypherParser.OC_AndExpressionContext, "oC_NotExpression"),
    ):
        if isinstance(ctx, level):
            parts = getattr(ctx, children)()
            return _static_type(parts[0]) if len(parts) == 1 else BOOL
    if isinstance(ctx, CypherParser.OC_NotExpressionContext):
        return BOOL if ctx.NOT() else _static_type(ctx.oC_ComparisonExpression())
    if isinstance(ctx, CypherParser.OC_ComparisonExpressionContext):
        if ctx.oC_PartialComparisonExpression():
            return BOOL
        return _static_type(ctx.oC_InExpression())
    if isinstance(ctx, CypherParser.OC_InExpressionContext):
        terms = ctx.oC_AddOrSubtractExpression()
        return _static_type(terms[0]) if len(terms) == 1 else BOOL
    if isinstance(ctx, CypherParser.OC_AddOrSubtractExpressionContext):
        terms = ctx.oC_MultiplyDivideModuloExpression()
        result = _static_type(terms[0])
        for i, term in enumerate(terms[1:], start=1):
            op = _interleaved_op(ctx, i)
            result = _addition_type(result, _static_type(term)) if op == "+" else _numeric_type(result, term)
        return result
    if isinstance(ctx, CypherParser.OC_MultiplyDivideModuloExpressionContext):
        terms = ctx.oC_PowerOfExpression()
        if len(terms) == 1:
            return _static_type(terms[0])
        # Any other operand is a type error in Cypher, so the result is a number.
        return INT if {_static_type(t) for t in terms} == {INT} else NUMBER
    if isinstance(ctx, CypherParser.OC_PowerOfExpressionContext):
        terms = ctx.oC_UnaryAddOrSubtractExpression()
        return _static_type(terms[0]) if len(terms) == 1 else FLOAT
    if isinstance(ctx, CypherParser.OC_UnaryAddOrSubtractExpressionContext) and any(
        ctx.getChild(i).getText() == "-" for i in range(ctx.getChildCount())
    ):
        inner = _static_type(ctx.oC_StringListNullOperatorExpression())
        return INT if inner == INT else NUMBER
    if isinstance(ctx, CypherParser.OC_UnaryAddOrSubtractExpressionContext):
        return _static_type(ctx.oC_StringListNullOperatorExpression())
    if isinstance(ctx, CypherParser.OC_StringListNullOperatorExpressionContext):
        if ctx.oC_NullOperatorExpression() or ctx.oC_StringOperatorExpression():
            return BOOL
        list_ops = ctx.oC_ListOperatorExpression()
        if list_ops:
            # A slice of a list is a list; a subscript is an element.
            return LIST if ".." in list_ops[-1].getText() else None
        return _static_type(ctx.oC_PropertyOrLabelsExpression())
    if isinstance(ctx, CypherParser.OC_PropertyOrLabelsExpressionContext):
        lookups = ctx.oC_PropertyLookup()
        if len(lookups) == 1 and ctx.oC_NodeLabels() is None:
            if not _TRUST_DECLARED.get():
                return None
            return declared_property_type(
                lookups[0].oC_PropertyKeyName().getText().strip(), _active_resolver.get()
            )
        if lookups:
            return None
        if ctx.oC_NodeLabels() is not None:
            return BOOL
        return _static_type(ctx.oC_Atom())
    if isinstance(ctx, CypherParser.OC_AtomContext):
        return _atom_type(ctx)
    return None


def declared_static_type(ctx: Any) -> str | None:
    """``_static_type`` with mapping-declared property types taken at their word.

    Only for AQL that checks the value at run time anyway (a declared string
    sorts by code point through ``IS_STRING(x) ? TO_HEX(x) : x``).
    """
    token = _TRUST_DECLARED.set(True)
    try:
        return _static_type(ctx)
    finally:
        _TRUST_DECLARED.reset(token)


def declared_property_type(prop: str, resolver: MappingResolver | None) -> str | None:
    """The type the mapping declares for property *prop*, when it is unambiguous.

    The variable's label is not known where an expression compiles, so every
    entity and relationship that declares *prop* must agree; one undeclared
    or conflicting declaration leaves the type unknown.
    """
    if resolver is None:
        return None
    found: set[str | None] = set()
    for name in [*resolver.all_entity_labels(), *resolver.all_relationship_types()]:
        info = resolver.resolve_properties(name).get(prop)
        if info is not None:
            found.add(_DECLARED_TYPES.get(str(info.type or "").lower()))
    return found.pop() if len(found) == 1 else None


def _atom_type(ctx: CypherParser.OC_AtomContext) -> str | None:
    literal = ctx.oC_Literal()
    if literal is not None:
        if literal.oC_NumberLiteral() is not None:
            text = literal.oC_NumberLiteral().getText().lower()
            is_hex = text.startswith(("0x", "-0x"))
            return INT if is_hex or not any(c in text for c in ".e") else FLOAT
        if literal.StringLiteral() is not None:
            return STRING
        if literal.oC_BooleanLiteral() is not None:
            return BOOL
        if literal.oC_ListLiteral() is not None:
            return LIST
        if literal.oC_MapLiteral() is not None:
            return MAP
        return NULL
    if ctx.oC_ParenthesizedExpression() is not None:
        return _static_type(ctx.oC_ParenthesizedExpression().oC_Expression())
    if ctx.oC_FunctionInvocation() is not None:
        return _function_type(ctx.oC_FunctionInvocation())
    if ctx.oC_ListComprehension() is not None or ctx.oC_PatternComprehension() is not None:
        return LIST
    if ctx.oC_CountSubquery() is not None or ctx.COUNT() is not None:
        return INT
    if (
        ctx.oC_ExistsSubquery() is not None
        or ctx.oC_RelationshipsPattern() is not None
        or ctx.oC_FilterExpression() is not None
    ):
        return BOOL
    return None


def _interleaved_op(ctx: Any, index: int) -> str:
    """The operator token before the *index*-th operand of an arithmetic level."""
    seen = 0
    for i in range(ctx.getChildCount()):
        child = ctx.getChild(i)
        if hasattr(child, "getRuleIndex"):
            seen += 1
            continue
        text = child.getText().strip()
        if text and seen == index:
            return text
    raise ValueError(f"no operator before operand {index}")


def _addition_type(left: str | None, right: str | None) -> str | None:
    if left == NULL or right == NULL:
        return NULL if LIST not in (left, right) else LIST
    if left == LIST or right == LIST:
        return LIST
    if left in _NUMERIC and right in _NUMERIC:
        return INT if left == right == INT else NUMBER
    if STRING in (left, right) and {left, right} <= {STRING, *_NUMERIC}:
        return STRING
    return None


def _numeric_type(left: str | None, right_ctx: Any) -> str:
    """The type of ``left - right`` (or ``*``, ``/``, ``%``): a number."""
    return INT if left == _static_type(right_ctx) == INT else NUMBER


def _null_guarded(expr: str, operands: tuple[str, ...]) -> str:
    """*expr* as a value that is null when any operand is null."""
    return _three_valued(expr, expr, operands, None)


def _list_addition(left: str, lt: str | None, right: str, rt: str | None) -> str:
    """``list + x``: concatenation with a list, else the element appended or prepended.

    A null operand makes the result null, as for every ``+`` in Neo4j:
    ``[1] + null`` and ``null + [1]`` are null (verified on Neo4j 5.26), while
    ``[1] + [null]`` is ``[1, null]``. AQL's ``PUSH`` would append the null.
    """
    if NULL in (lt, rt):
        return "null"
    return _null_guarded(_list_addition_unguarded(left, lt, right, rt), (left, right))


def _list_addition_unguarded(left: str, lt: str | None, right: str, rt: str | None) -> str:
    if lt == LIST and rt == LIST:
        return f"APPEND({left}, {right})"
    if lt == LIST:
        if rt is not None:
            return f"PUSH({left}, {right})"
        return f"(IS_ARRAY({right}) ? APPEND({left}, {right}) : PUSH({left}, {right}))"
    if rt == LIST:
        if lt is not None:
            return f"UNSHIFT({right}, {left})"
        return f"(IS_ARRAY({left}) ? APPEND({left}, {right}) : UNSHIFT({right}, {left}))"
    return (
        f"(IS_ARRAY({left}) ? (IS_ARRAY({right}) ? APPEND({left}, {right}) : PUSH({left}, {right})) "
        f": UNSHIFT({right}, {left}))"
    )


def compile_addition(left: str, lt: str | None, right: str, rt: str | None) -> str:
    """Cypher ``left + right``, given each operand's static type (or None).

    Numbers add; a string with a string or number concatenates (AQL's
    ``CONCAT`` renders the number); a list concatenates with a list and
    otherwise takes the other operand as an element. A null operand makes the
    result null (``[1] + null`` is null in Neo4j; ``[1] + [null]`` is
    ``[1, null]``).
    """
    if LIST in (lt, rt):
        return _list_addition(left, lt, right, rt)
    if NULL in (lt, rt) and lt is not None and rt is not None:
        return "null"
    if lt in _NUMERIC and rt in _NUMERIC:
        return _null_guarded(f"{left} + {right}", (left, right))
    if STRING in (lt, rt) and {lt, rt} <= {STRING, *_NUMERIC}:
        return _null_guarded(f"CONCAT({left}, {right})", (left, right))
    if lt in _NUMERIC and rt is None:
        return _null_guarded(_number_plus_unknown(left, right, number_first=True), (left,))
    if rt in _NUMERIC and lt is None:
        return _null_guarded(_number_plus_unknown(right, left, number_first=False), (right,))
    if lt == STRING and rt is None:
        return _null_guarded(_string_plus_unknown(left, right, string_first=True), (left,))
    if rt == STRING and lt is None:
        return _null_guarded(_string_plus_unknown(right, left, string_first=False), (right,))
    if lt is None and rt is None:
        return _dispatch(left, right)
    # A boolean or map operand is a type error in Cypher; it is null here.
    return "null"


def _number_plus_unknown(number: str, other: str, *, number_first: bool) -> str:
    """A number plus an operand of unknown type: add, concatenate or append."""
    pair = (number, other) if number_first else (other, number)
    as_list = f"UNSHIFT({other}, {number})" if number_first else f"PUSH({other}, {number})"
    return (
        f"(IS_NUMBER({other}) ? ({pair[0]} + {pair[1]}) : (IS_STRING({other}) ? CONCAT({pair[0]}, {pair[1]}) "
        f": (IS_ARRAY({other}) ? {as_list} : null)))"
    )


def _string_plus_unknown(string: str, other: str, *, string_first: bool) -> str:
    """A string plus an operand of unknown type: concatenate, or add to a list."""
    pair = (string, other) if string_first else (other, string)
    as_list = f"UNSHIFT({other}, {string})" if string_first else f"PUSH({other}, {string})"
    return f"(IS_ARRAY({other}) ? {as_list} : (({other} == null) ? null : CONCAT({pair[0]}, {pair[1]})))"


def _dispatch(left: str, right: str) -> str:
    """``left + right`` decided at run time by the operand types."""
    numbers = f"IS_NUMBER({left}) AND IS_NUMBER({right})"
    lists = f"IS_ARRAY({left}) OR IS_ARRAY({right})"
    strings = (
        f"(IS_STRING({left}) AND (IS_STRING({right}) OR IS_NUMBER({right}))) "
        f"OR (IS_NUMBER({left}) AND IS_STRING({right}))"
    )
    return (
        f"(({numbers}) ? ({left} + {right}) : (({lists}) ? {_list_addition(left, None, right, None)} "
        f": (({strings}) ? CONCAT({left}, {right}) : null)))"
    )


def additive_chain_propagates_null(types: list[str | None], ops: list[str]) -> bool:
    """True when ``t0 ± t1 ± …`` is null exactly when one of its operands is.

    That holds for numeric steps and for ``+`` that the types show is a string
    concatenation; a list step (``[1] + null`` is ``[1, null]``) or a ``+``
    whose operand types are unknown does not.
    """
    result = types[0]
    for op, right in zip(ops, types[1:], strict=True):
        if op != "+":
            result = INT if result == right == INT else NUMBER
        elif result in _NUMERIC and right in _NUMERIC:
            result = INT if result == right == INT else NUMBER
        elif STRING in (result, right) and {result, right} <= {STRING, *_NUMERIC}:
            result = STRING
        else:
            return False
    return True


def raw_additive_chain(texts: list[str], types: list[str | None], ops: list[str]) -> str:
    """``t0 ± t1 ± …`` for a chain that propagates null, without null guards.

    Cypher evaluates left to right (``1 + 2 + 'a'`` is ``'3a'``); consecutive
    string steps share one ``CONCAT``.
    """
    result, result_type = texts[0], types[0]
    concat: list[str] = []
    for op, right, right_type in zip(ops, texts[1:], types[1:], strict=True):
        if op == "+" and STRING in (result_type, right_type):
            concat = concat or [result]
            concat.append(right)
            result, result_type = f"CONCAT({', '.join(concat)})", STRING
            continue
        concat = []
        result = f"({result} {op} {right})"
        result_type = INT if result_type == right_type == INT else NUMBER
    return result


def raw_arithmetic(
    left: str,
    op: str,
    right: str,
    lt: str | None,
    rt: str | None,
    *,
    declared: tuple[str | None, str | None] = (None, None),
) -> str:
    """``left <op> right`` for ``*``, ``/`` and ``%``, without null guards.

    Two integers divide with truncation toward zero, as in Cypher (``7 / 2``
    is 3; AQL gives 3.5); AQL's ``%`` already takes the sign of the dividend.
    Operands the mapping declares integers (*declared*, see
    ``declared_static_type``) truncate when both values are whole numbers at
    run time, and otherwise divide as numbers.
    """
    truncated = f"(({left} - {left} % {right}) / {right})"
    if op == "/" and lt == rt == INT:
        return truncated
    if op == "/" and {lt, declared[0]} & {INT} and {rt, declared[1]} & {INT}:
        whole = f"FLOOR({left}) == {left} AND FLOOR({right}) == {right}"
        return f"(({whole}) ? {truncated} : ({left} / {right}))"
    return f"({left} {op} {right})"


def compile_additive_chain(operands: list[tuple[str, str | None]], ops: list[str]) -> str:
    """``t0 ± t1 ± …`` whose ``+`` needs the operand types at run time.

    Folds pairwise; each step guards its own operands.
    """
    result, result_type = operands[0]
    for op, (right, right_type) in zip(ops, operands[1:], strict=True):
        if op == "+":
            result = compile_addition(result, result_type, right, right_type)
            result_type = _addition_type(result_type, right_type)
        else:
            result = _null_guarded(f"{result} {op} {right}", (result, right))
            result_type = INT if result_type == right_type == INT else NUMBER
    return result


# The operands an arithmetic expression being compiled depends on: it is null
# when one of them is. See ``null_propagating``.
_NULL_OPERANDS: ContextVar[list[str] | None] = ContextVar("_NULL_OPERANDS", default=None)


def null_propagating(build: Callable[[], str]) -> str:
    """Compile an arithmetic node that is null exactly when an operand is.

    AQL reads a null operand as 0, so the result needs a null guard. Nested
    arithmetic shares the outermost node's guard: *build* compiles the
    operands, and each one that is not itself such arithmetic registers in
    ``_NULL_OPERANDS`` (``register_null_operand``). ``(n.a * 2) + 1`` is then
    ``((n.a == null) ? null : ((n.a * 2) + 1))``.
    """
    if _NULL_OPERANDS.get() is not None:
        return build()
    operands: list[str] = []
    token = _NULL_OPERANDS.set(operands)
    try:
        raw = build()
    finally:
        _NULL_OPERANDS.reset(token)
    return _null_guarded(raw, tuple(dict.fromkeys(operands)))


def collecting_null_operands() -> bool:
    return _NULL_OPERANDS.get() is not None


def compile_null_operand(compile_: Callable[[], str]) -> str:
    """Compile an operand of enclosing arithmetic as a self-contained value.

    Arithmetic inside it (``coalesce(n.a - 1, 0)``) keeps its own guard: the
    enclosing guard would be wrong there, since coalesce() turns null into 0.
    """
    operands = _NULL_OPERANDS.get()
    token = _NULL_OPERANDS.set(None)
    try:
        text = compile_()
    finally:
        _NULL_OPERANDS.reset(token)
    if operands is not None and not _is_obvious_non_null(text):
        operands.append(text)
    return text


def is_arithmetic_transparent(ctx: Any) -> bool:
    """True when *ctx* compiles as part of the enclosing arithmetic.

    That is arithmetic that propagates null, and the single-operand levels
    and parentheses that lead to it. Anything else is an operand.
    """
    if isinstance(ctx, CypherParser.OC_ExpressionContext):
        return True
    for level, children in (
        (CypherParser.OC_OrExpressionContext, "oC_XorExpression"),
        (CypherParser.OC_XorExpressionContext, "oC_AndExpression"),
        (CypherParser.OC_AndExpressionContext, "oC_NotExpression"),
    ):
        if isinstance(ctx, level):
            return len(getattr(ctx, children)()) == 1
    if isinstance(ctx, CypherParser.OC_NotExpressionContext):
        return not ctx.NOT()
    if isinstance(ctx, CypherParser.OC_ComparisonExpressionContext):
        return not ctx.oC_PartialComparisonExpression()
    if isinstance(ctx, CypherParser.OC_InExpressionContext):
        return len(ctx.oC_AddOrSubtractExpression()) == 1
    if isinstance(ctx, CypherParser.OC_AddOrSubtractExpressionContext):
        terms = ctx.oC_MultiplyDivideModuloExpression()
        ops = [_interleaved_op(ctx, i) for i in range(1, len(terms))]
        return additive_chain_propagates_null([_static_type(t) for t in terms], ops)
    if isinstance(
        ctx,
        (
            CypherParser.OC_MultiplyDivideModuloExpressionContext,
            CypherParser.OC_PowerOfExpressionContext,
            CypherParser.OC_UnaryAddOrSubtractExpressionContext,
        ),
    ):
        return True
    if isinstance(ctx, CypherParser.OC_StringListNullOperatorExpressionContext):
        return not (
            ctx.oC_NullOperatorExpression()
            or ctx.oC_ListOperatorExpression()
            or ctx.oC_StringOperatorExpression()
        )
    if isinstance(ctx, CypherParser.OC_PropertyOrLabelsExpressionContext):
        return not ctx.oC_PropertyLookup() and ctx.oC_NodeLabels() is None
    if isinstance(ctx, CypherParser.OC_AtomContext):
        return ctx.oC_ParenthesizedExpression() is not None
    return False
