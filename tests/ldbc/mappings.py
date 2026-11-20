"""Generate the ``ldbc_pg`` / ``ldbc_lpg`` mapping fixtures (WP-32).

The fixtures are the contract for what the loader builds (``loader.py``), so
they are generated from the same sources — :mod:`tests.ldbc.schema` for labels,
properties and endpoints, :func:`loader.pg_indexes` / :func:`loader.lpg_indexes`
for indexes — rather than written by hand. ``test_ldbc_mappings_unit.py`` fails
if a committed fixture differs from what this module builds.

Regenerate with::

    .venv/bin/python -m tests.ldbc.mappings --write

**Supertypes** (``Message``, ``Place``, ``Organisation``) are mapped with the
arango-query-core supertype styles (PRD §8.4, docs/ldbc_benchmark_plan.md D8,
WP-35), staged by layout:

* **LPG** (stage 1): ``MULTI_LABEL`` — every node's ``labels`` array lists its
  supertype, so ``(m:Message)`` is ``"Message" IN m.labels``.
* **PG** (stage 2): ``UNION`` — the members keep their own collections, so a
  scan of ``(m:Message)`` unions ``post`` and ``comment`` and a traversal target
  tests membership of either.

Polymorphic relationships declare union ``domain``/``range`` lists of concrete
labels, which the resolver supports.
"""

from __future__ import annotations

import argparse
import copy
import dataclasses
import json
import sys
from pathlib import Path
from typing import Any

from arango_query_core import MappingBundle

from tests.ldbc.loader import (
    LABELS_FIELD,
    LPG_EDGES,
    LPG_NODES,
    TYPE_FIELD,
    IndexSpec,
    Layout,
    lpg_indexes,
    pg_collection,
    pg_edge_collection,
    pg_indexes,
)
from tests.ldbc.schema import (
    CONCRETE_LABELS,
    EDGE_FILES,
    NODE_FILES,
    REL_TYPES,
    SUPERTYPES,
    Column,
    rel_domain,
    rel_range,
)

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "mappings"

#: Column kind → mapping property type (the vocabulary the OWL round-trip uses).
_PROP_TYPE = {
    "id": "number",
    "int": "number",
    "str": "string",
    "str_list": "array",
    "datetime": "datetime",
    "date": "date",
}


#: A data set's encoding → the mapping's ``encoding`` vocabulary.
_ENCODING = {"epoch_ms": "epoch_ms", "iso": "iso8601"}


def with_encoding(bundle: MappingBundle, encoding: str) -> MappingBundle:
    """*bundle* with every date and datetime property declaring how the data
    set stores it (``PropertyInfo.encoding``).

    One fixture serves both data sets (Interactive is epoch-ms, BI is ISO), so
    the encoding is added for the data set a query runs on.
    """
    physical = copy.deepcopy(bundle.physical_mapping)
    for section in ("entities", "relationships"):
        for spec in physical.get(section, {}).values():
            for prop in (spec.get("properties") or {}).values():
                if isinstance(prop, dict) and prop.get("type") in ("datetime", "date"):
                    prop["encoding"] = _ENCODING[encoding]
    return dataclasses.replace(bundle, physical_mapping=physical)


#: Statistics per database name: they cost one count per collection and type.
_STATISTICS: dict[str, dict[str, Any]] = {}


def with_statistics(bundle: MappingBundle, db: Any) -> MappingBundle:
    """*bundle* with the cardinality statistics of *db*, the database it runs on.

    A service's mapping carries them (from the schema analyzer, or
    ``schema_acquire.compute_statistics``); the fixtures do not, since the
    numbers belong to a data set. The translator reads them for choices whose
    cost depends on fan-out (WP-44), so the harness measures and checks the AQL
    a service would send.
    """
    from arango_cypher.schema_acquire import compute_statistics

    stats = _STATISTICS.get(db.name)
    if stats is None:
        stats = _STATISTICS[db.name] = compute_statistics(db, bundle)
    return dataclasses.replace(bundle, metadata={**bundle.metadata, "statistics": stats})


def fixture_path(layout: Layout) -> Path:
    return FIXTURE_DIR / f"ldbc_{layout}.export.json"


def _label_properties(label: str) -> list[Column]:
    for nf in NODE_FILES:
        if nf.label == label or (nf.label is None and label in _MEMBERS[nf.name]):
            return [c for c in nf.columns if c.kind in _PROP_TYPE]
    raise ValueError(f"no node file holds label {label!r}")


_MEMBERS = {"Place": ("City", "Country", "Continent"), "Organisation": ("University", "Company")}


def _rel_properties(rel_type: str) -> list[Column]:
    cols: dict[str, Column] = {}
    for ef in EDGE_FILES:
        if ef.rel_type == rel_type:
            for c in ef.columns:
                if c.kind in _PROP_TYPE:
                    cols.setdefault(c.name, c)
    return list(cols.values())


def _index_json(spec: IndexSpec, *, vci: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {
        "type": "persistent",
        "fields": list(spec.fields),
        "unique": spec.unique,
        "name": spec.name,
    }
    if vci:
        out["vci"] = True
    return out


def _physical_props(cols: list[Column], indexed: set[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for c in cols:
        prop: dict[str, Any] = {"field": c.name, "type": _PROP_TYPE[c.kind]}
        if c.name in indexed:
            prop["indexed"] = True
        out[c.name] = prop
    return out


def _endpoint(labels: list[str]) -> str | list[str]:
    return labels[0] if len(labels) == 1 else labels


def build(layout: Layout) -> dict[str, Any]:
    """The mapping export for *layout*, in the analyzer's ``export`` contract shape."""
    if layout not in ("pg", "lpg"):
        raise ValueError(f"unknown layout {layout!r}")

    specs = pg_indexes() if layout == "pg" else lpg_indexes()
    entities_cs: list[dict[str, Any]] = []
    entities_pm: dict[str, Any] = {}
    for label in CONCRETE_LABELS:
        cols = _label_properties(label)
        entities_cs.append(
            {
                "name": label,
                "labels": [label],
                "properties": [{"name": c.name, "type": _PROP_TYPE[c.kind]} for c in cols],
            }
        )
        if layout == "pg":
            coll = pg_collection(label)
            mine = [s for s in specs if s.collection == coll]
            indexed = {s.fields[0] for s in mine}
            entities_pm[label] = {
                "collectionName": coll,
                "style": "COLLECTION",
                "properties": _physical_props(cols, indexed),
                "indexes": [_index_json(s) for s in mine],
            }
        else:
            node_specs = [s for s in specs if s.collection == LPG_NODES]
            indexed = {f for s in node_specs for f in s.fields if f != TYPE_FIELD}
            entities_pm[label] = {
                "collectionName": LPG_NODES,
                "style": "LABEL",
                "typeField": TYPE_FIELD,
                "typeValue": label,
                "properties": _physical_props(cols, indexed),
                "indexes": [_index_json(s) for s in node_specs],
            }

    label_specs = [s for s in specs if s.collection == LPG_NODES and s.fields[0] == f"{LABELS_FIELD}[*]"]
    for sup, members in SUPERTYPES.items():
        cols: dict[str, Column] = {}
        for member in members:
            for c in _label_properties(member):
                cols.setdefault(c.name, c)
        entities_cs.append(
            {
                "name": sup,
                "labels": [sup],
                "subtypes": list(members),
                "properties": [{"name": c.name, "type": _PROP_TYPE[c.kind]} for c in cols.values()],
            }
        )
        if layout == "pg":
            # The members' collections carry the indexes; the supertype has none of its own.
            entities_pm[sup] = {"style": "UNION", "members": list(members)}
        else:
            entities_pm[sup] = {
                "collectionName": LPG_NODES,
                "style": "MULTI_LABEL",
                "labelsField": LABELS_FIELD,
                "labelValue": sup,
                "properties": _physical_props(list(cols.values()), set()),
                "indexes": [_index_json(s) for s in label_specs],
            }

    rels_cs: list[dict[str, Any]] = []
    rels_pm: dict[str, Any] = {}
    for rel_type in REL_TYPES:
        cols = _rel_properties(rel_type)
        domain, range_ = _endpoint(rel_domain(rel_type)), _endpoint(rel_range(rel_type))
        rels_cs.append(
            {
                "type": rel_type,
                "fromEntity": domain,
                "toEntity": range_,
                "properties": [{"name": c.name, "type": _PROP_TYPE[c.kind]} for c in cols],
            }
        )
        rel: dict[str, Any] = {"domain": domain, "range": range_, "properties": _physical_props(cols, set())}
        if layout == "pg":
            rel = {"edgeCollectionName": pg_edge_collection(rel_type), "style": "DEDICATED_COLLECTION", **rel}
        else:
            edge_specs = [s for s in specs if s.collection == LPG_EDGES]
            rel = {
                "edgeCollectionName": LPG_EDGES,
                "style": "GENERIC_WITH_TYPE",
                "typeField": TYPE_FIELD,
                "typeValue": rel_type,
                **rel,
                "indexes": [_index_json(s, vci=True) for s in edge_specs],
            }
        rels_pm[rel_type] = rel

    return {
        "conceptualSchema": {"entities": entities_cs, "relationships": rels_cs, "properties": []},
        "metadata": {
            "provider": "fixture",
            "model": None,
            "timestamp": "2026-10-24T00:00:00Z",
            "confidence": 1.0,
            "reviewRequired": False,
            "warnings": [
                "Generated by tests/ldbc/mappings.py. LDBC supertypes (Message, Place, Organisation) "
                + (
                    "are MULTI_LABEL over the nodes' labels array."
                    if layout == "lpg"
                    else "are UNION over their members' collections (PRD §8.4)."
                )
            ],
        },
        "physicalMapping": {"entities": entities_pm, "relationships": rels_pm},
    }


def render(layout: Layout) -> str:
    return json.dumps(build(layout), indent=2, sort_keys=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--write", action="store_true", help="write the fixtures instead of checking them")
    args = ap.parse_args(argv)
    stale = []
    for layout in ("pg", "lpg"):
        path = fixture_path(layout)  # type: ignore[arg-type]
        text = render(layout)  # type: ignore[arg-type]
        if args.write:
            path.write_text(text, encoding="utf-8")
            print(f"wrote {path}")
        elif not path.exists() or path.read_text(encoding="utf-8") != text:
            stale.append(str(path))
    if stale:
        print("stale LDBC mapping fixtures (run with --write):\n  " + "\n  ".join(stale), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
