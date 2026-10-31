"""Semantic analysis: reject a parsed query Neo4j rejects as invalid Cypher.

The parser accepts any query the openCypher grammar allows; Neo4j then runs a
semantic analysis over the parsed query and rejects queries that parse but
mean nothing — a variable declared twice, a variable used as a node and as a
relationship, a ``WITH`` expression without a name, two ``UNION`` branches
with different columns. Without the same analysis the translator would
compile such a query and ArangoDB would run it, returning rows for a query no
Cypher engine answers.

:func:`check_semantics` walks the query once, before translation, clause by
clause, tracking which variables are in scope and what the query shows of
their type (:mod:`semantic_types`). Expressions are checked by
:mod:`semantic_expr`. A violation raises :class:`CypherSemanticError`
(``CYPHER_SEMANTIC_ERROR`` / ``CYPHER_TYPE_ERROR``), never ``UNSUPPORTED``:
the query is wrong, not beyond this translator.

Scoping follows Neo4j:

- ``MATCH``/``OPTIONAL MATCH`` may reuse a bound node or relationship (that is
  a join); reusing a name bound to another kind of value is a
  ``VariableTypeConflict``. A path variable is always new: naming a bound
  variable is ``VariableAlreadyBound``.
- ``CREATE``/``MERGE`` may use a bound node only bare, as a relationship's end;
  re-declaring it alone or with labels or properties, or re-declaring a bound
  relationship, is ``VariableAlreadyBound``.
- ``WITH`` replaces the scope with its projection (``WITH *`` keeps it);
  ``UNWIND``, ``FOREACH``, comprehensions and quantifiers bind new names.
- ``CALL { ... }`` sees only what its leading ``WITH`` imports; ``EXISTS {}``
  and ``COUNT {}`` see the whole outer scope.

The walk only ever rejects what the query *shows* to be wrong. A type it
cannot see is ``ANY``; a scope it cannot see (``YIELD *``, ``RETURN *`` from a
subquery) is open, and an open scope turns the undefined-variable check off.
"""

from __future__ import annotations

from collections.abc import Iterator

from antlr4 import ParserRuleContext

from .._antlr.CypherParser import CypherParser
from .semantic_aggregation import check_aggregation, contains_aggregate
from .semantic_errors import semantic_error
from .semantic_expr import ExpressionChecker, Scope, bare_variable, sole_operand, variable_name
from .semantic_types import (
    LIST,
    NODE,
    REL,
    T_ANY,
    T_NODE,
    T_PATH,
    T_REL,
    SType,
    element_type,
    list_of,
)
from .sort_keys import record_sort_type

_P = CypherParser

#: A scope key no Cypher variable can have: the scope holds names the query
#: does not show (``YIELD *``, a subquery's ``RETURN *``).
_OPEN = "*"

_CLAUSES = (_P.OC_ReadingClauseContext, _P.OC_UpdatingClauseContext, _P.OC_WithContext, _P.OC_ReturnContext)


def check_semantics(tree: CypherParser.OC_CypherContext) -> None:
    """Raise :class:`CypherSemanticError` if Neo4j would reject *tree* as invalid."""
    regular = tree.oC_Statement().oC_Query().oC_RegularQuery()
    if regular is None:  # a standalone CALL: nothing to scope
        return
    _QueryChecker().regular_query(regular, imports=None)


class _Columns:
    """What a query part returns: named columns, ``RETURN *``, or nothing.

    ``names`` is ``None`` when there is no ``RETURN``. ``comparable`` is false
    when a column is named by its expression's text rather than by an alias
    or a variable: such a column is not compared across a ``UNION``.
    """

    def __init__(self, names: list[str] | None, *, star: bool = False, comparable: bool = True) -> None:
        self.names = names
        self.star = star
        self.comparable = comparable


class _QueryChecker:
    """The clause-level walk; also the :class:`ClauseHooks` of the expression checker."""

    def __init__(self) -> None:
        self.expr = ExpressionChecker(self)

    # -- queries ------------------------------------------------------------

    def regular_query(self, ctx: CypherParser.OC_RegularQueryContext, *, imports: Scope | None) -> _Columns:
        """Check a query, its UNION branches included; return its columns.

        *imports* is the outer scope of a ``CALL { ... }`` body, which only its
        leading ``WITH`` may read; ``None`` for a top-level query.
        """
        columns = self.single_query(ctx.oC_SingleQuery(), imports=imports)
        unions = ctx.oC_Union()
        if not unions:
            return columns
        if len({u.ALL() is not None for u in unions}) > 1:
            raise semantic_error(
                "InvalidClauseComposition", "UNION and UNION ALL can not be mixed in one query"
            )
        for union in unions:
            branch = self.single_query(union.oC_SingleQuery(), imports=imports)
            _check_union_columns(columns, branch)
        return columns

    def single_query(self, ctx: ParserRuleContext, *, imports: Scope | None) -> _Columns:
        scope: Scope = {}
        columns = _Columns(None)
        for index, clause in enumerate(_clauses(ctx)):
            if isinstance(clause, _P.OC_WithContext):
                importing = imports if index == 0 and imports is not None else None
                scope = self.with_clause(clause, scope, importing=importing)
            elif isinstance(clause, _P.OC_ReturnContext):
                columns = self.return_clause(clause, scope)
            else:
                self.clause(clause, scope)
        return columns

    def check_subquery_body(self, body: CypherParser.OC_SubqueryBodyContext, scope: Scope) -> None:
        """``EXISTS { ... }`` / ``COUNT { ... }``: correlated with the whole outer scope."""
        inner = dict(scope)
        for clause in _clauses(body):
            if isinstance(clause, _P.OC_WithContext):
                inner = self.with_clause(clause, inner, importing=None)
            elif isinstance(clause, _P.OC_ReturnContext):
                self.return_clause(clause, inner)
            else:
                self.clause(clause, inner)

    # -- clauses ------------------------------------------------------------

    def clause(self, ctx: ParserRuleContext, scope: Scope) -> None:
        """Check a reading or updating clause, binding its variables into *scope*."""
        inner = ctx.getChild(0)
        if isinstance(inner, _P.OC_MatchContext):
            self.bind_pattern(inner.oC_Pattern(), scope, mode="match")
            if inner.oC_Where() is not None:
                self.expr.predicate(inner.oC_Where().oC_Expression(), scope, where="WHERE")
        elif isinstance(inner, _P.OC_UnwindContext):
            self.unwind(inner, scope)
        elif isinstance(inner, _P.OC_InQueryCallContext):
            self.procedure_call(inner, scope)
        elif isinstance(inner, _P.OC_SubqueryCallContext):
            self.subquery_call(inner, scope)
        elif isinstance(inner, _P.OC_CreateContext):
            self.bind_pattern(inner.oC_Pattern(), scope, mode="create")
        elif isinstance(inner, _P.OC_MergeContext):
            self.bind_part(inner.oC_PatternPart(), scope, mode="merge")
            for action in inner.oC_MergeAction():
                self.expr.visit(action.oC_Set(), scope)
        elif isinstance(inner, _P.OC_ForeachContext):
            self.foreach(inner, scope)
        else:  # SET, DELETE, REMOVE: expressions only
            self.expr.visit(inner, scope)

    def unwind(self, ctx: CypherParser.OC_UnwindContext, scope: Scope) -> None:
        source = self.expr.visit(ctx.oC_Expression(), scope)
        name = variable_name(ctx.oC_Variable())
        if name in scope:
            raise semantic_error(
                "VariableAlreadyBound", f"Variable `{name}` already declared (UNWIND ... AS {name})"
            )
        scope[name] = element_type(source)

    def foreach(self, ctx: CypherParser.OC_ForeachContext, scope: Scope) -> None:
        source = self.expr.visit(ctx.oC_Expression(), scope)
        inner = {**scope, variable_name(ctx.oC_Variable()): element_type(source)}
        for clause in ctx.oC_UpdatingClause():
            self.clause(clause, inner)

    def procedure_call(self, ctx: CypherParser.OC_InQueryCallContext, scope: Scope) -> None:
        """An in-query ``CALL p(args) YIELD …``: the arguments are not
        aggregates; ``YIELD`` binds new names only, each once (``YIELD *``
        is for a standalone call); without ``YIELD`` nothing is bound."""
        invocation = ctx.oC_ExplicitProcedureInvocation()
        for arg in invocation.oC_Expression() or []:
            if contains_aggregate(arg):
                raise semantic_error(
                    "InvalidAggregation", f"an aggregate in a procedure argument: {arg.getText()}"
                )
        self.expr.visit(invocation, scope)
        items = ctx.oC_YieldItems()
        if items is None:
            return
        if items.getChild(0).getText() == "*":
            raise semantic_error("UnexpectedSyntax", "YIELD * is only allowed in a standalone procedure call")
        yielded: set[str] = set()
        for item in items.oC_YieldItem():
            name = variable_name(item.oC_Variable())
            if name in scope or name in yielded:
                raise semantic_error(
                    "VariableAlreadyBound", f"Variable `{name}` already declared (YIELD ... {name})"
                )
            yielded.add(name)
            scope[name] = T_ANY
        if items.oC_Where() is not None:
            self.expr.predicate(items.oC_Where().oC_Expression(), scope, where="WHERE")

    def subquery_call(self, ctx: CypherParser.OC_SubqueryCallContext, scope: Scope) -> None:
        columns = self.regular_query(ctx.oC_RegularQuery(), imports=scope)
        if columns.star:
            scope[_OPEN] = T_ANY
        for name in columns.names or ():
            scope.setdefault(name, T_ANY)

    # -- projections --------------------------------------------------------

    def with_clause(
        self, ctx: CypherParser.OC_WithContext, scope: Scope, *, importing: Scope | None
    ) -> Scope:
        """Check a ``WITH``; return the scope after it.

        *importing* is the outer scope when this ``WITH`` opens a ``CALL { }``
        body: its items read the outer variables.
        """
        source = importing if importing is not None else scope
        projected, _ = self.projection(ctx.oC_ProjectionBody(), source, keyword="WITH")
        if ctx.oC_Where() is not None:
            self.expr.predicate(ctx.oC_Where().oC_Expression(), {**source, **projected}, where="WHERE")
        return projected

    def return_clause(self, ctx: CypherParser.OC_ReturnContext, scope: Scope) -> _Columns:
        _, columns = self.projection(ctx.oC_ProjectionBody(), scope, keyword="RETURN")
        return columns

    def projection(
        self, body: CypherParser.OC_ProjectionBodyContext, scope: Scope, *, keyword: str
    ) -> tuple[Scope, _Columns]:
        items_ctx = body.oC_ProjectionItems()
        star = items_ctx.getChild(0).getText() == "*"
        projected: Scope = dict(scope) if star else {}
        order = body.oC_Order()
        # Before the items are named: Neo4j reports an ambiguous aggregation
        # ahead of a missing WITH alias (WithOrderBy4 [20] has both).
        check_aggregation(
            items_ctx.oC_ProjectionItem(),
            order,
            columns=[_column_name(item) for item in items_ctx.oC_ProjectionItem()],
            star_keys=[name for name in scope if star and name != _OPEN],
            keyword=keyword,
        )
        # column name -> whether an AS alias gave it
        names: dict[str, bool] = {}
        all_named = True
        for item in items_ctx.oC_ProjectionItem():
            expr = item.oC_Expression()
            t = self.expr.visit(expr, scope)
            self._record_extreme_argument(expr, scope)
            name = _projection_name(item, keyword)
            aliased = item.oC_Variable() is not None
            all_named = all_named and (aliased or bare_variable(expr) is not None)
            # Two columns spelled alike conflict when an alias spells one of
            # them; two unaliased copies of one expression (``RETURN n.id,
            # n.id``) are left to the translator, which keys them apart.
            if name in names and (aliased or names[name]):
                raise semantic_error(
                    "ColumnNameConflict",
                    f"Multiple result columns with the same name `{name}` are not supported ({keyword})",
                )
            names[name] = names.get(name, False) or aliased
            projected[name] = t
        visible = {**scope, **projected}
        if order is not None:
            for sort_item in order.oC_SortItem():
                record_sort_type(
                    sort_item.oC_Expression(), self.expr.visit(sort_item.oC_Expression(), visible)
                )
        for clause, word in ((body.oC_Skip(), "SKIP"), (body.oC_Limit(), "LIMIT")):
            if clause is not None:
                self.expr.visit(clause.oC_Expression(), scope)
                _check_row_count(clause.oC_Expression(), word)
        # A column named by its expression text is compared to no other: how
        # an engine spells that text is not something to reject a UNION over.
        return projected, _Columns(list(names), star=star, comparable=all_named)

    def _record_extreme_argument(self, expr: ParserRuleContext, scope: Scope) -> None:
        """For an item that is one ``min()``/``max()`` call, note its argument's type.

        The translator orders strings and mixed types for it as Cypher does
        (``sort_keys.extreme_spec``).
        """
        call = sole_operand(expr, _P.OC_FunctionInvocationContext)
        if not isinstance(call, _P.OC_FunctionInvocationContext):
            return
        args = call.oC_Expression()
        if call.oC_FunctionName().getText().strip().lower() in ("min", "max") and len(args) == 1:
            record_sort_type(expr, self.expr.visit(args[0], scope))

    # -- patterns -----------------------------------------------------------

    def bind_pattern(self, ctx: CypherParser.OC_PatternContext, scope: Scope, *, mode: str) -> None:
        for part in ctx.oC_PatternPart():
            self.bind_part(part, scope, mode=mode)

    def bind_part(self, part: CypherParser.OC_PatternPartContext, scope: Scope, *, mode: str) -> None:
        """Bind one pattern part's variables into *scope*.

        *mode* is ``match`` (a bound node or relationship is a join),
        ``create``/``merge`` (a bound variable may only be a bare end node) or
        ``predicate`` (every variable must already be bound).
        """
        anonymous = part.oC_AnonymousPatternPart()
        element = anonymous.oC_PatternElement()
        if element is None:
            element = anonymous.oC_ShortestPathPattern().oC_PatternElement()
        self.bind_element(element, scope, mode=mode)
        if part.oC_Variable() is not None:
            self._declare_path(variable_name(part.oC_Variable()), scope)

    def bind_element(self, element: ParserRuleContext, scope: Scope, *, mode: str) -> None:
        while isinstance(element, _P.OC_PatternElementContext) and element.oC_NodePattern() is None:
            element = element.oC_PatternElement()  # ( pattern )
        chains = element.oC_PatternElementChain()
        self.bind_node(element.oC_NodePattern(), scope, mode=mode, alone=not chains)
        for chain in chains:
            self.bind_relationship(chain.oC_RelationshipPattern(), scope, mode=mode)
            self.bind_node(chain.oC_NodePattern(), scope, mode=mode, alone=False)

    def _declare_path(self, name: str, scope: Scope) -> None:
        if name in scope:
            raise semantic_error(
                "VariableAlreadyBound",
                f"Variable `{name}` already declared: a path variable must be a new name",
            )
        scope[name] = T_PATH

    def bind_node(
        self, node: CypherParser.OC_NodePatternContext, scope: Scope, *, mode: str, alone: bool
    ) -> None:
        properties = node.oC_Properties()
        if properties is not None:
            self.expr.visit(properties, scope)
        if node.oC_Variable() is None:
            return
        name = variable_name(node.oC_Variable())
        bound = scope.get(name)
        if bound is None:
            self._undeclared(name, scope, mode=mode)
            scope[name] = T_NODE
            return
        if mode in ("create", "merge") and (
            alone or node.oC_NodeLabels() is not None or properties is not None
        ):
            raise semantic_error(
                "VariableAlreadyBound",
                f"Can't {mode.upper()} node `{name}`: the variable is already declared; "
                "a bound node can only be used bare, as a relationship's end",
            )
        if bound.known and bound.kind != NODE:
            raise _type_conflict(name, bound, "Node")

    def bind_relationship(
        self, rel: CypherParser.OC_RelationshipPatternContext, scope: Scope, *, mode: str
    ) -> None:
        detail = rel.oC_RelationshipDetail()
        if detail is None:
            return
        if detail.oC_Properties() is not None:
            self.expr.visit(detail.oC_Properties(), scope)
        if detail.oC_Variable() is None:
            return
        name = variable_name(detail.oC_Variable())
        variable_length = detail.oC_RangeLiteral() is not None
        bound = scope.get(name)
        if bound is None:
            self._undeclared(name, scope, mode=mode)
            scope[name] = list_of(T_REL) if variable_length else T_REL
            return
        if mode in ("create", "merge"):
            raise semantic_error(
                "VariableAlreadyBound",
                f"Can't {mode.upper()} relationship `{name}`: the variable is already declared",
            )
        # A relationship list (``[r*]``) may be re-matched as a list; leave
        # those combinations to the translator rather than guess.
        if bound.known and bound.kind not in (REL, LIST):
            raise _type_conflict(name, bound, "Relationship")

    def _undeclared(self, name: str, scope: Scope, *, mode: str) -> None:
        if mode == "predicate" and _OPEN not in scope:
            raise semantic_error(
                "UndefinedVariable",
                f"Variable `{name}` not defined: a pattern predicate can not introduce new variables",
            )

    def check_pattern_predicate(self, ctx: CypherParser.OC_RelationshipsPatternContext, scope: Scope) -> None:
        """A pattern used as an expression: it may only mention bound variables."""
        probe = dict(scope)
        self.bind_node(ctx.oC_NodePattern(), probe, mode="predicate", alone=False)
        for chain in ctx.oC_PatternElementChain():
            self.bind_relationship(chain.oC_RelationshipPattern(), probe, mode="predicate")
            self.bind_node(chain.oC_NodePattern(), probe, mode="predicate", alone=False)

    def bind_comprehension_pattern(
        self, ctx: CypherParser.OC_PatternComprehensionContext, scope: Scope
    ) -> Scope:
        """A pattern comprehension's pattern: new variables are local to it."""
        inner = dict(scope)
        pattern = ctx.oC_RelationshipsPattern()
        self.bind_node(pattern.oC_NodePattern(), inner, mode="match", alone=False)
        for chain in pattern.oC_PatternElementChain():
            self.bind_relationship(chain.oC_RelationshipPattern(), inner, mode="match")
            self.bind_node(chain.oC_NodePattern(), inner, mode="match", alone=False)
        if ctx.oC_Variable() is not None:
            self._declare_path(variable_name(ctx.oC_Variable()), inner)
        return inner


def _clauses(ctx: ParserRuleContext) -> Iterator[ParserRuleContext]:
    """The clauses of a query part, in order, through the multi-part nesting."""
    for child in ctx.getChildren():
        if isinstance(child, _CLAUSES):
            yield child
        elif isinstance(child, (_P.OC_SinglePartQueryContext, _P.OC_MultiPartQueryContext)):
            yield from _clauses(child)


def _column_name(item: CypherParser.OC_ProjectionItemContext) -> str:
    """The column a projection item names: its alias, its variable, or its text."""
    if item.oC_Variable() is not None:
        return variable_name(item.oC_Variable())
    return bare_variable(item.oC_Expression()) or item.oC_Expression().getText().strip()


def _projection_name(item: CypherParser.OC_ProjectionItemContext, keyword: str) -> str:
    """The column a projection item names; ``WITH`` must name every expression."""
    if item.oC_Variable() is not None:
        return variable_name(item.oC_Variable())
    expr = item.oC_Expression()
    bare = bare_variable(expr)
    if bare is not None:
        return bare
    if keyword == "WITH":
        raise semantic_error(
            "NoExpressionAlias",
            f"Expression in WITH must be aliased (use AS): `{' '.join(expr.getText().split())}`",
        )
    return expr.getText().strip()


def _check_union_columns(first: _Columns, branch: _Columns) -> None:
    if first.star or branch.star or not (first.comparable and branch.comparable):
        return
    if first.names is None or branch.names is None:
        return
    if set(first.names) != set(branch.names):
        raise semantic_error(
            "DifferentColumnsInUnion",
            f"All sub queries in a UNION must have the same column names: {first.names} vs {branch.names}",
        )


def _check_row_count(expr: CypherParser.OC_ExpressionContext, keyword: str) -> None:
    """``SKIP``/``LIMIT`` given a literal must be given a non-negative integer."""
    text = "".join(expr.getText().split())
    try:
        value = float(text)
    except ValueError:
        return  # not a literal: a parameter or expression the translator checks
    if not text.lstrip("+-").isdigit():
        raise semantic_error(
            "InvalidArgumentType",
            f"{keyword}: Invalid input. '{text}' is not a valid value. Must be an Integer.",
        )
    if value < 0:
        raise semantic_error(
            "NegativeIntegerArgument",
            f"{keyword}: Invalid input. '{text}' is not a valid value. Must be a non-negative integer.",
        )


def _type_conflict(name: str, bound: SType, wanted: str) -> Exception:
    return semantic_error(
        "VariableTypeConflict",
        f"Type mismatch: `{name}` is bound to a {bound.describe()}, and is used here as a {wanted}",
    )
