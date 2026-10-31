"""``POST /execute`` for a query that needs several AQL statements (a plan).

:func:`arango_cypher.translate_plan` cuts such a query into steps and
:func:`arango_cypher.execute_plan` runs them in one stream transaction. The
single-statement path of ``/execute`` passes its AQL through tenant Layer 4
(AST injection) and Layer 5 (EXPLAIN-plan validation) before running it; a
plan's steps read the rows of the step before them, which neither layer
certifies yet. So a plan runs only for a tenant-unbound session (workbench /
single-tenant) and is refused, fail-closed, for a tenant-bound one.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from arango.exceptions import ArangoServerError
from fastapi import HTTPException

from ..plan_exec import PlanExecutionError, execute_plan
from ..plans import TranslationPlan
from .models import ExecuteResponse
from .observability import log_endpoint_timing

_log = logging.getLogger("arango_cypher.service")


def plan_aql(plan: TranslationPlan) -> str:
    """The plan's statements as one text, each headed by its step number."""
    n = len(plan.steps)
    return "\n\n".join(f"// step {i} of {n}\n{step.aql}" for i, step in enumerate(plan.steps, start=1))


def execute_plan_response(
    plan: TranslationPlan,
    *,
    cypher: str,
    session: Any,
    max_rows: int | None,
    translate_ms: float,
    sanitize: Any,
) -> ExecuteResponse:
    """Run *plan* for ``/execute``; *sanitize* scrubs an error message for the client."""
    n = len(plan.steps)
    if getattr(session, "tenant_id", None) is not None:
        raise HTTPException(
            status_code=422,
            detail={
                "error": (
                    f"This query needs {n} AQL statements. Multi-statement plans do not run for a "
                    "tenant-bound session yet: their steps are not certified by the tenant-scope layers."
                ),
                "code": "MULTI_STATEMENT_TENANT_BOUND",
            },
        )
    t_exec = time.perf_counter()
    try:
        rows = execute_plan(session.db, plan)
    except PlanExecutionError as e:
        cause = e.__cause__
        _log.warning("plan step %d of %d failed: %s: %s", e.step, n, type(cause).__name__, cause)
        status = 422 if isinstance(cause, ArangoServerError) else 500
        raise HTTPException(
            status_code=status,
            detail={"error": sanitize(str(e)), "code": "PLAN_STEP_FAILED", "step": e.step},
        ) from e
    exec_ms = round((time.perf_counter() - t_exec) * 1000, 1)
    truncated = max_rows is not None and len(rows) > max_rows
    results = rows[:max_rows] if max_rows is not None else rows
    warnings = [
        {"message": f"Ran as {n} AQL statements in one transaction (multi-statement plan)."},
        *plan.warnings,
    ]
    if truncated:
        warnings.append(
            {
                "message": (
                    f"Showing the first {max_rows:,} rows; the query returns more. "
                    "Add LIMIT (with ORDER BY to choose which rows) to see a specific slice."
                )
            }
        )
    result_step = plan.steps[plan.result_step]
    log_endpoint_timing(
        "/execute",
        round(translate_ms + exec_ms, 1),
        translate_ms=translate_ms,
        exec_ms=exec_ms,
        rows=len(results),
        truncated=truncated,
        cypher_len=len(cypher or ""),
        aql_len=sum(len(s.aql) for s in plan.steps),
        plan_steps=n,
    )
    return ExecuteResponse(
        results=results,
        aql=plan_aql(plan),
        bind_vars={k: v for k, v in result_step.bind_vars.items() if k != result_step.input_param},
        warnings=warnings,
        exec_ms=exec_ms,
        translate_ms=translate_ms,
        truncated=truncated,
        row_limit=max_rows,
        plan_steps=[
            {
                "aql": s.aql,
                "bind_vars": s.bind_vars,
                "input_param": s.input_param,
                "output_param": s.output_param,
            }
            for s in plan.steps
        ],
    )
