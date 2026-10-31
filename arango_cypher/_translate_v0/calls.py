"""CALL / procedure clause compilation for the v0 translator.

Two kinds of procedure reach a ``CALL`` (see :mod:`arango_cypher.procedures`):

* ``arango.*`` procedures, whose registry compilers take the compiled argument
  list and return an AQL expression of values;
* every other registered procedure, which follows the invocation protocol:
  its compiler receives a :class:`ProcedureInvocation` and returns a
  :class:`CompiledProcedure` (an AQL expression of row objects and the names
  of its columns). Knowing the columns, this module implements Cypher's
  ``YIELD`` rules, void procedures and implicit arguments.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from arango_query_core import AqlQuery, CoreError

from .._antlr.CypherParser import CypherParser
from ..procedures import CompiledProcedure, ProcedureInvocation
from .arithmetic import BOOL, FLOAT, INT, LIST, MAP, NUMBER, STRING, _static_type
from .columns import object_key
from .naming import _aql_bind_name
from .semantic_errors import CypherSemanticError, semantic_error
from .state import _active_registry, _active_user_params, _active_user_vars


def _same(text: str) -> str:
    return text


#: The query's static types, as the Cypher type names signatures use.
_CYPHER_TYPE_NAMES = {
    INT: "INTEGER",
    FLOAT: "FLOAT",
    NUMBER: "NUMBER",
    STRING: "STRING",
    BOOL: "BOOLEAN",
    LIST: "LIST",
    MAP: "MAP",
}

ProcedureContext = (
    CypherParser.OC_ExplicitProcedureInvocationContext | CypherParser.OC_ImplicitProcedureInvocationContext
)


@dataclass(frozen=True)
class _Compiled:
    """A compiled procedure call. *columns* is None for an ``arango.*``
    procedure, whose rows are the values ``YIELD`` names."""

    aql: str | None
    columns: tuple[str, ...] | None

    @property
    def legacy(self) -> bool:
        return self.columns is None


@dataclass(frozen=True)
class _Yield:
    """A ``YIELD``: (variable, procedure output) pairs, ``*``, and its ``WHERE``."""

    items: list[tuple[str, str]]
    star: bool
    where: CypherParser.OC_WhereContext | None

    @property
    def absent(self) -> bool:
        return not self.items and not self.star


def _extract_procedure_name(proc_ctx: ProcedureContext) -> str:
    """Extract the fully-qualified procedure name (e.g. ``arango.search``)."""
    return proc_ctx.oC_ProcedureName().getText().strip()


def _extract_yield(yield_items_ctx: CypherParser.OC_YieldItemsContext | None) -> _Yield:
    if yield_items_ctx is None:
        return _Yield([], star=False, where=None)
    star = yield_items_ctx.getChild(0).getText() == "*"
    items: list[tuple[str, str]] = []
    for item in yield_items_ctx.oC_YieldItem() or []:
        var = item.oC_Variable().getText().strip()
        field = item.oC_ProcedureResultField()
        items.append((var, field.getText().strip() if field is not None else var))
    return _Yield(items, star=star, where=yield_items_ctx.oC_Where())


def _extract_yield_vars(
    yield_items_ctx: CypherParser.OC_YieldItemsContext | None,
) -> list[tuple[str, str | None]]:
    """Return ``[(variable, procedureResultField|None), ...]`` from a YIELD clause."""
    if yield_items_ctx is None:
        return []
    result: list[tuple[str, str | None]] = []
    for item in yield_items_ctx.oC_YieldItem() or []:
        prf = item.oC_ProcedureResultField()
        result.append((item.oC_Variable().getText().strip(), prf.getText().strip() if prf else None))
    return result


# --- compiling the procedure --------------------------------------------------------


def _static_cypher_type(arg: CypherParser.OC_ExpressionContext) -> str | None:
    if arg.getText().strip().lower() == "null":
        return "NULL"
    return _CYPHER_TYPE_NAMES.get(_static_type(arg) or "")


def _implicit_parameter(name: str) -> str:
    """AQL for the parameter an implicit call takes an input from."""
    if name not in _active_user_params.get():
        raise semantic_error(
            "MissingParameter",
            f"Expected parameter ${name}: an implicit procedure call takes each input from a parameter",
        )
    return _aql_bind_name(f"${name}")


def _compile_procedure(
    proc_ctx: ProcedureContext,
    *,
    standalone: bool,
    bind_vars: dict[str, Any],
    rewrite: Callable[[str], str] = _same,
) -> _Compiled:
    from .core import _compile_expression

    name = _extract_procedure_name(proc_ctx)
    explicit = isinstance(proc_ctx, CypherParser.OC_ExplicitProcedureInvocationContext)
    raw_args = (proc_ctx.oC_Expression() or []) if explicit else []
    registry = _active_registry.get()

    if name.lower().startswith("arango."):
        if registry is None:
            raise CoreError(
                f"arango.* procedure '{name}' requires a registry (pass TranslateOptions(registry=...) to translate)",
                code="EXTENSIONS_DISABLED",
            )
        args = [rewrite(_compile_expression(a, bind_vars)) for a in raw_args]
        return _Compiled(registry.compile_procedure(name.lower(), args, bind_vars), None)
    if registry is None:
        raise CoreError(f"Unsupported procedure in v0: {name}", code="UNSUPPORTED")

    invocation = ProcedureInvocation(
        name=name,
        args=tuple(rewrite(_compile_expression(a, bind_vars)) for a in raw_args) if explicit else None,
        arg_types=tuple(_static_cypher_type(a) for a in raw_args),
        standalone=standalone,
        parameter=_implicit_parameter,
    )
    try:
        compiled = registry.compile_procedure(name, invocation, bind_vars)
    except CypherSemanticError:
        raise
    except CoreError as exc:
        if exc.code == "UNKNOWN_EXTENSION":
            raise CypherSemanticError(
                "ProcedureNotFound", f"There is no procedure with the name `{name}` registered"
            ) from exc
        raise
    if not isinstance(compiled, CompiledProcedure):
        raise CoreError(
            f"procedure {name}: its compiler returned {type(compiled).__name__}, not a CompiledProcedure "
            "(see arango_cypher.procedures)",
            code="INVALID_EXTENSION",
        )
    return _Compiled(compiled.aql, tuple(compiled.columns))


def _yielded(compiled: _Compiled, yields: _Yield, name: str) -> list[tuple[str, str]]:
    """The (variable, column) pairs a call binds, checked against its columns."""
    assert compiled.columns is not None
    if yields.star or yields.absent:
        return [(c, c) for c in compiled.columns]
    for _, column in yields.items:
        if column not in compiled.columns:
            raise semantic_error(
                "UndefinedVariable",
                f"procedure {name} has no output `{column}` (outputs: {', '.join(compiled.columns) or 'none'})",
            )
    return yields.items


def _row_lines(
    compiled: _Compiled,
    pairs: list[tuple[str, str]],
    row: str,
    where: CypherParser.OC_WhereContext | None,
    bind_vars: dict[str, Any],
    *,
    aql_name: Callable[[str], str] = _same,
    rewrite: Callable[[str], str] = _same,
) -> list[str]:
    """``FOR row IN <rows>`` with a ``LET`` per yielded column and the ``WHERE``.
    *aql_name* gives a yielded variable's AQL name; *rewrite* maps the Cypher
    names an expression reads to their AQL names."""
    from .core import _compile_expression

    lines = [f"FOR {row} IN {compiled.aql}"]
    lines.extend(f"  LET {aql_name(var)} = {row}[{_string(column)}]" for var, column in pairs)
    if where is not None:
        lines.append(f"  FILTER {rewrite(_compile_expression(where.oC_Expression(), bind_vars))}")
    return lines


def _string(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _legacy_lines(aql: str, yield_vars: list[tuple[str, str | None]], row: str) -> list[str]:
    """An ``arango.*`` procedure: ``FOR var IN <values>``, or one ``LET`` per field."""
    if len(yield_vars) == 1:
        return [f"FOR {yield_vars[0][0]} IN {aql}"]
    lines = [f"FOR {row} IN {aql}"]
    for var_name, result_field in yield_vars:
        lines.append(f"  LET {var_name} = {row}.{result_field or var_name}")
    return lines


def in_query_call_lines(
    iqc: CypherParser.OC_InQueryCallContext,
    bind_vars: dict[str, Any],
    *,
    row: str,
    aql_name: Callable[[str], str] = _same,
    rewrite: Callable[[str], str] = _same,
) -> tuple[list[str], list[str]]:
    """The AQL lines of one in-query ``CALL`` (a void procedure adds none) and
    the Cypher variables it binds. *row* names the loop variable; *aql_name*
    and *rewrite* are as for :func:`_row_lines` (a ``CALL`` stage of a
    multi-part query binds fresh AQL names)."""
    proc_ctx = iqc.oC_ExplicitProcedureInvocation()
    if proc_ctx is None:
        raise CoreError(
            "Only explicit procedure invocations (with parentheses) are supported for in-query CALL",
            code="UNSUPPORTED",
        )
    compiled = _compile_procedure(proc_ctx, standalone=False, bind_vars=bind_vars, rewrite=rewrite)
    if compiled.legacy:
        assert compiled.aql is not None
        yield_vars = _extract_yield_vars(iqc.oC_YieldItems())
        return _legacy_lines(compiled.aql, yield_vars, row), [v for v, _ in yield_vars]
    name = _extract_procedure_name(proc_ctx)
    yields = _extract_yield(iqc.oC_YieldItems())
    if compiled.aql is None:  # void: the rows pass through
        if not yields.absent:
            raise semantic_error("UndefinedVariable", f"procedure {name} is void: it has no outputs to YIELD")
        return [], []
    if yields.star:
        raise semantic_error("UnexpectedSyntax", "YIELD * is only allowed in a standalone procedure call")
    if yields.absent:
        raise semantic_error(
            "UnexpectedSyntax",
            f"a procedure call inside a query must name the outputs it uses with YIELD "
            f"({name} has {', '.join(compiled.columns or ())})",
        )
    pairs = _yielded(compiled, yields, name)
    lines = _row_lines(compiled, pairs, row, yields.where, bind_vars, aql_name=aql_name, rewrite=rewrite)
    return lines, [var for var, _ in pairs]


def _call_row(index: int) -> str:
    return "_call_row" if index == 0 else f"_call_row_{index}"


# --- the three placements of a CALL ---------------------------------------------------


def _translate_standalone_call(
    sc: CypherParser.OC_StandaloneCallContext,
    *,
    bind_vars: dict[str, Any],
) -> AqlQuery:
    """Translate a top-level ``CALL proc() YIELD ...`` (no RETURN)."""
    proc_ctx = sc.oC_ExplicitProcedureInvocation() or sc.oC_ImplicitProcedureInvocation()
    if proc_ctx is None:
        raise CoreError("Invalid CALL syntax", code="UNSUPPORTED")
    compiled = _compile_procedure(proc_ctx, standalone=True, bind_vars=bind_vars)

    if compiled.legacy:
        assert compiled.aql is not None
        yield_vars = _extract_yield_vars(sc.oC_YieldItems())
        lines = _legacy_lines(compiled.aql, yield_vars, "_call_row")
        if len(yield_vars) == 1:
            lines.append(f"  RETURN {yield_vars[0][0]}")
        elif yield_vars:
            lines.append("  RETURN {" + ", ".join(f"{v}: {v}" for v, _ in yield_vars) + "}")
        else:
            lines.append("  RETURN _call_row")
        return AqlQuery(text="\n".join(lines), bind_vars=bind_vars)

    name = _extract_procedure_name(proc_ctx)
    yields = _extract_yield(sc.oC_YieldItems())
    if compiled.aql is None:  # void: no rows
        if not yields.absent and not yields.star:
            raise semantic_error("UndefinedVariable", f"procedure {name} is void: it has no outputs to YIELD")
        return AqlQuery(text="FOR _call_row IN []\n  RETURN _call_row", bind_vars=bind_vars)
    pairs = _yielded(compiled, yields, name)
    lines = _row_lines(compiled, pairs, "_call_row", yields.where, bind_vars)
    lines.append("  RETURN {" + ", ".join(f"{object_key(var)}: {var}" for var, _ in pairs) + "}")
    return AqlQuery(text="\n".join(lines), bind_vars=bind_vars)


def _translate_standalone_in_query_call(
    calls: list[CypherParser.OC_InQueryCallContext],
    *,
    spq: CypherParser.OC_SinglePartQueryContext,
    bind_vars: dict[str, Any],
) -> AqlQuery:
    """Translate in-query CALL(s) that appear without MATCH or UNWIND."""
    from .core import _append_return

    lines: list[str] = []
    yielded: list[str] = []
    for index, iqc in enumerate(calls):
        call_lines, names = in_query_call_lines(iqc, bind_vars, row=_call_row(index))
        lines.extend(call_lines)
        yielded.extend(names)
    # RETURN * reads the yielded variables.
    in_scope = list(_active_user_vars.get())
    _active_user_vars.set([*in_scope, *(n for n in yielded if n not in in_scope)])

    ret = spq.oC_Return()
    if ret is None:
        raise CoreError("RETURN is required in v0 subset", code="UNSUPPORTED")
    _append_return(ret.oC_ProjectionBody(), lines=lines, bind_vars=bind_vars)
    return AqlQuery(text="\n".join(lines), bind_vars=bind_vars)


def _inject_in_query_calls(
    result: AqlQuery,
    calls: list[CypherParser.OC_InQueryCallContext],
    bind_vars: dict[str, Any],
) -> AqlQuery:
    """Inject in-query CALL lines into an existing AQL result, before the RETURN."""
    call_lines: list[str] = []
    for index, iqc in enumerate(calls):
        call_lines.extend(in_query_call_lines(iqc, bind_vars, row=_call_row(index))[0])

    result_lines = result.text.splitlines()
    return_line = result_lines[-1] if result_lines else ""
    body_lines = result_lines[:-1]
    final_lines = body_lines + [f"  {cl}" for cl in call_lines] + [return_line]
    return AqlQuery(text="\n".join(final_lines), bind_vars=result.bind_vars)
