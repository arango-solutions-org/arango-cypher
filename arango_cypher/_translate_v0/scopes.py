"""Which names a nested construct can see from the query around it.

An ``EXISTS { (n)-->(m) }`` pattern correlates with ``m`` only when the
enclosing query binds ``m``; otherwise ``m`` is a fresh variable of the
subquery. The parse tree answers that: a name is outer when the enclosing
statement declares it (a pattern variable, a named path, an UNWIND or
projection alias) before the subquery, outside it.
"""

from __future__ import annotations

from typing import Any

from .._antlr.CypherParser import CypherParser

_DECLARING = (
    CypherParser.OC_NodePatternContext,
    CypherParser.OC_RelationshipDetailContext,
    CypherParser.OC_PatternPartContext,
    CypherParser.OC_UnwindContext,
    CypherParser.OC_ProjectionItemContext,
)


def _statement_root(ctx: Any) -> Any:
    node = ctx
    while node.parentCtx is not None and not isinstance(node, CypherParser.OC_StatementContext):
        node = node.parentCtx
    return node


def outer_bound_names(ctx: Any) -> set[str]:
    """Names declared in the statement enclosing *ctx* before it (outside its subtree).

    A pattern later in the query that names the variable references it; it
    does not bind it for *ctx*.
    """
    names: set[str] = set()
    limit = ctx.start.tokenIndex

    def walk(node: Any) -> None:
        if node is ctx:
            return
        start = getattr(node, "start", None)
        if start is not None and start.tokenIndex >= limit:
            return
        if isinstance(node, _DECLARING):
            var = node.oC_Variable() if hasattr(node, "oC_Variable") else None
            if var is not None:
                names.add(var.getText().strip())
        for i in range(node.getChildCount() if hasattr(node, "getChildCount") else 0):
            walk(node.getChild(i))

    walk(_statement_root(ctx))
    return names


def is_referenced(name: str, declaration: Any) -> bool:
    """Whether the statement around *declaration* names *name* anywhere else."""

    def walk(node: Any) -> bool:
        if node is declaration:
            return False
        if isinstance(node, CypherParser.OC_VariableContext) and node.getText().strip() == name:
            return True
        return any(
            walk(node.getChild(i))
            for i in range(node.getChildCount() if hasattr(node, "getChildCount") else 0)
        )

    return walk(_statement_root(declaration))
