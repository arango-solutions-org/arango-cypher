"""Run a multi-statement plan in one ArangoDB stream transaction.

Execution of a single query lives in arango-query-core (``AqlExecutor``); a
plan's statements belong there too, as a sequence of ``AqlQuery`` objects
whose rows feed the next one's bind parameter. Until the package ships that,
:func:`run_statements` is the upstream-shaped adapter: it takes only
``(AqlQuery, input_param)`` pairs and a python-arango database, so it moves
across unchanged.
"""

# upstream: arango-solutions-org/arango-query-core#21 — remove when arango-query-core ships a transactional multi-statement executor

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from arango_query_core import AqlQuery

from .plans import TranslationPlan

_log = logging.getLogger("arango_cypher.plan_exec")


@dataclass(frozen=True)
class Statement:
    """One statement of a sequence: its query, and the bind parameter it reads
    the previous statement's rows from (``None`` for the first)."""

    query: AqlQuery
    input_param: str | None = None


class PlanExecutionError(RuntimeError):
    """A statement of a plan (or its commit) failed; the transaction was aborted.

    *step* is the 1-based number of the statement that failed, or ``total + 1``
    for the commit; the cause is chained (``__cause__``).
    """

    def __init__(self, step: int, total: int, cause: BaseException) -> None:
        what = "the commit" if step > total else f"step {step} of {total}"
        super().__init__(f"{what} failed, transaction aborted: {cause}")
        self.step = step
        self.total = total


def run_statements(
    db: Any,
    statements: Sequence[Statement],
    *,
    write_collections: Sequence[str],
    batch_size: int | None = None,
    lock_timeout: int | None = None,
) -> list[Any]:
    """Run *statements* in order in one stream transaction; return the last one's rows.

    Each statement after the first receives the rows of the one before it as
    its ``input_param`` bind parameter. *write_collections* are declared for
    writing (reads of other collections are allowed implicitly). Any failure
    aborts the transaction — every statement's writes are undone — and is
    raised as :class:`PlanExecutionError` from the original error.
    """
    if not statements:
        raise ValueError("run_statements needs at least one statement")
    txn = db.begin_transaction(
        write=sorted(set(write_collections)), allow_implicit=True, lock_timeout=lock_timeout
    )
    rows: list[Any] = []
    step = 0
    try:
        for stmt in statements:
            step += 1
            bind_vars = dict(stmt.query.bind_vars)
            if stmt.input_param is not None:
                bind_vars[stmt.input_param] = rows
            cursor = txn.aql.execute(stmt.query.text, bind_vars=bind_vars, batch_size=batch_size)
            rows = list(cursor)  # read in full: the next statement and the commit need it
        step += 1
        txn.commit_transaction()
    except BaseException as exc:
        try:
            txn.abort_transaction()
        except Exception as abort_exc:  # noqa: BLE001 — reported, the original error wins
            _log.error(
                "aborting the plan transaction after step %d of %d failed: %s: %s",
                step,
                len(statements),
                type(abort_exc).__name__,
                abort_exc,
            )
        if isinstance(exc, Exception):
            raise PlanExecutionError(step, len(statements), exc) from exc
        raise
    return rows


def execute_plan(
    db: Any,
    plan: TranslationPlan,
    *,
    batch_size: int | None = None,
    lock_timeout: int | None = None,
) -> list[Any]:
    """Run *plan* (:func:`arango_cypher.translate_plan`) against *db*; return its result rows.

    A one-statement plan runs as a plain query, as :func:`arango_cypher.execute`
    does. A longer one runs in one stream transaction, every collection its
    steps bind declared for writing: the result step's rows are returned
    after the commit, and a failing step aborts the transaction
    (:class:`PlanExecutionError`).
    """
    if plan.result_step != len(plan.steps) - 1:
        raise ValueError("a plan's result step must be its last step")
    if plan.is_single_statement:
        q = plan.steps[0].query
        return list(db.aql.execute(q.aql, bind_vars=q.bind_vars, batch_size=batch_size))
    return run_statements(
        db,
        [Statement(step.query.to_aql_query(), step.input_param) for step in plan.steps],
        write_collections=sorted(plan.collections()),
        batch_size=batch_size,
        lock_timeout=lock_timeout,
    )
