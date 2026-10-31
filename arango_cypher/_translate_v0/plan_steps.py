"""One step of a multi-statement plan, compiled against the previous step's rows.

A plan (``arango_cypher.plans``) runs a query that does not fit one AQL
statement as several, in one stream transaction. Each step after the first
receives the rows the step before it returned, as the bind parameter
``@<rows_param>`` (one object per row, keyed by variable), and its Cypher
starts with ``WITH <imports>``. The step compiles as the body of a
``CALL { WITH … }`` subquery does, with the imports bound first::

    FOR _row IN @_step1_rows
      LET a = DOCUMENT(@@stepCollection, _row.a._key)   -- a node of a known collection
      LET r = _row.r == null ? null : DOCUMENT(_row.r._id)   -- an entity, collection unknown
      LET x = _row.x                                     -- a value

A node or relationship is read again by its key rather than taken from the
row, so the step sees what earlier steps wrote to it (or null, if one deleted
it); when its collection is known, the write translators can also write it
(``writes._infer_var_collections_from_pipeline``).
"""

from __future__ import annotations

from typing import Any

from arango_query_core import AqlQuery, CoreError, MappingResolver

from .._antlr.CypherParser import CypherParser
from .naming import _aql_collection_ref
from .state import StepInput, _active_user_vars

#: AQL name of the loop variable over the incoming rows.
ROW_VAR = "_row"
ENTITY_KINDS = frozenset({"node", "relationship"})


def _collection_key(collection: str, bind_vars: dict[str, Any]) -> str:
    """The ``@stepCollectionN`` bind key of *collection*, reused when it is already bound."""
    base = "@stepCollection"
    key, i = base, 2
    while key in bind_vars and bind_vars[key] != collection:
        key, i = f"{base}{i}", i + 1
    bind_vars[key] = collection
    return key


def step_prefix_lines(step: StepInput, bind_vars: dict[str, Any]) -> list[str]:
    """``FOR _row IN @rows`` and a ``LET`` per import (see the module docstring)."""
    if step.rows_param is None:
        raise CoreError("A plan step without input rows has no imports to bind", code="INTERNAL_ERROR")
    lines = [f"FOR {ROW_VAR} IN @{step.rows_param}"]
    for imp in step.imports:
        source = f"{ROW_VAR}.{imp.name}"
        if imp.kind in ENTITY_KINDS and imp.collection:
            ref = _aql_collection_ref(_collection_key(imp.collection, bind_vars))
            lines.append(f"  LET {imp.name} = DOCUMENT({ref}, {source}._key)")
        elif imp.kind in ENTITY_KINDS:
            lines.append(f"  LET {imp.name} = {source} == null ? null : DOCUMENT({source}._id)")
        else:
            lines.append(f"  LET {imp.name} = {source}")
    return lines


def translate_step(
    sq: CypherParser.OC_SingleQueryContext,
    step: StepInput,
    *,
    resolver: MappingResolver,
    bind_vars: dict[str, Any],
) -> AqlQuery:
    """Compile *sq* — ``WITH <imports> …`` — reading the rows of ``@<step.rows_param>``."""
    from .core import _translate_multi_part_query

    mpq = sq.oC_MultiPartQuery()
    if mpq is None or step.rows_param is None:
        raise CoreError(
            "A plan step that reads earlier rows must start with WITH <imports>", code="INTERNAL_ERROR"
        )
    if step.rows_param in bind_vars:
        raise CoreError(
            f"Parameter ${step.rows_param} is reserved for multi-statement plans", code="INVALID_ARGUMENT"
        )
    # The executor passes the rows; an empty list keeps the query runnable alone.
    bind_vars[step.rows_param] = []
    names = [imp.name for imp in step.imports]
    _active_user_vars.set(names)
    return _translate_multi_part_query(
        mpq,
        resolver=resolver,
        bind_vars=bind_vars,
        imports={name: name for name in names},
        outer_names=frozenset({ROW_VAR}),
        prefix_lines=step_prefix_lines(step, bind_vars),
    )
