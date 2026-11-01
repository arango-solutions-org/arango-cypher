"""Name normalisation and fresh-name helpers for translator codegen."""

from __future__ import annotations

import re
from typing import Any

from arango_query_core import CoreError

from .._antlr.CypherParser import CypherParser
from .bind_names import _BIND_NAME_RE


def _affix_name(name: str, *, prefix: str = "", suffix: str = "") -> str:
    """Build an identifier from *name*, inside its backticks when it has them.

    A backtick-quoted name (```a b```, or a reserved word quoted by
    ``_quote_aql_reserved_names``) composed naively gives ``_`a b`_distinct``,
    which AQL cannot parse; this gives ```_a b_distinct```.
    """
    if len(name) >= 2 and name[0] == "`" and name[-1] == "`":
        return f"`{prefix}{name[1:-1]}{suffix}`"
    return f"{prefix}{name}{suffix}"


def _pick_fresh_var(name: str, *, forbidden_vars: set[str]) -> str:
    if name not in forbidden_vars:
        forbidden_vars.add(name)
        return name
    i = 1
    while _affix_name(name, suffix=f"_{i}") in forbidden_vars:
        i += 1
    out = _affix_name(name, suffix=f"_{i}")
    forbidden_vars.add(out)
    return out


_IDENT = r"[A-Za-z_][A-Za-z0-9_]*"
_FOR_DECL = re.compile(rf"\bFOR\s+({_IDENT})(?:\s*,\s*({_IDENT}))?(?:\s*,\s*({_IDENT}))?\s+IN\b")
_LET_DECL = re.compile(rf"\bLET\s+({_IDENT})\s*=(?!=)")
_COLLECT_CLAUSE = re.compile(r"\bCOLLECT\b(.*)")
_ASSIGN = re.compile(rf"(?:^|,)\s*({_IDENT})\s*=(?!=)")
_INTO_DECL = re.compile(rf"\bINTO\s+({_IDENT})")
_AGGREGATE_CLAUSE = re.compile(r"\bAGGREGATE\b(.*)")


def _declared_aql_vars(lines: list[str]) -> set[str]:
    """Every AQL variable the emitted *lines* declare (FOR, LET, COLLECT,
    AGGREGATE, INTO).

    For callers with no scope set of their own that are about to declare a
    variable — reusing a declared name is ERR 1511 ("assigned multiple
    times"). Deliberately over-inclusive: a name declared inside a subquery
    is counted too, which costs at most an unneeded rename, never a collision.
    """
    declared: set[str] = set()
    for line in lines:
        for match in _FOR_DECL.finditer(line):
            declared.update(g for g in match.groups() if g)
        declared.update(_LET_DECL.findall(line))
        declared.update(_INTO_DECL.findall(line))
        for clause_re in (_COLLECT_CLAUSE, _AGGREGATE_CLAUSE):
            clause = clause_re.search(line)
            if clause:
                head = re.split(r"\b(?:INTO|AGGREGATE|WITH COUNT|OPTIONS|KEEP)\b", clause.group(1))[0]
                declared.update(_ASSIGN.findall(head))
    return declared


_BIND_KEY_UNSAFE = re.compile(r"[^A-Za-z0-9_]")


def _pick_bind_key(base: str, bind_vars: dict[str, Any]) -> str:
    """Return an unused bind-variable name built from *base*.

    Many bases embed a Cypher variable (``f"{var}TypeField"``), and a variable
    may be backtick-quoted (```a b```). AQL reads a bind name as ASCII
    letters, digits and underscores only, so a quoted variable made the query
    an ERR 1501 syntax error. Every other character becomes ``_``; a leading
    ``@`` (a collection parameter) is kept.
    """
    prefix = "@" if base.startswith("@") else ""
    base = prefix + _BIND_KEY_UNSAFE.sub("_", base[len(prefix) :])
    if base not in bind_vars:
        return base
    i = 2
    while f"{base}{i}" in bind_vars:
        i += 1
    return f"{base}{i}"


def _aql_collection_ref(bind_key: str) -> str:
    if not bind_key.startswith("@"):
        raise CoreError("Collection bind key must start with '@'", code="INTERNAL_ERROR")
    return f"@@{bind_key[1:]}"


def _strip_label_backticks(name: str) -> str:
    """Strip a single pair of enclosing backticks from an escaped label."""
    if len(name) >= 2 and name.startswith("`") and name.endswith("`"):
        return name[1:-1]
    return name


_SHOWN_NAME_CHARS = 80


#: Words AQL reserves (ArangoDB 3.11, any case). Spelled bare as a variable,
#: an attribute after a dot or an object key, each is ERR 1501; quoted in
#: backticks each is a plain name. AQL's contextual keywords (KEEP, PRUNE,
#: SEARCH, OPTIONS, COUNT, NEW, OLD, ...) are valid names and not listed.
_AQL_RESERVED = frozenset(
    {
        "aggregate", "all", "all_shortest_paths", "and", "any", "asc", "collect", "desc",
        "distinct", "false", "filter", "for", "graph", "in", "inbound", "insert", "into",
        "k_paths", "k_shortest_paths", "let", "like", "limit", "none", "not", "null", "or",
        "outbound", "remove", "replace", "return", "shortest_path", "sort", "true", "update",
        "upsert", "window", "with",
    }
)  # fmt: skip


#: A name AQL accepts unquoted, as a variable or after a dot: ASCII, and a
#: letter after any leading underscores.
_AQL_NAME_RE = re.compile(r"_*[A-Za-z][A-Za-z0-9_]*")
#: How a variable name must start, quoted or not: ``_001``, ``1a`` and ``ä``
#: are ERR 1501 "variable name has an invalid format" even in backticks,
#: while ```a b``` is a valid quoted name.
_AQL_VARIABLE_START_RE = re.compile(r"_*[A-Za-z]")


def _variable_symbols(tree: Any) -> list[Any]:
    """The token of every variable in *tree* (definitions and uses)."""
    out = []
    stack = [tree]
    while stack:
        node = stack.pop()
        if isinstance(node, CypherParser.OC_VariableContext):
            leaf = node
            while getattr(leaf, "children", None) and len(leaf.children) == 1:
                leaf = leaf.children[0]
            symbol = getattr(leaf, "symbol", None)
            if symbol is not None and symbol.text:
                out.append(symbol)
            continue
        stack.extend(getattr(node, "children", None) or [])
    return out


def _rename_invalid_aql_variables(tree: Any) -> None:
    """Give every Cypher variable AQL cannot spell a valid name of its own.

    Cypher allows ``_001`` (TCK Create4), ``ä`` or ```1a```; AQL refuses each
    as a variable name, quoted or not (a name must start, after any
    underscores, with an ASCII letter). Each is renamed in the tree, so every
    site emits the new name: ``v`` and the name with its other characters
    made underscores (``_001`` -> ``v_001``, ``ä`` -> ``v__``), with a numeric
    suffix while that clashes with another variable of the query. A name that
    starts well but holds other characters (```a b```, ``aä``) is kept and
    backtick-quoted (:func:`_quote_aql_reserved_names`). A column named by the
    variable as written keeps that name (``columns.source_text`` reads the
    query text, not the token); an alias or an inferred short key takes the
    new one.
    """
    symbols = _variable_symbols(tree)
    names = {_strip_label_backticks(s.text) for s in symbols}
    taken = set(names)
    renamed: dict[str, str] = {}
    for name in sorted(names):
        if _AQL_VARIABLE_START_RE.match(name):
            continue
        base = "v" + ("" if name.startswith("_") else "_") + re.sub(r"[^A-Za-z0-9_]", "_", name)
        fresh, i = base, 2
        while fresh in taken:
            fresh, i = f"{base}_{i}", i + 1
        taken.add(fresh)
        renamed[name] = fresh
    for symbol in symbols:
        new = renamed.get(_strip_label_backticks(symbol.text))
        if new is not None:
            symbol.text = new


def _quote_aql_reserved_names(tree: Any) -> None:
    """Backtick-quote every variable and property key AQL reserves.

    Names reach the AQL text verbatim at many sites, so a Cypher variable
    called ``like`` (LDBC BI6) emitted ``FOR person3, like IN ...``, an
    ERR 1501 syntax error, and so did ``n.limit`` or ``{in: 1}``. Rewriting the
    token once, here, makes every site emit ```like```, which AQL reads as the
    plain name. Labels and relationship types are looked up in the mapping
    and bound as parameters, so they are left alone.
    """
    stack = [tree]
    while stack:
        node = stack.pop()
        if isinstance(node, (CypherParser.OC_VariableContext, CypherParser.OC_PropertyKeyNameContext)):
            leaf = node
            while getattr(leaf, "children", None) and len(leaf.children) == 1:
                leaf = leaf.children[0]
            symbol = getattr(leaf, "symbol", None)
            text = symbol.text if symbol is not None else None
            if symbol is not None and text and not text.startswith("`"):
                # So is a name AQL cannot spell bare: a property key after a dot
                # (``n._001``), a variable with a non-ASCII letter (``aä``).
                if text.lower() in _AQL_RESERVED or not _AQL_NAME_RE.fullmatch(text):
                    symbol.text = f"`{text}`"
            continue
        stack.extend(getattr(node, "children", None) or [])


def _reject_unsafe_escaped_names(tree: Any) -> None:
    """Refuse backtick-quoted Cypher names that AQL would read differently.

    Escaped names (variables, aliases, property keys, labels) reach the AQL
    text verbatim, backticks included, at many sites. Inside AQL backticks a
    ``\\`` starts an escape, so ``n.`a\\``` closes its quoting one character
    later than Cypher's and splices the rest of the query into the AQL as code.
    Cypher spells a literal backtick as a doubled one, which AQL reads as two
    adjacent names. Content free of both characters means the same thing in
    either language, so the check is made once, here, rather than at every
    site that copies a name.
    """
    stack = [tree]
    while stack:
        node = stack.pop()
        symbol = getattr(node, "symbol", None)
        if symbol is not None:
            text = symbol.text or ""
            if symbol.type == CypherParser.EscapedSymbolicName and ("\\" in text or "`" in text[1:-1]):
                shown = text if len(text) <= _SHOWN_NAME_CHARS else text[:_SHOWN_NAME_CHARS] + "…"
                raise CoreError(
                    f"Name {shown} contains a backslash or a backtick, which cannot be "
                    "passed safely to AQL; rename it",
                    code="UNSUPPORTED",
                )
            continue
        stack.extend(node.getChildren())


def _aql_bind_name(parameter: str) -> str:
    """Return the AQL bind reference for a Cypher parameter (``$name``)."""
    name = _strip_label_backticks(parameter[1:])
    if name.isdigit():
        # A positional parameter (``$0``): AQL bind names may be all digits.
        return f"@{name}"
    if not _BIND_NAME_RE.fullmatch(name):
        raise CoreError(
            f"Parameter {parameter[:_SHOWN_NAME_CHARS]} is not a valid AQL bind parameter "
            "name; use letters, digits and underscores",
            code="UNSUPPORTED",
        )
    return f"@{name}"


_STRING_LITERAL_RE = re.compile(r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"")


def _rewrite_vars(text: str, var_env: dict[str, str]) -> str:
    """Best-effort variable rewrite for post-WITH scopes.

    One pass over *text*, longest key first: a replacement is never rewritten
    again. Substituting key by key did, so with ``{"a.name": "name", "name":
    "name_g1"}`` an ``ORDER BY a.name`` became ``name`` and then ``name_g1`` —
    the other column (LDBC BI3 sorted by ``person.id`` for ``forum.id``).

    String literals are left alone, and so is an object key (a name between
    ``{`` or ``,`` and ``:``): ``collect({score: score})`` renamed both, so
    the collected maps lost their ``score`` attribute (LDBC BI14). A name
    before the ``:`` of a ternary is a value, and is renamed.
    """
    if not text or not var_env:
        return text
    keys = sorted(var_env.keys(), key=len, reverse=True)
    # A key is a whole name: not part of a longer word, a quoted name, a
    # property access (``x.v`` keeps its property ``v``) or a bind parameter
    # (``@id`` is never the variable ``id``).
    pattern = re.compile(
        r"(?P<pre>[{,]\s*)?(?<![\w`.@])(?P<name>"
        + "|".join(re.escape(k) for k in keys)
        + r")(?![\w`])(?P<post>\s*:(?!:))?"
    )

    def replace(m: re.Match[str]) -> str:
        if m.group("pre") is not None and m.group("post") is not None:
            return m.group(0)  # an object key
        return (m.group("pre") or "") + var_env[m.group("name")] + (m.group("post") or "")

    out: list[str] = []
    pos = 0
    for literal in _STRING_LITERAL_RE.finditer(text):
        out.append(pattern.sub(replace, text[pos : literal.start()]))
        out.append(literal.group(0))
        pos = literal.end()
    out.append(pattern.sub(replace, text[pos:]))
    return "".join(out)
