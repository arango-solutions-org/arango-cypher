"""Procedures for ``CALL``: signatures, the invocation protocol, and
table-backed procedures.

The translator compiles ``CALL ns.name(args) YIELD …`` through the
:class:`~arango_query_core.ExtensionRegistry` passed to ``translate``. A
procedure outside the ``arango.*`` namespace is registered with
``registry.register_procedure(name, compiler)`` and follows the **invocation
protocol**: the translator calls ``compiler(invocation, bind_vars)`` with a
:class:`ProcedureInvocation`, and the compiler returns a
:class:`CompiledProcedure` — an AQL expression that evaluates to the
procedure's rows (objects keyed by output name), and the names of its output
columns. With the columns known, the translator implements Cypher's rules
for ``YIELD`` (``YIELD a, b AS c``, ``YIELD *`` and no ``YIELD`` in a
standalone call, ``YIELD … WHERE``), for void procedures (no outputs: an
in-query call leaves the rows as they are), and for implicit arguments
(``CALL p`` takes each input from the parameter of the same name). (The
``arango.*`` procedures predate this protocol: their compilers receive the
compiled argument list and return an AQL expression of values.)

:class:`TableProcedure` is a ready-made compiler: a procedure whose result is a
fixed table of rows, keyed by its inputs. A call returns, in table order, the
rows whose input columns equal the arguments. It checks the call against the
declared signature as Neo4j does (the number of arguments, an argument whose
type the query shows, a missing implicit parameter). It is what the openCypher
TCK's ``there exists a procedure`` fixtures need, and it serves as a test
double for any procedure an application provides::

    registry = ExtensionRegistry(policy=ExtensionPolicy(enabled=True))
    register_table_procedure(
        registry,
        "test.my.proc(name :: STRING?, id :: INTEGER?) :: (city :: STRING?)",
        [{"name": "Stefan", "id": 1, "city": "Berlin"}],
    )
    translate("CALL test.my.proc('Stefan', 1) YIELD city RETURN city",
              mapping=mapping, registry=registry)
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from arango_query_core import CoreError, ExtensionRegistry

from ._translate_v0.semantic_errors import semantic_error

#: Cypher type names a signature may use (``?`` marks a nullable value;
#: ``LIST? OF T`` lists).
_TYPE_NAMES = frozenset(
    {
        "ANY",
        "BOOLEAN",
        "STRING",
        "INTEGER",
        "FLOAT",
        "NUMBER",
        "MAP",
        "LIST",
        "NODE",
        "RELATIONSHIP",
        "PATH",
        "POINT",
        "DATE",
        "DATETIME",
        "LOCALDATETIME",
        "TIME",
        "LOCALTIME",
        "DURATION",
    }
)
#: Which static argument types (as the query shows them) a declared type accepts.
_ACCEPTS: dict[str, frozenset[str]] = {
    "INTEGER": frozenset({"INTEGER"}),
    "FLOAT": frozenset({"FLOAT", "INTEGER"}),
    "NUMBER": frozenset({"NUMBER", "INTEGER", "FLOAT"}),
    "STRING": frozenset({"STRING"}),
    "BOOLEAN": frozenset({"BOOLEAN"}),
    "LIST": frozenset({"LIST"}),
    "MAP": frozenset({"MAP"}),
}

_FIELD_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*::\s*([A-Za-z][A-Za-z ?]*?)\s*$")
_SIGNATURE_RE = re.compile(r"^\s*([A-Za-z_][\w.]*)\s*\((.*?)\)\s*::\s*\((.*?)\)\s*:?\s*$")


@dataclass(frozen=True)
class ProcedureField:
    """One input or output of a procedure: its name and Cypher type
    (``INTEGER``, ``STRING``, ``LIST OF STRING``, ``ANY`` …)."""

    name: str
    type: str = "ANY"
    nullable: bool = True

    def accepts(self, static_type: str | None) -> bool:
        """Whether an argument of *static_type* (as the query shows it;
        ``None`` when it does not) may be passed. ``NULL`` passes a nullable
        field."""
        if static_type is None:
            return True
        if static_type == "NULL":
            return self.nullable
        base = self.type.split()[0]
        accepted = _ACCEPTS.get(base)
        return accepted is None or static_type in accepted


@dataclass(frozen=True)
class ProcedureSignature:
    """A procedure's inputs and outputs, in order. No outputs: a void procedure."""

    inputs: tuple[ProcedureField, ...] = ()
    outputs: tuple[ProcedureField, ...] = ()

    @property
    def void(self) -> bool:
        return not self.outputs

    @property
    def output_names(self) -> tuple[str, ...]:
        return tuple(f.name for f in self.outputs)


def parse_signature(text: str) -> tuple[str, ProcedureSignature]:
    """``'test.my.proc(in :: INTEGER?) :: (out :: STRING?)'`` → name and signature.

    The form Neo4j lists procedures in (and the TCK declares them in).
    """
    match = _SIGNATURE_RE.match(text)
    if match is None:
        raise ValueError(f"not a procedure signature: {text!r}")
    name, inputs, outputs = match.groups()
    return name, ProcedureSignature(_parse_fields(inputs, text), _parse_fields(outputs, text))


def _parse_fields(text: str, signature: str) -> tuple[ProcedureField, ...]:
    if not text.strip():
        return ()
    fields: list[ProcedureField] = []
    for part in text.split(","):
        match = _FIELD_RE.match(part)
        if match is None:
            raise ValueError(f"not a procedure field: {part.strip()!r} in {signature!r}")
        name, raw_type = match.groups()
        # ``INTEGER?`` is nullable; in ``LIST? OF STRING?`` the first ``?``
        # is the list's own.
        nullable = raw_type.split()[0].endswith("?")
        type_name = " ".join(raw_type.replace("?", "").split()).upper()
        if type_name.split()[0] not in _TYPE_NAMES:
            raise ValueError(f"unknown Cypher type {raw_type!r} in {signature!r}")
        fields.append(ProcedureField(name, type_name, nullable))
    return tuple(fields)


@dataclass(frozen=True)
class ProcedureInvocation:
    """What the translator tells a procedure compiler about one ``CALL``.

    ``args`` holds the compiled AQL of each explicit argument, or is ``None``
    for an implicit call (``CALL p`` without parentheses), whose inputs come
    from parameters through :meth:`parameter`. ``arg_types`` is the Cypher
    type of each explicit argument where the query shows it (``INTEGER``,
    ``FLOAT``, ``NUMBER``, ``STRING``, ``BOOLEAN``, ``LIST``, ``MAP``,
    ``NULL``), else ``None``. ``standalone`` is true for a ``CALL`` that is
    the whole query.
    """

    name: str
    args: tuple[str, ...] | None
    arg_types: tuple[str | None, ...]
    standalone: bool
    parameter: Callable[[str], str]


@dataclass(frozen=True)
class CompiledProcedure:
    """A compiled call: *aql* evaluates to an array of row objects keyed by
    *columns* (``None`` for a void procedure, which yields no rows)."""

    aql: str | None
    columns: tuple[str, ...]


ProcedureCompiler = Callable[[ProcedureInvocation, dict[str, Any]], CompiledProcedure]


class TableProcedure:
    """A procedure whose result is a fixed table (see the module docstring).

    *rows* are mappings from every input and output name to a value; a call
    returns the rows whose input values equal the arguments (``null`` equals
    ``null``, ``42`` equals ``42.0``), in table order. The table travels as a
    bind parameter, never interpolated into the AQL.
    """

    def __init__(self, signature: ProcedureSignature, rows: Sequence[Mapping[str, Any]]) -> None:
        names = [f.name for f in (*signature.inputs, *signature.outputs)]
        if len(set(names)) != len(names):
            raise ValueError(f"a procedure's input and output names must differ: {names}")
        for i, row in enumerate(rows):
            missing = [n for n in names if n not in row]
            extra = [k for k in row if k not in names]
            if missing or extra:
                raise ValueError(f"row {i} of a table procedure: missing {missing}, unknown {extra}")
        if signature.void and rows:
            raise ValueError("a void procedure (no outputs) has no rows")
        self.signature = signature
        self.rows = [dict(row) for row in rows]

    def __call__(self, invocation: ProcedureInvocation, bind_vars: dict[str, Any]) -> CompiledProcedure:
        inputs = self.signature.inputs
        if invocation.args is None:
            args = tuple(invocation.parameter(f.name) for f in inputs)
        else:
            args = invocation.args
            if len(args) != len(inputs):
                raise semantic_error(
                    "InvalidNumberOfArguments",
                    f"procedure {invocation.name} takes {len(inputs)} argument(s) "
                    f"({', '.join(f.name for f in inputs) or 'none'}), got {len(args)}",
                )
            for field, static_type in zip(inputs, invocation.arg_types, strict=True):
                if not field.accepts(static_type):
                    raise semantic_error(
                        "InvalidArgumentType",
                        f"procedure {invocation.name}: argument {field.name} is {field.type}"
                        f"{'?' if field.nullable else ''}, got {static_type}",
                    )
        if self.signature.void:
            return CompiledProcedure(aql=None, columns=())
        table = _fresh_bind_name(bind_vars, "procRows")
        bind_vars[table] = self.rows
        conditions = [f"_proc_row[{_quoted(f.name)}] == ({arg})" for f, arg in zip(inputs, args, strict=True)]
        columns = self.signature.output_names
        projection = ", ".join(f"{_quoted(c)}: _proc_row[{_quoted(c)}]" for c in columns)
        filters = "".join(f" FILTER {c}" for c in conditions)
        return CompiledProcedure(
            aql=f"(FOR _proc_row IN @{table}{filters} RETURN {{{projection}}})", columns=columns
        )


def _quoted(name: str) -> str:
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _fresh_bind_name(bind_vars: dict[str, Any], prefix: str) -> str:
    n = 0
    while f"{prefix}{n}" in bind_vars:
        n += 1
    return f"{prefix}{n}"


def register_procedure(registry: ExtensionRegistry, name: str, compiler: ProcedureCompiler) -> None:
    """Register a procedure that follows the invocation protocol."""
    if name.lower().startswith("arango."):
        raise CoreError(
            f"'{name}': the arango.* namespace is for the built-in procedures, "
            "which use the older argument-list protocol",
            code="INVALID_ARGUMENT",
        )
    registry.register_procedure(name, compiler)


def register_table_procedure(
    registry: ExtensionRegistry,
    signature: str | tuple[str, ProcedureSignature],
    rows: Sequence[Mapping[str, Any]],
) -> TableProcedure:
    """Register a table-backed procedure; *signature* is the text Neo4j lists
    (``'ns.name(in :: INTEGER?) :: (out :: STRING?)'``) or a
    ``(name, ProcedureSignature)`` pair."""
    name, parsed = parse_signature(signature) if isinstance(signature, str) else signature
    procedure = TableProcedure(parsed, rows)
    register_procedure(registry, name, procedure)
    return procedure


__all__ = [
    "CompiledProcedure",
    "ProcedureCompiler",
    "ProcedureField",
    "ProcedureInvocation",
    "ProcedureSignature",
    "TableProcedure",
    "parse_signature",
    "register_procedure",
    "register_table_procedure",
]
