"""Automatic mapping acquisition from a live ArangoDB database.

Provides three tiers of mapping acquisition:
1. Analyzer (primary): delegates to arangodb-schema-analyzer for full ontology
   extraction across PG, LPG, and hybrid schemas
2. Heuristic (fallback): fast classification + simple mapping construction when
   the analyzer is not installed
3. Auto (default): analyzer first, heuristic fallback on ImportError
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

# Upstream fingerprint primitives (arangodb-schema-analyzer >= 0.3.0) are
# imported lazily inside :func:`_shape_fingerprint` / :func:`_full_fingerprint`.
# Lazy so this module keeps working when only the heuristic mapping tier is
# installed (the `analyzer` extra is optional — see module docstring tier 2).
# The wrappers bake in our cache-collection exclusion; see §5 of
# docs/schema_analyzer_issues/WAVE_4M_ARCHITECTURE.md for why excluding the
# cache collection is a correctness invariant, not a perf tweak.
from arango_query_core import (
    CoreError,
    MappingBundle,
    MappingResolver,
    MappingSource,
    is_valid_collection_name,
)

from ._arango_sync import bind, sync
from .schema_cache import (
    DEFAULT_CACHE_COLLECTION,
    DEFAULT_CACHE_KEY,
    ArangoSchemaCache,
)
from .service_collections import hide_service_collections, service_collections


def _dict_or_empty(value: Any) -> dict[str, Any]:
    """*value* if it is a dict, else a fresh empty one."""
    return value if isinstance(value, dict) else {}


logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from arango.database import StandardDatabase


# Schema-cache freshness TTL (seconds). Within this window a cached mapping is
# trusted *without* re-running the live-database fingerprint check, which walks
# every collection (counts + indexes) and costs several seconds on a remote
# cluster — turning every cache hit into a multi-second operation and defeating
# the cache. The TTL bounds how stale a served mapping can be; the explicit
# "Refresh schema" path (``force_refresh=True``) bypasses it entirely for an
# immediate rebuild. Set ``ARANGO_CYPHER_SCHEMA_CACHE_TTL_S=0`` to disable the
# fast-path and always fingerprint (the pre-TTL behaviour).
def _default_cache_ttl_seconds() -> int:
    raw = os.environ.get("ARANGO_CYPHER_SCHEMA_CACHE_TTL_S")
    if raw is None or raw.strip() == "":
        return 300
    try:
        val = int(raw)
    except ValueError:
        logger.warning("Invalid ARANGO_CYPHER_SCHEMA_CACHE_TTL_S=%r; falling back to 300s", raw)
        return 300
    return max(0, val)


CACHE_TTL_SECONDS = _default_cache_ttl_seconds()


# LPG full-label-set floor for analyzer-backed mappings. A labeled property
# graph keeps its entire label vocabulary in one discriminator field, so the
# analyzer's default top-K value cap silently drops real labels and disables
# label-rooted queries. Set ``ARANGO_CYPHER_MIN_TYPE_VALUE_COUNT`` to a positive
# floor to keep every discriminator value at/above that document count.
# 0 / unset = analyzer default (top-K), i.e. unchanged behaviour.
def _default_min_type_value_count() -> int:
    raw = os.environ.get("ARANGO_CYPHER_MIN_TYPE_VALUE_COUNT")
    if raw is None or raw.strip() == "":
        return 0
    try:
        return max(0, int(raw))
    except ValueError:
        logger.warning("Invalid ARANGO_CYPHER_MIN_TYPE_VALUE_COUNT=%r; ignoring", raw)
        return 0


# In-memory fast path: (bundle, ts, shape_fp, full_fp) keyed by db name + cache key.
_mapping_cache: dict[str, tuple[MappingBundle, float, str, str]] = {}

# Operational counter — incremented every time `_build_fresh_bundle` falls
# through to the heuristic path because `schema_analyzer` could not be
# imported. Read-only for the outside world; the current release does not
# yet expose a /metrics endpoint, but operators can inspect it from a Python
# shell or a future metrics surface will aggregate it.
_heuristic_fallback_counter: int = 0


#: Analyzer notes, matched by message prefix, with the code and severity the
#: service reports them under. The UI repeats the baseline prefix
#: (ui/src/api/client.ts BASELINE_NOTE_PREFIX) for servers older than this.
#: ``info`` describes normal operation: the service always runs the analyzer
#: without an LLM (``AgenticSchemaAnalyzer()`` below), so this note is on every
#: analyzer mapping, and the Workbench keeps info notes out of its warning
#: banner.
_ANALYZER_NOTES: tuple[tuple[str, str, str], ...] = (
    ("LLM provider not configured", "ANALYZER_BASELINE_NO_LLM", "info"),
)
WARNING_SEVERITIES = frozenset({"info", "warning", "error"})
#: Analyzer messages that lead with their own category ("ORPHAN_AUTOGRAPH_KG: ...").
_CATEGORY_PREFIX = re.compile(r"^([A-Z][A-Z0-9_]{2,}):\s")


def _note_code(message: str) -> str:
    """A code unique to *message*: its category (or ``ANALYZER_NOTE``) plus a
    short hash. Clients key dismissals on the code, so a shared code would let
    one dismissal hide every other — and every later — analyzer note."""
    match = _CATEGORY_PREFIX.match(message)
    category = match.group(1) if match else "ANALYZER_NOTE"
    return f"{category}:{hashlib.sha1(message.encode('utf-8')).hexdigest()[:8]}"


def normalize_warnings(warnings: Any) -> list[dict[str, Any]]:
    """Schema warnings as ``{code, message, severity[, install_hint]}`` dicts.

    The analyzer reports warnings (and errors) as plain strings, while this
    module attaches structured ones (:func:`_attach_warning`), so a bundle's
    ``metadata["warnings"]`` is mixed. Schema responses normalize it here, so
    clients see one shape. Strings are matched against :data:`_ANALYZER_NOTES`;
    anything unrecognized gets a message-specific code (:func:`_note_code`)
    and severity ``warning``. Entries with no message are dropped. A bare
    string is one warning, not one per character; any other non-list is none.
    """
    if isinstance(warnings, str):
        warnings = [warnings]
    elif not isinstance(warnings, (list, tuple)):
        return []
    out: list[dict[str, Any]] = []
    for w in warnings:
        if isinstance(w, str):
            message = w.strip()
            if not message:
                continue
            code, severity = _note_code(message), "warning"
            for prefix, known_code, known_severity in _ANALYZER_NOTES:
                if message.startswith(prefix):
                    code, severity = known_code, known_severity
                    break
            out.append({"code": code, "message": message, "severity": severity})
        elif isinstance(w, dict):
            raw_message = w.get("message")
            if not isinstance(raw_message, str) or not raw_message.strip():
                continue
            raw_code = w.get("code")
            raw_severity = w.get("severity")
            entry: dict[str, Any] = {
                "code": raw_code
                if isinstance(raw_code, str) and raw_code
                else _note_code(raw_message.strip()),
                "message": raw_message.strip(),
                "severity": raw_severity if raw_severity in WARNING_SEVERITIES else "warning",
            }
            hint = w.get("install_hint")
            if isinstance(hint, str) and hint:
                entry["install_hint"] = hint
            out.append(entry)
    return out


def _attach_warning(
    bundle: MappingBundle,
    *,
    code: str,
    message: str,
    install_hint: str | None = None,
) -> MappingBundle:
    """Return a copy of ``bundle`` with an additional structured warning.

    Warnings live at ``bundle.metadata["warnings"]``. Entries this function
    adds are dicts (``code``, ``message``, ``severity`` and optionally
    ``install_hint``), but the analyzer's own entries are plain strings, so
    readers must go through :func:`normalize_warnings`. Each call appends;
    existing warnings are preserved. Deliberately copies the
    metadata dict so the original bundle (and any cached reference to it)
    is not mutated under the caller's feet.
    """
    meta = dict(bundle.metadata or {})
    warnings = list(meta.get("warnings") or [])
    warning: dict[str, Any] = {"code": code, "message": message, "severity": "warning"}
    if install_hint:
        warning["install_hint"] = install_hint
    warnings.append(warning)
    meta["warnings"] = warnings
    return MappingBundle(
        conceptual_schema=bundle.conceptual_schema,
        physical_mapping=bundle.physical_mapping,
        metadata=meta,
        owl_turtle=bundle.owl_turtle,
        source=bundle.source,
    )


def _bundle_needs_reacquire(bundle: MappingBundle) -> bool:
    """True when a cached heuristic-fallback bundle should be rebuilt.

    Returns True iff (a) the bundle carries an ``ANALYZER_NOT_INSTALLED``
    warning from an earlier heuristic fallback, AND (b) ``schema_analyzer``
    is now importable in this process. The second check makes the retry
    loop self-healing: when an operator installs the analyzer and the
    next request lands on this worker, the cached degraded bundle is
    treated as a miss and the analyzer path runs.
    """
    warnings = (bundle.metadata or {}).get("warnings") or []
    if not any(isinstance(w, dict) and w.get("code") == "ANALYZER_NOT_INSTALLED" for w in warnings):
        return False
    try:
        import schema_analyzer  # noqa: F401
    except ImportError:
        return False
    return True


def _cache_key(db: StandardDatabase) -> str:
    """Stable cache key: database name only. Used as the dict key.

    The actual staleness check is done via :func:`_shape_fingerprint` and
    :func:`_full_fingerprint` which inspect the live collection set.
    """
    try:
        return db.name
    except Exception:
        return ""


# Characters legal in an ArangoDB document ``_key`` are a superset of what a
# graph name can contain, but we still sanitise defensively so a graph name can
# never break the persistent cache key (PRD §17.4 "Cache isolation").
_GRAPH_KEY_SAFE_RE = re.compile(r"[^A-Za-z0-9_.\-]")


def _graph_scoped_cache_key(base: str, graph_name: str | None) -> str:
    """Return a graph-aware variant of a cache key.

    ``base`` with no graph → unchanged (the unscoped "all collections" view).
    ``base`` + ``graph_name`` → ``"<base>::graph::<sanitised-name>"`` so each
    named graph — and the unscoped view — gets an independent cache slot and
    they never alias one another.
    """
    if not graph_name:
        return base
    safe = _GRAPH_KEY_SAFE_RE.sub("_", graph_name)
    return f"{base}::graph::{safe}" if base else f"graph::{safe}"


def graph_collections(db: StandardDatabase, graph_name: str) -> tuple[set[str], set[str]]:
    """Resolve the vertex- and edge-collection membership of a named graph.

    Returns ``(vertex_collections, edge_collections)``. The vertex set is the
    union of every edge definition's from/to vertex collections plus the graph's
    orphan collections; the edge set is the union of every edge definition's edge
    collection (PRD §17.1).

    Raises :class:`CoreError` (code ``UNKNOWN_GRAPH``) when the graph does not
    exist so callers can map it to an HTTP 404.
    """
    try:
        exists = db.has_graph(graph_name)
    except Exception:  # pragma: no cover - defensive: treat probe failure as missing
        exists = False
    if not exists:
        raise CoreError(f"Named graph {graph_name!r} does not exist", code="UNKNOWN_GRAPH")

    graph = db.graph(graph_name)
    vertex: set[str] = set()
    edges: set[str] = set()
    # ``Graph.vertex_collections()`` returns the full set of vertex
    # collections in the graph — both those referenced by an edge definition
    # and the orphan (edge-less) ones — so it subsumes orphan enumeration.
    try:
        names: list[str] = sync(graph.vertex_collections() or [])
        vertex.update(names)
    except Exception:  # pragma: no cover - defensive across driver versions
        pass
    for ed in sync(graph.edge_definitions() or []):
        edge_col = ed.get("edge_collection") or ed.get("edgeCollection")
        if edge_col:
            edges.add(edge_col)
        # Defensive: also fold the declared from/to vertex collections in,
        # in case a driver version returns a partial vertex_collections list.
        for key in ("from_vertex_collections", "fromVertexCollections"):
            vertex.update(ed.get(key) or [])
        for key in ("to_vertex_collections", "toVertexCollections"):
            vertex.update(ed.get(key) or [])
    return vertex, edges


def _filter_bundle_to_graph(
    bundle: MappingBundle,
    vertex_collections: set[str],
    edge_collections: set[str],
) -> MappingBundle:
    """Return a copy of ``bundle`` restricted to a named graph's collections.

    Filters the physical mapping's entities (by ``collectionName`` ∈
    ``vertex_collections``) and relationships (by ``edgeCollectionName`` ∈
    ``edge_collections``), then prunes the conceptual schema and
    ``metadata.statistics`` to the surviving labels / relationship types so every
    downstream consumer (resolver summary, NL prompt, transpiler) sees only the
    scoped graph (PRD §17.4).
    """
    pm = bundle.physical_mapping or {}
    pm_entities = _dict_or_empty(pm.get("entities"))
    pm_rels = _dict_or_empty(pm.get("relationships"))

    kept_entities = {
        label: emap
        for label, emap in pm_entities.items()
        if isinstance(emap, dict) and emap.get("collectionName") in vertex_collections
    }
    kept_rels = {
        rtype: rmap
        for rtype, rmap in pm_rels.items()
        if isinstance(rmap, dict)
        and (rmap.get("edgeCollectionName") or rmap.get("collectionName")) in edge_collections
    }
    kept_labels = set(kept_entities)
    kept_rtypes = set(kept_rels)

    new_pm = {**pm, "entities": kept_entities, "relationships": kept_rels}

    cs = bundle.conceptual_schema or {}
    new_cs = dict(cs)
    cs_entities = cs.get("entities")
    if isinstance(cs_entities, list):
        new_cs["entities"] = [
            e
            for e in cs_entities
            if not isinstance(e, dict) or (e.get("name") or e.get("label") or e.get("entity")) in kept_labels
        ]
    cs_rels = cs.get("relationships")
    if isinstance(cs_rels, list):
        new_cs["relationships"] = [
            r for r in cs_rels if not isinstance(r, dict) or r.get("type") in kept_rtypes
        ]

    meta = bundle.metadata or {}
    new_meta = dict(meta)
    stats = meta.get("statistics")
    if isinstance(stats, dict):
        new_stats = dict(stats)
        if isinstance(stats.get("entities"), dict):
            new_stats["entities"] = {k: v for k, v in stats["entities"].items() if k in kept_labels}
        if isinstance(stats.get("relationships"), dict):
            new_stats["relationships"] = {k: v for k, v in stats["relationships"].items() if k in kept_rtypes}
        new_meta["statistics"] = new_stats

    return MappingBundle(
        conceptual_schema=new_cs,
        physical_mapping=new_pm,
        metadata=new_meta,
        owl_turtle=bundle.owl_turtle,
        source=bundle.source,
    )


def _reconstruct_graph_membership(physical_mapping: dict[str, Any]) -> dict[str, dict[str, list[str]]]:
    """Build a ``graphMembership`` summary from per-entry ``graphs`` tags.

    The schema analyzer's *analyze* response carries ``metadata.graphMembership``,
    but the CSI export (what we consume via ``export_mapping``) does not — it
    only carries the per-entry ``physicalMapping[*].graphs`` annotation. Per the
    analyzer's transpiler-integration contract, CSI consumers reconstruct the
    summary from those per-entry tags, which is what this does.

    Returns ``{graph_name: {"vertexCollections": [...], "edgeCollections": [...]}}``
    with sorted, de-duplicated collection lists. Returns an empty dict when no
    entry carries a graph tag (an ungraphed database, or a pre-tagging analyzer
    build) so callers can treat the bundle as un-scopeable and fall back to a
    live graph lookup.
    """
    pm = physical_mapping or {}
    entities = _dict_or_empty(pm.get("entities"))
    rels = _dict_or_empty(pm.get("relationships"))

    vertex_by_graph: dict[str, set[str]] = {}
    edge_by_graph: dict[str, set[str]] = {}

    for emap in entities.values():
        if not isinstance(emap, dict):
            continue
        coll = emap.get("collectionName")
        for g in emap.get("graphs") or []:
            vertex_by_graph.setdefault(g, set())
            if coll:
                vertex_by_graph[g].add(coll)
            edge_by_graph.setdefault(g, set())

    for rmap in rels.values():
        if not isinstance(rmap, dict):
            continue
        coll = rmap.get("edgeCollectionName") or rmap.get("collectionName")
        for g in rmap.get("graphs") or []:
            edge_by_graph.setdefault(g, set())
            if coll:
                edge_by_graph[g].add(coll)
            vertex_by_graph.setdefault(g, set())

    names = set(vertex_by_graph) | set(edge_by_graph)
    return {
        g: {
            "vertexCollections": sorted(vertex_by_graph.get(g, set())),
            "edgeCollections": sorted(edge_by_graph.get(g, set())),
        }
        for g in sorted(names)
    }


def _graph_membership_collections(bundle: MappingBundle, graph_name: str) -> tuple[set[str], set[str]] | None:
    """Resolve a named graph's collection membership from the bundle itself.

    Reads ``metadata.graphMembership`` (populated by :func:`acquire_mapping_bundle`
    from the analyzer) so a scoped view can be derived without a live database
    round-trip. Returns ``(vertex_collections, edge_collections)`` when the graph
    is represented in the membership summary, or ``None`` when the bundle is not
    graph-aware or does not know this graph (caller falls back to the live
    :func:`graph_collections` lookup).

    Two shapes are accepted:

    * **Analyzer-native** — per-graph entries nested under a ``graphs`` key, with
      sibling ``status`` / ``graphCount`` / ``ungraphed`` metadata::

        {"graphs": {"<name>": {"vertexCollections": [...], "edgeCollections": [...]}}, ...}

    * **Reconstructed fallback** (:func:`_reconstruct_graph_membership`) — a flat
      ``{"<name>": {"vertexCollections": [...], "edgeCollections": [...]}}`` map.
    """
    meta = bundle.metadata or {}
    gm = meta.get("graphMembership") or meta.get("graph_membership")
    if not isinstance(gm, dict):
        return None

    # Analyzer-native shape: per-graph entries live under "graphs"; the sibling
    # "status"/"graphCount"/"ungraphed" keys are NOT graphs, so never fall back
    # to top-level lookup when the nested structure is present.
    nested = gm.get("graphs")
    entry = nested.get(graph_name) if isinstance(nested, dict) else gm.get(graph_name)
    if not isinstance(entry, dict):
        return None

    vertex = set(entry.get("vertexCollections") or entry.get("vertex_collections") or [])
    edges = set(entry.get("edgeCollections") or entry.get("edge_collections") or [])
    return vertex, edges


def _scope_bundle_to_graph(db: StandardDatabase, bundle: MappingBundle, graph_name: str) -> MappingBundle:
    """Filter ``bundle`` to a named graph, preferring embedded membership.

    When the bundle carries analyzer-provided graph membership the scope is
    derived entirely in memory (no database call); otherwise we resolve the
    graph's membership live via :func:`graph_collections` (which raises
    ``CoreError(code="UNKNOWN_GRAPH")`` for a non-existent graph). Both paths
    funnel through the same :func:`_filter_bundle_to_graph` so the result shape
    is identical regardless of how membership was obtained.
    """
    mem = _graph_membership_collections(bundle, graph_name)
    if mem is not None:
        vertex, edges = mem
    else:
        vertex, edges = graph_collections(db, graph_name)
    return _filter_bundle_to_graph(bundle, vertex, edges)


def _fallback_fingerprint(
    db: StandardDatabase,
    *,
    include_counts: bool,
    cache_collection: str = DEFAULT_CACHE_COLLECTION,
) -> str:
    """Coarse local fingerprint used only when ``schema_analyzer`` is unavailable.

    The heuristic mapping tier is advertised as "works without the analyzer
    extra" (see module docstring), so we still need *some* stable digest for
    the cache-freshness check. Upstream hashes far more (types + every index
    signature); this fallback only notices collection set / count changes.
    Acceptable because the degraded path already opts out of analyzer-level
    precision. Re-introduces ~6 LOC versus the ~51 LOC removed in PR-2.
    """
    try:
        cols = sync(db.collections() or [])
    except Exception:
        cols = []
    names = sorted(
        c.get("name", "")
        for c in cols
        if isinstance(c, dict)
        and isinstance(c.get("name"), str)
        and not c["name"].startswith("_")
        and c["name"] not in service_collections(cache_collection)
    )
    parts = [db.name, *names]
    if include_counts:
        for name in names:
            try:
                parts.append(f"{name}:{db.collection(name).count()}")
            except Exception:
                parts.append(f"{name}:-1")
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def _shape_fingerprint(db: StandardDatabase, *, cache_collection: str = DEFAULT_CACHE_COLLECTION) -> str:
    """Hash of the schema *shape*: collection set, types, and index digests.

    Thin wrapper around ``schema_analyzer.fingerprint_physical_shape`` (v0.3.0+)
    that bakes in our cache-collection exclusion. Kept as a named function (a)
    so existing imports in tests and callers continue to resolve and (b) so
    every caller in this module hits the same exclusion policy without
    rediscovering it.

    Excludes row counts so ordinary writes (INSERT / UPDATE / REMOVE without
    a schema shape change) do not invalidate the fingerprint. This is the
    fingerprint that decides whether a full re-introspection is needed.

    NOTE (cache re-key event, 2026-04-20): when this module was rewired to
    upstream at v0.3.0, the on-disk hash format changed — existing entries
    in ``_arango_schema_cache`` will miss their fingerprint check exactly
    once and be rebuilt. No action required on the operator side; the next
    ``get_mapping()`` call after deployment refills the cache under the new
    fingerprint.
    """
    try:
        from schema_analyzer import fingerprint_physical_shape
    except ImportError:
        return _fallback_fingerprint(db, include_counts=False, cache_collection=cache_collection)

    return fingerprint_physical_shape(db, exclude_collections=set(service_collections(cache_collection)))


def _full_fingerprint(db: StandardDatabase, *, cache_collection: str = DEFAULT_CACHE_COLLECTION) -> str:
    """Shape fingerprint + per-collection row counts.

    Thin wrapper around ``schema_analyzer.fingerprint_physical_counts``
    (v0.3.0+) with our cache-collection exclusion applied. See
    :func:`_shape_fingerprint` for the rationale and the one-time cache
    re-key event.

    Changes whenever either the schema shape or any collection's row count
    changes. When this differs but :func:`_shape_fingerprint` matches, the
    cached mapping remains valid and only cardinality statistics need
    re-computation (the stats-only refresh path).
    """
    try:
        from schema_analyzer import fingerprint_physical_counts
    except ImportError:
        return _fallback_fingerprint(db, include_counts=True, cache_collection=cache_collection)

    return fingerprint_physical_counts(db, exclude_collections=set(service_collections(cache_collection)))


@dataclass(frozen=True)
class SchemaChangeReport:
    """Result of a lightweight schema-change probe.

    Returned by :func:`describe_schema_change`. Compared to :func:`get_mapping`
    this probe does not load or rebuild the mapping; it answers only the
    question "would ``get_mapping()`` need to do real work?". Use it to
    short-circuit application-level refresh logic (e.g. skip prompt rebuilds,
    cache-bust downstream views, signal clients) when nothing has changed.

    ``status`` values:

    - ``"unchanged"`` — shape and counts both match cache; the cached mapping
      is fully valid and includes up-to-date statistics.
    - ``"stats_changed"`` — shape matches but counts differ. Calling
      ``get_mapping()`` will reuse the cached conceptual schema + physical
      mapping and refresh only the cardinality statistics in metadata.
    - ``"shape_changed"`` — the collection set, a collection's type, or an
      index set has changed. Calling ``get_mapping()`` triggers a full
      re-introspection (analyzer or heuristic).
    - ``"no_cache"`` — nothing cached yet; first call since service start or
      after an explicit invalidation. ``get_mapping()`` will do a full
      introspection.
    """

    status: Literal["unchanged", "stats_changed", "shape_changed", "no_cache"]
    current_shape_fingerprint: str
    current_full_fingerprint: str
    cached_shape_fingerprint: str | None
    cached_full_fingerprint: str | None

    @property
    def unchanged(self) -> bool:
        """Ergonomic predicate: ``True`` iff ``status == "unchanged"``."""
        return self.status == "unchanged"

    @property
    def needs_full_rebuild(self) -> bool:
        """``True`` when the next ``get_mapping()`` will re-introspect."""
        return self.status in ("shape_changed", "no_cache")


def describe_schema_change(
    db: StandardDatabase,
    *,
    cache_collection: str = DEFAULT_CACHE_COLLECTION,
    cache_key: str = DEFAULT_CACHE_KEY,
) -> SchemaChangeReport:
    """Report whether the schema has changed since the last cached mapping.

    Cheap: runs ``db.collections()`` + per-collection ``count()`` +
    ``indexes()``. No document sampling, no AQL ``COLLECT``, no LLM call.
    Typical cost: ~20 ms for a 50-collection schema.

    Inspects the in-memory cache first, then the persistent ArangoDB
    collection cache. Does not mutate either cache — purely read-only.
    """
    shape_fp = _shape_fingerprint(db, cache_collection=cache_collection)
    full_fp = _full_fingerprint(db, cache_collection=cache_collection)
    key = _cache_key(db)
    cache = ArangoSchemaCache(collection_name=cache_collection, cache_key=cache_key)

    cached_shape: str | None = None
    cached_full: str | None = None

    mem = _mapping_cache.get(key)
    if mem is not None:
        _bundle, _ts, cached_shape, cached_full = mem
    else:
        persisted = cache.get(db)
        if persisted is not None:
            _bundle, cached_shape, cached_full = persisted

    if cached_shape is None:
        status: Literal["unchanged", "stats_changed", "shape_changed", "no_cache"] = "no_cache"
    elif cached_shape != shape_fp:
        status = "shape_changed"
    elif cached_full != full_fp:
        status = "stats_changed"
    else:
        status = "unchanged"

    return SchemaChangeReport(
        status=status,
        current_shape_fingerprint=shape_fp,
        current_full_fingerprint=full_fp,
        cached_shape_fingerprint=cached_shape,
        cached_full_fingerprint=cached_full,
    )


_IES_TO_Y_WORDS = {
    "companies",
    "cities",
    "categories",
    "stories",
    "bodies",
    "parties",
    "entries",
    "queries",
    "countries",
    "activities",
    "properties",
    "policies",
    "strategies",
    "histories",
    "industries",
    "libraries",
    "boundaries",
    "commodities",
    "entities",
    "identities",
    "priorities",
    "securities",
    "territories",
    "utilities",
    "vulnerabilities",
}


def _singularize(name: str) -> str:
    """Naive English singularization for collection-name-to-label conversion."""
    lower = name.lower()
    # "ies" → "y" only for known patterns (not "movies", "series", "species")
    if lower.endswith("ies") and len(name) > 4:
        if lower in _IES_TO_Y_WORDS:
            return name[:-3] + "y"
        # Heuristic: if the char before "ies" is a consonant pair or single consonant
        # and the result would be a short stem, prefer ies→y
        # Otherwise strip just the "s" to preserve the root (movies→movie)
        prefix = name[:-3]
        if len(prefix) >= 2 and prefix[-1].lower() not in "aeiou" and prefix[-2].lower() not in "aeiou":
            return prefix + "y"
        return name[:-1]
    if (
        lower.endswith("ses")
        or lower.endswith("xes")
        or lower.endswith("zes")
        or lower.endswith("ches")
        or lower.endswith("shes")
    ):
        return name[:-2]
    if lower.endswith("s") and not lower.endswith("ss") and not lower.endswith("us"):
        return name[:-1]
    return name


def _pascal_case(name: str) -> str:
    parts = re.split(r"[_\-\s]+", name)
    return "".join(p.capitalize() for p in parts if p)


def _collection_label(collection_name: str) -> str:
    """Infer a conceptual label from a collection name (e.g., 'users' -> 'User').

    Preserves existing PascalCase/camelCase capitalization when there are
    no word separators (underscores, hyphens, spaces).  Only applies
    capitalize-each-part logic when separators are present.
    """
    singular = _singularize(collection_name)
    if re.search(r"[_\-\s]", singular):
        return _pascal_case(singular)
    # Already a single token — preserve internal caps (e.g. EdrThreat),
    # just ensure the first letter is upper.
    return singular[0].upper() + singular[1:] if singular else singular


def classify_schema(db: StandardDatabase) -> str:
    """Fast heuristic: sample collections and classify as 'pg', 'lpg', 'hybrid', or 'unknown'.

    Strategy:
    - List all document collections and edge collections
    - For document collections: sample N docs, check if they have a common 'type'/'labels' field
      - If all docs have a type field with varying values -> LPG
      - If collection names match conceptual types (no type field) -> PG
    - For edge collections: check if they're dedicated or have a type/relation field
    - If mixed -> hybrid
    - If unclear -> unknown
    """
    db = hide_service_collections(db)
    try:
        all_cols = sync(db.collections())
    except Exception:
        return "unknown"

    doc_cols = []
    edge_cols = []
    for c in all_cols:
        if not isinstance(c, dict):
            continue
        name = c.get("name", "")
        if name.startswith("_"):
            continue
        if c.get("type") in (3, "edge"):
            edge_cols.append(name)
        else:
            doc_cols.append(name)

    if not doc_cols:
        return "unknown"

    type_field_names = {"type", "_type", "label", "labels", "kind", "entityType"}
    sample_size = 20

    doc_signals: list[str] = []
    for col_name in doc_cols:
        try:
            cursor = db.aql.execute(
                "FOR doc IN @@col LIMIT @n RETURN doc",
                bind_vars=bind({"@col": col_name, "n": sample_size}),
            )
            docs = list(sync(cursor))
        except Exception:
            doc_signals.append("unknown")
            continue

        if not docs:
            doc_signals.append("unknown")
            continue

        found_type_field = None
        for tf in type_field_names:
            count = sum(1 for d in docs if isinstance(d, dict) and tf in d)
            if count >= len(docs) * 0.8:
                found_type_field = tf
                break

        if found_type_field:
            try:
                distinct_cursor = db.aql.execute(
                    f"FOR doc IN @@col COLLECT v = doc.`{found_type_field}` RETURN v",
                    bind_vars={"@col": col_name},
                )
                values = {str(v) for v in sync(distinct_cursor) if v is not None}
            except Exception:
                values = set()
            if len(values) > 1:
                doc_signals.append("lpg")
            else:
                doc_signals.append("pg")
        else:
            doc_signals.append("pg")

    edge_signals: list[str] = []
    edge_type_fields = {"type", "relation", "relType", "_type"}
    for col_name in edge_cols:
        try:
            cursor = db.aql.execute(
                "FOR doc IN @@col LIMIT @n RETURN doc",
                bind_vars=bind({"@col": col_name, "n": sample_size}),
            )
            docs = list(sync(cursor))
        except Exception:
            edge_signals.append("unknown")
            continue

        if not docs:
            edge_signals.append("pg")
            continue

        found_type_field = None
        for tf in edge_type_fields:
            count = sum(1 for d in docs if isinstance(d, dict) and tf in d)
            if count >= len(docs) * 0.8:
                found_type_field = tf
                break

        if found_type_field:
            edge_signals.append("lpg")
        else:
            edge_signals.append("pg")

    all_signals = doc_signals + edge_signals
    meaningful = [s for s in all_signals if s != "unknown"]
    if not meaningful:
        return "unknown"

    pg_count = meaningful.count("pg")
    lpg_count = meaningful.count("lpg")

    if lpg_count == 0:
        return "pg"
    if pg_count == 0:
        return "lpg"
    return "hybrid"


# ---------------------------------------------------------------------------
# Data-quality profiling (sentinel detection, numeric-like strings)
# ---------------------------------------------------------------------------

# Case-insensitive string values commonly used as "null" sentinels in dirty data.
_SENTINEL_TOKENS: set[str] = {
    "NULL",
    "NONE",
    "NIL",
    "N/A",
    "NA",
    "UNKNOWN",
    "TBD",
    "TBA",
    "#N/A",
    "(NULL)",
}

# A sentinel candidate must occupy at least this share of the sampled values
# to be reported. Prevents isolated "-" or "" values from spuriously flagging
# legitimate columns.
_SENTINEL_MIN_SHARE = 0.02

# Numeric-like detection: share of non-sentinel strings that parse as numbers.
_NUMERIC_LIKE_MIN_SHARE = 0.8

# How many distinct sample values to keep per property for LLM context.
_SAMPLE_VALUES_KEEP = 4
_SAMPLE_VALUE_MAXLEN = 48


def _is_sentinel_token(s: str) -> bool:
    """Return True if ``s`` is a well-known null-sentinel string."""
    return s.strip().upper() in _SENTINEL_TOKENS


def _is_numeric_like(s: str) -> bool:
    """Return True if ``s`` parses as a number (int or float)."""
    try:
        float(s)
        return True
    except (TypeError, ValueError):
        return False


def _infer_value_type(val: Any) -> str:
    if val is None:
        return "null"
    if isinstance(val, bool):
        return "boolean"
    if isinstance(val, int | float):
        return "number"
    if isinstance(val, str):
        return "string"
    if isinstance(val, list):
        return "array"
    if isinstance(val, dict):
        return "object"
    return "string"


# Domain-agnostic semantic roles for a property, inferred from sampled values.
# Consumers (entity resolver, NL prompt) choose a matching strategy from the
# role rather than from hardcoded field names: identifiers → exact match,
# names/free-text → fuzzy, categorical/temporal/numeric → equality/range and
# never a fuzzy entity-name target.
ROLE_IDENTIFIER = "identifier"
ROLE_NAME = "name"
ROLE_FREE_TEXT = "free_text"
ROLE_CATEGORICAL = "categorical"
ROLE_TEMPORAL = "temporal"
ROLE_NUMERIC = "numeric"
ROLE_BOOLEAN = "boolean"
ROLE_OTHER = "other"

_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2})?(\.\d+)?(Z|[+-]\d{2}:?\d{2})?)?$")


def _classify_property_role(
    values: list[Any],
    dominant_type: str,
    numeric_like: bool,
) -> str:
    """Infer a domain-agnostic semantic role for one property.

    Heuristic and cheap (uses only the already-sampled ``values``). The goal is
    to let downstream code reason about *what kind of thing* a field holds
    (an identifier vs a human name vs a category vs a date) without hardcoding
    field-name lists like ``ticker``/``symbol``/``id``.
    """
    if dominant_type == "boolean":
        return ROLE_BOOLEAN

    strings = [v for v in values if isinstance(v, str) and not _is_sentinel_token(v)]

    # Temporal: ISO-8601 date/datetime strings dominate.
    if strings:
        iso_hits = sum(1 for s in strings if _ISO_DATE_RE.match(s.strip()))
        if iso_hits / len(strings) >= 0.8:
            return ROLE_TEMPORAL

    if dominant_type == "number" or numeric_like:
        return ROLE_NUMERIC

    if not strings:
        return ROLE_OTHER

    lowered = [s.strip().lower() for s in strings if s.strip()]
    if not lowered:
        return ROLE_OTHER

    n = len(lowered)
    distinct = len(set(lowered))
    distinct_ratio = distinct / n
    avg_len = sum(len(s) for s in lowered) / n
    max_len = max(len(s) for s in lowered)
    multiword_share = sum(1 for s in lowered if " " in s) / n
    token_share = sum(1 for s in lowered if " " not in s and len(s) <= 24) / n

    # Identifier: (near-)unique, short, single-token (e.g. tickers, codes, keys).
    if distinct_ratio >= 0.9 and max_len <= 32 and token_share >= 0.8 and multiword_share < 0.2:
        return ROLE_IDENTIFIER

    # Categorical: few distinct values relative to the sample, not long.
    if distinct_ratio <= 0.2 and distinct <= 50 and avg_len <= 40:
        return ROLE_CATEGORICAL

    # Free text: long and/or multi-word with high variety.
    if avg_len > 64 or (multiword_share >= 0.5 and avg_len > 24):
        return ROLE_FREE_TEXT

    # Otherwise a human-readable name/label.
    return ROLE_NAME


def _profile_property_values(
    values: list[Any],
    total_docs: int,
) -> dict[str, Any]:
    """Compute type / sentinel / numeric-like / sample metadata for one field.

    ``values`` is the list of raw values observed for this field across the
    sampled documents (same length as the number of docs where the field
    was present). ``total_docs`` is the total number of sampled docs
    (so ``required`` can be derived).
    """
    if not values:
        return {"field": "", "type": "string"}

    type_counts: dict[str, int] = {}
    for v in values:
        t = _infer_value_type(v)
        type_counts[t] = type_counts.get(t, 0) + 1

    dominant_type = max(type_counts, key=type_counts.get)  # type: ignore[arg-type]

    sentinel_counts: dict[str, int] = {}
    non_sentinel_strings: list[str] = []
    for v in values:
        if not isinstance(v, str):
            continue
        if _is_sentinel_token(v):
            key = v.strip().upper()
            sentinel_counts[key] = sentinel_counts.get(key, 0) + 1
        else:
            non_sentinel_strings.append(v)

    min_count = max(1, int(_SENTINEL_MIN_SHARE * len(values)))
    sentinel_values = sorted(
        [k for k, n in sentinel_counts.items() if n >= min_count],
        key=lambda k: -sentinel_counts[k],
    )

    numeric_like = False
    if non_sentinel_strings:
        numeric_hits = sum(1 for s in non_sentinel_strings if _is_numeric_like(s))
        if numeric_hits / len(non_sentinel_strings) >= _NUMERIC_LIKE_MIN_SHARE:
            numeric_like = True

    sample_values: list[str] = []
    seen: set[str] = set()
    for v in values:
        if not isinstance(v, str):
            continue
        if _is_sentinel_token(v):
            continue
        key = v[:_SAMPLE_VALUE_MAXLEN]
        if key in seen:
            continue
        seen.add(key)
        sample_values.append(key)
        if len(sample_values) >= _SAMPLE_VALUES_KEEP:
            break

    out: dict[str, Any] = {"type": dominant_type}
    out["role"] = _classify_property_role(values, dominant_type, numeric_like)
    if sentinel_values:
        out["sentinelValues"] = sentinel_values
    if numeric_like:
        out["numericLike"] = True
    if sample_values:
        out["sampleValues"] = sample_values
    if total_docs and len(values) == total_docs:
        out["required"] = True
    return out


def _sample_properties(
    db: StandardDatabase,
    collection_name: str,
    sample_size: int = 50,
) -> list[dict[str, Any]]:
    """Sample docs and return enriched property profiles.

    Each entry contains the property ``name`` plus data-quality metadata:
    ``type``, ``sentinelValues`` (string sentinels like 'NULL'),
    ``numericLike`` (non-sentinel string values parse as numbers), and
    ``sampleValues`` (a few representative values for LLM context).
    """
    try:
        cursor = db.aql.execute(
            "FOR doc IN @@col LIMIT @n RETURN doc",
            bind_vars=bind({"@col": collection_name, "n": sample_size}),
        )
        docs = list(sync(cursor))
    except Exception:
        return []

    if not docs:
        return []

    field_values: dict[str, list[Any]] = {}
    for doc in docs:
        if not isinstance(doc, dict):
            continue
        for key, val in doc.items():
            if key.startswith("_"):
                continue
            field_values.setdefault(key, []).append(val)

    out: list[dict[str, Any]] = []
    for name in sorted(field_values.keys()):
        prof = _profile_property_values(field_values[name], len(docs))
        entry: dict[str, Any] = {"name": name, "field": name, **prof}
        out.append(entry)
    return out


# Type-field candidate tiers.  Tier-1 names are unambiguous class discriminators
# (accepted on the 80%-coverage rule alone).  Tier-2 names are ambiguous — they
# are frequently used as scalar data fields too — so they additionally require a
# low cardinality ratio and class-like values.  See docs/schema_inference_bugfix_prd.md §4.1.
_TIER1_TYPE_FIELDS = ["type", "_type", "entityType"]
_TIER2_TYPE_FIELDS = ["label", "labels", "kind"]
_DOC_TYPE_FIELDS = _TIER1_TYPE_FIELDS + _TIER2_TYPE_FIELDS
_EDGE_TYPE_FIELDS = ["type", "relation", "relType", "_type", "label"]

_FILE_EXTENSION_SUFFIXES = (
    ".rst",
    ".md",
    ".pdf",
    ".asciidoc",
    ".txt",
    ".rtf",
    ".docx",
    ".html",
    ".json",
    ".xml",
    ".yaml",
    ".yml",
    ".ttl",
    ".owl",
)

# Tier-2 cardinality cap: reject if distinct-value count exceeds this.
_TIER2_ABSOLUTE_CARDINALITY_CAP = 50


def _looks_class_like(value: str) -> bool:
    """True when a candidate discriminator value plausibly names a class.

    A class-like name is a short alphanumeric token without dots, slashes, or
    whitespace, and does not end in a common file-extension suffix.
    """
    if not value or not value.strip():
        return False
    if any(c in value for c in (".", "/", " ", "\t")):
        return False
    lv = value.lower()
    if any(lv.endswith(suf) for suf in _FILE_EXTENSION_SUFFIXES):
        return False
    return True


def _detect_type_field(
    db: StandardDatabase,
    collection_name: str,
    candidates: list[str] | None = None,
    *,
    notes_sink: list[dict[str, Any]] | None = None,
) -> str | None:
    """Detect the type/label discriminator field in a collection, if any.

    A candidate must pass the existing 80% coverage rule.  Tier-1 candidates
    (``type``, ``_type``, ``entityType``) are accepted on coverage alone.
    Tier-2 candidates (``label``, ``labels``, ``kind``) must additionally:
      - have a distinct-value count ≤ ``max(50, int(0.5 * row_count))``, and
      - every sampled distinct value must be class-like (no dot, slash,
        whitespace, or file-extension suffix).

    When ``notes_sink`` is provided, each considered-but-rejected candidate is
    appended as ``{"field", "tier", "reason"}`` for observability.  Edge-side
    candidates (``relation``, ``relType``) are treated as tier-1.
    """
    if candidates is None:
        candidates = _DOC_TYPE_FIELDS
    try:
        cursor = db.aql.execute(
            "FOR doc IN @@col LIMIT @n RETURN doc",
            bind_vars=bind({"@col": collection_name, "n": 20}),
        )
        docs = list(sync(cursor))
    except Exception:
        return None

    if not docs:
        return None

    def _tier(field: str) -> int:
        return 2 if field in _TIER2_TYPE_FIELDS else 1

    for tf in candidates:
        count = sum(1 for d in docs if isinstance(d, dict) and tf in d)
        if count < len(docs) * 0.8:
            if notes_sink is not None and count > 0:
                notes_sink.append(
                    {
                        "field": tf,
                        "tier": _tier(tf),
                        "reason": f"coverage {count}/{len(docs)} below 80% threshold",
                    }
                )
            continue

        if _tier(tf) == 1:
            return tf

        try:
            row_count = int(sync(db.collection(collection_name).count() or 0))
        except Exception:
            row_count = len(docs)

        distinct_values = _type_field_values(db, collection_name, tf)
        distinct_count = len(distinct_values)

        cardinality_cap = max(_TIER2_ABSOLUTE_CARDINALITY_CAP, int(0.5 * row_count))
        if distinct_count > cardinality_cap:
            if notes_sink is not None:
                notes_sink.append(
                    {
                        "field": tf,
                        "tier": 2,
                        "reason": (
                            f"{distinct_count} distinct values over {row_count} rows "
                            f"exceeds cardinality cap {cardinality_cap}"
                        ),
                    }
                )
            continue

        non_class_like = [v for v in distinct_values if not _looks_class_like(v)]
        if non_class_like:
            sample = non_class_like[0]
            if notes_sink is not None:
                notes_sink.append(
                    {
                        "field": tf,
                        "tier": 2,
                        "reason": (
                            f"value {sample!r} is not class-like "
                            f"(contains '.', '/', whitespace, or a file extension)"
                        ),
                    }
                )
            continue

        return tf
    return None


def _type_field_values(db: StandardDatabase, collection_name: str, type_field: str) -> list[str]:
    """Get distinct values for a type field."""
    try:
        cursor = db.aql.execute(
            f"FOR doc IN @@col COLLECT val = doc.`{type_field}` RETURN val",
            bind_vars={"@col": collection_name},
        )
        vals: list[str] = []
        for v in sync(cursor):
            if v is None:
                continue
            if isinstance(v, list):
                vals.extend(str(x) for x in v)
            else:
                vals.append(str(v))
        return sorted(set(vals))
    except Exception:
        return []


def _sample_properties_filtered(
    db: StandardDatabase,
    collection_name: str,
    type_field: str,
    type_value: str,
    sample_size: int = 50,
) -> list[dict[str, Any]]:
    """Sample documents matching a specific type value and return enriched
    property profiles (same shape as :func:`_sample_properties`).
    """
    skip_fields = {"_key", "_id", "_rev", "_from", "_to", type_field, "labels"}
    try:
        cursor = db.aql.execute(
            f"FOR doc IN @@col FILTER doc.`{type_field}` == @val LIMIT @n RETURN doc",
            bind_vars=bind({"@col": collection_name, "val": type_value, "n": sample_size}),
        )
        docs = list(sync(cursor))
    except Exception:
        return []

    if not docs:
        return []

    field_values: dict[str, list[Any]] = {}
    for doc in docs:
        if not isinstance(doc, dict):
            continue
        for key, val in doc.items():
            if key in skip_fields:
                continue
            field_values.setdefault(key, []).append(val)

    out: list[dict[str, Any]] = []
    for name in sorted(field_values.keys()):
        prof = _profile_property_values(field_values[name], len(docs))
        entry: dict[str, Any] = {"name": name, "field": name, **prof}
        out.append(entry)
    return out


def _infer_lpg_edge_endpoints(
    db: StandardDatabase,
    edge_collection: str,
    type_field: str,
    type_value: str,
    entities_pm: dict[str, Any],
) -> tuple[str, str]:
    """Infer domain and range entity labels for a specific LPG edge type.

    Samples edges matching the type_value, resolves the _from/_to documents,
    and looks up their type to find the correct conceptual entity label.
    """
    col_type_map: dict[tuple[str, str], tuple[str, str]] = {}
    for label, pm in entities_pm.items():
        col = pm.get("collectionName", "")
        tf = pm.get("typeField")
        tv = pm.get("typeValue")
        if tf and tv:
            col_type_map[(col, tv)] = (label, tf)
        elif col:
            col_type_map[(col, "")] = (label, "")

    try:
        cursor = db.aql.execute(
            f"FOR e IN @@col FILTER e.`{type_field}` == @val LIMIT 10 RETURN {{f: e._from, t: e._to}}",
            bind_vars={"@col": edge_collection, "val": type_value},
        )
        samples = list(sync(cursor))
    except Exception:
        return ("Any", "Any")

    if not samples:
        return ("Any", "Any")

    def _resolve_label(doc_id: str) -> str:
        col = doc_id.split("/")[0] if "/" in doc_id else ""
        if (col, "") in col_type_map:
            return col_type_map[(col, "")][0]
        try:
            doc = db.document({"_id": doc_id})
        except Exception:
            return "Any"
        if not isinstance(doc, dict):
            return "Any"
        for ent_label, pm in entities_pm.items():
            tf = pm.get("typeField")
            tv = pm.get("typeValue")
            if tf and doc.get(tf) == tv and pm.get("collectionName") == col:
                return ent_label
        return "Any"

    from_labels: set[str] = set()
    to_labels: set[str] = set()
    for s in samples:
        from_labels.add(_resolve_label(s["f"]))
        to_labels.add(_resolve_label(s["t"]))

    domain = sorted(from_labels - {"Any"})[0] if (from_labels - {"Any"}) else "Any"
    range_ = sorted(to_labels - {"Any"})[0] if (to_labels - {"Any"}) else "Any"
    return (domain, range_)


def _infer_dedicated_edge_endpoints(
    db: StandardDatabase,
    edge_collection: str,
    entities_pm: dict[str, Any],
) -> tuple[str, str]:
    """Infer domain/range for a dedicated (PG-style) edge collection.

    Samples ``_from``/``_to`` document IDs, extracts their collection names,
    and maps those to entity labels via the physical mapping.
    """
    col_to_label: dict[str, str] = {}
    for label, pm in entities_pm.items():
        col = pm.get("collectionName", "")
        if col:
            col_to_label[col] = label

    try:
        cursor = db.aql.execute(
            "FOR e IN @@col LIMIT 20 RETURN {f: e._from, t: e._to}",
            bind_vars={"@col": edge_collection},
        )
        samples = list(sync(cursor))
    except Exception:
        return ("Any", "Any")

    if not samples:
        return ("Any", "Any")

    from_labels: set[str] = set()
    to_labels: set[str] = set()
    for s in samples:
        f_id = s.get("f", "")
        t_id = s.get("t", "")
        f_col = f_id.split("/")[0] if "/" in f_id else ""
        t_col = t_id.split("/")[0] if "/" in t_id else ""
        from_labels.add(col_to_label.get(f_col, "Any"))
        to_labels.add(col_to_label.get(t_col, "Any"))

    domain = sorted(from_labels - {"Any"})[0] if (from_labels - {"Any"}) else "Any"
    range_ = sorted(to_labels - {"Any"})[0] if (to_labels - {"Any"}) else "Any"
    return (domain, range_)


# Open-vocabulary GraphRAG graphs put every relationship in one shared,
# type-discriminated edge collection (e.g. `relations` keyed by a `type` field)
# and can have tens of thousands of distinct `type` values with a very heavy
# head. Materializing them all bloats the mapping, the NL prompt, and the schema
# graph, so we keep the top-K by edge volume (which covers the vast majority of
# edges) and summarize the rest. The transpiler still executes any type at query
# time (it is discriminator-based); only the *schema view* is capped.
DEFAULT_MAX_RELATIONSHIP_TYPES = 200


def _props_to_pm_props(props: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Module-level twin of the nested ``_props_to_pm`` in the heuristic builder:
    convert a sampled property list into physical-mapping property entries,
    preserving the data-quality hints and semantic ``role``."""
    out: dict[str, dict[str, Any]] = {}
    for p in props:
        name = p.get("name", "")
        if not name:
            continue
        entry: dict[str, Any] = {"field": p.get("field", name), "type": p.get("type", "string")}
        for k in ("sentinelValues", "numericLike", "sampleValues", "required", "role"):
            if k in p:
                entry[k] = p[k]
        out[name] = entry
    return out


def _shared_typed_edge_collections(bundle: MappingBundle) -> dict[str, str]:
    """Return ``{edgeCollection: typeField}`` for GENERIC_WITH_TYPE edge
    collections (relationship types discriminated by a field on one shared
    collection)."""
    pm = bundle.physical_mapping or {}
    rels = pm.get("relationships", {})
    if not isinstance(rels, dict):
        return {}
    out: dict[str, str] = {}
    for rmap in rels.values():
        if not isinstance(rmap, dict):
            continue
        coll = rmap.get("edgeCollectionName")
        tf = rmap.get("typeField")
        if coll and tf and rmap.get("style") == "GENERIC_WITH_TYPE":
            out[str(coll)] = str(tf)
    return out


def _aggregate_edge_endpoints(
    db: StandardDatabase,
    edge_collection: str,
    type_field: str,
    sample_limit: int,
) -> dict[str, tuple[Any, Any]]:
    """Return ``{typeValue: (fromType, toType)}`` dominant endpoint *type values*
    per relationship type, from a single sampled aggregation over the edges'
    ``_fromType``/``_toType`` discriminators. Returns ``{}`` when those fields
    are absent (caller then leaves endpoints unresolved rather than guessing)."""
    try:
        sample = next(
            sync(db.aql.execute("FOR e IN @@c LIMIT 1 RETURN e", bind_vars={"@c": edge_collection})),
            None,
        )
    except Exception:
        return {}
    if not isinstance(sample, dict) or "_fromType" not in sample or "_toType" not in sample:
        return {}
    try:
        rows = list(
            sync(
                db.aql.execute(
                    f"FOR e IN @@c LIMIT @lim "
                    f"COLLECT t = e.`{type_field}`, ft = e._fromType, tt = e._toType "
                    f"WITH COUNT INTO n RETURN {{t: t, ft: ft, tt: tt, n: n}}",
                    bind_vars=bind({"@c": edge_collection, "lim": sample_limit}),
                )
            )
        )
    except Exception as exc:  # noqa: BLE001
        logger.info("edge endpoint aggregation failed for %s: %s", edge_collection, exc)
        return {}
    best: dict[str, tuple[Any, Any, int]] = {}
    for r in rows:
        t = r.get("t")
        if t is None:
            continue
        key = str(t)
        n = int(r.get("n", 0) or 0)
        cur = best.get(key)
        if cur is None or n > cur[2]:
            best[key] = (r.get("ft"), r.get("tt"), n)
    return {t: (v[0], v[1]) for t, v in best.items()}


def _normalize_open_vocab_edges(
    db: StandardDatabase,
    bundle: MappingBundle,
    *,
    max_types: int = DEFAULT_MAX_RELATIONSHIP_TYPES,
    sample_limit: int = 1_000_000,
) -> MappingBundle:
    """Cap + correct relationships on shared, type-discriminated edge collections.

    For each GENERIC_WITH_TYPE edge collection, keep the top-K relationship types
    by edge volume (one COLLECT) and set each kept type's domain/range from the
    dominant ``_fromType``/``_toType`` (one sampled aggregation), mapped to the
    entity labels. Relationships on other collections (e.g. DEDICATED_COLLECTION)
    pass through untouched. No-ops on schemas without such a collection, so it is
    safe to run on every freshly-built bundle.
    """
    shared = _shared_typed_edge_collections(bundle)
    if not shared:
        return bundle

    pm = bundle.physical_mapping or {}
    cs = bundle.conceptual_schema or {}
    rels_pm = pm.get("relationships", {}) if isinstance(pm.get("relationships"), dict) else {}
    cs_rels = cs.get("relationships", []) if isinstance(cs.get("relationships"), list) else []

    # typeValue -> entity label (vertices share a type-discriminated collection).
    type_to_label: dict[str, str] = {}
    ents_pm = pm.get("entities", {}) if isinstance(pm.get("entities"), dict) else {}
    for label, emap in ents_pm.items():
        if isinstance(emap, dict) and emap.get("typeValue") is not None:
            type_to_label.setdefault(str(emap["typeValue"]), str(label))

    def _label_for(type_val: Any) -> str:
        if type_val is None:
            return "Any"
        return type_to_label.get(str(type_val), str(type_val))

    # Pass-through: relationships NOT on a normalized collection keep as-is.
    def _coll_of(rtype: str) -> str | None:
        rm = rels_pm.get(rtype)
        return rm.get("edgeCollectionName") if isinstance(rm, dict) else None

    new_pm: dict[str, Any] = {t: r for t, r in rels_pm.items() if _coll_of(t) not in shared}
    new_cs: list[Any] = [
        r for r in cs_rels if not (isinstance(r, dict) and _coll_of(str(r.get("type"))) in shared)
    ]
    cap_notes: list[dict[str, Any]] = []

    for coll, type_field in shared.items():
        try:
            top = list(
                sync(
                    db.aql.execute(
                        f"FOR e IN @@c COLLECT t = e.`{type_field}` WITH COUNT INTO n "
                        f"SORT n DESC LIMIT @k RETURN {{t: t, n: n}}",
                        bind_vars=bind({"@c": coll, "k": max_types}),
                    )
                )
            )
            total_types = next(
                sync(
                    db.aql.execute(
                        f"RETURN LENGTH(FOR e IN @@c COLLECT t = e.`{type_field}` RETURN 1)",
                        bind_vars={"@c": coll},
                    )
                ),
                0,
            )
        except Exception as exc:  # noqa: BLE001 - keep the originals on failure
            logger.warning("normalize edges: frequency query failed for %s: %s", coll, exc)
            for t, r in rels_pm.items():
                if _coll_of(t) == coll:
                    new_pm[t] = r
            for r in cs_rels:
                if isinstance(r, dict) and _coll_of(str(r.get("type"))) == coll:
                    new_cs.append(r)
            continue

        endpoints = _aggregate_edge_endpoints(db, coll, type_field, sample_limit)
        shared_props = _sample_properties(db, coll)
        shared_props_pm = _props_to_pm_props(shared_props)

        kept = 0
        for row in top:
            t = row.get("t")
            if t is None:
                continue
            t = str(t)
            edge_count = int(row.get("n", 0) or 0)
            ft, tt = endpoints.get(t, (None, None))
            domain, range_ = _label_for(ft), _label_for(tt)
            new_cs.append({"type": t, "fromEntity": domain, "toEntity": range_, "properties": shared_props})
            new_pm[t] = {
                "style": "GENERIC_WITH_TYPE",
                "edgeCollectionName": coll,
                "typeField": type_field,
                "typeValue": t,
                "domain": domain,
                "range": range_,
                # Edge volume for this type (from the top-K frequency pass) — drives
                # volume-weighted rendering / ranking in the schema graph.
                "edgeCount": edge_count,
                "properties": shared_props_pm,
            }
            kept += 1

        if isinstance(total_types, int) and total_types > kept:
            cap_notes.append({"edgeCollection": coll, "totalTypes": total_types, "keptTypes": kept})
            logger.info(
                "normalize edges: capped %s to top %d of %d relationship types",
                coll,
                kept,
                total_types,
            )

    new_conceptual = dict(cs)
    new_conceptual["relationships"] = new_cs
    new_physical = dict(pm)
    new_physical["relationships"] = new_pm
    new_meta = dict(bundle.metadata or {})
    if cap_notes:
        new_meta["relationshipTypeCaps"] = cap_notes
    return MappingBundle(
        conceptual_schema=new_conceptual,
        physical_mapping=new_physical,
        metadata=new_meta,
        owl_turtle=bundle.owl_turtle,
        source=bundle.source,
    )


def _build_heuristic_mapping(db: StandardDatabase, schema_type: str) -> MappingBundle:
    """Build a MappingBundle from heuristics for PG or LPG schemas."""
    db = hide_service_collections(db)
    try:
        all_cols = sync(db.collections())
    except Exception as exc:
        raise CoreError("Failed to list collections", code="INVALID_ARGUMENT") from exc

    doc_cols = []
    edge_cols = []
    for c in all_cols:
        if not isinstance(c, dict):
            continue
        name = c.get("name", "")
        if name.startswith("_"):
            continue
        if c.get("type") in (3, "edge"):
            edge_cols.append(name)
        else:
            doc_cols.append(name)

    entities_cs: list[dict[str, Any]] = []
    entities_pm: dict[str, Any] = {}
    relationships_cs: list[dict[str, Any]] = []
    relationships_pm: dict[str, Any] = {}
    heuristic_notes: dict[str, dict[str, Any]] = {}

    def _props_to_pm(props: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        """Convert property list to physical-mapping properties dict.

        Preserves data-quality hints (sentinelValues, numericLike, sampleValues)
        and the semantic ``role`` emitted by :func:`_sample_properties` so
        downstream layers (entity resolver, NL prompts, result rendering) can
        surface them.
        """
        out: dict[str, dict[str, Any]] = {}
        for p in props:
            name = p.get("name", "")
            if not name:
                continue
            entry: dict[str, Any] = {
                "field": p.get("field", name),
                "type": p.get("type", "string"),
            }
            for k in ("sentinelValues", "numericLike", "sampleValues", "required", "role"):
                if k in p:
                    entry[k] = p[k]
            out[name] = entry
        return out

    if schema_type == "pg":
        for col_name in doc_cols:
            label = _collection_label(col_name)
            props = _sample_properties(db, col_name)
            entities_cs.append({"name": label, "labels": [label], "properties": props})
            entities_pm[label] = {
                "style": "COLLECTION",
                "collectionName": col_name,
                "properties": _props_to_pm(props),
            }

        for col_name in edge_cols:
            rel_type = col_name.upper()
            props = _sample_properties(db, col_name)
            domain, range_ = _infer_dedicated_edge_endpoints(db, col_name, entities_pm)
            relationships_cs.append(
                {
                    "type": rel_type,
                    "fromEntity": domain,
                    "toEntity": range_,
                    "properties": props,
                }
            )
            relationships_pm[rel_type] = {
                "style": "DEDICATED_COLLECTION",
                "edgeCollectionName": col_name,
                "domain": domain,
                "range": range_,
                "properties": _props_to_pm(props),
            }
    elif schema_type in ("lpg", "hybrid"):
        for col_name in doc_cols:
            rejected: list[dict[str, Any]] = []
            type_field = _detect_type_field(db, col_name, notes_sink=rejected)
            if type_field:
                values = _type_field_values(db, col_name, type_field)
                for val in values:
                    label = _pascal_case(val)
                    props = _sample_properties_filtered(db, col_name, type_field, val)
                    entities_cs.append({"name": label, "labels": [label], "properties": props})
                    entities_pm[label] = {
                        "style": "LABEL",
                        "collectionName": col_name,
                        "typeField": type_field,
                        "typeValue": val,
                        "properties": _props_to_pm(props),
                    }
            else:
                label = _collection_label(col_name)
                props = _sample_properties(db, col_name)
                entities_cs.append({"name": label, "labels": [label], "properties": props})
                entities_pm[label] = {
                    "style": "COLLECTION",
                    "collectionName": col_name,
                    "properties": _props_to_pm(props),
                }
            if rejected or type_field is None:
                heuristic_notes[col_name] = {
                    "rejected_candidates": rejected,
                    "accepted_field": type_field,
                    "resolved_style": "LABEL" if type_field else "COLLECTION",
                }

        for col_name in edge_cols:
            detected_field = _detect_type_field(db, col_name, candidates=_EDGE_TYPE_FIELDS)

            if detected_field:
                values = _type_field_values(db, col_name, detected_field)
                for val in values:
                    domain, range_ = _infer_lpg_edge_endpoints(db, col_name, detected_field, val, entities_pm)
                    props = _sample_properties_filtered(db, col_name, detected_field, val)
                    relationships_cs.append(
                        {
                            "type": val,
                            "fromEntity": domain,
                            "toEntity": range_,
                            "properties": props,
                        }
                    )
                    relationships_pm[val] = {
                        "style": "GENERIC_WITH_TYPE",
                        "edgeCollectionName": col_name,
                        "typeField": detected_field,
                        "typeValue": val,
                        "properties": _props_to_pm(props),
                    }
            else:
                rel_type = col_name.upper()
                props = _sample_properties(db, col_name)
                domain, range_ = _infer_dedicated_edge_endpoints(db, col_name, entities_pm)
                relationships_cs.append(
                    {
                        "type": rel_type,
                        "fromEntity": domain,
                        "toEntity": range_,
                        "properties": props,
                    }
                )
                relationships_pm[rel_type] = {
                    "style": "DEDICATED_COLLECTION",
                    "edgeCollectionName": col_name,
                    "domain": domain,
                    "range": range_,
                    "properties": _props_to_pm(props),
                }

    _SKIP_INDEX_TYPES = {"primary", "edge"}
    col_indexes: dict[str, list[dict[str, Any]]] = {}
    for col_name in doc_cols + edge_cols:
        try:
            raw_indexes = sync(db.collection(col_name).indexes())
            filtered = []
            for idx in raw_indexes:
                if not isinstance(idx, dict):
                    continue
                idx_type = idx.get("type", "")
                if idx_type in _SKIP_INDEX_TYPES:
                    continue
                filtered.append(
                    {
                        "type": idx_type,
                        "fields": idx.get("fields", []),
                        "unique": idx.get("unique", False),
                        "sparse": idx.get("sparse", False),
                        "name": idx.get("name", ""),
                    }
                )
            if filtered:
                col_indexes[col_name] = filtered
        except Exception:
            pass

    for pm_entry in entities_pm.values():
        col = pm_entry.get("collectionName", "")
        if col in col_indexes:
            pm_entry["indexes"] = col_indexes[col]

    for pm_entry in relationships_pm.values():
        col = pm_entry.get("edgeCollectionName", "")
        if col in col_indexes:
            pm_entry["indexes"] = col_indexes[col]

    conceptual_schema = {
        "entities": entities_cs,
        "relationships": relationships_cs,
    }
    physical_mapping = {
        "entities": entities_pm,
        "relationships": relationships_pm,
    }

    metadata: dict[str, Any] = {"source": "heuristic", "schemaType": schema_type}
    if heuristic_notes:
        metadata["heuristic_notes"] = heuristic_notes

    return MappingBundle(
        conceptual_schema=conceptual_schema,
        physical_mapping=physical_mapping,
        metadata=metadata,
        source=MappingSource(kind="heuristic", notes=f"Built from {schema_type} heuristic classification"),
    )


def acquire_mapping_bundle(
    db: StandardDatabase, *, include_owl: bool = False, min_type_value_count: int | None = None
) -> MappingBundle:
    """Call arangodb-schema-analyzer to produce a MappingBundle from a live database.

    Uses AgenticSchemaAnalyzer with baseline inference (no LLM required).
    If arangodb-schema-analyzer is not installed, raises ImportError.

    ``min_type_value_count`` > 0 requests LPG full-label-set mode from the
    analyzer (keep every ``type`` value at/above that document count, not just
    the top-K), so an LPG's full label vocabulary reaches the resolver. ``None``
    falls back to ``ARANGO_CYPHER_MIN_TYPE_VALUE_COUNT`` (0 = analyzer default).
    """
    try:
        from schema_analyzer import AgenticSchemaAnalyzer, export_mapping
        from schema_analyzer.owl_export import export_conceptual_model_as_owl_turtle
    except ImportError:
        raise ImportError(
            "arangodb-schema-analyzer is not installed. "
            "Install it with: pip install 'arango-cypher-py[analyzer]' — the "
            "canonical pin, so this guidance cannot drift from pyproject.toml."
        ) from None

    analyzer = AgenticSchemaAnalyzer()
    # The analyzer skips only system collections; the transpilers' own cache
    # and examples collections would otherwise become classes (issue #19).
    db = hide_service_collections(db)
    floor = min_type_value_count if min_type_value_count is not None else _default_min_type_value_count()
    if floor and floor > 0:
        # Only pass the kwarg when opted in, so older analyzer builds without the
        # parameter keep working when full-label-set mode is off.
        analysis_result = analyzer.analyze_physical_schema(db, min_type_value_count=floor)
    else:
        analysis_result = analyzer.analyze_physical_schema(db)

    analysis_dict = {
        "conceptualSchema": analysis_result.conceptual_schema,
        "physicalMapping": analysis_result.physical_mapping,
        "metadata": analysis_result.metadata.model_dump(by_alias=True),
    }

    export = export_mapping(analysis_dict, target="cypher")

    pm = export.get("physicalMapping", {})

    owl_turtle: str | None = None
    if include_owl:
        owl_turtle = export_conceptual_model_as_owl_turtle(analysis_dict)

    metadata = export.get("metadata", {})
    # Named-graph membership (analyzer transpiler-integration contract): the
    # analyze response carries ``metadata.graphMembership`` but the CSI export we
    # consume here does not — it only carries the per-entry ``graphs`` tags. Lift
    # the summary from the raw analysis metadata when present, otherwise
    # reconstruct it from the per-entry tags, so downstream graph scoping can
    # resolve a named graph's collections from the bundle alone (no live graph
    # lookup). Older, pre-tagging analyzer builds yield an empty summary, which
    # we omit so the bundle is correctly treated as not graph-aware.
    graph_membership = (
        metadata.get("graphMembership")
        or (analysis_dict.get("metadata") or {}).get("graphMembership")
        or _reconstruct_graph_membership(pm)
    )
    if graph_membership:
        metadata = {**metadata, "graphMembership": graph_membership}
    if metadata.get("warnings"):
        metadata = {**metadata, "warnings": normalize_warnings(metadata["warnings"])}

    bundle = MappingBundle(
        conceptual_schema=export.get("conceptualSchema", {}),
        physical_mapping=pm,
        metadata=metadata,
        owl_turtle=owl_turtle,
        source=MappingSource(
            kind="schema_analyzer_export",
            notes="Generated by arangodb-schema-analyzer (baseline)",
        ),
    )

    # Surface upstream reconciliation (issue #4 / PR-3): the analyzer's
    # reconcile pass may backfill collections the LLM missed. When the
    # LLM-path is used (not the baseline), emit an observability warning so
    # we retain visibility we used to get from running the backfill here.
    recon = bundle.metadata.get("reconciliation") if bundle.metadata else None
    if isinstance(recon, dict):
        backfilled = recon.get("backfilled_collections") or recon.get("backfilledCollections")
        if backfilled:
            logger.warning(
                "schema_analyzer backfilled %d collection(s) missing from the LLM mapping: %s",
                len(backfilled),
                sorted(backfilled) if isinstance(backfilled, list | tuple | set) else backfilled,
            )

    # Surface upstream sharding profile (arangodb-schema-analyzer v0.5 /
    # upstream PRD §6.2 bullet 3): the analyzer classifies every database
    # into exactly one deployment style and emits the evidence as
    # metadata.shardingProfile. Layer-5 (EXPLAIN-plan validator, see
    # docs/multitenant_prd.md §7) reads `style` once at session start.
    # We log it at INFO for deployment observability and escalate to
    # WARNING when the classifier reports `status == "degraded"` (i.e. it
    # fell back to a default because required evidence was missing).
    sharding = bundle.metadata.get("shardingProfile") if bundle.metadata else None
    if isinstance(sharding, dict):
        style = sharding.get("style")
        status = (
            sharding.get("status")
            or sharding.get("shardingProfileStatus")
            or bundle.metadata.get("shardingProfileStatus")
        )
        if status == "degraded":
            logger.warning(
                "schema_analyzer shardingProfile is degraded (style=%s): "
                "evidence was incomplete, downstream layers will assume "
                "the default and may enforce stricter guardrails.",
                style,
            )
        else:
            logger.info("schema_analyzer shardingProfile: style=%s", style)

    # Surface upstream multitenancy classification (arangodb-schema-analyzer
    # v0.6 / upstream PRD §6.2 bullet 4): the analyzer classifies every
    # database into exactly one of seven multitenancy styles and emits the
    # evidence as metadata.multitenancy. Layer-4 (tenant_scope discovery)
    # already consumes ``tenantKey`` for field-name detection; we log
    # ``style`` + ``physicalEnforcement`` here for deployment observability,
    # escalating to WARNING when the classifier reports degraded evidence.
    multitenancy = bundle.metadata.get("multitenancy") if bundle.metadata else None
    if isinstance(multitenancy, dict):
        mt_style = multitenancy.get("style")
        enforcement = multitenancy.get("physicalEnforcement")
        mt_status = multitenancy.get("status") or bundle.metadata.get("multitenancyStatus")
        if mt_status == "degraded":
            logger.warning(
                "schema_analyzer multitenancy is degraded (style=%s, "
                "physicalEnforcement=%s): downstream tenant guardrail "
                "will fall back to its local heuristic.",
                mt_style,
                enforcement,
            )
        elif mt_style and mt_style != "none":
            logger.info(
                "schema_analyzer multitenancy: style=%s physicalEnforcement=%s",
                mt_style,
                enforcement,
            )

    return bundle


# NOTE (PR-3, 2026-04-20): `_backfill_missing_collections` (~160 LOC) and
# `_fixup_dedicated_edges` (~80 LOC) used to live here. Both closed
# schema-analyzer capability gaps (issues #3 and #4) that shipped upstream
# in arangodb-schema-analyzer v0.2.0 and are now invariants of the
# `AgenticSchemaAnalyzer.analyze_physical_schema` pipeline:
#
#   - Multi-type edge detection → `GENERIC_WITH_TYPE` splits: handled by
#     upstream `analyzer._prepare_analysis` + `export_mapping`.
#   - Collection reconciliation / backfill for LLM omissions: handled by
#     upstream `schema_analyzer.reconcile.reconcile_physical_mapping`; its
#     summary surfaces in `metadata.reconciliation` (consumed above in
#     `acquire_mapping_bundle` to emit a warning when backfilling occurred).
#
# The golden-diff gate (`scripts/pr3_workaround_diff.py`) confirmed that
# upstream output is byte-identical with vs. without these post-processors
# across every fixture DB (movies_pg, movies_lpg, cypher_{pg,lpg,hybrid},
# northwind_test), so deleting them is a safe no-op on the output contract.


def compute_statistics(
    db: StandardDatabase,
    bundle: MappingBundle,
) -> dict[str, Any]:
    """Compute cardinality statistics for the physical model described by *bundle*.

    Returns a dict suitable for storing in ``MappingBundle.metadata["statistics"]``.
    Uses fast AQL ``LENGTH()`` for collection counts and derives per-relationship
    fan-out/fan-in metrics.
    """
    import datetime

    pm = bundle.physical_mapping
    cs = bundle.conceptual_schema
    entities = pm.get("entities", {}) if isinstance(pm.get("entities"), dict) else {}
    rels = pm.get("relationships", {}) if isinstance(pm.get("relationships"), dict) else {}

    cs_rels = cs.get("relationships", []) if isinstance(cs.get("relationships"), list) else []
    # Each endpoint is a label or, for a polymorphic relationship, a list of them.
    cs_rel_lookup: dict[str, tuple[Any, Any]] = {}
    for cr in cs_rels:
        if isinstance(cr, dict):
            cs_rel_lookup[cr.get("type", "")] = (
                cr.get("fromEntity", ""),
                cr.get("toEntity", ""),
            )

    col_counts: dict[str, dict[str, Any]] = {}
    entity_counts: dict[str, dict[str, Any]] = {}
    rel_stats: dict[str, dict[str, Any]] = {}

    seen_collections: set[str] = set()

    for label, emap in entities.items():
        if not isinstance(emap, dict):
            continue
        col_name = emap.get("collectionName", label)
        # AQL identifier safety: collection names land in this function from
        # whatever JSON the caller (UI, schema-analyzer export, hand-edited
        # mapping) supplied, so we re-validate against the ArangoDB
        # collection-name grammar before splicing them into an AQL string.
        # Invalid names short-circuit to count=0 — the same outcome the
        # except: branch produced before, just without the round-trip to
        # the DB and the resulting noisy server-side parse error.
        col_name_safe = is_valid_collection_name(col_name)
        if col_name not in seen_collections:
            count = 0
            if col_name_safe:
                try:
                    cursor = db.aql.execute(f"RETURN LENGTH(`{col_name}`)")
                    count = next(sync(cursor), 0)
                except Exception:
                    count = 0
            col_counts[col_name] = {"count": count, "is_edge": False}
            seen_collections.add(col_name)

        style = emap.get("style", "COLLECTION")
        type_field = emap.get("typeField")
        type_value = emap.get("typeValue")
        if style == "UNION":
            continue  # its members' counts, summed below
        if col_name_safe and style == "MULTI_LABEL" and emap.get("labelsField"):
            # A supertype in a shared collection: the documents whose labels array lists it.
            try:
                cursor = db.aql.execute(
                    f"FOR d IN `{col_name}` FILTER @lv IN d[@lf] COLLECT WITH COUNT INTO c RETURN c",
                    bind_vars={"lv": emap.get("labelValue") or label, "lf": emap["labelsField"]},
                )
                entity_counts[label] = {"estimated_count": next(sync(cursor), 0)}
            except Exception:
                entity_counts[label] = {"estimated_count": col_counts.get(col_name, {}).get("count", 0)}
            continue
        if col_name_safe and style in ("LABEL", "GENERIC_WITH_TYPE") and type_field and type_value:
            try:
                aql = (
                    f"FOR d IN `{col_name}` FILTER d.`{type_field}` == @tv COLLECT WITH COUNT INTO c RETURN c"
                )
                cursor = db.aql.execute(aql, bind_vars={"tv": type_value})
                entity_count = next(sync(cursor), 0)
            except Exception:
                entity_count = col_counts.get(col_name, {}).get("count", 0)
        else:
            entity_count = col_counts.get(col_name, {}).get("count", 0)

        entity_counts[label] = {"estimated_count": entity_count}

    # A UNION supertype has no collection of its own: it is every document of its members.
    for label, emap in entities.items():
        if isinstance(emap, dict) and emap.get("style") == "UNION":
            try:
                members = MappingResolver(bundle).supertype_members(label)
            except CoreError:
                members = []
            entity_counts[label] = {
                "estimated_count": sum(entity_counts.get(m, {}).get("estimated_count", 0) for m in members)
            }

    for rtype, rmap in rels.items():
        if not isinstance(rmap, dict):
            continue
        edge_col = rmap.get("edgeCollectionName", rtype)
        if not edge_col:
            continue

        # Same validation contract as the entity loop above — see comment there.
        edge_col_safe = is_valid_collection_name(edge_col)
        if edge_col not in seen_collections:
            edge_count = 0
            if edge_col_safe:
                try:
                    cursor = db.aql.execute(f"RETURN LENGTH(`{edge_col}`)")
                    edge_count = next(sync(cursor), 0)
                except Exception:
                    edge_count = 0
            col_counts[edge_col] = {"count": edge_count, "is_edge": True}
            seen_collections.add(edge_col)

        style = rmap.get("style", "DEDICATED_COLLECTION")
        type_field = rmap.get("typeField")
        type_value = rmap.get("typeValue")

        if edge_col_safe and style == "GENERIC_WITH_TYPE" and type_field and type_value:
            try:
                aql = (
                    f"FOR e IN `{edge_col}` FILTER e.`{type_field}` == @tv COLLECT WITH COUNT INTO c RETURN c"
                )
                cursor = db.aql.execute(aql, bind_vars={"tv": type_value})
                edge_count = next(sync(cursor), 0)
            except Exception:
                edge_count = col_counts.get(edge_col, {}).get("count", 0)
        else:
            edge_count = col_counts.get(edge_col, {}).get("count", 0)

        domain_label = rmap.get("domain", "") or ""
        range_label = rmap.get("range", "") or ""
        if (not domain_label or not range_label) and rtype in cs_rel_lookup:
            cs_from, cs_to = cs_rel_lookup[rtype]
            if not domain_label:
                domain_label = cs_from
            if not range_label:
                range_label = cs_to
        source_count = _endpoint_count(domain_label, entity_counts)
        target_count = _endpoint_count(range_label, entity_counts)

        avg_out = (edge_count / source_count) if source_count > 0 else 0.0
        avg_in = (edge_count / target_count) if target_count > 0 else 0.0

        if source_count > 0 and target_count > 0:
            selectivity = edge_count / (source_count * target_count)
        else:
            selectivity = 1.0

        pattern = _classify_cardinality(avg_out, avg_in)

        rel_stats[rtype] = {
            "edge_count": edge_count,
            "source_count": source_count,
            "target_count": target_count,
            "avg_out_degree": round(avg_out, 2),
            "avg_in_degree": round(avg_in, 2),
            "cardinality_pattern": pattern,
            "selectivity": round(selectivity, 6),
        }

    return {
        "computed_at": datetime.datetime.now(datetime.UTC).isoformat(),
        "collections": col_counts,
        "entities": entity_counts,
        "relationships": rel_stats,
    }


def _endpoint_count(endpoint: Any, entity_counts: dict[str, dict[str, Any]]) -> int:
    """The documents a relationship endpoint stands for: the sum over its labels.

    A polymorphic relationship lists several (``domain: ["Post", "Comment"]``);
    looking that list up as a dict key raised ``TypeError`` and lost the
    statistics for the whole bundle.
    """
    labels = [endpoint] if isinstance(endpoint, str) else endpoint if isinstance(endpoint, list) else []
    distinct = dict.fromkeys(lab for lab in labels if isinstance(lab, str) and lab)
    return sum(int(entity_counts.get(lab, {}).get("estimated_count", 0)) for lab in distinct)


def _classify_cardinality(avg_out: float, avg_in: float) -> str:
    """Classify a relationship as 1:1, 1:N, N:1, or N:M based on average degrees."""
    out_is_one = avg_out <= 1.5
    in_is_one = avg_in <= 1.5
    if out_is_one and in_is_one:
        return "1:1"
    if not out_is_one and in_is_one:
        return "1:N"
    if out_is_one and not in_is_one:
        return "N:1"
    return "N:M"


def enrich_bundle_with_statistics(
    db: StandardDatabase,
    bundle: MappingBundle,
) -> MappingBundle:
    """Return a new MappingBundle with cardinality statistics in metadata."""
    stats = compute_statistics(db, bundle)
    new_meta = {**bundle.metadata, "statistics": stats}
    return MappingBundle(
        conceptual_schema=bundle.conceptual_schema,
        physical_mapping=bundle.physical_mapping,
        metadata=new_meta,
        owl_turtle=bundle.owl_turtle,
        source=bundle.source,
    )


def _fresh_cached_bundle(
    db: StandardDatabase,
    key: str,
    persistent: ArangoSchemaCache | None,
    ttl_seconds: int,
) -> MappingBundle | None:
    """Return a recently-validated cached bundle without re-fingerprinting.

    The TTL fast-path: if a cache entry (in-memory first, then persistent) is
    younger than ``ttl_seconds`` and is not a degraded heuristic bundle that
    should be rebuilt, trust it directly. This skips the ~per-collection
    live-database fingerprint walk that otherwise runs on every
    :func:`get_mapping` call and dominates latency on remote clusters.

    Returns ``None`` when there is no entry, the entry is too old (or of unknown
    age), or the entry is a heuristic fallback that can now be upgraded — in all
    of which cases the caller falls back to the authoritative fingerprint check.

    ``ttl_seconds == 0`` disables the fast-path entirely (always returns
    ``None``), restoring the pre-TTL fingerprint-on-every-call behaviour.
    """
    if ttl_seconds <= 0 or not key:
        return None

    now = time.time()
    mem = _mapping_cache.get(key)
    if mem is not None:
        bundle, ts, _shape_fp, _full_fp = mem
        if (now - ts) < ttl_seconds and not _bundle_needs_reacquire(bundle):
            logger.debug("Schema cache fast-path (in-memory) for %s; age %.1fs", key, now - ts)
            return bundle

    if persistent is None:
        return None
    hit = persistent.get_with_age(db)
    if hit is None:
        return None
    bundle, shape_fp, full_fp, age = hit
    if age is None or age >= ttl_seconds:
        return None
    if _bundle_needs_reacquire(bundle):
        return None
    # Hydrate the in-memory tier so the next call in this process skips the
    # persistent round-trip too. Use the persisted timestamp's age to seed a
    # consistent expiry rather than resetting the clock on every read.
    _mapping_cache[key] = (bundle, now - age, shape_fp, full_fp)
    logger.debug("Schema cache fast-path (persistent) for %s; age %.1fs", key, age)
    return bundle


def _read_cached_slot(
    db: StandardDatabase,
    mem_key: str,
    cache_collection: str | None,
    persistent_cache_key: str,
) -> MappingBundle | None:
    """Read one cache slot: in-memory tier first, then the persistent tier.

    Returns the cached bundle, or ``None`` on a miss. On a persistent-tier hit
    the in-memory tier is hydrated so subsequent reads in this process skip the
    persistent round-trip. Never builds or fingerprints — pure read.
    """
    if not mem_key:
        return None
    mem = _mapping_cache.get(mem_key)
    if mem is not None:
        return mem[0]
    if not cache_collection:
        return None
    persistent = ArangoSchemaCache(collection_name=cache_collection, cache_key=persistent_cache_key)
    hit = persistent.get(db)
    if hit is None:
        return None
    bundle, shape_fp, full_fp = hit
    _mapping_cache[mem_key] = (bundle, time.time(), shape_fp, full_fp)
    return bundle


def read_cached_mapping(
    db: StandardDatabase,
    *,
    cache_collection: str | None = DEFAULT_CACHE_COLLECTION,
    cache_key: str = DEFAULT_CACHE_KEY,
    graph_name: str | None = None,
) -> MappingBundle | None:
    """Read a mapping from the catalog (cache) only — never build or fingerprint.

    This is the request-path accessor for the catalog architecture: schema
    analysis is performed out of band by the sidecar
    (:mod:`arango_cypher.catalog`), which writes the analyzed bundle to the
    shared persistent cache. The service serves whatever the catalog holds, with
    freshness owned by the sidecar's schedule — so a user request never triggers
    the expensive analyzer/fingerprint work that made introspection block for
    tens of seconds.

    Returns the cached :class:`MappingBundle` (in-memory tier first, then the
    persistent cache), or ``None`` on a true miss (the database has not been
    analyzed yet). Callers translate ``None`` into a "schema pending" response
    and may kick off an out-of-band warm.

    Unlike :func:`get_mapping`, this never computes a fingerprint, never calls
    the analyzer, and never writes a freshly built bundle — it is read-only and
    cheap (worst case: one persistent-cache document read).

    Graph scoping (PRD §17) is satisfied without a separately-warmed scoped
    cache slot: a scoped mapping is just a *filtered view* of the full-DB
    mapping, so on a scoped miss we derive it from the cached unscoped bundle by
    intersecting with the named graph's collections (a cheap metadata read + an
    in-memory filter, no analyzer). This means selecting a named graph in the UI
    works the moment the database has been analyzed once — the sidecar only has
    to warm the unscoped view per database, not every (database, graph) pair.
    """
    base_key = _cache_key(db)
    key = _graph_scoped_cache_key(base_key, graph_name)
    if not key:
        return None

    persistent_key = _graph_scoped_cache_key(cache_key, graph_name)
    bundle = _read_cached_slot(db, key, cache_collection, persistent_key)
    if bundle is not None:
        return bundle

    # Scoped miss: derive the scope from the cached full-DB mapping instead of
    # reporting "pending". We deliberately do not hydrate the derived bundle so
    # it always reflects the current cached full mapping (no stale scoped slot
    # to invalidate when the full mapping is refreshed).
    if graph_name:
        full = _read_cached_slot(db, base_key, cache_collection, cache_key)
        if full is not None:
            try:
                return _scope_bundle_to_graph(db, full, graph_name)
            except CoreError:
                # Unknown graph (live fallback could not resolve it): treat as a
                # miss so the caller reports "pending" rather than 500-ing.
                return None
    return None


def get_mapping(
    db: StandardDatabase,
    *,
    strategy: str = "auto",
    include_owl: bool = False,
    cache_collection: str | None = DEFAULT_CACHE_COLLECTION,
    cache_key: str = DEFAULT_CACHE_KEY,
    force_refresh: bool = False,
    graph_name: str | None = None,
    revalidate: bool = False,
) -> MappingBundle:
    """3-tier mapping acquisition with two-tier caching.

    ``revalidate=True`` skips only the TTL fast path: the live fingerprints are
    always compared with the cached schema's, and the schema is re-analyzed
    only if they differ (a background recheck; ``force_refresh`` rebuilds
    unconditionally).

    Strategies
    ----------
    ``strategy="auto"`` (default): analyzer first (all schema types: PG, LPG,
    hybrid); heuristic fallback if the analyzer is not installed.

    ``strategy="analyzer"``: always call ``acquire_mapping_bundle()`` (raises
    if the analyzer is not installed).

    ``strategy="heuristic"``: never call the analyzer; build a best-effort
    mapping from ``classify_schema`` + heuristics.

    Caching
    -------
    Two fingerprints drive the cache decisions:

    - A *shape* fingerprint (collections + types + index digests). When it
      matches, the cached conceptual + physical mapping is reused.
    - A *full* fingerprint (shape + row counts). When it matches, cached
      cardinality statistics are reused too; when only it differs,
      statistics are recomputed on top of the cached mapping (the
      "stats-only refresh" fast path).

    Caches are layered:

    1. Process-local ``dict`` for same-session hits.
    2. Optional persistent ArangoDB collection (``cache_collection``) for
       cross-restart and cross-instance sharing. Pass ``cache_collection=None``
       to disable persistence (e.g. for read-only DB users).

    ``force_refresh=True`` bypasses both caches and rebuilds from scratch.
    """
    if strategy not in ("auto", "analyzer", "heuristic"):
        raise CoreError(
            f"Invalid strategy: {strategy!r}. Must be 'auto', 'analyzer', or 'heuristic'.",
            code="INVALID_ARGUMENT",
        )

    # Graph scoping (PRD §17): each named graph — and the unscoped "all
    # collections" view — gets an independent cache slot in both tiers so a
    # scoped bundle never aliases the full-DB bundle.
    key = _graph_scoped_cache_key(_cache_key(db), graph_name)
    effective_cache_key = _graph_scoped_cache_key(cache_key, graph_name)
    persistent = (
        ArangoSchemaCache(collection_name=cache_collection, cache_key=effective_cache_key)
        if cache_collection
        else None
    )

    # TTL fast-path: a recently-validated cache entry is trusted without the
    # expensive live-database fingerprint walk (the dominant cost on remote
    # clusters). force_refresh skips this so "Refresh schema" always rebuilds.
    if not force_refresh and not revalidate:
        fresh = _fresh_cached_bundle(db, key, persistent, CACHE_TTL_SECONDS)
        if fresh is not None:
            return fresh

    # Slow path (cache empty, stale, or past its TTL): compute the authoritative
    # fingerprints and validate / rebuild against them.
    _fp_exclude = cache_collection or DEFAULT_CACHE_COLLECTION
    shape_fp = _shape_fingerprint(db, cache_collection=_fp_exclude)
    full_fp = _full_fingerprint(db, cache_collection=_fp_exclude)

    if not force_refresh and key:
        cached = _lookup_cache(db, key, persistent)
        if cached is not None:
            bundle, cached_shape, cached_full = cached
            if cached_shape == shape_fp:
                if _bundle_needs_reacquire(bundle):
                    # The cached bundle was built by the heuristic fallback
                    # because the analyzer was unavailable. Now that the
                    # analyzer is importable again, the cached entry is
                    # degraded — drop it and rebuild so the next operator
                    # does not inherit a known-bad mapping.
                    logger.info(
                        "Cached mapping for %s carries ANALYZER_NOT_INSTALLED "
                        "and analyzer is now available; rebuilding",
                        key,
                    )
                    _mapping_cache.pop(key, None)
                elif cached_full == full_fp:
                    logger.debug("Schema unchanged for %s; using cached mapping", key)
                    return bundle
                else:
                    logger.info(
                        "Schema shape stable for %s; refreshing cardinality statistics only",
                        key,
                    )
                    bundle = _safe_refresh_statistics(db, bundle)
                    _save_cache(db, key, bundle, shape_fp, full_fp, persistent)
                    return bundle
            else:
                logger.info("Schema shape changed for %s; full re-introspection", key)

    bundle = _build_fresh_bundle(
        db,
        strategy=strategy,
        include_owl=include_owl,
        graph_name=graph_name,
        cache_collection=cache_collection,
    )
    bundle = _safe_refresh_statistics(db, bundle)
    if key:
        _save_cache(db, key, bundle, shape_fp, full_fp, persistent)
    return bundle


def _build_fresh_bundle(
    db: StandardDatabase,
    *,
    strategy: str,
    include_owl: bool,
    graph_name: str | None = None,
    cache_collection: str | None = None,
) -> MappingBundle:
    """Run the chosen acquisition strategy and attach OWL Turtle if requested.

    The analysis sees the database without the service collections (this
    service's cache as configured, see :mod:`arango_cypher.service_collections`).

    When ``graph_name`` is set the freshly-built (whole-database) bundle is
    filtered down to that named graph's collection membership before statistics
    are computed, so the scoped view never carries non-member collections
    (PRD §17.4).
    """
    db = hide_service_collections(db, cache_collection)
    if strategy == "analyzer":
        bundle = acquire_mapping_bundle(db, include_owl=include_owl)
    elif strategy == "heuristic":
        schema_type = classify_schema(db)
        bundle = _build_heuristic_mapping(
            db,
            schema_type if schema_type in ("pg", "lpg", "hybrid") else "lpg",
        )
    else:
        try:
            bundle = acquire_mapping_bundle(db, include_owl=include_owl)
        except ImportError:
            global _heuristic_fallback_counter
            _heuristic_fallback_counter += 1
            logger.warning(
                "Heuristic schema path used — install arangodb-schema-analyzer "
                "for accurate mappings on hybrid schemas.",
            )
            schema_type = classify_schema(db)
            bundle = _build_heuristic_mapping(
                db,
                schema_type if schema_type in ("pg", "lpg", "hybrid") else "lpg",
            )
            bundle = _attach_warning(
                bundle,
                code="ANALYZER_NOT_INSTALLED",
                message=(
                    "arangodb-schema-analyzer is not installed; the mapping "
                    "was built by the heuristic fallback and may misclassify "
                    "hybrid schemas."
                ),
                install_hint="pip install arangodb-schema-analyzer",
            )

    # Normalize open-vocabulary, type-discriminated edge collections regardless
    # of which builder ran: cap to the top relationship types by edge volume and
    # set correct per-type domain/range from the edges' _fromType/_toType. Safe
    # no-op on schemas without such a collection.
    try:
        bundle = _normalize_open_vocab_edges(db, bundle)
    except Exception:  # noqa: BLE001 - normalization is best-effort, never fatal
        logger.warning("open-vocab edge normalization failed; using raw mapping", exc_info=True)

    if include_owl and not bundle.owl_turtle:
        try:
            from arango_query_core.owl_turtle import mapping_to_turtle

            owl_turtle = mapping_to_turtle(bundle)
            bundle = MappingBundle(
                conceptual_schema=bundle.conceptual_schema,
                physical_mapping=bundle.physical_mapping,
                metadata=bundle.metadata,
                owl_turtle=owl_turtle,
                source=bundle.source,
            )
        except Exception:
            logger.warning(
                "Failed to generate OWL Turtle for heuristic mapping",
                exc_info=True,
            )

    if graph_name:
        bundle = _scope_bundle_to_graph(db, bundle, graph_name)
        pm = bundle.physical_mapping or {}
        logger.info(
            "Scoped mapping to named graph %r: %d entit(y/ies), %d relationship(s)",
            graph_name,
            len(pm.get("entities") or {}),
            len(pm.get("relationships") or {}),
        )

    return bundle


def _safe_refresh_statistics(db: StandardDatabase, bundle: MappingBundle) -> MappingBundle:
    """Re-compute cardinality statistics without failing the caller.

    Statistics are a best-effort metadata enrichment: a failure here (e.g.
    permission denied on a typed edge COLLECT) must not prevent the caller
    from getting their mapping back.

    PR-3 (2026-04-20) short-circuit: when the analyzer has already populated
    ``metadata.statistics`` with an ``ok`` status (issue #2 / upstream
    ``schema_analyzer.statistics.compute_statistics`` shipped in v0.2.0),
    the upstream block is byte-identical to what the local
    :func:`compute_statistics` would produce and we skip the duplicate pass.
    The local implementation is retained as the fallback for (a) the
    heuristic tier whose bundles do not carry upstream stats, (b) the
    stats-only refresh path on a cached bundle after row counts drift,
    and (c) defensive rebuilds if upstream reports ``partial`` /
    ``skipped_no_db``.
    """
    meta = bundle.metadata or {}
    existing = meta.get("statistics")
    status = meta.get("statisticsStatus") or meta.get("statistics_status")
    if isinstance(existing, dict) and existing.get("relationships") and status == "ok":
        logger.debug("Using analyzer-supplied metadata.statistics; skipping local recompute")
        return bundle

    try:
        return enrich_bundle_with_statistics(db, bundle)
    except Exception:
        logger.warning("Failed to compute cardinality statistics", exc_info=True)
        return bundle


def _lookup_cache(
    db: StandardDatabase,
    key: str,
    persistent: ArangoSchemaCache | None,
) -> tuple[MappingBundle, str, str] | None:
    """Check the in-memory cache first, then the persistent cache.

    Hydrates the in-memory cache from the persistent cache on hit so the
    next call in this process skips the DB roundtrip.
    """
    mem = _mapping_cache.get(key)
    if mem is not None:
        bundle, _ts, shape_fp, full_fp = mem
        return bundle, shape_fp, full_fp
    if persistent is None:
        return None
    hit = persistent.get(db)
    if hit is None:
        return None
    bundle, shape_fp, full_fp = hit
    _mapping_cache[key] = (bundle, time.time(), shape_fp, full_fp)
    return bundle, shape_fp, full_fp


def _save_cache(
    db: StandardDatabase,
    key: str,
    bundle: MappingBundle,
    shape_fp: str,
    full_fp: str,
    persistent: ArangoSchemaCache | None,
) -> None:
    """Write to both cache tiers. Persistent failure is non-fatal."""
    _mapping_cache[key] = (bundle, time.time(), shape_fp, full_fp)
    if persistent is not None:
        persistent.set(
            db,
            bundle=bundle,
            shape_fingerprint=shape_fp,
            full_fingerprint=full_fp,
        )


def invalidate_cache(
    db: StandardDatabase,
    *,
    cache_collection: str | None = DEFAULT_CACHE_COLLECTION,
    cache_key: str = DEFAULT_CACHE_KEY,
    graph_name: str | None = None,
) -> None:
    """Drop both in-memory and persistent caches for this database.

    Use after a manual schema migration or when you want the next
    ``get_mapping()`` call to re-introspect unconditionally. ``graph_name``
    scopes the invalidation to a single named-graph cache slot (PRD §17);
    omit it to drop the unscoped "all collections" slot.
    """
    key = _graph_scoped_cache_key(_cache_key(db), graph_name)
    if key:
        _mapping_cache.pop(key, None)
    if cache_collection:
        effective_cache_key = _graph_scoped_cache_key(cache_key, graph_name)
        ArangoSchemaCache(collection_name=cache_collection, cache_key=effective_cache_key).invalidate(db)
