"""The TCK's procedure fixtures, through the public procedure registry.

A scenario declares each procedure it calls::

    And there exists a procedure test.my.proc(in :: INTEGER?) :: (out :: STRING?):
      | in   | out   |
      | null | 'nix' |

That is exactly a table-backed procedure (``arango_cypher.procedures``): the
signature as Neo4j lists it, and the rows keyed by input and output names.
The harness registers each one with :func:`register_table_procedure` in a
registry of its own per scenario, and translates the scenario's queries with
it — the same mechanism an application uses for its own procedures.
"""

from __future__ import annotations

from typing import Any

from arango_query_core import ExtensionPolicy, ExtensionRegistry

from arango_cypher.procedures import parse_signature, register_table_procedure

from .gherkin import Scenario
from .normalize import parse_param_value

PROCEDURE_STEP = "there exists a procedure "


def scenario_registry(scenario: Scenario) -> ExtensionRegistry:
    """A registry holding the scenario's fixture procedures (possibly none,
    so that calling an unknown procedure is ``ProcedureNotFound``)."""
    registry = ExtensionRegistry(policy=ExtensionPolicy(enabled=True))
    for step in scenario.steps:
        if step.keyword in {"Given", "And"} and step.text.startswith(PROCEDURE_STEP):
            signature = step.text[len(PROCEDURE_STEP) :].strip().rstrip(":").strip()
            register_table_procedure(registry, parse_signature(signature), _rows(step.data_table))
    return registry


def _rows(table: list[list[str]] | None) -> list[dict[str, Any]]:
    """A fixture's rows: the header names the inputs and outputs; a void
    procedure's table is a lone ``|``."""
    if not table or not any(cell.strip() for cell in table[0]):
        return []
    header = [cell.strip() for cell in table[0]]
    return [
        {name: parse_param_value(cell.strip()) for name, cell in zip(header, row, strict=True)}
        for row in table[1:]
    ]


def scenario_params(scenario: Scenario) -> dict[str, Any]:
    """The scenario's ``parameters are:`` values (``| name | value |`` rows)."""
    params: dict[str, Any] = {}
    for step in scenario.steps:
        if step.keyword in {"Given", "And"} and step.text.startswith("parameters are:"):
            for row in step.data_table or []:
                name, raw = row
                params[name] = parse_param_value(raw)
    return params
