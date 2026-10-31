"""UPSERT documents for MERGE actions that set a whole map.

``ON CREATE SET r = a`` replaces the relationship's properties with ``a``'s
(a node's properties, or a map); ``ON MATCH SET r += {…}`` merges a map into
them (a null value removing that key). An AQL object literal cannot spread a
map, so these actions turn the UPSERT's documents into expressions:

- INSERT: the pattern's document, then each action in order — a property
  (``MERGE(doc, {k: v})``), a ``+=`` map (``MERGE(doc, map)``), a ``=`` map
  (``MERGE(KEEP(doc, <system keys>), map)``) — and null values dropped;
- UPDATE becomes ``REPLACE`` of ``OLD`` with the same actions applied, so a
  ``=`` removes the keys the map does not have.

The system keys (``_from``, ``_to``, the type field) survive a ``=``. Actions
that set only properties keep the plain ``{…}`` documents and UPDATE.
"""

from __future__ import annotations


class MapAction(str):
    """An action's value that is a whole map; *replaces* for ``=``, else ``+=``."""

    replaces: bool

    def __new__(cls, value: str, *, replaces: bool) -> MapAction:
        action = super().__new__(cls, value)
        action.replaces = replaces
        return action


def has_map_actions(fields: list[str]) -> bool:
    return any(isinstance(f, MapAction) for f in fields)


def _apply(doc: str, fields: list[str], keep: list[str]) -> str:
    """*doc* with *fields* (``k: v`` strings and :class:`MapAction`) applied in order."""
    run: list[str] = []
    for field in [*fields, None]:
        if field is not None and not isinstance(field, MapAction):
            run.append(field)
            continue
        if run:
            doc = f"MERGE({doc}, {{{', '.join(run)}}})"
            run = []
        if isinstance(field, MapAction):
            if field.replaces:
                kept = ", ".join(f'"{k}"' for k in keep)
                doc = f"MERGE(KEEP({doc}, {kept}), {field})"
            else:
                doc = f"MERGE({doc}, {field})"
    return f"FIRST(LET _doc = {doc} RETURN UNSET(_doc, ATTRIBUTES(_doc)[* FILTER _doc[CURRENT] == null]))"


def _value(field: str) -> str:
    return field.split(": ", 1)[1] if ": " in field else field


def insert_document(base: list[str], actions: list[str], keep: list[str]) -> str:
    """The INSERT document: the pattern's fields, then the ON CREATE actions.

    Cypher stores no null property: an action setting one to the literal
    ``null`` is left out. (A value that is null only at run time is still
    stored, as before.)
    """
    actions = [a for a in actions if isinstance(a, MapAction) or _value(a).strip() != "null"]
    if not has_map_actions(actions):
        return "{" + ", ".join([*base, *actions]) + "}"
    return _apply("{" + ", ".join(base) + "}", actions, keep)


def update_clause(actions: list[str], keep: list[str]) -> str:
    """``UPDATE {…}`` for property actions; ``REPLACE <OLD with actions>`` for map actions.

    The UPDATE removes a property set to null only under ``keepNull: false``,
    which :func:`upsert_options` adds.
    """
    if not has_map_actions(actions):
        return "UPDATE {" + ", ".join(actions) + "}"
    return f"REPLACE {_apply('OLD', actions, keep)}"


def upsert_options(actions: list[str]) -> str:
    """``OPTIONS {keepNull: false}`` when an ON MATCH action sets a property to the
    literal ``null`` (removing it, as Cypher does)."""
    if has_map_actions(actions) or not any(_value(a).strip() == "null" for a in actions):
        return ""
    return " OPTIONS {keepNull: false}"
