"""Expression rules of the semantic analysis pass: types the query shows.

:class:`ExpressionChecker` walks one Cypher expression in a scope (variable
name -> :class:`SType`), infers the static type of each sub-expression, and
raises :class:`CypherSemanticError` where Neo4j's semantic analysis rejects
the expression:

- ``NOT``/``AND``/``OR``/``XOR``/``WHERE`` over a value that is not a boolean;
- arithmetic on strings, booleans, lists, maps or graph elements;
- ``IN`` over a value that is not a list; subscripting a value that is neither
  a list nor a map, or a list with a non-integer;
- a property read off a number, string, boolean, list or path;
- a function given a value of a kind it never accepts (``type()`` of a node,
  ``length()`` of a relationship, ``range()`` of a float, ``toBoolean()`` of a
  list, ...);
- an integer literal outside 64 bits, a float literal outside double range;
- a pattern expression used as a value (``size((a)-->())``,
  ``RETURN (a)-->()``): as in Neo4j 5 it may only be a predicate, and
  ``COUNT { … }`` / ``EXISTS { … }`` / a pattern comprehension replace it.

Only types the query *shows* count (see :mod:`semantic_types`); anything else
is ``ANY`` and passes. The clause-level walk — scopes, patterns, projections —
is :mod:`semantic_check`, which this module calls back through
:class:`ClauseHooks` for the three expression forms that contain clauses or
patterns (``EXISTS {}``/``COUNT {}``, pattern predicates, pattern
comprehensions).
"""

from __future__ import annotations

import math
import re
from typing import Protocol

from antlr4 import ParserRuleContext

from .._antlr.CypherParser import CypherParser
from .arithmetic import _interleaved_op
from .literals import parse_integer_literal
from .naming import _strip_label_backticks
from .semantic_errors import semantic_error, type_error
from .semantic_types import (
    BOOL,
    ENTITIES,
    FLOAT,
    INT,
    KNOWN,
    LIST,
    MAP,
    NODE,
    NULL,
    NUMERIC,
    PATH,
    REL,
    STRING,
    T_ANY,
    T_BOOL,
    T_FLOAT,
    T_INT,
    T_MAP,
    T_NODE,
    T_NULL,
    T_REL,
    T_STRING,
    SType,
    element_type,
    list_literal,
    list_of,
)

Scope = dict[str, SType]

_P = CypherParser

_MAX_INT = 2**63 - 1
#: Kinds no arithmetic operator other than ``+`` accepts.
_NOT_ARITHMETIC = frozenset({STRING, BOOL, LIST, MAP, NODE, REL, PATH})
#: Kinds a property can not be read from (temporal values and points are ANY).
_NO_PROPERTIES = frozenset({INT, FLOAT, BOOL, STRING, LIST, PATH})
#: Kinds Neo4j rejects as a ``WHERE`` predicate. A list is left out: older
#: Cypher coerces a list (a pattern expression's value) to a boolean there.
_NOT_A_PREDICATE = frozenset({INT, FLOAT, STRING, MAP, NODE, REL, PATH})
_SIGNED_INT = re.compile(r"^[+-]?\d+$")


class ClauseHooks(Protocol):
    """What the expression checker needs from the clause-level checker."""

    def check_subquery_body(self, body: CypherParser.OC_SubqueryBodyContext, scope: Scope) -> None: ...

    def check_pattern_predicate(
        self, ctx: CypherParser.OC_RelationshipsPatternContext, scope: Scope
    ) -> None: ...

    def bind_comprehension_pattern(
        self, ctx: CypherParser.OC_PatternComprehensionContext, scope: Scope
    ) -> Scope: ...


def variable_name(ctx: CypherParser.OC_VariableContext) -> str:
    """The name an ``oC_Variable`` binds, without its escaping backticks."""
    return _strip_label_backticks(ctx.getText().strip())


def sole_operand(expr: ParserRuleContext, target: type[ParserRuleContext]) -> ParserRuleContext | None:
    """The *target* context *expr* only wraps (no operator on the way), else None."""
    node: ParserRuleContext = expr
    while not isinstance(node, target):
        rules = [c for c in node.getChildren() if isinstance(c, ParserRuleContext)]
        tokens = [
            c for c in node.getChildren() if not isinstance(c, ParserRuleContext) and c.getText().strip()
        ]
        if len(rules) != 1 or tokens:
            return None
        node = rules[0]
    return node


def bare_variable(expr: ParserRuleContext) -> str | None:
    """The variable *expr* is, when it is nothing but a variable."""
    atom = sole_operand(expr, _P.OC_AtomContext)
    variable = atom.oC_Variable() if atom is not None else None
    return variable_name(variable) if variable is not None else None


#: function name -> (kinds of its first argument it rejects, result type,
#: error detail, compile-time?). Compile-time rules raise Neo4j's SyntaxError
#: (``CYPHER_SEMANTIC_ERROR``); the others are errors Neo4j raises while it
#: runs the query (``CYPHER_TYPE_ERROR``).
_ARGUMENT_RULES: dict[str, tuple[frozenset[str], SType, str, bool]] = {
    "type": (KNOWN - {REL}, T_STRING, "InvalidArgumentType", True),
    "length": (frozenset({NODE, REL, INT, FLOAT, BOOL, MAP}), T_INT, "InvalidArgumentType", True),
    "size": (frozenset({NODE, REL, PATH, INT, FLOAT, BOOL}), T_INT, "InvalidArgumentType", True),
    "properties": (_NO_PROPERTIES, T_MAP, "InvalidArgumentType", True),
    "keys": (_NO_PROPERTIES, list_of(T_STRING), "InvalidArgumentType", True),
    "labels": (KNOWN - {NODE}, list_of(T_STRING), "InvalidArgumentType", True),
    "nodes": (KNOWN - {PATH}, list_of(T_NODE), "InvalidArgumentType", True),
    "relationships": (KNOWN - {PATH}, list_of(T_REL), "InvalidArgumentType", True),
    "startnode": (KNOWN - {REL}, T_NODE, "InvalidArgumentType", True),
    "endnode": (KNOWN - {REL}, T_NODE, "InvalidArgumentType", True),
    "toboolean": (frozenset({FLOAT, LIST, MAP}) | ENTITIES, T_BOOL, "InvalidArgumentValue", False),
    "tointeger": (frozenset({LIST, MAP}) | ENTITIES, T_INT, "InvalidArgumentValue", False),
    "tofloat": (frozenset({BOOL, LIST, MAP}) | ENTITIES, T_FLOAT, "InvalidArgumentValue", False),
    "tostring": (frozenset({LIST, MAP}) | ENTITIES, T_STRING, "InvalidArgumentValue", False),
}


class ExpressionChecker:
    """Infer and check the static types of Cypher expressions."""

    def __init__(self, hooks: ClauseHooks) -> None:
        self._hooks = hooks
        # >0 while a list comprehension's projection is checked once per
        # element type of a list literal: an error found there is one Neo4j
        # raises when it reaches that element at run time.
        self._per_element = 0

    # -- entry points -------------------------------------------------------

    def visit(self, ctx: ParserRuleContext | None, scope: Scope) -> SType:
        """Check *ctx* and return its static type."""
        if ctx is None:
            return T_ANY
        ctx = _skip_pass_through(ctx)
        handler = _HANDLERS.get(type(ctx))
        if handler is not None:
            return handler(self, ctx, scope)
        for child in ctx.getChildren():
            if isinstance(child, ParserRuleContext):
                self.visit(child, scope)
        return T_ANY

    def predicate(self, ctx: ParserRuleContext, scope: Scope, *, where: str) -> SType:
        """Check a ``WHERE`` predicate: it must be able to be a boolean."""
        t = self.visit(ctx, scope)
        if t.kind in _NOT_A_PREDICATE:
            raise semantic_error(
                "InvalidArgumentType",
                f"Type mismatch: {where} expected Boolean but was {t.describe()} in `{_shown(ctx)}`",
            )
        return t

    # -- boolean operators --------------------------------------------------

    def _require_boolean(self, t: SType, operator: str, ctx: ParserRuleContext) -> None:
        if t.known and t.kind != BOOL:
            raise semantic_error(
                "InvalidArgumentType",
                f"Type mismatch: {operator} expected Boolean but was {t.describe()} in `{_shown(ctx)}`",
            )

    def _boolean_chain(self, parts: list[ParserRuleContext], operator: str, scope: Scope) -> SType:
        if len(parts) == 1:
            return self.visit(parts[0], scope)
        for part in parts:
            self._require_boolean(self.visit(part, scope), operator, part)
        return T_BOOL

    def _or(self, ctx: CypherParser.OC_OrExpressionContext, scope: Scope) -> SType:
        return self._boolean_chain(ctx.oC_XorExpression(), "OR", scope)

    def _xor(self, ctx: CypherParser.OC_XorExpressionContext, scope: Scope) -> SType:
        return self._boolean_chain(ctx.oC_AndExpression(), "XOR", scope)

    def _and(self, ctx: CypherParser.OC_AndExpressionContext, scope: Scope) -> SType:
        return self._boolean_chain(ctx.oC_NotExpression(), "AND", scope)

    def _not(self, ctx: CypherParser.OC_NotExpressionContext, scope: Scope) -> SType:
        inner = ctx.oC_ComparisonExpression()
        t = self.visit(inner, scope)
        if not ctx.NOT():
            return t
        self._require_boolean(t, "NOT", inner)
        return T_BOOL

    def _comparison(self, ctx: CypherParser.OC_ComparisonExpressionContext, scope: Scope) -> SType:
        t = self.visit(ctx.oC_InExpression(), scope)
        partials = ctx.oC_PartialComparisonExpression()
        for partial in partials:
            self.visit(partial.oC_InExpression(), scope)
        return T_BOOL if partials else t

    def _in(self, ctx: CypherParser.OC_InExpressionContext, scope: Scope) -> SType:
        terms = ctx.oC_AddOrSubtractExpression()
        t = self.visit(terms[0], scope)
        for term in terms[1:]:
            t = _membership(self.visit(term, scope), term)
        return t

    # -- arithmetic ---------------------------------------------------------

    def _require_number(self, t: SType, operator: str, ctx: ParserRuleContext) -> None:
        if t.kind in _NOT_ARITHMETIC:
            raise semantic_error(
                "InvalidArgumentType",
                f"Type mismatch: `{operator}` expected Float or Integer but was {t.describe()} in `{_shown(ctx)}`",
            )

    def _add_or_subtract(self, ctx: CypherParser.OC_AddOrSubtractExpressionContext, scope: Scope) -> SType:
        terms = ctx.oC_MultiplyDivideModuloExpression()
        result = self.visit(terms[0], scope)
        for i, term in enumerate(terms[1:], start=1):
            right = self.visit(term, scope)
            if _interleaved_op(ctx, i) == "-":
                self._require_number(result, "-", ctx)
                self._require_number(right, "-", ctx)
                result = _numeric_result([result, right])
            else:
                result = self._addition(result, right, ctx)
        return result

    def _addition(self, left: SType, right: SType, ctx: ParserRuleContext) -> SType:
        kinds = {left.kind, right.kind}
        if LIST in kinds:
            return list_of(T_ANY)
        bad = (BOOL in kinds and kinds <= NUMERIC | {BOOL}) or (
            kinds & (ENTITIES | {MAP}) and left.known and right.known
        )
        if bad:
            raise semantic_error(
                "InvalidArgumentType",
                f"Type mismatch: cannot add {left.describe()} and {right.describe()} in `{_shown(ctx)}`",
            )
        if NULL in kinds:
            return T_NULL
        if STRING in kinds and kinds <= NUMERIC | {STRING}:
            return T_STRING
        return _numeric_result([left, right])

    def _multiply(self, ctx: CypherParser.OC_MultiplyDivideModuloExpressionContext, scope: Scope) -> SType:
        terms = ctx.oC_PowerOfExpression()
        types = [self.visit(term, scope) for term in terms]
        if len(types) == 1:
            return types[0]
        for i, t in enumerate(types):
            # The operator next to operand i: the one before it, or for the
            # first operand the one after it.
            self._require_number(t, _interleaved_op(ctx, max(i, 1)), ctx)
        return _numeric_result(types)

    def _power(self, ctx: CypherParser.OC_PowerOfExpressionContext, scope: Scope) -> SType:
        terms = ctx.oC_UnaryAddOrSubtractExpression()
        types = [self.visit(term, scope) for term in terms]
        if len(types) == 1:
            return types[0]
        for t in types:
            self._require_number(t, "^", ctx)
        return T_NULL if any(t.kind == NULL for t in types) else T_FLOAT

    def _unary(self, ctx: CypherParser.OC_UnaryAddOrSubtractExpressionContext, scope: Scope) -> SType:
        t = self.visit(ctx.oC_StringListNullOperatorExpression(), scope)
        signs = [c.getText() for c in ctx.getChildren() if c.getText() in ("+", "-")]
        if not signs:
            return t
        self._require_number(t, signs[0], ctx)
        return t if t.kind in NUMERIC | {NULL} else T_ANY

    # -- list, string and null operators -------------------------------------

    def _string_list_null(
        self, ctx: CypherParser.OC_StringListNullOperatorExpressionContext, scope: Scope
    ) -> SType:
        t = self.visit(ctx.oC_PropertyOrLabelsExpression(), scope)
        # ``IN`` is a level of its own (``_in``); these are subscripts,
        # slices and the string and null predicates.
        for op in ctx.getChildren():
            if not isinstance(op, ParserRuleContext):
                continue
            if isinstance(op, _P.OC_ListOperatorExpressionContext):
                t = self._list_access(t, op, scope)
            elif isinstance(op, _P.OC_StringOperatorExpressionContext):
                self.visit(op.oC_PropertyOrLabelsExpression(), scope)
                t = T_BOOL
            elif isinstance(op, _P.OC_NullOperatorExpressionContext):
                t = T_BOOL
        return t

    def _list_access(
        self, base: SType, op: CypherParser.OC_ListOperatorExpressionContext, scope: Scope
    ) -> SType:
        """A subscript ``[i]`` or slice ``[i..j]`` of *base*."""
        exprs = op.oC_Expression()
        if any(c.getText() == ".." for c in op.getChildren()):
            for expr in exprs:
                self.visit(expr, scope)
            return base if base.kind in (LIST, NULL) else T_ANY
        index_ctx = exprs[0]
        index = self.visit(index_ctx, scope)
        return self._subscript(base, index, index_ctx, op)

    def _subscript(
        self, base: SType, index: SType, index_ctx: ParserRuleContext, op: ParserRuleContext
    ) -> SType:
        if base.kind in (BOOL, INT, FLOAT, STRING, PATH):
            raise semantic_error(
                "InvalidArgumentType",
                f"Type mismatch: `[]` expected a List or a Map but was {base.describe()} in `{_shown(op)}`",
            )
        if base.kind == LIST:
            if index.known and index.kind != INT:
                raise semantic_error(
                    "InvalidArgumentType",
                    f"Type mismatch: a list index must be an Integer but was {index.describe()} in `{_shown(op)}`",
                )
            position = _int_constant(index_ctx)
            if (
                position is not None
                and base.items is not None
                and -len(base.items) <= position < len(base.items)
            ):
                return base.items[position]
            return base.elem or T_ANY
        if base.kind in (MAP, NODE, REL) and index.known and index.kind != STRING:
            raise semantic_error(
                "MapElementAccessByNonString",
                f"a map, node or relationship is subscripted by a String key, not {index.describe()}, in `{_shown(op)}`",
            )
        return T_ANY

    def _property_or_labels(
        self, ctx: CypherParser.OC_PropertyOrLabelsExpressionContext, scope: Scope
    ) -> SType:
        t = self.visit(ctx.oC_Atom(), scope)
        for _ in ctx.oC_PropertyLookup():
            if t.kind in _NO_PROPERTIES:
                raise semantic_error(
                    "InvalidArgumentType",
                    f"Type mismatch: a property is read from a Map, Node, Relationship, temporal value or Point, "
                    f"not from {t.describe()}, in `{_shown(ctx)}`",
                )
            t = T_ANY
        if ctx.oC_NodeLabels() is not None:
            return T_BOOL
        return t

    # -- atoms --------------------------------------------------------------

    def _atom(self, ctx: CypherParser.OC_AtomContext, scope: Scope) -> SType:
        if ctx.oC_Variable() is not None:
            return scope.get(variable_name(ctx.oC_Variable()), T_ANY)
        if ctx.oC_Literal() is not None:
            return self.visit(ctx.oC_Literal(), scope)
        if ctx.oC_FilterExpression() is not None:  # all / any / none / single
            self._quantifier(ctx.oC_FilterExpression(), scope)
            return T_BOOL
        if ctx.COUNT() is not None and ctx.oC_CountSubquery() is None:  # count(*)
            return T_INT
        if ctx.oC_ExistsSubquery() is not None:
            self._hooks.check_subquery_body(ctx.oC_ExistsSubquery().oC_SubqueryBody(), scope)
            return T_BOOL
        if ctx.oC_CountSubquery() is not None:
            self._hooks.check_subquery_body(ctx.oC_CountSubquery().oC_SubqueryBody(), scope)
            return T_INT
        if ctx.oC_RelationshipsPattern() is not None:
            _require_predicate_position(ctx)
            self._hooks.check_pattern_predicate(ctx.oC_RelationshipsPattern(), scope)
            return T_BOOL
        if ctx.oC_ParenthesizedExpression() is not None:
            return self.visit(ctx.oC_ParenthesizedExpression().oC_Expression(), scope)
        if ctx.oC_Parameter() is not None:
            return T_ANY
        for child in ctx.getChildren():
            if isinstance(child, ParserRuleContext):
                return self.visit(child, scope)
        return T_ANY

    def _literal(self, ctx: CypherParser.OC_LiteralContext, scope: Scope) -> SType:
        number = ctx.oC_NumberLiteral()
        if number is not None:
            return _number_type(number)
        if ctx.StringLiteral() is not None:
            return T_STRING
        if ctx.oC_BooleanLiteral() is not None:
            return T_BOOL
        if ctx.oC_MapLiteral() is not None:
            for value in ctx.oC_MapLiteral().oC_Expression():
                self.visit(value, scope)
            return T_MAP
        if ctx.oC_ListLiteral() is not None:
            return list_literal([self.visit(e, scope) for e in ctx.oC_ListLiteral().oC_Expression()])
        return T_NULL

    def _iteration(self, ctx: CypherParser.OC_IdInCollContext, scope: Scope) -> tuple[str, SType, Scope]:
        """Bind ``x IN list``: the variable, the list's type, the inner scope."""
        source = self.visit(ctx.oC_Expression(), scope)
        name = variable_name(ctx.oC_Variable())
        return name, source, {**scope, name: element_type(source)}

    def _quantifier(self, ctx: CypherParser.OC_FilterExpressionContext, scope: Scope) -> None:
        _, _, inner = self._iteration(ctx.oC_IdInColl(), scope)
        where = ctx.oC_Where()
        if where is not None:
            self.predicate(where.oC_Expression(), inner, where="the quantifier predicate")

    def _list_comprehension(self, ctx: CypherParser.OC_ListComprehensionContext, scope: Scope) -> SType:
        filter_ctx = ctx.oC_FilterExpression()
        name, source, inner = self._iteration(filter_ctx.oC_IdInColl(), scope)
        where = filter_ctx.oC_Where()
        if where is not None:
            self.predicate(where.oC_Expression(), inner, where="the list comprehension predicate")
        projection = ctx.oC_Expression()
        if projection is None:
            return source if source.kind == LIST else list_of(T_ANY)
        result = self.visit(projection, inner)
        if where is None and source.items:
            # Every element of a list literal reaches the projection: check it
            # against each element's own type, not only the one they share.
            self._per_element += 1
            try:
                for item in dict.fromkeys(source.items):
                    if item != inner[name]:
                        self.visit(projection, {**scope, name: item})
            finally:
                self._per_element -= 1
        return list_of(result)

    def _pattern_comprehension(self, ctx: CypherParser.OC_PatternComprehensionContext, scope: Scope) -> SType:
        inner = self._hooks.bind_comprehension_pattern(ctx, scope)
        exprs = ctx.oC_Expression()
        if ctx.WHERE() is not None:
            self.predicate(exprs[0], inner, where="the pattern comprehension predicate")
        return list_of(self.visit(exprs[-1], inner))

    def _reduce(self, ctx: CypherParser.OC_ReduceExpressionContext, scope: Scope) -> SType:
        init, body = ctx.oC_Expression()
        self.visit(init, scope)
        _, _, inner = self._iteration(ctx.oC_IdInColl(), scope)
        inner[variable_name(ctx.oC_Variable())] = T_ANY
        self.visit(body, inner)
        return T_ANY

    # -- functions ----------------------------------------------------------

    def _function(self, ctx: CypherParser.OC_FunctionInvocationContext, scope: Scope) -> SType:
        spelled = ctx.oC_FunctionName().getText().strip()
        name = spelled.lower()
        args = ctx.oC_Expression()
        types = [self.visit(arg, scope) for arg in args]
        if "." in name or not types:
            return T_ANY
        rule = _ARGUMENT_RULES.get(name)
        if rule is not None:
            rejected, result, detail, compile_time = rule
            if types[0].kind in rejected:
                message = f"{spelled}() does not accept {types[0].describe()} in `{_shown(ctx)}`"
                if compile_time and not self._per_element:
                    raise semantic_error(detail, message)
                raise type_error("InvalidArgumentValue" if self._per_element else detail, message)
            return result
        if name == "range":
            return self._range(ctx, args, types)
        if name == "collect":
            return list_of(types[0])
        if name == "count":
            return T_INT
        if name in ("head", "last"):
            first = types[0]
            if first.items:
                return first.items[0 if name == "head" else -1]
            return element_type(first)
        if name == "tail":
            return list_of(types[0].elem or T_ANY) if types[0].kind == LIST else T_ANY
        if name == "exists":
            return T_BOOL
        return T_ANY

    def _range(self, ctx: ParserRuleContext, args: list[ParserRuleContext], types: list[SType]) -> SType:
        for t in types:
            if t.known and t.kind != INT:
                raise type_error(
                    "InvalidArgumentType",
                    f"range() takes Integer arguments, not {t.describe()}, in `{_shown(ctx)}`",
                )
        if len(args) == 3 and _int_constant(args[2]) == 0:
            raise type_error("NumberOutOfRange", f"range() step must not be zero in `{_shown(ctx)}`")
        return list_of(T_INT)


#: Precedence levels that, holding one operand and no operator, are only a
#: wrapper around it. Every sub-expression is wrapped in a dozen of them.
_PASS_THROUGH = frozenset(
    {
        _P.OC_ExpressionContext,
        _P.OC_OrExpressionContext,
        _P.OC_XorExpressionContext,
        _P.OC_AndExpressionContext,
        _P.OC_NotExpressionContext,
        _P.OC_ComparisonExpressionContext,
        _P.OC_InExpressionContext,
        _P.OC_AddOrSubtractExpressionContext,
        _P.OC_MultiplyDivideModuloExpressionContext,
        _P.OC_PowerOfExpressionContext,
        _P.OC_UnaryAddOrSubtractExpressionContext,
        _P.OC_StringListNullOperatorExpressionContext,
        _P.OC_PropertyOrLabelsExpressionContext,
    }
)


def _skip_pass_through(ctx: ParserRuleContext) -> ParserRuleContext:
    """Descend through wrapper levels iteratively.

    Walking them recursively costs two Python frames a level, and a list
    literal nested 40 deep (a TCK scenario) would exceed the interpreter's
    recursion limit.
    """
    while type(ctx) in _PASS_THROUGH:
        only: ParserRuleContext | None = None
        for child in ctx.getChildren():
            if isinstance(child, ParserRuleContext):
                if only is not None:
                    return ctx
                only = child
            elif child.getText().strip():
                return ctx  # an operator or keyword token
        if only is None:
            return ctx
        ctx = only
    return ctx


def _membership(container: SType, op: ParserRuleContext) -> SType:
    """``x IN container``: the container must be able to be a list."""
    if container.known and container.kind != LIST:
        raise semantic_error(
            "InvalidArgumentType",
            f"Type mismatch: IN expected a List but was {container.describe()} in `{_shown(op)}`",
        )
    return T_BOOL


def _numeric_result(types: list[SType]) -> SType:
    kinds = {t.kind for t in types}
    if NULL in kinds:
        return T_NULL
    if kinds == {INT}:
        return T_INT
    if kinds <= NUMERIC:
        return T_FLOAT
    return T_ANY


def _number_type(ctx: CypherParser.OC_NumberLiteralContext) -> SType:
    """The type of a number literal, rejecting one outside its type's range."""
    text = ctx.getText().strip()
    if ctx.oC_DoubleLiteral() is not None:
        if math.isinf(float(text)):
            raise semantic_error("FloatingPointOverflow", f"the floating point number {text} is too large")
        return T_FLOAT
    value = parse_integer_literal(text)
    limit = _MAX_INT + 1 if _negated(ctx) else _MAX_INT
    if value > limit:
        sign = "-" if _negated(ctx) else ""
        raise semantic_error("IntegerOverflow", f"integer {sign}{text} is too large for a 64-bit integer")
    return T_INT


#: Rule contexts between a number literal and the unary sign applied to it.
_LITERAL_WRAPPERS = (
    _P.OC_LiteralContext,
    _P.OC_AtomContext,
    _P.OC_PropertyOrLabelsExpressionContext,
    _P.OC_StringListNullOperatorExpressionContext,
)


def _negated(number: CypherParser.OC_NumberLiteralContext) -> bool:
    """Whether a unary minus applies directly to *number* (``-9223372036854775808``)."""
    node = number.parentCtx
    while isinstance(node, _LITERAL_WRAPPERS):
        if node.getChildCount() != 1:
            return False
        node = node.parentCtx
    if not isinstance(node, _P.OC_UnaryAddOrSubtractExpressionContext):
        return False
    return sum(1 for c in node.getChildren() if c.getText() == "-") % 2 == 1


def _int_constant(ctx: ParserRuleContext) -> int | None:
    """The value of an expression that is a decimal integer literal, else None."""
    text = "".join(ctx.getText().split())
    return int(text) if _SIGNED_INT.match(text) else None


#: Levels whose single operand passes its value through unchanged.
_TRANSPARENT_LEVELS = (
    _P.OC_AtomContext,
    _P.OC_ParenthesizedExpressionContext,
    _P.OC_PowerOfExpressionContext,
    _P.OC_MultiplyDivideModuloExpressionContext,
    _P.OC_AddOrSubtractExpressionContext,
    _P.OC_InExpressionContext,
)
_BOOLEAN_LEVELS = (_P.OC_AndExpressionContext, _P.OC_XorExpressionContext, _P.OC_OrExpressionContext)


def _require_predicate_position(atom: CypherParser.OC_AtomContext) -> None:
    """A pattern expression (``(a)-->()``) is only valid as a predicate, as in
    Neo4j 5: a ``WHERE`` condition, an operand of ``AND`` / ``OR`` / ``XOR`` /
    ``NOT``, a ``CASE WHEN`` condition, or the argument of ``exists()``.
    Anywhere else its value would be used (``size((a)-->())``,
    ``RETURN (a)-->()``), which Neo4j 5 rejects with ``UnexpectedSyntax``."""
    if not _in_predicate_position(atom):
        pattern = _source(atom.oC_RelationshipsPattern())
        raise semantic_error(
            "UnexpectedSyntax",
            f"the pattern expression {pattern} is used as a value; a pattern expression can only be a "
            f"predicate (WHERE {pattern}). Use COUNT {{ {pattern} }} to count its matches, "
            f"EXISTS {{ {pattern} }} to test for one, or a pattern comprehension "
            f"[p = {pattern} | p] to collect them",
        )


def _in_predicate_position(atom: ParserRuleContext) -> bool:
    node: ParserRuleContext = atom
    while True:
        parent = node.parentCtx
        if parent is None:
            return False
        if isinstance(parent, _BOOLEAN_LEVELS):
            if len([c for c in parent.getChildren() if isinstance(c, ParserRuleContext)]) > 1:
                return True
        elif isinstance(parent, _P.OC_NotExpressionContext):
            if parent.NOT():
                return True
        elif isinstance(parent, _P.OC_ComparisonExpressionContext):
            if parent.oC_PartialComparisonExpression():
                return False
        elif isinstance(parent, _P.OC_UnaryAddOrSubtractExpressionContext):
            if any(
                not isinstance(c, ParserRuleContext) and c.getText() in "+-" for c in parent.getChildren()
            ):
                return False
        elif isinstance(parent, _P.OC_StringListNullOperatorExpressionContext):
            if (
                parent.oC_NullOperatorExpression()
                or parent.oC_StringOperatorExpression()
                or parent.oC_ListOperatorExpression()
            ):
                return False
        elif isinstance(parent, _P.OC_PropertyOrLabelsExpressionContext):
            if parent.oC_PropertyLookup() or parent.oC_NodeLabels() is not None:
                return False
        elif isinstance(parent, _TRANSPARENT_LEVELS):
            if len([c for c in parent.getChildren() if isinstance(c, ParserRuleContext)]) > 1:
                return False
        elif isinstance(parent, _P.OC_ExpressionContext):
            pass
        elif isinstance(parent, _P.OC_WhereContext):
            return True
        elif isinstance(parent, _P.OC_CaseAlternativesContext):
            return node is parent.oC_Expression(0)  # WHEN <predicate> THEN <value>
        elif isinstance(parent, _P.OC_FunctionInvocationContext):
            return parent.oC_FunctionName().getText().strip().lower() == "exists"
        else:
            return False
        node = parent


def _source(ctx: ParserRuleContext) -> str:
    """The query text of *ctx* as written (spacing kept), for a message."""
    start, stop = ctx.start, ctx.stop
    if start is None or stop is None:
        return _shown(ctx)
    return start.getInputStream().getText(start.start, stop.stop).strip()


def _shown(ctx: ParserRuleContext) -> str:
    """The query text of *ctx* for an error message, shortened."""
    text = " ".join(ctx.getText().split())
    return text if len(text) <= 80 else text[:77] + "..."


_HANDLERS = {
    _P.OC_ExpressionContext: lambda self, ctx, scope: self.visit(ctx.oC_OrExpression(), scope),
    _P.OC_OrExpressionContext: ExpressionChecker._or,
    _P.OC_XorExpressionContext: ExpressionChecker._xor,
    _P.OC_AndExpressionContext: ExpressionChecker._and,
    _P.OC_NotExpressionContext: ExpressionChecker._not,
    _P.OC_ComparisonExpressionContext: ExpressionChecker._comparison,
    _P.OC_InExpressionContext: ExpressionChecker._in,
    _P.OC_AddOrSubtractExpressionContext: ExpressionChecker._add_or_subtract,
    _P.OC_MultiplyDivideModuloExpressionContext: ExpressionChecker._multiply,
    _P.OC_PowerOfExpressionContext: ExpressionChecker._power,
    _P.OC_UnaryAddOrSubtractExpressionContext: ExpressionChecker._unary,
    _P.OC_StringListNullOperatorExpressionContext: ExpressionChecker._string_list_null,
    _P.OC_PropertyOrLabelsExpressionContext: ExpressionChecker._property_or_labels,
    _P.OC_AtomContext: ExpressionChecker._atom,
    _P.OC_LiteralContext: ExpressionChecker._literal,
    _P.OC_ListComprehensionContext: ExpressionChecker._list_comprehension,
    _P.OC_PatternComprehensionContext: ExpressionChecker._pattern_comprehension,
    _P.OC_ReduceExpressionContext: ExpressionChecker._reduce,
    _P.OC_FunctionInvocationContext: ExpressionChecker._function,
}
