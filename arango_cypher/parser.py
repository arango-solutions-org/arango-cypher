from __future__ import annotations

import re
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from antlr4 import CommonTokenStream, InputStream
from antlr4.error.ErrorListener import ErrorListener
from arango_query_core import CoreError

from ._antlr.CypherLexer import CypherLexer
from ._antlr.CypherParser import CypherParser

#: Python frames allowed while parsing (see ``parse_cypher``).
_PARSE_RECURSION_LIMIT = 4000


@dataclass(frozen=True)
class ParseResult:
    tree: Any
    # The (possibly normalized) source the tree was actually built from. Token
    # offsets in ``tree`` are relative to this string, not the caller's original
    # — offset-based rewriters (e.g. Layer-3 tenant injection) must use it.
    normalized: str = ""


# ``EXISTS { (pattern) }`` / ``COUNT { (pattern) }`` — the pattern-shorthand form
# where the leading ``MATCH`` is implicit. The in-repo grammar's subquery body
# requires an explicit reading clause, so we insert the implicit ``MATCH`` before
# parsing. Matches the keyword + ``{`` only when the brace content begins with a
# pattern ``(`` — a real reading clause always starts with a keyword
# (MATCH/UNWIND/CALL), never ``(``, so this never rewrites the already-supported
# ``EXISTS { MATCH … }`` form.
_PATTERN_SUBQUERY_RE = re.compile(r"(?i)(?<![A-Za-z0-9_])(?:EXISTS|COUNT)\s*\{\s*(?=\()")


def _insert_implicit_match(cypher: str) -> str:
    """Rewrite ``EXISTS/COUNT { (pattern) …}`` → ``… { MATCH (pattern) …}``.

    String and backtick-quoted literals are skipped so a literal containing
    ``exists{(`` is never rewritten.
    """
    if "{" not in cypher:
        return cypher
    out: list[str] = []
    i = 0
    n = len(cypher)
    quote: str | None = None
    while i < n:
        ch = cypher[i]
        if quote is not None:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(cypher[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ("'", '"', "`"):
            quote = ch
            out.append(ch)
            i += 1
            continue
        m = _PATTERN_SUBQUERY_RE.match(cypher, i)
        if m:
            out.append(m.group(0))
            out.append("MATCH ")
            i = m.end()
            continue
        out.append(ch)
        i += 1
    return "".join(out)


class _RaisingErrorListener(ErrorListener):
    def syntaxError(self, recognizer, offendingSymbol, line, column, msg, e):  # noqa: N802
        raise CoreError(f"Cypher syntax error at {line}:{column}: {msg}", code="CYPHER_SYNTAX_ERROR")


def parse_cypher(cypher: str) -> ParseResult:
    if not isinstance(cypher, str) or not cypher.strip():
        raise CoreError("cypher must be a non-empty string", code="INVALID_ARGUMENT")

    cypher = _insert_implicit_match(cypher)
    stream = InputStream(cypher)
    lexer = CypherLexer(stream)
    lexer.removeErrorListeners()
    lexer.addErrorListener(_RaisingErrorListener())

    tokens = CommonTokenStream(lexer)
    parser = CypherParser(tokens)
    parser.removeErrorListeners()
    parser.addErrorListener(_RaisingErrorListener())

    with deep_recursion():
        tree = parser.oC_Cypher()
    return ParseResult(tree=tree, normalized=cypher)


@contextmanager
def deep_recursion() -> Iterator[None]:
    """Allow deep Python recursion while parsing or walking a parse tree.

    The parser and every tree walk (``getText``, the semantic check, the
    expression compiler) recurse once or twice per precedence level, 16 of
    them from an expression down to a list literal, so a few dozen nested
    literals pass Python's default limit of 1000 frames. The limit is raised
    for the duration, never lowered below one the caller set.
    """
    limit = sys.getrecursionlimit()
    if limit < _PARSE_RECURSION_LIMIT:
        sys.setrecursionlimit(_PARSE_RECURSION_LIMIT)
    try:
        yield
    finally:
        if limit < _PARSE_RECURSION_LIMIT:
            sys.setrecursionlimit(limit)
