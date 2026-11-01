"""Write clauses a translator would drop: refused, or (SET after MERGE) folded in.

The single-part router (``core._translate_single_query``) hands a query part
to one write translator by the kind of write it sees, and each compiles only
its own clauses. A clause it does not compile used to be left out of the AQL
without an error — silent data loss. These guards refuse such clauses with the
reason (``UNSUPPORTED``); :func:`trailing_set_fields` compiles the one mix
that has a direct UPSERT form, ``MERGE (n …) SET n.p = v``.
"""

from __future__ import annotations

import re
from typing import Any

from arango_query_core import CoreError

from .._antlr.CypherParser import CypherParser
from .naming import _strip_label_backticks


def refuse_dropped_writes(translator: str, **others: list[Any]) -> None:
    """Refuse write clauses the *translator* chosen for a query part would drop.

    The single-part router picks one translator by the kind of write it sees
    (FOREACH, MERGE, CREATE, …); each compiles its own clauses only. A clause
    of another kind beside it (``CREATE (:X) MERGE (:X)``, ``MATCH (a) SET
    a.x = 1 FOREACH (…)``) used to be left out of the AQL without a word —
    silent data loss — so it is refused with the reason instead.
    """
    present = [kind for kind, clauses in others.items() if clauses]
    if present:
        raise CoreError(
            f"{' and '.join(present)} beside {translator} in one query part is not supported: only the "
            f"{translator} would be written. Run them as separate statements.",
            code="UNSUPPORTED",
        )


def refuse_untranslated_foreach_parts(
    spq: CypherParser.OC_SinglePartQueryContext, foreach_clauses: list[CypherParser.OC_ForeachContext]
) -> None:
    """Refuse the parts of a FOREACH query :func:`_translate_foreach_query` would drop."""
    if len(foreach_clauses) > 1:
        raise CoreError(
            "Several FOREACH clauses in one query part are not supported: the second would run once "
            "per element of the first's list",
            code="UNSUPPORTED",
        )
    for rc in spq.oC_ReadingClause() or []:
        match = rc.oC_Match()
        if match is None or match.OPTIONAL() is not None:
            raise CoreError(
                f"{'OPTIONAL MATCH' if match is not None else 'UNWIND or CALL'} before FOREACH is not "
                "supported: only MATCH is compiled there",
                code="UNSUPPORTED",
            )
    if spq.oC_Return() is not None:
        raise CoreError("RETURN after FOREACH is not supported: it would not be returned", code="UNSUPPORTED")


def trailing_set_fields(
    trailing_sets: list[CypherParser.OC_SetContext], merged_var: str, bind_vars: dict[str, Any]
) -> list[tuple[str, str]]:
    """``(property, AQL value)`` for each ``SET n.p = v`` after a node MERGE.

    Only a property of the merged node, set from a value that does not read
    the node, can join the UPSERT documents (the document is not bound
    there); anything else would be dropped and is refused.
    """
    from .writes import _atom_variable, _compile_expression

    out: list[tuple[str, str]] = []
    for set_ctx in trailing_sets:
        for si in set_ctx.oC_SetItem() or []:
            prop_expr = si.oC_PropertyExpression()
            target = _atom_variable(prop_expr.oC_Atom()) if prop_expr is not None else None
            lookups = (prop_expr.oC_PropertyLookup() or []) if prop_expr is not None else []
            if not merged_var or target != merged_var or len(lookups) != 1:
                raise CoreError(
                    f"SET after MERGE is supported only for a property of the merged node ({si.getText()} "
                    "would not be written)",
                    code="UNSUPPORTED",
                )
            if merged_var in _variables_read(si.oC_Expression()):
                raise CoreError(
                    f"SET after MERGE reading the merged node ({si.getText()}) is not supported",
                    code="UNSUPPORTED",
                )
            out.append(
                (
                    lookups[0].oC_PropertyKeyName().getText().strip(),
                    _compile_expression(si.oC_Expression(), bind_vars),
                )
            )
    return out


def _variables_read(ctx: Any) -> set[str]:
    """The variable names an expression reads."""
    found: set[str] = set()
    stack = [ctx]
    while stack:
        node = stack.pop()
        if isinstance(node, CypherParser.OC_VariableContext):
            found.add(_strip_label_backticks(node.getText().strip()))
        stack.extend(
            node.getChild(i) for i in range(node.getChildCount() if hasattr(node, "getChildCount") else 0)
        )
    return found


#: A data-modification operation in AQL text (the translator spells them in capitals).
_MODIFICATION = re.compile(r"\b(?:INSERT|UPDATE|REPLACE|REMOVE|UPSERT)\b")
_STRING_LITERAL = re.compile(r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"")


def refuse_document_after_write(aql: str) -> None:
    """Refuse AQL that calls ``DOCUMENT()`` after a write in the same statement.

    ArangoDB refuses it while optimizing (ERR 1579, "access after
    data-modification by function DOCUMENT"), whichever collection either
    names: ``MERGE (a)-[r:T]-(b) RETURN startNode(r)`` compiled to such a
    query. Refused here, the query can run as a multi-statement plan, the
    read in a step of its own (``arango_cypher.plans``).
    """
    wrote = False
    for line in aql.splitlines():
        code = _STRING_LITERAL.sub("''", line)
        write = _MODIFICATION.search(code)
        after_write = code if wrote else (code[write.end() :] if write else "")
        if "DOCUMENT(" in after_write:
            raise CoreError(
                "This query reads a document (DOCUMENT(), e.g. for startNode()/endNode()) after "
                "writing in the same AQL statement, which ArangoDB refuses (ERR 1579)",
                code="NOT_IMPLEMENTED",
            )
        wrote = wrote or write is not None
