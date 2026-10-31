"""Result normalization for TCK expected-vs-actual comparison.

TCK expected results use Neo4j/Cypher conventions (node/relationship literals,
unquoted map keys, etc.). This module bridges the gap to ArangoDB result values.
"""

from __future__ import annotations

import json
import re
from typing import Any

_ARANGO_META_KEYS = frozenset({"_id", "_key", "_rev", "_old_rev"})
_ARANGO_EDGE_META_KEYS = frozenset({"_id", "_key", "_rev", "_old_rev", "_from", "_to"})

_NODE_RE = re.compile(
    r"^\(?"
    r"(?P<labels>(?:\s*:\s*[A-Za-z_]\w*)*)"
    r"\s*(?:\{(?P<props>.*)\})?"
    r"\s*\)?$",
    re.DOTALL,
)

_REL_RE = re.compile(
    r"^\[?"
    r"(?::(?P<type>[A-Za-z_]\w*))?"
    r"\s*(?:\{(?P<props>.*)\})?"
    r"\s*\]?$",
    re.DOTALL,
)

#: Key of a normalized path: ``{"_path": [node, rel, node, ...]}``, each
#: relationship carrying ``_dir`` (``"->"`` or ``"<-"``) as the TCK writes it.
PATH_KEY = "_path"


def normalize_expected_value(text: str) -> Any:
    """Parse a TCK expected-value cell into a Python value.

    Handles: integers, floats, strings ('foo' or "foo"), booleans (true/false),
    null, lists ([1, 2]), maps ({key: value}),
    node literals (:Label {prop: value}), relationship literals [:TYPE {prop: value}],
    path literals <(:A)-[:T]->(:B)>.
    """
    s = text.strip()
    if not s:
        return s

    if s == "null":
        return None
    if s == "true":
        return True
    if s == "false":
        return False

    if len(s) >= 2 and ((s.startswith("'") and s.endswith("'")) or (s.startswith('"') and s.endswith('"'))):
        return _unescape_cypher_string(s[1:-1])

    if _try_int(s) is not None:
        return _try_int(s)
    f = _try_float(s)
    if f is not None:
        return f

    if s.startswith("[") and s.endswith("]"):
        if re.match(r"^\[:", s):
            return _parse_rel_literal(s)
        return _parse_list(s)

    if s.startswith("{") and s.endswith("}"):
        return _parse_map(s)

    if s.startswith("<(") and s.endswith(")>"):
        return _parse_path_literal(s)

    if s.startswith("(") or (s.startswith(":") and not s.startswith(":[")):
        return _parse_node_literal(s)

    return s


_CYPHER_ESCAPES = {"\\": "\\", "'": "'", '"': '"', "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}


def _unescape_cypher_string(body: str) -> str:
    """A TCK string cell is a Cypher string literal: ``'\\''`` is ``'``."""
    out: list[str] = []
    i = 0
    while i < len(body):
        ch = body[i]
        if ch == "\\" and i + 1 < len(body):
            nxt = body[i + 1]
            if nxt in "uU" and i + 6 <= len(body):
                out.append(chr(int(body[i + 2 : i + 6], 16)))
                i += 6
                continue
            out.append(_CYPHER_ESCAPES.get(nxt.lower() if nxt in "BFNRT" else nxt, "\\" + nxt))
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def normalize_actual_value(value: Any, *, as_node: bool = False, as_rel: bool = False) -> Any:
    """Normalize an ArangoDB result value for comparison.

    Strips _id, _key, _rev from documents. Normalizes numeric types.
    When *as_node* is True, converts LPG ``type`` field to ``_labels`` array.
    When *as_rel* is True, strips ``_from``/``_to`` and converts ``type`` to ``_type``.
    """
    if isinstance(value, dict):
        if _is_actual_path(value):
            return _normalize_actual_path(value)
        if "_id" in value:
            # A stored document is a node or a relationship whatever the
            # column's expected cells look like (a list element, a map value,
            # a column mixing nodes, relationships and maps): an edge document
            # carries ``_from``/``_to``.
            as_rel = "_from" in value and "_to" in value
            as_node = not as_rel
        else:
            # A map is a map, even in a column whose other cells are nodes.
            as_node = as_rel = False
        strip_keys = _ARANGO_EDGE_META_KEYS if as_rel else _ARANGO_META_KEYS
        cleaned = {k: normalize_actual_value(v) for k, v in value.items() if k not in strip_keys}
        if as_node:
            if "type" in cleaned:
                type_val = cleaned.pop("type")
                labels_val = cleaned.pop("labels", None)
                if isinstance(labels_val, list):
                    cleaned["_labels"] = sorted(labels_val)
                elif isinstance(type_val, str):
                    cleaned["_labels"] = [type_val]
            elif "_labels" not in cleaned:
                cleaned["_labels"] = []
        if as_rel and "type" in cleaned:
            cleaned["_type"] = cleaned.pop("type")
        return cleaned
    if isinstance(value, list):
        return [normalize_actual_value(v, as_rel=as_rel, as_node=as_node) for v in value]
    if isinstance(value, float) and value == int(value) and not (value != value):
        return int(value)
    return value


def _is_actual_path(value: dict[str, Any]) -> bool:
    """An ArangoDB path value: ``{"nodes"|"vertices": [...], "edges": [...]}``."""
    nodes = value.get("nodes", value.get("vertices"))
    return (
        set(value) in ({"nodes", "edges"}, {"vertices", "edges"})
        and isinstance(nodes, list)
        and isinstance(value["edges"], list)
        and len(nodes) == len(value["edges"]) + 1
    )


def _normalize_actual_path(value: dict[str, Any]) -> dict[str, Any]:
    """``{"nodes", "edges"}`` -> ``{"_path": [node, rel, node, ...]}``.

    A relationship's ``_dir`` is ``"->"`` when it leaves the node before it
    in the path (its ``_from`` is that node's ``_id``), else ``"<-"`` — how a
    TCK path literal writes direction.
    """
    nodes = value.get("nodes", value.get("vertices")) or []
    edges = value["edges"]
    out: list[Any] = [normalize_actual_value(nodes[0], as_node=True)]
    for i, edge in enumerate(edges):
        rel = normalize_actual_value(edge, as_rel=True)
        if isinstance(rel, dict) and isinstance(edge, dict) and isinstance(nodes[i], dict):
            rel = {**rel, "_dir": "->" if edge.get("_from") == nodes[i].get("_id") else "<-"}
        out.append(rel)
        out.append(normalize_actual_value(nodes[i + 1], as_node=True))
    return {PATH_KEY: out}


def values_equal(actual: Any, expected: Any, *, ignore_list_order: bool = False) -> bool:
    """Cypher-value equality for comparing a result cell with a TCK cell.

    Stricter than Python ``==``: a boolean never equals a number (Python says
    ``True == 1``), and ``NaN`` equals ``NaN`` (a TCK cell ``NaN`` asserts the
    value is NaN).  Integers and floats compare numerically, because ArangoDB
    returns a whole float as an integer.  With ``ignore_list_order`` lists
    compare as multisets, at every depth.
    """
    if isinstance(actual, bool) or isinstance(expected, bool):
        return isinstance(actual, bool) and isinstance(expected, bool) and actual == expected
    if isinstance(actual, (int, float)) and isinstance(expected, (int, float)):
        if actual != actual and expected != expected:
            return True
        return actual == expected
    if isinstance(actual, list) and isinstance(expected, list):
        if len(actual) != len(expected):
            return False
        if not ignore_list_order:
            return all(
                values_equal(a, e, ignore_list_order=False) for a, e in zip(actual, expected, strict=True)
            )
        return _multiset_equal(actual, expected, ignore_list_order=True)
    if isinstance(actual, dict) and isinstance(expected, dict):
        if actual.keys() != expected.keys():
            return False
        return all(
            values_equal(actual[k], expected[k], ignore_list_order=ignore_list_order) for k in expected
        )
    return type(actual) is type(expected) and actual == expected


def _multiset_equal(actual: list[Any], expected: list[Any], *, ignore_list_order: bool) -> bool:
    remaining = list(actual)
    for e in expected:
        for i, a in enumerate(remaining):
            if values_equal(a, e, ignore_list_order=ignore_list_order):
                remaining.pop(i)
                break
        else:
            return False
    return not remaining


def results_match(
    actual_rows: list[dict[str, Any]],
    expected_table: list[dict[str, str]],
    *,
    ordered: bool = False,
    ignore_list_order: bool = False,
) -> tuple[bool, str]:
    """Compare actual query results against expected TCK table.

    Returns (match, explanation_if_mismatch). When *ordered* is False,
    rows are compared as multisets.
    """
    if len(actual_rows) != len(expected_table):
        return False, (f"row count mismatch: got {len(actual_rows)}, expected {len(expected_table)}")

    norm_actual = _normalize_actual_rows(actual_rows, expected_table)
    norm_expected = _normalize_expected_rows(expected_table)

    if ordered:
        for idx, (a, e) in enumerate(zip(norm_actual, norm_expected, strict=True)):
            if not values_equal(a, e, ignore_list_order=ignore_list_order):
                return False, f"row {idx} mismatch: {a!r} != {e!r}"
        return True, ""

    remaining = list(norm_actual)
    for e in sorted(norm_expected, key=_row_sort_key):
        for i, a in enumerate(remaining):
            if values_equal(a, e, ignore_list_order=ignore_list_order):
                remaining.pop(i)
                break
        else:
            closest = sorted(remaining, key=_row_sort_key)[0] if remaining else None
            return False, f"expected row not found: {e!r}; first unmatched actual: {closest!r}"
    return True, ""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _try_int(s: str) -> int | None:
    try:
        v = int(s)
        if str(v) == s or (s.startswith("+") and str(v) == s[1:]):
            return v
    except ValueError:
        pass
    return None


def _try_float(s: str) -> float | None:
    if "." not in s and "e" not in s.lower():
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _split_top_level(s: str, delimiter: str = ",") -> list[str]:
    """Split *s* by *delimiter* respecting nested brackets/parens/quotes."""
    parts: list[str] = []
    depth = 0
    cur: list[str] = []
    in_sq = False
    in_dq = False
    prev = ""
    for ch in s:
        if ch == "'" and not in_dq and prev != "\\":
            in_sq = not in_sq
        elif ch == '"' and not in_sq and prev != "\\":
            in_dq = not in_dq
        elif not in_sq and not in_dq:
            if ch in "([{":
                depth += 1
            elif ch in ")]}":
                depth -= 1
            elif ch == delimiter and depth == 0:
                parts.append("".join(cur).strip())
                cur = []
                prev = ch
                continue
        cur.append(ch)
        prev = ch
    tail = "".join(cur).strip()
    if tail:
        parts.append(tail)
    return parts


def _parse_list(s: str) -> list[Any]:
    inner = s[1:-1].strip()
    if not inner:
        return []
    items = _split_top_level(inner)
    return [normalize_expected_value(item) for item in items]


def _parse_map(s: str) -> dict[str, Any]:
    inner = s[1:-1].strip()
    if not inner:
        return {}
    entries = _split_top_level(inner)
    result: dict[str, Any] = {}
    for entry in entries:
        colon_idx = entry.find(":")
        if colon_idx < 0:
            continue
        key = entry[:colon_idx].strip().strip("'\"")
        val = entry[colon_idx + 1 :].strip()
        result[key] = normalize_expected_value(val)
    return result


def _parse_props(prop_str: str) -> dict[str, Any]:
    """Parse a Cypher property map fragment like ``name: 'Alice', age: 30``."""
    if not prop_str or not prop_str.strip():
        return {}
    return _parse_map("{" + prop_str + "}")


def _parse_node_literal(s: str) -> dict[str, Any]:
    s = s.strip()
    if s.startswith("(") and s.endswith(")"):
        s = s[1:-1].strip()
    m = _NODE_RE.match(s)
    if not m:
        return {"_raw": s}
    labels_str = m.group("labels") or ""
    labels = list(dict.fromkeys(lab.strip() for lab in labels_str.split(":") if lab.strip()))
    props = _parse_props(m.group("props") or "")
    return {"_labels": sorted(labels), **props}


def _parse_rel_literal(s: str) -> dict[str, Any]:
    s = s.strip()
    if s.startswith("[") and s.endswith("]"):
        s = s[1:-1].strip()
    m = _REL_RE.match(s)
    if not m:
        return {"_raw": s}
    rel_type = m.group("type")
    props = _parse_props(m.group("props") or "")
    result: dict[str, Any] = {}
    if rel_type:
        result["_type"] = rel_type
    result.update(props)
    return result


def _scan_balanced(s: str, start: int, open_ch: str, close_ch: str) -> int:
    """Index just past the *close_ch* matching ``s[start] == open_ch`` (quote-aware)."""
    depth = 0
    quote = ""
    i = start
    while i < len(s):
        ch = s[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = ""
        elif ch in "'\"":
            quote = ch
        elif ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise ValueError(f"unbalanced {open_ch}{close_ch} in {s!r}")


def _parse_path_literal(s: str) -> Any:
    """Parse ``<(:A)-[:T {k: 1}]->(:B)<-[:U]-()>`` into ``{"_path": [...]}``.

    Text that is not a well-formed path literal is returned unchanged, so it
    can only compare equal to an identical string.
    """
    body = s[1:-1].strip()
    out: list[Any] = []
    i = 0
    try:
        while i < len(body):
            if body[i] != "(":
                return s
            end = _scan_balanced(body, i, "(", ")")
            out.append(_parse_node_literal(body[i:end]))
            i = end
            if i >= len(body):
                break
            if body.startswith("<-[", i):
                direction, i = "<-", i + 2
            elif body.startswith("-[", i):
                direction, i = "->", i + 1
            else:
                return s
            end = _scan_balanced(body, i, "[", "]")
            rel = _parse_rel_literal(body[i:end])
            i = end
            if direction == "<-" and body.startswith("-", i) and not body.startswith("->", i):
                i += 1
            elif direction == "->" and body.startswith("->", i):
                i += 2
            else:
                return s
            out.append({**rel, "_dir": direction})
    except ValueError:
        return s
    return {PATH_KEY: out}


def _is_node_expected(expected_val: str) -> bool:
    """Check if an expected value looks like a node literal."""
    s = expected_val.strip()
    return s.startswith("(") or (s.startswith(":") and not s.startswith("[:"))


def _is_rel_expected(expected_val: str) -> bool:
    """Check if an expected value looks like a relationship literal or list thereof."""
    s = expected_val.strip()
    if s.startswith("[:"):
        return True
    if s.startswith("[[:"):
        return True
    return False


def _normalize_actual_rows(
    actual: list[dict[str, Any]],
    expected_table: list[dict[str, str]],
) -> list[dict[str, Any]]:
    """Normalize actual rows, keeping only columns present in the expected table."""
    if not expected_table:
        return [normalize_actual_value(r) for r in actual]
    expected_keys = set(expected_table[0].keys())

    node_cols: set[str] = set()
    rel_cols: set[str] = set()
    for row in expected_table:
        for k, v in row.items():
            if _is_node_expected(v):
                node_cols.add(k)
            elif _is_rel_expected(v):
                rel_cols.add(k)

    out: list[dict[str, Any]] = []
    for row in actual:
        if isinstance(row, dict):
            has_expected = any(k in row for k in expected_keys)
            if has_expected:
                filtered: dict[str, Any] = {}
                for k in expected_keys:
                    if k in row:
                        val = row[k]
                        is_node = k in node_cols
                        is_rel = k in rel_cols
                        filtered[k] = normalize_actual_value(val, as_node=is_node, as_rel=is_rel)
                out.append(filtered)
            elif len(expected_keys) == 1:
                col_name = next(iter(expected_keys))
                is_node = col_name in node_cols
                is_rel = col_name in rel_cols
                out.append({col_name: normalize_actual_value(row, as_node=is_node, as_rel=is_rel)})
            else:
                out.append(normalize_actual_value(row))
        else:
            if len(expected_keys) == 1:
                col_name = next(iter(expected_keys))
                out.append({col_name: normalize_actual_value(row)})
            else:
                out.append(normalize_actual_value(row))
    return out


def _normalize_expected_rows(table: list[dict[str, str]]) -> list[dict[str, Any]]:
    return [{k: normalize_expected_value(v) for k, v in row.items()} for row in table]


def _row_sort_key(row: dict[str, Any]) -> str:
    try:
        return json.dumps(row, sort_keys=True, default=str)
    except TypeError:
        return repr(sorted(row.items()))


def results_contain(
    actual_rows: list[dict[str, Any]],
    expected_table: list[dict[str, str]],
    *,
    ordered: bool = False,
) -> tuple[bool, str]:
    """Check that actual results contain at least the expected rows (subset match).

    Unlike results_match, extra actual rows are allowed.
    """
    if len(actual_rows) < len(expected_table):
        return False, (f"too few rows: got {len(actual_rows)}, need at least {len(expected_table)}")

    norm_actual = _normalize_actual_rows(actual_rows, expected_table)
    norm_expected = _normalize_expected_rows(expected_table)

    if ordered:
        ai = 0
        for e_row in norm_expected:
            found = False
            while ai < len(norm_actual):
                if values_equal(norm_actual[ai], e_row):
                    found = True
                    ai += 1
                    break
                ai += 1
            if not found:
                return False, f"expected row not found in order: {e_row!r}"
        return True, ""

    remaining = list(norm_actual)
    for e_row in norm_expected:
        found = False
        for i, a_row in enumerate(remaining):
            if values_equal(a_row, e_row):
                remaining.pop(i)
                found = True
                break
        if not found:
            return False, f"expected row not found: {e_row!r}"
    return True, ""


def parse_param_value(text: str) -> Any:
    """Parse a TCK parameter value cell into a Python value.

    Reuses normalize_expected_value but also handles bare unquoted strings
    that should remain as strings.
    """
    return normalize_expected_value(text)
