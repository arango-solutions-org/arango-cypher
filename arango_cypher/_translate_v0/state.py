"""Shared mutable translation state for the v0 translator."""

from __future__ import annotations

from collections.abc import Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from arango_query_core import ExtensionPolicy, ExtensionRegistry, MappingResolver

_active_registry: ContextVar[ExtensionRegistry | None] = ContextVar(
    "_active_registry",
    default=None,
)
_active_resolver: ContextVar[MappingResolver | None] = ContextVar(
    "_active_resolver",
    default=None,
)
_active_warnings: ContextVar[list[str]] = ContextVar(
    "_active_warnings",
    default=[],  # noqa: B039  # always .set() before .get() in translate_v0
)
_active_path_vars: ContextVar[dict[str, tuple[list[str], list[str]]]] = ContextVar(
    "_active_path_vars",
    default={},  # noqa: B039  # always .set() before .get() in translate_v0
)
# ``TranslateOptions.case_insensitive_inline_strings`` of the running translation.
_active_case_insensitive_inline: ContextVar[bool] = ContextVar(
    "_active_case_insensitive_inline",
    default=False,
)

#: ``UNWIND`` clauses after the MATCH of a single-part query, as AQL ``FOR``
#: lines and their variables: the RETURN compiler emits them first, so they
#: precede any COLLECT, SORT or LIMIT of the projection (``_append_return``).
_pending_unwinds: ContextVar[tuple[list[str], list[str]] | None] = ContextVar(
    "_pending_unwinds", default=None
)

#: The caller's parameter names: bind variables merged from a CALL subquery
#: never rename them (``_merge_bind_vars``).
_active_user_params: ContextVar[frozenset[str]] = ContextVar("_active_user_params", default=frozenset())

#: Every RETURN row is an object keyed by its columns, even a single column
#: (which otherwise comes back bare). Set while a CALL subquery body compiles,
#: so the caller can read its columns by name.
_object_rows: ContextVar[bool] = ContextVar("_object_rows", default=False)

#: Cypher variables a WITH bound to a temporal value or a duration, with which
#: (``"computed"`` / ``"duration"``; ``temporal.bind_with``).
_temporal_vars: ContextVar[Mapping[str, str]] = ContextVar("_temporal_vars", default=MappingProxyType({}))

# Ordered list of the *user-named* variables currently in scope, used to expand
# ``RETURN *`` / ``WITH *`` (auto-generated traversal / anonymous-node variables
# must not appear in a ``*`` projection). Pattern compilation sets it from the
# matched pattern; ``WITH`` replaces it with the projected variables.
_active_user_vars: ContextVar[list[str]] = ContextVar(
    "_active_user_vars",
    default=[],  # noqa: B039  # always .set() before .get() in translate_v0
)


@dataclass
class _HopMeta:
    """Pre-processed metadata for a single hop in a relationship chain."""

    v_var: str
    v_trav: str
    v_labels: list[str]
    v_primary: str | None
    v_map: dict[str, Any] | None
    v_bound: bool
    v_prop_filters: list[str]
    rel_type: str | None
    rel_var: str
    rel_range: tuple[int, int]
    rel_named: bool
    r_prop_filters: list[str]
    direction: str
    r_map: dict[str, Any]
    r_style: str | None
    edge_collection: str
    edge_key: str
    r_type_field: str | None
    r_type_value: str | None
    #: A variable-length hop whose edges are needed (a named relationship, or
    #: a named path): the traversal's path variable, and the Cypher name bound
    #: to its list of edges (``LET r = path.edges``; ``rel_var`` is then the
    #: traversal's last-edge variable).
    hop_path: str | None = None
    rel_list: str | None = None


@dataclass(frozen=True)
class TranslateOptions:
    extensions: ExtensionPolicy = ExtensionPolicy(enabled=False)
    registry: ExtensionRegistry | None = None
    #: Inline string properties (``(n {name: 'Alice'})``) compare
    #: case-insensitively. Off by default: Cypher compares exactly, and
    #: ``LOWER()`` keeps an index from serving the lookup.
    case_insensitive_inline_strings: bool = False
    #: Name an unaliased RETURN column by its expression as written
    #: (``n.name``, ``count(*)``), as openCypher does, instead of the inferred
    #: short key (``name``, ``count``) existing clients read (``columns.py``).
    cypher_column_names: bool = False
    #: Read a string in one of Neo4j's temporal forms (``'12:31+01:00'``,
    #: ``'P1D'``) as the temporal value it spells where its type is only known
    #: at run time: accessors, arithmetic with durations, ORDER BY
    #: (``temporal_strings.py``). Off by default: such a string is then text.
    temporal_strings: bool = False
