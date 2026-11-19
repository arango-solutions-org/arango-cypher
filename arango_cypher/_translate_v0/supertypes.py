"""Where a matched node's documents live: one label's collection, or a
``UNION`` supertype's member collections (PRD §8.4, WP-35 stage 2).

A ``UNION`` supertype (LDBC's ``Message`` over PG's ``post`` and ``comment``
collections; arango-query-core ``SUPERTYPE_STYLES``) has no collection of its
own, so the two places a node's collection enters the AQL are lowered over its
members instead:

* **Scan start** — ``FOR m IN @@collection`` becomes a ``UNION`` of one
  subquery per member. The node's inline property filters are pushed into each
  branch, so ``(m:Message {id: $id})`` uses each member's index instead of
  materialising every member document first.
* **Collection check** — ``IS_SAME_COLLECTION(@c, v)`` on a traversal target or
  label predicate becomes "``v`` is a document of one of the members".

A member mapped ``COLLECTION`` is identified by its collection alone; a member
mapped ``LABEL`` (a type-discriminated shared collection, as in a hybrid
layout) also needs its type field. Other member styles fail closed.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from arango_query_core import CoreError, MappingResolver

from .label_tests import label_test, labels_field
from .naming import _aql_collection_ref, _pick_bind_key, _strip_label_backticks


def _collection_of(label: str, emap: dict[str, Any]) -> str:
    coll = emap.get("collectionName")
    if not isinstance(coll, str) or not coll:
        raise CoreError(f"Invalid entity mapping collectionName for: {label}", code="INVALID_MAPPING")
    return coll


def _union_members(label: str, resolver: MappingResolver) -> list[tuple[str, dict[str, Any]]]:
    """The concrete members of the ``UNION`` supertype *label* with their mappings."""
    label = _strip_label_backticks(label)
    members = [(m, resolver.resolve_entity(m)) for m in resolver.supertype_members(label)]
    for member, emap in members:
        _collection_of(member, emap)
        style = emap.get("style")
        if style not in ("COLLECTION", "LABEL"):
            raise CoreError(
                f"Supertype {label!r} is mapped UNION, but its member {member!r} is mapped {style}: "
                "only COLLECTION and LABEL members can be unioned",
                code="INVALID_MAPPING",
            )
        if style == "LABEL" and not (emap.get("typeField") and emap.get("typeValue")):
            raise CoreError(
                f"LABEL member {member!r} of supertype {label!r} needs 'typeField' and 'typeValue'",
                code="INVALID_MAPPING",
            )
    return members


def _member_type_filter(var: str, member: str, emap: dict[str, Any], bind_vars: dict[str, Any]) -> str:
    tf_key = _pick_bind_key(f"{var}MemberTypeField", bind_vars)
    tv_key = _pick_bind_key(f"{var}MemberTypeValue", bind_vars)
    bind_vars[tf_key] = emap["typeField"]
    bind_vars[tv_key] = emap["typeValue"]
    return label_test(var, tf_key, tv_key, emap, bind_vars)


def multi_label_member_types(emap: dict[str, Any], resolver: MappingResolver) -> tuple[str, list[str]] | None:
    """``(typeField, [typeValue, …])`` naming a ``MULTI_LABEL`` supertype's
    members by their type field, or ``None`` when the members do not allow it.

    A ``MULTI_LABEL`` supertype is found by its labels array, which only an
    array index on that field can serve, so ``(m:Message {id: $id})`` read
    every Message to find one (LDBC IS4 on LPG). When every member is a
    ``LABEL`` entity in the supertype's collection, discriminated by one type
    field and with no labels array of its own (so the type field alone decides
    membership), the supertype's documents are those whose type is a member's.
    That test can use the type-led indexes an LPG collection has; the caller
    keeps the labels-array test beside it, so the rows do not change.
    """
    coll = emap.get("collectionName")
    name = next(
        (n for n in resolver.all_entity_labels() if n and resolver.resolve_entity(n) == emap),
        None,
    )
    if name is None or not isinstance(coll, str) or not coll:
        return None
    type_fields: set[str] = set()
    values: list[str] = []
    for member in resolver.supertype_members(name):
        m = resolver.resolve_entity(member)
        tf, tv = m.get("typeField"), m.get("typeValue")
        if (
            m.get("style") != "LABEL"
            or m.get("collectionName") != coll
            or labels_field(m) is not None
            or not (isinstance(tf, str) and tf and isinstance(tv, str) and tv)
        ):
            return None
        type_fields.add(tf)
        if tv not in values:
            values.append(tv)
    if len(type_fields) != 1:
        return None
    return type_fields.pop(), values


def union_membership_filter(
    var: str, label: str, resolver: MappingResolver, bind_vars: dict[str, Any]
) -> str:
    """An AQL boolean that is true when *var* is a document of one of *label*'s members.

    Members in their own collections share one ``IN`` test over the collection
    names; ``PARSE_IDENTIFIER(null).collection`` is ``null``, so a null *var*
    (an unmatched OPTIONAL node) is not a member.
    """
    whole: list[str] = []
    parts: list[str] = []
    for member, emap in _union_members(label, resolver):
        coll = _collection_of(member, emap)
        if emap.get("style") == "COLLECTION":
            if coll not in whole:
                whole.append(coll)
            continue
        coll_key = _pick_bind_key(f"{var}MemberColl", bind_vars)
        bind_vars[coll_key] = coll
        parts.append(
            f"(IS_SAME_COLLECTION(@{coll_key}, {var}) AND {_member_type_filter(var, member, emap, bind_vars)})"
        )
    if whole:
        key = _pick_bind_key(f"{var}Members", bind_vars)
        bind_vars[key] = whole
        parts.insert(0, f"PARSE_IDENTIFIER({var}).collection IN @{key}")
    return parts[0] if len(parts) == 1 else "(" + " OR ".join(parts) + ")"


def union_scan_source(
    var: str,
    label: str,
    resolver: MappingResolver,
    bind_vars: dict[str, Any],
    *,
    filters: Sequence[str] = (),
    after: str | None = None,
) -> str:
    """An AQL expression over every document of *label*'s members, for ``FOR var IN <it>``.

    Each branch declares *var* itself, so *filters* (written against *var*)
    apply inside it unchanged; AQL accepts the outer ``FOR var`` declared
    after the branches close.

    *after* names a variable already bound where the scan runs. The branches
    then read it (``FILTER x == x``, true for any value, null included), so
    ArangoDB cannot hoist them above the loop that binds it: hoisted, the
    member documents ride along every row of that loop and the query's memory
    grows with rows × documents (LDBC BI1 on PG, 3,660 messages, passed 1 GB).
    That growth is ArangoDB 3.11's: on 3.12.12 the same hoisted plan peaks at
    about 2 MB. The dependency can go when the correctness tier moves off 3.11
    (``tests/integration/test_supertype_scan_placement_live.py`` pins it).
    """
    branches: list[str] = []
    seen: set[str] = set()
    for member, emap in _union_members(label, resolver):
        coll = _collection_of(member, emap)
        member_filters = list(filters)
        if after is not None:
            member_filters.insert(0, f"{after} == {after}")
        if emap.get("style") == "LABEL":
            member_filters.insert(0, _member_type_filter(var, member, emap, bind_vars))
        elif coll in seen:
            continue
        seen.add(coll)
        key = _pick_bind_key(f"@{var}Member", bind_vars)
        bind_vars[key] = coll
        body = "".join(f" FILTER {f}" for f in member_filters)
        branches.append(f"(FOR {var} IN {_aql_collection_ref(key)}{body} RETURN {var})")
    if len(branches) == 1:
        return branches[0]
    return "UNION(" + ", ".join(branches) + ")"


def node_collection_filter(
    var: str,
    label: str,
    emap: dict[str, Any],
    resolver: MappingResolver,
    bind_vars: dict[str, Any],
    *,
    key_base: str,
) -> str:
    """The check that *var* is in *label*'s collection — the members' for a ``UNION`` supertype."""
    if emap.get("style") == "UNION":
        return union_membership_filter(var, label, resolver, bind_vars)
    key = _pick_bind_key(key_base, bind_vars)
    bind_vars[key] = _collection_of(label, emap)
    return f"IS_SAME_COLLECTION(@{key}, {var})"


def node_scan_source(
    var: str,
    label: str,
    emap: dict[str, Any],
    resolver: MappingResolver,
    bind_vars: dict[str, Any],
    *,
    coll_key: str,
    filters: Sequence[str] = (),
    after: str | None = None,
) -> tuple[str, bool]:
    """What ``FOR var IN …`` iterates for a node labelled *label*.

    Returns ``(source, pushed)``. For a ``UNION`` supertype *source* is the
    member union and *pushed* is true: *filters* are already applied inside it
    and the caller must not apply them again. Otherwise *coll_key* is bound to
    the label's collection, *source* is its ``@@`` reference, and *filters* are
    left to the caller. *after* is as for :func:`union_scan_source`.
    """
    if emap.get("style") == "UNION":
        return union_scan_source(var, label, resolver, bind_vars, filters=filters, after=after), True
    bind_vars[coll_key] = _collection_of(label, emap)
    return _aql_collection_ref(coll_key), False
