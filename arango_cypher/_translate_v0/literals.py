"""Literal and type-expression helpers for the v0 translator."""

from __future__ import annotations

import re
from typing import Any

from arango_query_core import CoreError


def _aql_string_literal(value: str) -> str:
    """Return a minimally escaped AQL double-quoted string literal."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


# One escape sequence of an openCypher string literal (the grammar admits only
# these after a backslash). ``\U`` takes eight hex digits when eight follow and
# four otherwise; ``\u`` always takes four, as in Neo4j.
_CYPHER_ESCAPE = re.compile(r"\\(?:U([0-9A-Fa-f]{8})|[Uu]([0-9A-Fa-f]{4})|([BFNRT])|(.))", re.DOTALL)

_SIMPLE_ESCAPES = {"b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}


def _not_a_scalar(escape: str, why: str) -> CoreError:
    return CoreError(f"String escape {escape} is not a Unicode code point: {why}", code="UNSUPPORTED")


def cypher_string_value(literal: str) -> str:
    """The value of an openCypher string literal, quotes included in *literal*.

    A four-digit escape is a UTF-16 code unit, so a surrogate pair written as
    two of them (``\\uD83D\\uDE00``) is one character, as in Neo4j. A code point
    past U+10FFFF, an eight-digit surrogate, and a surrogate without its other
    half are refused: none is a character, and ArangoDB rejects one in a bind
    variable (ERR 600). :func:`_aql_string_from_cypher` accepts exactly the
    literals this does.
    """
    body = literal[1:-1]
    out: list[str] = []
    pending: tuple[int, str] | None = None  # a high surrogate and its escape text
    pos = 0

    def flush_pending() -> None:
        if pending is not None:
            raise _not_a_scalar(pending[1], "a high surrogate with no low surrogate after it")

    for match in _CYPHER_ESCAPE.finditer(body):
        if match.start() > pos:
            flush_pending()
            out.append(body[pos : match.start()])
        pos = match.end()
        wide, narrow, upper, other = match.groups()
        escape = match.group(0)
        if wide is not None or narrow is not None:
            code = int(wide or narrow, 16)
            if code > 0x10FFFF:
                raise _not_a_scalar(escape, "past U+10FFFF")
            if 0xDC00 <= code <= 0xDFFF and narrow is not None and pending is not None:
                high = pending[0]
                pending = None
                out.append(chr(0x10000 + ((high - 0xD800) << 10) + (code - 0xDC00)))
                continue
            flush_pending()
            if 0xD800 <= code <= 0xDBFF and narrow is not None:
                pending = (code, escape)
                continue
            if 0xD800 <= code <= 0xDFFF:
                raise _not_a_scalar(escape, "a surrogate without its other half")
            out.append(chr(code))
            continue
        flush_pending()
        letter = upper.lower() if upper else other
        out.append(_SIMPLE_ESCAPES.get(letter, letter))
    flush_pending()
    out.append(body[pos:])
    return "".join(out)


def _aql_string_from_cypher(literal: str) -> str:
    """An openCypher string literal, quotes included, as an AQL string literal.

    The two languages agree on where a string ends (both read a backslash and
    the character after it as one escape), so the text is kept and only the
    escapes AQL reads differently are rewritten: AQL takes ``\\N``, ``\\T``,
    ``\\B``, ``\\F`` and ``\\R`` as plain letters where openCypher means the
    lowercase escapes, and has no ``\\U``, so an eight-digit one becomes a
    ``\\u`` surrogate pair, which AQL decodes after reading the string, and a
    four-digit one a ``\\u``. A literal :func:`cypher_string_value` refuses is
    refused here too, so folded and unfolded strings agree.
    """
    cypher_string_value(literal)

    def rewrite(match: re.Match[str]) -> str:
        wide, narrow, upper, _ = match.groups()
        if upper:
            return "\\" + upper.lower()
        if narrow is not None:
            return f"\\u{narrow}"
        if wide is not None:
            code = int(wide, 16)
            if code < 0x10000:
                return f"\\u{code:04x}"
            code -= 0x10000
            return f"\\u{0xD800 + (code >> 10):04x}\\u{0xDC00 + (code & 0x3FF):04x}"
        return match.group(0)

    return _CYPHER_ESCAPE.sub(rewrite, literal)


def _compile_type_of_relationship(
    rel_type: str, rel_var: str, rel_style: str | None, bind_vars: dict[str, Any]
) -> str:
    if rel_style == "GENERIC_WITH_TYPE":
        if "relTypeField" not in bind_vars:
            raise CoreError("relTypeField missing for GENERIC_WITH_TYPE", code="INVALID_MAPPING")
        return f"{rel_var}[@relTypeField]"
    return _aql_string_literal(rel_type)


def parse_integer_literal(text: str) -> int:
    """The value of a Cypher integer literal: decimal, ``0x`` hex, ``0o`` or legacy ``0`` octal."""
    t = text.lower()
    if t.startswith("0x"):
        return int(t[2:], 16)
    if t.startswith("0o"):
        return int(t[2:], 8)
    if len(t) > 1 and t.startswith("0"):
        return int(t[1:], 8)
    return int(t)
