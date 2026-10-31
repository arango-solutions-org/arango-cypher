"""``x IN list[0]`` is ``x IN (list[0])``.

The openCypher 9 grammar chains list operators left to right, so
``3 IN list[0]`` parses as ``(3 IN list)[0]``: a subscript of a boolean, a
TypeError (List5). In Cypher a subscript or slice binds tighter than ``IN``.
The query text is re-parenthesised before translation — ``IN`` followed by
subscripts or slices gets its operand and those operators wrapped — and
parsed again.
"""

from __future__ import annotations

from typing import Any

from .._antlr.CypherParser import CypherParser


def _edits(node: Any, out: list[tuple[int, str]]) -> None:
    if isinstance(node, CypherParser.OC_StringListNullOperatorExpressionContext):
        ops = [
            child
            for child in node.getChildren()
            if isinstance(
                child,
                (
                    CypherParser.OC_ListOperatorExpressionContext,
                    CypherParser.OC_StringOperatorExpressionContext,
                    CypherParser.OC_NullOperatorExpressionContext,
                ),
            )
        ]
        i = 0
        while i < len(ops):
            op = ops[i]
            if isinstance(op, CypherParser.OC_ListOperatorExpressionContext) and op.IN() is not None:
                j = i + 1
                while (
                    j < len(ops)
                    and isinstance(ops[j], CypherParser.OC_ListOperatorExpressionContext)
                    and ops[j].IN() is None
                ):
                    j += 1
                if j > i + 1:
                    operand = op.oC_PropertyOrLabelsExpression()
                    out.append((operand.start.start, "("))
                    out.append((ops[j - 1].stop.stop + 1, ")"))
                i = j
                continue
            i += 1
    for k in range(node.getChildCount() if hasattr(node, "getChildCount") else 0):
        _edits(node.getChild(k), out)


def reassociate_in_subscripts(cypher: str, tree: Any) -> str | None:
    """*cypher* with each ``IN`` operand's subscripts parenthesised, or None if none."""
    edits: list[tuple[int, str]] = []
    _edits(tree, edits)
    if not edits:
        return None
    text = cypher
    for pos, piece in sorted(edits, key=lambda e: e[0], reverse=True):
        text = text[:pos] + piece + text[pos:]
    return text
