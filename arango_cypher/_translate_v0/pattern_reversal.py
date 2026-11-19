"""Read a pattern from its bound end (``MATCH (m:Message)-[:HAS_TAG]->(tag)``
after ``tag`` is bound).

A MATCH after WITH, and an OPTIONAL MATCH, compile a pattern from its first
node. When that node is new and the last one is already bound, the pattern
scanned every ``Message`` for each ``tag`` and kept those whose traversal
reached it: right rows, but the work of a cross product (LDBC BI2 ran past the
30 s limit at the smallest scale). Read backwards, with each relationship's
direction flipped, the same pattern is a traversal from the bound ``tag``.

The reversed pattern is a view over the parse tree with the accessors the
bound-pattern compiler reads; nothing is copied or re-parsed.

A first MATCH reads a pattern from its *selective* end the same way
(:func:`from_selective_end`, WP-44): a pattern written from an unconstrained
node, ``(p:Person)<-[:HAS_CREATOR]-(m)-[:HAS_TAG]->(t:Tag {name: $tag})``,
scanned every Person, when the Tag's name index finds the one tag the rows
start from (LDBC BI16). Which end is selective is decided from the mapping's
indexes alone, so the translation needs no database statistics.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from typing import Any

from arango_query_core import CoreError, MappingResolver

from .._antlr.CypherParser import CypherParser
from .naming import _strip_label_backticks

#: Index types whose leading field serves an equality lookup.
_LOOKUP_INDEX_TYPES = frozenset({"persistent", "hash", "skiplist"})


class _ReversedRelationship:
    """A relationship pattern with its arrowheads swapped."""

    def __init__(self, ctx: CypherParser.OC_RelationshipPatternContext) -> None:
        self._ctx = ctx

    def oC_LeftArrowHead(self) -> Any:  # noqa: N802 — mirrors the ANTLR accessor
        return self._ctx.oC_RightArrowHead()

    def oC_RightArrowHead(self) -> Any:  # noqa: N802
        return self._ctx.oC_LeftArrowHead()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._ctx, name)


class _Chain:
    def __init__(self, relationship: Any, node: CypherParser.OC_NodePatternContext) -> None:
        self._relationship = relationship
        self._node = node

    def oC_RelationshipPattern(self) -> Any:  # noqa: N802
        return self._relationship

    def oC_NodePattern(self) -> CypherParser.OC_NodePatternContext:  # noqa: N802
        return self._node


class _ReversedElement:
    def __init__(self, elem: CypherParser.OC_PatternElementContext) -> None:
        chains = elem.oC_PatternElementChain() or []
        nodes = [elem.oC_NodePattern(), *(c.oC_NodePattern() for c in chains)]
        relationships = [c.oC_RelationshipPattern() for c in chains]
        self._start = nodes[-1]
        self._chains = [
            _Chain(_ReversedRelationship(relationships[i]), nodes[i])
            for i in reversed(range(len(relationships)))
        ]

    def oC_NodePattern(self) -> CypherParser.OC_NodePatternContext:  # noqa: N802
        return self._start

    def oC_PatternElementChain(self) -> list[_Chain]:  # noqa: N802
        return self._chains


def _variable(node: CypherParser.OC_NodePatternContext | None) -> str | None:
    if node is None or node.oC_Variable() is None:
        return None
    return node.oC_Variable().getText().strip()


def from_bound_end(elem: CypherParser.OC_PatternElementContext, bound: Mapping[str, str]) -> Any:
    """*elem*, or its reversal when its first node is new and its last one bound."""
    chains = elem.oC_PatternElementChain() or []
    if elem.oC_NodePattern() is None or not chains:
        return elem
    if _variable(elem.oC_NodePattern()) in bound:
        return elem
    if _variable(chains[-1].oC_NodePattern()) not in bound:
        return elem
    return _ReversedElement(elem)


def _inline_keys(node: CypherParser.OC_NodePatternContext) -> list[str]:
    """The keys of *node*'s inline property map (``{name: $x}``), if it has a literal one."""
    props = node.oC_Properties()
    if props is None or props.oC_MapLiteral() is None:
        return []
    return [k.getText().strip() for k in props.oC_MapLiteral().oC_PropertyKeyName() or []]


def _labels(node: CypherParser.OC_NodePatternContext) -> list[str]:
    labels_ctx = node.oC_NodeLabels()
    if labels_ctx is None:
        return []
    return [
        _strip_label_backticks(nl.oC_LabelName().getText().strip()) for nl in labels_ctx.oC_NodeLabel() or []
    ]


def _label_has_lookup(label: str, keys: list[str], resolver: MappingResolver) -> bool:
    """Whether an index of *label* leads with the field of one of *keys*.

    A ``LABEL`` entity's type field leading the index is skipped: the label
    test fixes it (LPG's ``(type, name)``). A supertype has a lookup when
    every member has one, since its scan runs per member.
    """
    try:
        if resolver.is_supertype(label):
            return all(_label_has_lookup(m, keys, resolver) for m in resolver.supertype_members(label))
        emap = resolver.resolve_entity(label)
    except CoreError:
        return False
    props = resolver.resolve_properties(label)
    fields = {props[k].field if k in props else k for k in keys}
    type_field = emap.get("typeField") if emap.get("style") == "LABEL" else None
    for idx in resolver.resolve_indexes(label):
        if idx.vci or idx.type not in _LOOKUP_INDEX_TYPES or not idx.fields:
            continue
        lead = list(idx.fields)
        if type_field and lead[0] == type_field:
            lead = lead[1:]
        if lead and lead[0] in fields:
            return True
    return False


def _has_lookup(node: CypherParser.OC_NodePatternContext, resolver: MappingResolver) -> bool:
    """Whether *node* is found by an index: an inline property equality on an indexed field."""
    keys = _inline_keys(node)
    return bool(keys) and any(_label_has_lookup(lab, keys, resolver) for lab in _labels(node))


def _order_is_observable(elem: CypherParser.OC_PatternElementContext) -> bool:
    """Whether reading *elem* backwards would change a value the query sees:
    a variable-length relationship's variable lists its relationships in path order."""
    for chain in elem.oC_PatternElementChain() or []:
        detail = chain.oC_RelationshipPattern().oC_RelationshipDetail()
        if detail is not None and detail.oC_RangeLiteral() is not None and detail.oC_Variable() is not None:
            return True
    return False


def from_selective_end(
    elem: CypherParser.OC_PatternElementContext,
    resolver: MappingResolver,
    *,
    bound: Collection[str] = (),
    named: bool = False,
) -> Any:
    """*elem*, or its reversal when only its last node is found by an index.

    Kept as written when either end is already *bound* (a bound end is the
    start already, or :func:`from_bound_end`'s to choose), when the first node
    has an index lookup of its own, when the last one has none, or when the
    order is observable: a named path (*named*) or a variable-length
    relationship with a variable.
    """
    chains = elem.oC_PatternElementChain() or []
    if named or elem.oC_NodePattern() is None or not chains or _order_is_observable(elem):
        return elem
    if _variable(elem.oC_NodePattern()) in bound or _variable(chains[-1].oC_NodePattern()) in bound:
        return elem
    if _has_lookup(elem.oC_NodePattern(), resolver):
        return elem
    if not _has_lookup(chains[-1].oC_NodePattern(), resolver):
        return elem
    return _ReversedElement(elem)
