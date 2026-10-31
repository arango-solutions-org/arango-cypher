"""AQL formatting and post-processing helpers for the v0 translator."""

from __future__ import annotations

import re

from arango_query_core import AqlQuery, MappingResolver

_INDENT = "  "
_FOR_RE = re.compile(r"^\s*FOR\b")
_TERMINAL_RE = re.compile(r"^\s*(?:RETURN|SORT|LIMIT|COLLECT)\b")
_FILTER_LET_RE = re.compile(r"^\s*(?:FILTER|LET|PRUNE)\b")


_WITH_BIND_KEY = "@withCollection"


def _with_collection_refs(
    vertex_collections: set[str], bind_vars: dict[str, object]
) -> tuple[list[str], dict[str, object]]:
    """``@@`` references for *vertex_collections*, and the bind vars they need.

    Collection names come from the mapping, which callers supply with the
    request, so they are never spliced into the AQL text: a name such as
    ``IAM-AWS-DOCS-DEMO_Chunks`` does not parse as a bare identifier, and a
    crafted one could inject AQL. A collection the query already binds reuses
    that bind variable; the rest get fresh ``@withCollectionN`` names that
    clash with no existing bind variable. Ordered by collection name, so the
    output is deterministic.
    """
    existing: dict[str, str] = {}
    for key, val in bind_vars.items():
        if key.startswith("@") and isinstance(val, str) and val not in existing:
            existing[val] = key
    added: dict[str, object] = {}
    refs: list[str] = []
    n = 0
    for coll in sorted(vertex_collections):
        bound = existing.get(coll)
        if bound is None:
            while f"{_WITH_BIND_KEY}{n}" in bind_vars:
                n += 1
            bound = f"{_WITH_BIND_KEY}{n}"
            n += 1
            added[bound] = coll
        refs.append("@" + bound)
    return refs, {**bind_vars, **added}


def _prepend_with_collections(result: AqlQuery, resolver: MappingResolver) -> AqlQuery:
    """Prepend ``WITH @@c1, @@c2, ...`` listing all vertex collections.

    ArangoDB requires a leading ``WITH`` declaration of all vertex collections
    accessed during graph traversals. This is mandatory in cluster deployments
    and harmless in single-server mode. Edge collections are excluded. The
    collections are bind parameters (:func:`_with_collection_refs`).
    """
    has_traversal = re.search(
        r"\b(?:OUTBOUND|INBOUND|ANY|SHORTEST_PATH|ALL_SHORTEST_PATHS)\b",
        result.text,
    )
    if not has_traversal:
        return result

    edge_collections: set[str] = set()
    rels = resolver.bundle.physical_mapping.get("relationships", {})
    if isinstance(rels, dict):
        for rmap in rels.values():
            ec = rmap.get("edgeCollectionName") or rmap.get("collectionName")
            if isinstance(ec, str) and ec:
                edge_collections.add(ec)

    vertex_collections: set[str] = set()

    for key, val in result.bind_vars.items():
        if key.startswith("@") and isinstance(val, str) and val:
            if val not in edge_collections:
                vertex_collections.add(val)

    entities = resolver.bundle.physical_mapping.get("entities", {})
    if isinstance(entities, dict):
        for emap in entities.values():
            coll = emap.get("collectionName")
            if isinstance(coll, str) and coll and coll not in edge_collections:
                vertex_collections.add(coll)

    if not vertex_collections:
        return result

    refs, bind_vars = _with_collection_refs(vertex_collections, dict(result.bind_vars))
    with_line = "WITH " + ", ".join(refs)
    return AqlQuery(
        text=with_line + "\n" + result.text,
        bind_vars=bind_vars,
        debug=result.debug,
        warnings=result.warnings,
    )


def _reindent_aql(text: str) -> str:
    """Re-indent AQL to reflect the nesting structure of FOR loops."""
    raw_lines = text.split("\n")
    out: list[str] = []
    depth = 0
    first_for_depth = -1

    for raw in raw_lines:
        stripped = raw.strip()
        if not stripped:
            out.append("")
            continue

        if stripped.startswith("WITH "):
            out.append(stripped)
            continue

        if _FOR_RE.match(stripped):
            indent = _INDENT * depth
            out.append(indent + stripped)
            if first_for_depth < 0:
                first_for_depth = depth
            depth += 1
        elif _TERMINAL_RE.match(stripped):
            out.append(_INDENT * depth + stripped)
        elif _FILTER_LET_RE.match(stripped):
            out.append(_INDENT * depth + stripped)
        else:
            out.append(_INDENT * depth + stripped)

    return "\n".join(out)


_VARLEN_TRAVERSAL_RE = re.compile(
    r"FOR \w+, (?P<e>\w+)(?:, \w+)? IN (?P<min>\d+)\.\.(?P<max>\d+) (?:OUTBOUND|INBOUND|ANY) \S+ @@\w+"
)
_NEXT_FOR_RE = re.compile(r"\bFOR ")


def _guard_varlength_edge_types(text: str) -> str:
    """Make a variable-length traversal over a type-discriminated edge
    collection follow only edges of its relationship type.

    Every emitter checks the type of the traversal's *last* edge only
    (``FILTER r[@relTypeField] == @relTypeValue``). On an LPG edge collection
    that let ``[:REPLY_OF*0..]`` walk through every other relationship type —
    wrong paths, and an exponential blow-up (LDBC IS2 timed out) — and dropped
    the zero-hop row, where ``r`` is null. For each variable-length traversal
    whose edge variable has such a filter, this adds
    ``PRUNE r != null AND r[@tf] != @tv``, so expansion stops at the first
    edge of another type (the path that ends in it is still removed by the
    last-edge filter), and for a zero minimum lets ``r == null`` through.
    """
    edits: list[tuple[int, int, str]] = []  # (start, end, replacement) on the original text
    for m in _VARLEN_TRAVERSAL_RE.finditer(text):
        if (m.group("min"), m.group("max")) == ("1", "1"):
            continue
        edge = m.group("e")
        nxt = _NEXT_FOR_RE.search(text, m.end())
        window_end = nxt.start() if nxt else len(text)
        f = re.compile(rf"FILTER {re.escape(edge)}\[@(\w+)\] == @(\w+)").search(text, m.end(), window_end)
        if f is None:
            continue  # a dedicated edge collection: every edge has the type
        tf, tv = f.group(1), f.group(2)
        edits.append((m.end(), m.end(), f" PRUNE {edge} != null AND {edge}[@{tf}] != @{tv}"))
        if m.group("min") == "0":
            edits.append((f.start(), f.end(), f"FILTER ({edge} == null OR {edge}[@{tf}] == @{tv})"))
    for start, end, replacement in sorted(edits, reverse=True):
        text = text[:start] + replacement + text[end:]
    return text


_ANY_TRAVERSAL_RANGE_RE = re.compile(
    r"(?P<head>\bFOR [\w, ]+ IN )(?P<min>\d+)\.\.(?P<max>\d+)(?= (?:OUTBOUND|INBOUND|ANY) )"
)


def empty_varlength_ranges(text: str) -> str:
    """A variable-length range whose minimum exceeds its maximum (``*2..1``,
    ``*..0``) matches nothing, as in Cypher; AQL refuses such a depth (ERR
    1501). The traversal keeps a valid ``0..0`` depth and is followed by
    ``FILTER false``."""
    out: list[str] = []
    for line in text.split("\n"):
        m = _ANY_TRAVERSAL_RANGE_RE.search(line)
        if m and int(m.group("min")) > int(m.group("max")):
            indent = line[: len(line) - len(line.lstrip())]
            out.append(line[: m.start("min")] + "0..0" + line[m.end("max") :])
            out.append(f"{indent}  FILTER false")
            continue
        out.append(line)
    return "\n".join(out)
