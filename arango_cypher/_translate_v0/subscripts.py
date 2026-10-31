"""``x[i]`` with Cypher's run-time type errors.

AQL's ``x[i]`` is null for every ill-typed access: an index into a number, a
string or a boolean; a list read with a string key; a map read with a number.
Cypher raises a TypeError there (List1 [7]/[9], Map2 [6]-[8]). When the query
does not show that the access is well typed — a list read with an integer, a
map read with a string — the subscript checks the run-time types and fails
the query with the Cypher error (``FAIL()``); null on either side is null.
Statically well-typed accesses compile to the plain ``(x)[i]``.
"""

from __future__ import annotations

import re

from .arithmetic import INT, LIST, MAP, STRING
from .literals import _aql_string_literal

_SIMPLE = re.compile(r"@?[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*|-?\d+")

_NOT_LIST_INDEX = _aql_string_literal("TypeError (InvalidArgumentType): a list index must be an integer")
_NOT_MAP_KEY = _aql_string_literal("TypeError (MapElementAccessByNonString): a map key must be a string")
_NOT_INDEXABLE = _aql_string_literal("TypeError (InvalidArgumentType): only a list or a map can be indexed")


def compile_subscript(base: str, index: str, base_type: str | None, index_type: str | None) -> str:
    """``base[index]``, guarded unless the static types show it well typed."""
    if (base_type == LIST and index_type == INT) or (base_type == MAP and index_type == STRING):
        return f"({base})[{index}]"
    body = (
        "(_b == null OR _i == null ? null"
        f" : IS_ARRAY(_b) ? (IS_NUMBER(_i) AND _i == FLOOR(_i) ? _b[_i] : FAIL({_NOT_LIST_INDEX}))"
        f" : IS_OBJECT(_b) ? (IS_STRING(_i) ? _b[_i] : FAIL({_NOT_MAP_KEY}))"
        f" : FAIL({_NOT_INDEXABLE}))"
    )
    if _SIMPLE.fullmatch(base.strip()) and _SIMPLE.fullmatch(index.strip()):
        # Plain references: repeat them rather than open a subquery.
        return re.sub(r"\b_b\b", lambda _: base.strip(), re.sub(r"\b_i\b", lambda _: index.strip(), body))
    # Distinct names per nesting level: ``x[0][1]`` nests one guard in another.
    k = len(re.findall(r"FOR _sb\d+ IN", base + index))
    b, i = f"_sb{k}", f"_si{k}"
    body = re.sub(r"\b_b\b", b, re.sub(r"\b_i\b", i, body))
    return f"FIRST(FOR {b} IN [{base}] FOR {i} IN [{index}] RETURN {body})"
