"""RETURN column names: inferred short keys, or Cypher's (opt-in).

By default an unaliased RETURN item is keyed by a short name inferred from
the expression (``n.name`` → ``name``, ``count(*)`` → ``count``), which the
API, UI and NL clients read. openCypher names the column by the expression as
written (``n.name``, ``count(*)``, ``n.missing IS NULL``). With
``TranslateOptions.cypher_column_names`` the translator does that: the key is
the item's source text, quoted in the AQL object (``{"n.name": n.name}``).

Only the name of the column changes. The AQL variables a COLLECT or a sort
binds keep the inferred names, so ORDER BY, DISTINCT and grouping resolve as
before; a single unaliased column is still returned bare.
"""

from __future__ import annotations

import json
import re
from contextvars import ContextVar
from typing import Any

from .naming import _AQL_RESERVED
from .state import _object_rows

#: ``TranslateOptions.cypher_column_names`` of the running translation.
_active_cypher_column_names: ContextVar[bool] = ContextVar("_active_cypher_column_names", default=False)

#: A bare AQL object key: ``_001`` is not one (ERR 1501), so it is a string.
_AQL_IDENTIFIER = re.compile(r"_*[A-Za-z][A-Za-z0-9_]*|`[^`]+`")


def cypher_column_names() -> bool:
    return _active_cypher_column_names.get()


def source_text(ctx: Any) -> str:
    """The query text *ctx* was parsed from, as written (spacing kept)."""
    start, stop = ctx.start, ctx.stop
    if start is None or stop is None:
        return ctx.getText().strip()
    return start.getInputStream().getText(start.start, stop.stop).strip()


def written_column(item: Any) -> str | None:
    """Cypher's column name for an unaliased projection item, when the option is on.

    ``None`` when the item has an alias (its name is the alias), the option
    is off (the caller infers a short key, as it always has), or the RETURN
    ends a CALL subquery: its rows are read back by the enclosing query under
    the keys it was compiled with, not shown as columns.
    """
    if item.oC_Variable() is not None or not cypher_column_names() or _object_rows.get():
        return None
    return source_text(item.oC_Expression())


def object_key(name: str) -> str:
    """*name* as an AQL object key: bare when it is an identifier AQL does not
    reserve, else a string (``RETURN true`` names its column ``true``, and a
    bare ``true`` key is ERR 1501)."""
    if _AQL_IDENTIFIER.fullmatch(name) and name.lower() not in _AQL_RESERVED:
        return name
    return json.dumps(name)
