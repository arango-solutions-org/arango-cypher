"""UNWIND clause compilation for the v0 translator."""

from __future__ import annotations

from typing import Any

from arango_query_core import AqlQuery, CoreError

from .._antlr.CypherParser import CypherParser


def unwind_source(expr_ctx: Any, compiled: str) -> str:
    """The AQL list an ``UNWIND expr`` loops over.

    Cypher unwinds null to no rows and any other non-list value to one row; an
    AQL FOR over either is an error (ERR 1563). A null literal or a value the
    query shows not to be a list is wrapped; a value of unknown type is
    looped over as it is.
    """
    from .arithmetic import LIST, _static_type

    if compiled.strip() == "null":
        return "[]"
    kind = _static_type(expr_ctx)
    if kind is not None and kind != LIST:
        return f"[{compiled}]"
    return compiled


def _translate_standalone_unwind(
    unwind_clauses: list[CypherParser.OC_UnwindContext],
    *,
    spq: CypherParser.OC_SinglePartQueryContext,
    bind_vars: dict[str, Any],
) -> AqlQuery:
    """Translate one or more UNWIND clauses without any MATCH."""
    from .core import _append_return, _compile_expression

    lines: list[str] = []
    for uw in unwind_clauses:
        expr = unwind_source(uw.oC_Expression(), _compile_expression(uw.oC_Expression(), bind_vars))
        var = uw.oC_Variable().getText().strip()
        lines.append(f"FOR {var} IN {expr}")

    ret = spq.oC_Return()
    if ret is None:
        raise CoreError("RETURN is required in v0 subset", code="UNSUPPORTED")
    _append_return(ret.oC_ProjectionBody(), lines=lines, bind_vars=bind_vars)
    return AqlQuery(text="\n".join(lines), bind_vars=bind_vars)


def _compile_unwind_lines(
    unwind_clauses: list[CypherParser.OC_UnwindContext],
    bind_vars: dict[str, Any],
) -> list[str]:
    """Compile UNWIND clauses into ``FOR var IN expr`` lines."""
    from .core import _compile_expression

    lines: list[str] = []
    for uw in unwind_clauses:
        expr = unwind_source(uw.oC_Expression(), _compile_expression(uw.oC_Expression(), bind_vars))
        var = uw.oC_Variable().getText().strip()
        lines.append(f"FOR {var} IN {expr}")
    return lines
