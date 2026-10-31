"""The clauses of a query and the variables in scope between them, for plans.

A multi-statement plan (``arango_cypher.plans``) cuts a query between two
clauses and hands the rows across. What crosses is every variable in scope at
the cut, with what it is — a node or relationship (read again in the next
step, from its collection when the query says which), a path, or a value.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from antlr4 import ParserRuleContext
from arango_query_core import CoreError, MappingResolver

from .._antlr.CypherParser import CypherParser
from .naming import _AQL_RESERVED, _strip_label_backticks

_P = CypherParser
CLAUSE_TYPES = (
    _P.OC_ReadingClauseContext,
    _P.OC_UpdatingClauseContext,
    _P.OC_WithContext,
    _P.OC_ReturnContext,
)
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class PlanUnsupported(CoreError):
    """The query cannot be cut into plan steps (the reason says why)."""

    def __init__(self, message: str) -> None:
        super().__init__(message, code="NOT_IMPLEMENTED")


@dataclass(frozen=True)
class Binding:
    """What a variable holds: ``node``, ``relationship``, ``path`` or ``value``.

    *labels* are a node's labels or a relationship's types, as its pattern
    gave them.
    """

    kind: str
    labels: tuple[str, ...] = ()


Scope = dict[str, Binding]


def query_clauses(single_query: CypherParser.OC_SingleQueryContext) -> list[ParserRuleContext]:
    """The clauses of *single_query*, in order, through the multi-part nesting."""
    out: list[ParserRuleContext] = []

    def walk(ctx: ParserRuleContext) -> None:
        for child in ctx.getChildren():
            if isinstance(child, CLAUSE_TYPES):
                out.append(child)
            elif isinstance(child, (_P.OC_SinglePartQueryContext, _P.OC_MultiPartQueryContext)):
                walk(child)

    walk(single_query)
    return out


def _name(var: CypherParser.OC_VariableContext) -> str:
    name = var.getText().strip()
    if name.startswith("`"):
        name = name[1:-1].replace("``", "`")
    if not _IDENTIFIER.fullmatch(name) or name.lower() in _AQL_RESERVED:
        raise PlanUnsupported(
            f"variable {name!r} cannot be handed between plan steps: it is not a plain AQL name"
        )
    return name


def _pattern_bindings(ctx: ParserRuleContext, scope: Scope) -> None:
    """Bind the nodes, relationships and named path *ctx* (a pattern) declares."""

    def walk(node: ParserRuleContext) -> None:
        if isinstance(node, _P.OC_PropertiesContext):
            return  # a map, or a pattern comprehension with variables of its own
        if isinstance(node, _P.OC_PatternPartContext) and node.oC_Variable() is not None:
            scope.setdefault(_name(node.oC_Variable()), Binding("path"))
        elif isinstance(node, _P.OC_NodePatternContext) and node.oC_Variable() is not None:
            labels = tuple(
                _strip_label_backticks(nl.oC_LabelName().getText().strip())
                for nl in (node.oC_NodeLabels().oC_NodeLabel() if node.oC_NodeLabels() is not None else [])
            )
            name = _name(node.oC_Variable())
            if name not in scope or (labels and not scope[name].labels):
                scope[name] = Binding("node", labels)
        elif isinstance(node, _P.OC_RelationshipDetailContext) and node.oC_Variable() is not None:
            types_ctx = node.oC_RelationshipTypes()
            types = tuple(
                _strip_label_backticks(t.getText().strip())
                for t in (types_ctx.oC_RelTypeName() if types_ctx else [])
            )
            # A variable-length relationship is a list of relationships: a value.
            kind = "value" if node.oC_RangeLiteral() is not None else "relationship"
            scope.setdefault(_name(node.oC_Variable()), Binding(kind, types))
        for i in range(node.getChildCount()):
            child = node.getChild(i)
            if isinstance(child, ParserRuleContext):
                walk(child)

    walk(ctx)


def _projection(body: CypherParser.OC_ProjectionBodyContext, scope: Scope) -> Scope:
    items_ctx = body.oC_ProjectionItems()
    out: Scope = dict(scope) if items_ctx.getChild(0).getText() == "*" else {}
    for item in items_ctx.oC_ProjectionItem() or []:
        expr = item.oC_Expression().getText().strip()
        source = scope.get(expr)
        if item.oC_Variable() is not None:
            out[_name(item.oC_Variable())] = source or Binding("value")
        elif source is not None:
            out[expr] = source
        else:
            raise PlanUnsupported(f"WITH {expr} needs an alias to be handed between plan steps")
    return out


def scope_after(clause: ParserRuleContext, scope: Scope) -> Scope:
    """The variables in scope after *clause*, given those before it."""
    if isinstance(clause, _P.OC_WithContext):
        return _projection(clause.oC_ProjectionBody(), scope)
    if isinstance(clause, _P.OC_ReturnContext):
        return {}
    out = dict(scope)
    inner = clause.getChild(0)
    if isinstance(inner, _P.OC_MatchContext):
        _pattern_bindings(inner.oC_Pattern(), out)
    elif isinstance(inner, _P.OC_UnwindContext):
        out[_name(inner.oC_Variable())] = Binding("value")
    elif isinstance(inner, _P.OC_CreateContext):
        _pattern_bindings(inner.oC_Pattern(), out)
    elif isinstance(inner, _P.OC_MergeContext):
        _pattern_bindings(inner.oC_PatternPart(), out)
    elif isinstance(
        inner, (_P.OC_SetContext, _P.OC_DeleteContext, _P.OC_RemoveContext, _P.OC_ForeachContext)
    ):
        pass
    else:
        raise PlanUnsupported(f"{type(inner).__name__[3:-7]} cannot be part of a multi-statement plan")
    return out


def collection_of(binding: Binding, resolver: MappingResolver) -> str | None:
    """The collection a node or relationship lives in, when the mapping says so unambiguously."""
    from .core import _infer_unlabeled_collection, _pick_primary_entity_label

    try:
        if binding.kind == "node":
            if not binding.labels:
                return _infer_unlabeled_collection(resolver)
            entity = resolver.resolve_entity(_pick_primary_entity_label(list(binding.labels), resolver))
            if entity.get("style") == "UNION":
                return None  # a supertype: its members live in several collections
            name = entity.get("collectionName")
        elif binding.kind == "relationship" and len(binding.labels) == 1:
            rel = resolver.resolve_relationship(binding.labels[0])
            name = rel.get("edgeCollectionName") or rel.get("collectionName")
        else:
            return None
    except CoreError:
        return None  # ambiguous or unmapped: the step reads it by its id, and cannot write it
    return name if isinstance(name, str) and name else None
