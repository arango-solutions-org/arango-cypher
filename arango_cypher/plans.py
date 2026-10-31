"""Multi-statement plans: a Cypher query as several AQL statements in one transaction.

AQL cannot read a collection after writing it in the same statement, nor
write one collection twice (ERR 1579); nor can one UPSERT/INSERT node and a
REMOVE share a collection. Cypher has no such limit: each clause sees what
the clauses before it wrote. :func:`translate_plan` therefore cuts a query
that does not fit one statement between clauses, into steps:

- each step is one AQL statement, compiled by the same translator as
  :func:`arango_cypher.translate`;
- every step but the last returns one object per row, keyed by the
  variables in scope at the cut; the next step receives those rows as the bind
  parameter named by :attr:`PlanStep.output_param` (``@_step1_rows`` …) and
  reads each node and relationship again, so it sees what earlier steps
  wrote (``_translate_v0/plan_steps.py``);
- the last step's rows are the query's result.

openCypher applies each clause to all rows before the next clause runs, so
running the clauses of one step over all rows before the next step starts is
the same order. Clauses are grouped greedily: a step takes as many clauses as
the translator compiles into one statement, so a query that fits one
statement is a one-step plan, identical to :func:`translate`'s output.

:func:`arango_cypher.plan_exec.execute_plan` runs a plan in one stream
transaction, so its writes commit together or not at all.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Any

from arango_query_core import CoreError, ExtensionPolicy, ExtensionRegistry, MappingBundle, MappingResolver

from ._antlr.CypherParser import CypherParser
from ._translate_v0.plan_scope import PlanUnsupported, Scope, collection_of, query_clauses, scope_after
from ._translate_v0.semantic_check import check_semantics
from ._translate_v0.state import StepImport, StepInput
from .parser import parse_cypher
from .translate_v0 import TranslateOptions, translate_v0

#: The column a step returns when no variable is in scope, so the next step still
#: sees one row per row (``CREATE (:X) CREATE (:X) MERGE (:X)``).
ROW_MARKER = "_plan_row"


@dataclass(frozen=True)
class PlanStep:
    """One AQL statement of a plan.

    *input_param* is the bind parameter this step receives the previous
    step's rows as (``None`` for the first step); *output_param* the one the
    next step receives this step's rows as (``None`` for the result step).
    The executor sets *input_param* in :attr:`bind_vars`; it holds ``[]``
    until then.
    """

    query: Any  # api.TranspiledQuery (not imported here: api imports this module)
    input_param: str | None = None
    output_param: str | None = None

    @property
    def aql(self) -> str:
        return str(self.query.aql)

    @property
    def bind_vars(self) -> dict[str, Any]:
        return dict(self.query.bind_vars)


@dataclass(frozen=True)
class TranslationPlan:
    """The AQL statements of a query, in order, and which one's rows are its result."""

    steps: tuple[PlanStep, ...]
    result_step: int

    @property
    def is_single_statement(self) -> bool:
        return len(self.steps) == 1

    @property
    def warnings(self) -> list[dict[str, Any]]:
        return [w for step in self.steps for w in (step.query.warnings or [])]

    def collections(self) -> frozenset[str]:
        """Every collection a step binds (``@@name``): what a transaction must declare."""
        return frozenset(
            v
            for step in self.steps
            for k, v in step.query.bind_vars.items()
            if k.startswith("@") and isinstance(v, str)
        )


def rows_param(index: int) -> str:
    """The bind parameter step *index* (1-based) hands its rows to the next step as."""
    return f"_step{index}_rows"


def _drop_unused_params(bind_vars: dict[str, Any], params: dict[str, Any] | None, aql: str) -> dict[str, Any]:
    """A step binds only the query parameters its own AQL reads (ArangoDB refuses unused ones)."""
    out = dict(bind_vars)
    for name in params or {}:
        if name in out and re.search(rf"(?<![@\w])@{re.escape(name)}(?!\w)", aql) is None:
            del out[name]
    return out


def _imports(scope: Scope, resolver: MappingResolver) -> tuple[StepImport, ...]:
    if not scope:
        return (StepImport(ROW_MARKER, "value"),)
    return tuple(
        StepImport(name, b.kind, collection_of(b, resolver) if b.kind in ("node", "relationship") else None)
        for name, b in scope.items()
    )


def _hand_on(scope: Scope) -> str:
    """The RETURN that ends a step that is not the last: every variable in scope."""
    if not scope:
        return f"RETURN 1 AS {ROW_MARKER}"
    return "RETURN " + ", ".join(f"{name} AS {name}" for name in scope)


def lower_to_steps(
    cypher: str,
    *,
    mapping: MappingBundle,
    params: dict[str, Any] | None,
    options: TranslateOptions,
    wrap: Any,
) -> list[PlanStep] | None:
    """Cut *cypher* into plan steps; ``None`` when it is not a query a plan can run.

    *wrap* turns a step's :class:`AqlQuery` into the public query type. A plan
    is for a query with a write and at least two clauses; it is attempted only
    after the whole query failed to compile as one statement.
    """
    pr = parse_cypher(cypher)
    regular = pr.tree.oC_Statement().oC_Query().oC_RegularQuery()
    if regular is None or regular.oC_Union():
        return None
    try:
        check_semantics(pr.tree)  # an invalid query stays an error, never a plan
    except CoreError:
        return None
    clauses = query_clauses(regular.oC_SingleQuery())
    if len(clauses) < 2 or not any(isinstance(c, CypherParser.OC_UpdatingClauseContext) for c in clauses):
        return None
    resolver = MappingResolver(mapping)
    try:
        scopes: list[Scope] = [{}]
        for clause in clauses:
            scopes.append(scope_after(clause, scopes[-1]))
    except PlanUnsupported:
        return None

    def text(i: int, j: int) -> str:
        return " ".join(pr.normalized[c.start.start : c.stop.stop + 1] for c in clauses[i : j + 1])

    steps: list[PlanStep] = []
    n, i = len(clauses), 0
    while i < n:
        imports = _imports(scopes[i], resolver) if steps else ()
        head = ("WITH " + ", ".join(f"{imp.name} AS {imp.name}" for imp in imports) + " ") if steps else ""
        chosen = None
        for j in range(n - 1, i - 1, -1):
            if i == 0 and j == n - 1:
                continue  # the whole query: it already failed as one statement
            final = j == n - 1
            body = text(i, j) + ("" if final else " " + _hand_on(scopes[j + 1]))
            step_input = StepInput(rows_param(len(steps)) if steps else None, imports, object_rows=not final)
            try:
                q = translate_v0(
                    head + body,
                    mapping=mapping,
                    params=params,
                    options=replace(options, step_input=step_input),
                )
            except CoreError:
                continue
            chosen = (j, q)
            break
        if chosen is None:
            return None
        j, q = chosen
        q = replace(q, bind_vars=_drop_unused_params(q.bind_vars, params, q.text))
        if steps:
            steps[-1] = replace(steps[-1], output_param=rows_param(len(steps)))
        steps.append(PlanStep(wrap(q), input_param=rows_param(len(steps)) if steps else None))
        i = j + 1
    return steps


def translate_plan(
    cypher: str,
    *,
    mapping: MappingBundle | None = None,
    extensions: ExtensionPolicy | None = None,
    registry: ExtensionRegistry | None = None,
    params: dict[str, Any] | None = None,
    case_insensitive_inline_strings: bool = False,
    cypher_column_names: bool = False,
    temporal_strings: bool = False,
) -> TranslationPlan:
    """Translate *cypher* into a :class:`TranslationPlan` (see the module docstring).

    Takes the same arguments as :func:`arango_cypher.translate`. A query that
    fits one AQL statement is a one-step plan whose query is what
    :func:`translate` returns; one that does not is cut into steps. A query
    neither compiles raises the error :func:`translate` raises for it.
    Run a plan with :func:`arango_cypher.execute_plan`.
    """
    from .api import TranspiledQuery, _translate_one, translate_options

    try:
        whole = _translate_one(
            cypher,
            mapping=mapping,
            extensions=extensions,
            registry=registry,
            params=params,
            case_insensitive_inline_strings=case_insensitive_inline_strings,
            cypher_column_names=cypher_column_names,
            temporal_strings=temporal_strings,
        )
        return TranslationPlan(steps=(PlanStep(whole),), result_step=0)
    except CoreError:
        assert mapping is not None  # _translate_one refuses a missing mapping first
        steps = lower_to_steps(
            cypher,
            mapping=mapping,
            params=params,
            options=translate_options(
                extensions, registry, case_insensitive_inline_strings, cypher_column_names, temporal_strings
            ),
            wrap=TranspiledQuery.from_aql_query,
        )
        if steps is None:
            raise
        return TranslationPlan(steps=tuple(steps), result_step=len(steps) - 1)
