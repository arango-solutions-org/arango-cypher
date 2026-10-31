"""SET / REMOVE / DELETE inside a MATCH … WITH pipeline.

A query part may write the rows it matched and go on: ``MATCH (n:N) SET
n.num = n.num + 1 WITH n WHERE n.num % 2 = 0 RETURN n.num`` (TCK Set6),
``MATCH (n) WITH n, n.num AS num DELETE n WITH num … RETURN num`` (Delete6).
The pipeline's FOR loops are already in ``lines``; the writes nest inside
them, one ``UPDATE``/``REPLACE``/``REMOVE`` per target, and what follows
reads a written variable as the document after the write (``LET n_new =
NEW``), as Cypher's later clauses see the new values.

AQL refuses to read a collection after writing it in the same query
(ERR 1579), and to write one collection twice. So after a write the query
may filter, project, aggregate and ``UNWIND`` the rows it has — but a later
``MATCH``, ``CALL`` or second write is refused here, with the reason, rather
than handed to ArangoDB.

A variable an OPTIONAL MATCH binds is null on the rows it did not match;
SET, REMOVE and DELETE of null do nothing (openCypher), so a write of such a
variable runs over ``(x == null ? [] : [x])`` (:func:`nullable_vars`).
"""

from __future__ import annotations

import re
from typing import Any

from arango_query_core import AqlQuery, CoreError, MappingResolver

from .._antlr.CypherParser import CypherParser
from .naming import _aql_collection_ref, _pick_bind_key, _pick_fresh_var
from .property_writes import keep_fields_for

#: ``LET r = _opt_row.r``: a variable an OPTIONAL MATCH binds (null when it did not match).
_OPTIONAL_BINDING = re.compile(r"^\s*LET (\w+) = _opt_row\w*\.\w+\s*$")
#: ``LET a = DOCUMENT(@@c, _row.a._key)``: a plan step's import (null in a null row).
_STEP_IMPORT = re.compile(r"^\s*LET (\w+) = DOCUMENT\(@@\w+, _row\.")
#: ``LET x = r``: a WITH alias, null when its source is.
_COPY = re.compile(r"^\s*LET (\w+) = (\w+)\s*$")


def nullable_vars(lines: list[str]) -> set[str]:
    """The AQL variables of the pipeline *lines* that an OPTIONAL MATCH may leave null."""
    out: set[str] = set()
    for line in lines:
        m = _OPTIONAL_BINDING.match(line) or _STEP_IMPORT.match(line)
        if m is not None:
            out.add(m.group(1))
            continue
        m = _COPY.match(line)
        if m is not None and m.group(2) in out:
            out.add(m.group(1))
    return out


#: ``LET _set_n = (FOR _w IN … UPDATE …)``: a null-guarded write (:func:`emit_pipeline_writes`).
_GUARDED_WRITE = re.compile(r"^(\s*)LET \w+ = \((FOR _w IN .*)\)$")


def end_on_write(lines: list[str]) -> None:
    """Make a query that ends on a null-guarded write end on the write itself.

    AQL ends a query with RETURN or a write, not a LET: with nothing after it,
    the last ``LET _set_n = (FOR _w IN … UPDATE …)`` becomes ``FOR _w IN …
    UPDATE …`` — the same writes, as the last statement.
    """
    m = _GUARDED_WRITE.match(lines[-1]) if lines else None
    if m is not None:
        lines[-1] = m.group(1) + m.group(2)


def _unless_null(var: str) -> str:
    return f"({var} == null ? [] : [{var}])"


#: What a query may not do after it wrote mid-pipeline, and why.
AFTER_WRITE_REFUSAL = (
    "{what} after SET/REMOVE/DELETE in the same query is not supported: AQL cannot read a "
    "collection after writing it (ERR 1579), nor write one twice; run it as a separate statement"
)


def emit_pipeline_writes(
    *,
    set_clauses: list[CypherParser.OC_SetContext],
    delete_clauses: list[CypherParser.OC_DeleteContext],
    remove_clauses: list[Any],
    lines: list[str],
    var_env: dict[str, str],
    bind_vars: dict[str, Any],
    resolver: MappingResolver,
    rebind_to: set[str] | None = None,
) -> dict[str, str]:
    """Append the writes to *lines*; return the variable environment after them.

    With *rebind_to* (the AQL names in scope, extended in place) each written
    variable is rebound to its document after the write (``LET x_new =
    NEW``), for clauses that follow; without it *var_env* comes back as is.
    """
    from .writes import (
        _compile_expression,
        _infer_var_collections_from_pipeline,
        _property_writes,
        _refuse_repeated_collections,
        _rewrite_vars,
        _stored_write_line,
    )

    if not (set_clauses or delete_clauses or remove_clauses):
        raise CoreError(
            "SET/DELETE/REMOVE after WITH requires at least one mutating clause",
            code="UNSUPPORTED",
        )

    var_collections = _infer_var_collections_from_pipeline(lines)
    if not var_collections:
        raise CoreError(
            "SET/DELETE/REMOVE after WITH requires a document variable from MATCH",
            code="NOT_IMPLEMENTED",
        )

    def _coll_key_for(target_var: str) -> str:
        aql_var = var_env.get(target_var, target_var)
        key = var_collections.get(aql_var) or var_collections.get(target_var)
        if key is None:
            raise CoreError(
                f"SET/DELETE/REMOVE after WITH targets {target_var!r}, which is not a "
                "MATCH-bound document variable",
                code="NOT_IMPLEMENTED",
            )
        if key not in bind_vars:
            raise CoreError(
                f"Missing collection bind for SET/DELETE/REMOVE target {target_var!r}",
                code="UNSUPPORTED",
            )
        return key

    def _rewrite_target(name: str) -> str:
        return var_env.get(name, name)

    writes = _property_writes(
        set_clauses,
        remove_clauses,
        bind_vars=bind_vars,
        target_of=_rewrite_target,
        rewrite_value=(lambda value: _rewrite_vars(value, var_env)) if var_env else None,
    )
    physical = resolver.bundle.physical_mapping
    nullable = nullable_vars(lines)
    written: list[tuple[str, str]] = []
    env = dict(var_env)
    for target_var in writes.ops:
        key = _coll_key_for(target_var)
        write = writes.stored_write(target_var, keep_fields_for(bind_vars[key], physical))
        updated = None
        if rebind_to is not None:
            updated = _pick_fresh_var(f"{target_var}_new", forbidden_vars=rebind_to)
        if target_var in nullable:
            # Of null nothing is written, and the variable stays null after.
            statement = _stored_write_line("_w", _aql_collection_ref(key), write, indent="")
            if updated is not None:
                lines.append(
                    f"  LET {updated} = FIRST(FOR _w IN {_unless_null(target_var)} {statement} RETURN NEW)"
                )
            else:
                lines.append(f"  LET _set_{target_var} = (FOR _w IN {_unless_null(target_var)} {statement})")
        else:
            lines.append(_stored_write_line(target_var, _aql_collection_ref(key), write))
            if updated is not None:
                lines.append(f"  LET {updated} = NEW")
        written.append((f"SET/REMOVE on {target_var}", bind_vars[key]))
        if updated is not None:
            for name, aql in list(env.items()):
                if aql == target_var:
                    env[name] = updated
            if target_var not in var_env.values():
                env[target_var] = updated

    for dc in delete_clauses:
        is_detach = dc.DETACH() is not None
        for de in dc.oC_Expression() or []:
            del_var = _compile_expression(de, bind_vars)
            if var_env:
                del_var = _rewrite_vars(del_var, var_env)
            # Expression must resolve to a simple variable for collection lookup.
            simple = del_var.strip()
            if not re.fullmatch(r"\w+", simple):
                raise CoreError(
                    "DELETE after WITH only supports a simple MATCH-bound variable",
                    code="NOT_IMPLEMENTED",
                )
            if is_detach:
                for idx, ec in enumerate(resolver.all_edge_collections()):
                    ec_key = _pick_bind_key("@detachEdge", bind_vars)
                    bind_vars[ec_key] = ec
                    ec_ref = _aql_collection_ref(ec_key)
                    de_edge = f"_de{idx}"
                    start = f"_ds IN {_unless_null(simple)} FOR " if simple in nullable else ""
                    origin = "_ds" if start else simple
                    lines.append(
                        f"  LET _edgeRm{idx} = (FOR {start}{de_edge} IN 1..1 ANY {origin} {ec_ref} "
                        f"REMOVE {de_edge} IN {ec_ref})"
                    )
                    written.append((f"DETACH DELETE {simple} (its {ec} edges)", ec))
            coll_ref = _aql_collection_ref(_coll_key_for(simple))
            if simple in nullable:
                lines.append(
                    f"  LET _rm_{simple} = (FOR _w IN {_unless_null(simple)} REMOVE _w IN {coll_ref})"
                )
            else:
                lines.append(f"  REMOVE {simple} IN {coll_ref}")
            written.append((f"DELETE {simple}", bind_vars[_coll_key_for(simple)]))

    _refuse_repeated_collections(written)
    return env


def split_writes(
    clauses: list[CypherParser.OC_UpdatingClauseContext],
) -> tuple[list[CypherParser.OC_SetContext], list[CypherParser.OC_DeleteContext], list[Any]]:
    """The SET, DELETE and REMOVE clauses of *clauses*; anything else is refused."""
    sets, deletes, removes = [], [], []
    for clause in clauses:
        if clause.oC_Set() is not None:
            sets.append(clause.oC_Set())
        elif clause.oC_Delete() is not None:
            deletes.append(clause.oC_Delete())
        elif clause.oC_Remove() is not None:
            removes.append(clause.oC_Remove())
        else:
            kind = type(clause.getChild(0)).__name__.removeprefix("OC_").removesuffix("Context").upper()
            raise CoreError(
                f"{kind} before WITH is not supported here: only SET, REMOVE and DELETE may write "
                "before a WITH, unless every write of the query is a CREATE",
                code="UNSUPPORTED",
            )
    return sets, deletes, removes


def append_midquery_writes(
    clauses: list[CypherParser.OC_UpdatingClauseContext],
    *,
    lines: list[str],
    var_env: dict[str, str],
    bind_vars: dict[str, Any],
    resolver: MappingResolver,
    forbidden_vars: set[str],
    already_wrote: bool,
) -> dict[str, str]:
    """The writes of one query part that a WITH follows; the environment after them."""
    if already_wrote:
        raise CoreError(AFTER_WRITE_REFUSAL.format(what="A second write"), code="UNSUPPORTED")
    sets, deletes, removes = split_writes(clauses)
    return emit_pipeline_writes(
        set_clauses=sets,
        delete_clauses=deletes,
        remove_clauses=removes,
        lines=lines,
        var_env=var_env,
        bind_vars=bind_vars,
        resolver=resolver,
        rebind_to=forbidden_vars,
    )


def refuse_after_write(what: str) -> CoreError:
    """The refusal for a clause that would read or write after a mid-pipeline write."""
    return CoreError(AFTER_WRITE_REFUSAL.format(what=what), code="UNSUPPORTED")


def translate_multi_reading_mutation(
    spq: CypherParser.OC_SinglePartQueryContext,
    *,
    set_clauses: list[CypherParser.OC_SetContext],
    delete_clauses: list[CypherParser.OC_DeleteContext],
    remove_clauses: list[Any],
    resolver: MappingResolver,
    bind_vars: dict[str, Any],
) -> AqlQuery:
    """SET / REMOVE / DELETE after several reading clauses: ``MATCH (n) OPTIONAL
    MATCH (n)-[r]->() REMOVE r.num RETURN n`` (TCK Remove1 [6]).

    The reading clauses compile as a multi-part query's do (a MATCH, then
    MATCH, OPTIONAL MATCH — whose unmatched rows are kept, null-extended — or
    UNWIND reading the rows so far); the writes and the RETURN follow as after
    a WITH (:func:`writes._append_multipart_mutate_tail`), each row written
    after every read of it.
    """
    from .core import (
        _compile_following_match,
        _compile_leading_match,
        _compile_pipeline_unwind,
        _set_user_vars_from_parts,
    )
    from .writes import _append_multipart_mutate_tail

    reading = spq.oC_ReadingClause() or []
    first = reading[0].oC_Match() if reading else None
    if first is None:
        raise CoreError(
            "SET/DELETE/REMOVE after reading clauses that do not start with MATCH is not supported",
            code="UNSUPPORTED",
        )
    lines, forbidden_vars, var_env = _compile_leading_match(first, resolver=resolver, bind_vars=bind_vars)
    _set_user_vars_from_parts(first.oC_Pattern().oC_PatternPart() or [])
    for rc in reading[1:]:
        match, unwind = rc.oC_Match(), rc.oC_Unwind()
        if match is not None:
            var_env, _ = _compile_following_match(
                match,
                resolver=resolver,
                bind_vars=bind_vars,
                forbidden_vars=forbidden_vars,
                var_env=var_env,
                lines=lines,
            )
            _set_user_vars_from_parts(match.oC_Pattern().oC_PatternPart() or [])
        elif unwind is not None:
            var_env = _compile_pipeline_unwind(
                unwind, bind_vars=bind_vars, forbidden_vars=forbidden_vars, var_env=var_env, lines=lines
            )
        else:
            raise CoreError(
                f"SET/DELETE/REMOVE after {rc.getText()[:40]!r} is not supported: only MATCH, "
                "OPTIONAL MATCH and UNWIND may read before them",
                code="UNSUPPORTED",
            )
    return _append_multipart_mutate_tail(
        set_clauses=set_clauses,
        delete_clauses=delete_clauses,
        remove_clauses=remove_clauses,
        tail=spq,
        lines=lines,
        var_env=var_env,
        bind_vars=bind_vars,
        resolver=resolver,
    )
