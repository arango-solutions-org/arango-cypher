"""Relationships of several types, ``-[:A|B]->`` (LDBC plan WP-38).

The pattern's types travel as one ``A|B`` string and resolve to one mapping,
so every traversal emitter handles them unchanged:

- dedicated edge collections (PG): ``edgeCollectionName`` is the list of the
  types' collections, and a traversal follows them all
  (``OUTBOUND u @@e, @@e_1``);
- one type-discriminated collection (LPG): ``typeValue`` is the list of the
  types' values, and the type filter is a membership test
  (``e[@tf] IN @tv``).

The emitters bind those lists as they bind single values. A last pass,
:func:`expand_multi_type_bindings`, rewrites the text around them: a list of
collections becomes one bind parameter per collection, and a comparison of
the type field against a list becomes ``IN`` / ``NOT IN`` (``ALL IN`` for the
type guard's path condition). It runs after
``_guard_varlength_edge_types``, which therefore still sees one edge
collection per traversal and turns the type test into a PRUNE as usual.
"""

from __future__ import annotations

import re
from typing import Any

from arango_query_core import AqlQuery, CoreError, MappingResolver

from .literals import _aql_string_literal
from .naming import _strip_label_backticks

MULTI_TYPE_SEPARATOR = "|"


def _edge_collection(rmap: dict[str, Any]) -> Any:
    return rmap.get("edgeCollectionName") or rmap.get("collectionName")


def _is_edge_collection_value(value: Any) -> bool:
    """An edge collection binding: a name, or (several types) a list of names."""
    if isinstance(value, list):
        return bool(value) and all(isinstance(v, str) and v for v in value)
    return isinstance(value, str) and bool(value)


def resolve_multi_type(rel_type: str, resolver: MappingResolver) -> dict[str, Any]:
    """One relationship mapping for the types of ``A|B``.

    Refuses a mix of dedicated and type-discriminated mappings, and
    type-discriminated ones in different collections or under different type
    fields: no single traversal follows them.
    """
    types = [_strip_label_backticks(t) for t in rel_type.split(MULTI_TYPE_SEPARATOR)]
    maps = [resolver.resolve_relationship(t) for t in types]
    styles = {m.get("style") for m in maps}
    if styles == {"DEDICATED_COLLECTION"}:
        collections: list[str] = []
        for m in maps:
            if _edge_collection(m) not in collections:
                collections.append(_edge_collection(m))
        return {
            "style": "DEDICATED_COLLECTION",
            "edgeCollectionName": collections[0] if len(collections) == 1 else collections,
            "types": types,
        }
    if styles == {"GENERIC_WITH_TYPE"}:
        collections_set = {_edge_collection(m) for m in maps}
        type_fields = {m.get("typeField") for m in maps}
        if len(collections_set) == 1 and len(type_fields) == 1:
            return {
                "style": "GENERIC_WITH_TYPE",
                "edgeCollectionName": collections_set.pop(),
                "typeField": type_fields.pop(),
                "typeValue": [m.get("typeValue") for m in maps],
                "types": types,
            }
    raise CoreError(
        f"Relationship types {rel_type} are mapped to different edge collections or styles; "
        "one traversal cannot follow them all",
        code="UNSUPPORTED",
    )


def multi_type_runtime_expr(ref: str, rel_type: str, resolver: MappingResolver) -> str:
    """``type(r)`` of an ``[r:A|B]`` edge in dedicated collections: the type
    whose collection the edge lives in."""
    out = "null"
    for t in reversed(rel_type.split(MULTI_TYPE_SEPARATOR)):
        coll = _edge_collection(resolver.resolve_relationship(_strip_label_backticks(t)))
        out = (
            f"(PARSE_IDENTIFIER({ref}._id).collection == {_aql_string_literal(coll)} "
            f"? {_aql_string_literal(_strip_label_backticks(t))} : {out})"
        )
    return out


def expand_multi_type_bindings(query: AqlQuery) -> AqlQuery:
    """Rewrite list-valued bindings a multi-type relationship left (module docstring)."""
    text = query.text
    bind_vars = dict(query.bind_vars)
    for key, value in list(bind_vars.items()):
        if not isinstance(value, list):
            continue
        if key.startswith("@"):
            keys = [key]
            bind_vars[key] = value[0]
            for i, coll in enumerate(value[1:], start=1):
                extra = f"{key}_{i}"
                while extra in bind_vars:
                    extra += "_"
                bind_vars[extra] = coll
                keys.append(extra)
            refs = ", ".join(f"@{k}" for k in keys)
            text = re.sub(rf"@@{re.escape(key[1:])}\b", refs.replace("\\", "\\\\"), text)
        else:
            # Only the internal type test compares a ``[@typeField]`` with a
            # list; a user parameter holding a list is left alone.
            text = re.sub(rf"(\[@\w+\]) == @{re.escape(key)}\b", rf"\1 IN @{key}", text)
            text = re.sub(rf"(\[@\w+\]) != @{re.escape(key)}\b", rf"\1 NOT IN @{key}", text)
            # The type guard's path condition (``p.edges[*][@tf] ALL == @tv``).
            text = re.sub(rf"(\[@\w+\]) ALL == @{re.escape(key)}\b", rf"\1 ALL IN @{key}", text)
    return AqlQuery(text=text, bind_vars=bind_vars, debug=query.debug, warnings=query.warnings)
