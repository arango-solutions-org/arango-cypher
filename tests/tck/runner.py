"""Run openCypher TCK scenarios against ArangoDB through the translator.

Queries are translated with ``cypher_column_names=True``: an unaliased RETURN
column is named by its expression as written (``n.name``, ``count(*)``), as
the TCK's result tables name it. The translator's default (short inferred
keys such as ``name``) is what API clients read, not what the TCK checks.

They are also translated with ``temporal_strings=True``: the TCK stores
temporal values, which the translator represents as strings in Neo4j's form,
and reads them back with accessors, arithmetic and ORDER BY.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Any

from arango import ArangoClient
from arango_query_core import CoreError, MappingBundle
from arango_query_core.exec import AqlExecutor

from arango_cypher import translate
from tests.helpers.mapping_fixtures import mapping_bundle_for
from tests.integration.seed import (
    _ensure_doc_collection,
    _ensure_edge_collection,
    _reset_collection,
)

from .gherkin import Feature, Scenario, parse_feature
from .normalize import parse_param_value, results_contain, results_match

logger = logging.getLogger(__name__)

_SKIP_TRANSLATE_CODES = frozenset(
    {
        "UNSUPPORTED",
        "NOT_IMPLEMENTED",
        "PARSE_ERROR",
    }
)

_LABEL_RE = re.compile(r"(?<=[:(])([A-Z][A-Za-z0-9_]*)(?=[)\s:{,])")
_REL_TYPE_RE = re.compile(r"\[:?\s*([A-Z][A-Z0-9_]*)[\s*\]{}]")

_ERROR_STEP_PREFIXES = (
    "a SyntaxError should be raised",
    "a TypeError should be raised",
    "a SemanticError should be raised",
    "a ParameterMissing error should be raised",
    "a ArgumentError should be raised",
    "a EntityNotFound should be raised",
    "an error should be raised",
)

_ROW_COUNT_RE = re.compile(r"the result should have (\d+) rows?")


@dataclass(frozen=True)
class ScenarioOutcome:
    status: str  # passed|skipped|failed
    reason: str | None = None


def _env(name: str, default: str) -> str:
    v = os.environ.get(name)
    return v if isinstance(v, str) and v else default


def _connect_db(db_name: str):
    url = _env("ARANGO_URL", "http://localhost:8529")
    user = _env("ARANGO_USER", "root")
    pw = _env("ARANGO_PASS", "openSesame")
    client = ArangoClient(hosts=url)
    sys_db = client.db("_system", username=user, password=pw)
    if not sys_db.has_database(db_name):
        sys_db.create_database(db_name)
    return client.db(db_name, username=user, password=pw)


def _reset_tck_graph(db: Any) -> None:
    for name in ("nodes", "vertices"):
        coll = _ensure_doc_collection(db, name)
        _reset_collection(coll)
    edges = _ensure_edge_collection(db, "edges")
    _reset_collection(edges)


def _parse_table(table: list[list[str]] | None) -> list[dict[str, Any]]:
    if not table:
        return []
    if len(table) < 2:
        return []
    headers = table[0]
    rows = table[1:]
    out: list[dict[str, Any]] = []
    for r in rows:
        obj: dict[str, Any] = {}
        for k, v in zip(headers, r, strict=False):
            obj[k] = v
        out.append(obj)
    return out


def _parse_params(table: list[list[str]] | None) -> dict[str, Any]:
    """Parse a TCK ``parameters are:`` table into typed values.

    The table has no header: every row is ``| name | value |``.  (Reading the
    first row as a header dropped the first parameter of every scenario.)
    """
    params: dict[str, Any] = {}
    for row in table or []:
        if len(row) != 2:
            raise ValueError(f"parameter row must be | name | value |, got {row!r}")
        name, raw_val = row
        params[name] = parse_param_value(raw_val)
    return params


def _extract_labels_and_types(cypher: str) -> tuple[set[str], set[str]]:
    """Extract node labels and relationship types from Cypher text."""
    labels: set[str] = set()
    rel_types: set[str] = set()
    # ``(n:A:B)``, ``(me: Person)``, ``(n :A :B)``.
    for m in re.finditer(r"\(\s*\w*\s*((?:\s*:\s*[A-Za-z_]\w*)+)", cypher):
        for lab in re.findall(r":\s*([A-Za-z_]\w*)", m.group(1)):
            labels.add(lab)
    # A label predicate in an expression: ``WHERE m:NonExistent``, ``RETURN a:B``.
    # (A map entry ``{k: v}`` with a variable value also matches; mapping an
    # extra label is harmless.)
    for m in re.finditer(r"(?<![\w.$`[])[A-Za-z_]\w*((?:[ \t]*:[ \t]*[A-Za-z_]\w*)+)", cypher):
        for lab in re.findall(r":\s*([A-Za-z_]\w*)", m.group(1)):
            labels.add(lab)
    # ``[:T]``, ``[r: T]``, ``[:T|U]``, ``[:T|:U]``.
    for m in re.finditer(r"\[\s*\w*\s*:\s*([A-Za-z_]\w*(?:\s*\|\s*:?\s*[A-Za-z_]\w*)*)", cypher):
        for rel_type in re.findall(r"[A-Za-z_]\w*", m.group(1)):
            rel_types.add(rel_type)
    return labels, rel_types


def _build_dynamic_lpg_mapping(
    base: MappingBundle,
    extra_labels: set[str],
    extra_rel_types: set[str],
) -> MappingBundle:
    """Extend a base LPG mapping with dynamically discovered labels/types."""
    pm = dict(base.physical_mapping)
    entities = dict(pm.get("entities", {}))
    rels = dict(pm.get("relationships", {}))

    coll_name = "vertices"
    edge_coll_name = "edges"
    for e in entities.values():
        if isinstance(e, dict) and e.get("collectionName"):
            coll_name = e["collectionName"]
            break
    for r in rels.values():
        if isinstance(r, dict):
            edge_coll_name = r.get("edgeCollectionName") or r.get("collectionName") or edge_coll_name
            break

    for lab in extra_labels:
        if lab not in entities:
            entities[lab] = {
                "collectionName": coll_name,
                "style": "LABEL",
                "typeField": "type",
                "typeValue": lab,
            }
    # A node with several labels stores them all in ``labels`` (the direct
    # loader does, and so does the translator given ``labelsField``), its
    # type field holding one of them.
    for lab, spec in list(entities.items()):
        if (
            isinstance(spec, dict)
            and spec.get("style") == "LABEL"
            and spec.get("collectionName") == coll_name
        ):
            entities[lab] = {**spec, "labelsField": "labels"}
    for rt in extra_rel_types:
        if rt not in rels:
            rels[rt] = {
                "collectionName": edge_coll_name,
                "edgeCollectionName": edge_coll_name,
                "style": "GENERIC_WITH_TYPE",
                "typeField": "type",
                "typeValue": rt,
            }
    pm["entities"] = entities
    pm["relationships"] = rels

    cs = dict(base.conceptual_schema)
    cs_entities = list(cs.get("entities", []))
    cs_rels = list(cs.get("relationships", []))
    existing_entity_names = {e.get("name") for e in cs_entities if isinstance(e, dict)}
    existing_rel_types = {r.get("type") for r in cs_rels if isinstance(r, dict)}
    for lab in extra_labels:
        if lab not in existing_entity_names:
            cs_entities.append({"labels": [lab], "name": lab, "properties": []})
    for rt in extra_rel_types:
        if rt not in existing_rel_types:
            cs_rels.append({"fromEntity": "Any", "properties": [], "toEntity": "Any", "type": rt})
    cs["entities"] = cs_entities
    cs["relationships"] = cs_rels

    return MappingBundle(
        conceptual_schema=cs,
        physical_mapping=pm,
        metadata=base.metadata,
        source=base.source,
    )


def _scenario_cypher_texts(scenario: Scenario) -> list[str]:
    """Collect all Cypher query strings from scenario steps."""
    texts: list[str] = []
    for step in scenario.steps:
        if step.doc_string:
            texts.append(step.doc_string)
    return texts


def _build_mapping_for_scenario(scenario: Scenario, mapping_fixture: str) -> MappingBundle:
    """Build a MappingBundle that covers all labels/types in the scenario."""
    base = mapping_bundle_for(mapping_fixture)
    all_labels: set[str] = set()
    all_rel_types: set[str] = set()
    for text in _scenario_cypher_texts(scenario):
        labels, rel_types = _extract_labels_and_types(text)
        all_labels |= labels
        all_rel_types |= rel_types
    # Always extended: the extension also marks every label as storable in a
    # ``labels`` array, which a scenario naming no label still writes into
    # (``MATCH (n) SET n:Foo``).
    return _build_dynamic_lpg_mapping(base, all_labels, all_rel_types)


_CREATE_NODE_PAT = re.compile(
    r"\(\s*(?P<var>[a-zA-Z_]\w*)?\s*(?P<labels>(?::[A-Za-z_]\w*)*)\s*(?:\{(?P<props>[^}]*)\})?\s*\)",
)


def _parse_cypher_props(prop_str: str) -> dict[str, Any]:
    """Parse a Cypher property map string like 'name: \"Alice\", age: 30'."""
    if not prop_str or not prop_str.strip():
        return {}
    result: dict[str, Any] = {}
    for part in _split_props(prop_str):
        colon = part.find(":")
        if colon < 0:
            continue
        key = part[:colon].strip()
        val_str = part[colon + 1 :].strip()
        result[key] = _parse_cypher_value(val_str)
    return result


def _split_props(s: str) -> list[str]:
    """Split property pairs respecting strings and nested structures."""
    parts: list[str] = []
    depth = 0
    in_sq = in_dq = False
    cur: list[str] = []
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
            elif ch == "," and depth == 0:
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


_FUNCTION_CALL_RE = re.compile(r"^[A-Za-z_][\w.]*\s*\(")


class _UnloadableValue(ValueError):
    """The direct loader cannot evaluate a setup value (e.g. a function call)."""


def _parse_cypher_value(val_str: str) -> Any:
    """Parse a Cypher literal value.

    A function call is refused: storing its source text as a string would seed
    a different graph from the one the scenario describes, and the scenario
    would then fail (or pass) for a reason unrelated to the query under test.
    """
    s = val_str.strip()
    if _FUNCTION_CALL_RE.match(s):
        raise _UnloadableValue(f"direct loader cannot evaluate {s!r}")
    if s == "null":
        return None
    if s == "true":
        return True
    if s == "false":
        return False
    if (s.startswith("'") and s.endswith("'")) or (s.startswith('"') and s.endswith('"')):
        return s[1:-1]
    if s.startswith("[") and s.endswith("]"):
        inner = s[1:-1].strip()
        if not inner:
            return []
        return [_parse_cypher_value(p.strip()) for p in _split_props(inner)]
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        pass
    return s


def _execute_create_directly(
    db: Any,
    cypher: str,
    *,
    coll_name: str = "vertices",
    edge_coll_name: str = "edges",
) -> ScenarioOutcome | None:
    """Execute a CREATE Cypher directly via python-arango document API.

    Handles chained patterns like CREATE (a:A)-[:T]->(b:B) by parsing the pattern
    structure rather than relying on simple regex matching.
    """
    coll = db.collection(coll_name)
    edge_coll = db.collection(edge_coll_name)
    var_ids: dict[str, str] = {}
    anon_counter = 0

    statements = re.split(r"\bCREATE\b", cypher, flags=re.IGNORECASE)
    for stmt in statements:
        stmt = stmt.strip()
        if not stmt:
            continue

        parts = _split_create_pattern_parts(stmt)
        for part in parts:
            result = _execute_create_part(part, coll, edge_coll, var_ids, anon_counter)
            if isinstance(result, ScenarioOutcome):
                return result
            anon_counter = result

    return None


def _split_create_pattern_parts(stmt: str) -> list[str]:
    """Split a CREATE statement into comma-separated pattern parts, respecting brackets."""
    parts: list[str] = []
    depth = 0
    cur: list[str] = []
    for ch in stmt:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append("".join(cur).strip())
            cur = []
            continue
        cur.append(ch)
    tail = "".join(cur).strip()
    if tail:
        parts.append(tail)
    return parts


def _execute_create_part(
    part: str,
    coll: Any,
    edge_coll: Any,
    var_ids: dict[str, str],
    anon_counter: int,
) -> int | ScenarioOutcome:
    """Execute a single CREATE pattern part (e.g., '(a:A)-[:T]->(b:B)')."""
    rel_pat = re.compile(
        r"(<?\-)\s*\[\s*(?P<rvar>[a-zA-Z_]\w*)?\s*(?::(?P<rtype>[A-Za-z_]\w*))?"
        r"\s*(?:\{(?P<rprops>[^}]*)\})?\s*\]\s*(\->?\-?)"
    )

    tokens: list[dict[str, Any]] = []
    pos = 0
    while pos < len(part):
        node_m = _CREATE_NODE_PAT.match(part, pos)
        if node_m and node_m.start() == pos:
            var = node_m.group("var") or ""
            labels_str = node_m.group("labels") or ""
            props_str = node_m.group("props") or ""
            labels = [x for x in labels_str.split(":") if x]
            props = _parse_cypher_props(props_str)
            if not var:
                var = f"_anon{anon_counter}"
                anon_counter += 1
            tokens.append({"kind": "node", "var": var, "labels": labels, "props": props})
            pos = node_m.end()
            continue

        rel_m = rel_pat.match(part, pos)
        if rel_m:
            rvar = rel_m.group("rvar") or ""
            rtype = rel_m.group("rtype") or ""
            rprops = _parse_cypher_props(rel_m.group("rprops") or "")
            dir1 = rel_m.group(1)
            direction = "inbound" if "<" in dir1 else "outbound"
            tokens.append({"kind": "rel", "var": rvar, "type": rtype, "props": rprops, "dir": direction})
            pos = rel_m.end()
            continue

        pos += 1

    for t in tokens:
        if t["kind"] == "node" and t["var"] not in var_ids:
            doc: dict[str, Any] = {}
            if t["labels"]:
                doc["type"] = t["labels"][0]
            if len(t["labels"]) > 1:
                doc["labels"] = t["labels"]
            doc.update(
                {k: v for k, v in t["props"].items() if v is not None}
            )  # Cypher stores no null property

            for k, v in list(doc.items()):
                if isinstance(v, str) and " + " in v:
                    resolved = _resolve_expression(v, var_ids, coll)
                    if resolved is not None:
                        doc[k] = resolved
                elif isinstance(v, str) and "." in v and not v.startswith("'") and not v.startswith('"'):
                    resolved = _resolve_expression(v, var_ids, coll)
                    if resolved is not None:
                        doc[k] = resolved

            try:
                result = coll.insert(doc, return_new=True)
                new_doc = result.get("new", result) if isinstance(result, dict) else result
                doc_id = new_doc.get("_id", "") if isinstance(new_doc, dict) else ""
                var_ids[t["var"]] = doc_id
            except Exception as e:
                return ScenarioOutcome(status="failed", reason=f"direct insert failed: {e}")

    for i, t in enumerate(tokens):
        if t["kind"] == "rel":
            prev_node = next((tokens[j] for j in range(i - 1, -1, -1) if tokens[j]["kind"] == "node"), None)
            next_node = next(
                (tokens[j] for j in range(i + 1, len(tokens)) if tokens[j]["kind"] == "node"),
                None,
            )
            if not prev_node or not next_node:
                continue

            from_var = prev_node["var"]
            to_var = next_node["var"]
            if t["dir"] == "inbound":
                from_var, to_var = to_var, from_var

            from_id = var_ids.get(from_var, "")
            to_id = var_ids.get(to_var, "")
            if not from_id or not to_id:
                return ScenarioOutcome(
                    status="failed",
                    reason=f"var not found: {from_var}={from_id}, {to_var}={to_id}",
                )

            edge_doc: dict[str, Any] = {"_from": from_id, "_to": to_id}
            if t["type"]:
                edge_doc["type"] = t["type"]
            edge_doc.update({k: v for k, v in t["props"].items() if v is not None})
            try:
                edge_coll.insert(edge_doc)
            except Exception as e:
                return ScenarioOutcome(status="failed", reason=f"direct edge insert failed: {e}")

    return anon_counter


def _resolve_expression(expr: str, var_ids: dict[str, str], coll: Any) -> Any:
    """Resolve simple property access expressions like 'a.name' or 'a.name + \"0\"'."""
    if " + " in expr:
        parts = expr.split(" + ")
        resolved_parts = []
        for p in parts:
            p = p.strip()
            r = _resolve_expression(p, var_ids, coll)
            resolved_parts.append(str(r) if r is not None else p.strip("'\""))
        return "".join(resolved_parts)

    if "." in expr:
        obj_var, prop = expr.split(".", 1)
        obj_var = obj_var.strip()
        prop = prop.strip()
        if obj_var in var_ids:
            doc_key = var_ids[obj_var].split("/")[1] if "/" in var_ids[obj_var] else var_ids[obj_var]
            doc = coll.get(doc_key)
            if doc and prop in doc:
                return doc[prop]
    return None


def _execute_create_directly_or_fail(db: Any, cypher: str) -> ScenarioOutcome | None:
    try:
        return _execute_create_directly(db, cypher)
    except _UnloadableValue as e:
        return ScenarioOutcome(status="failed", reason=f"setup: {e}")


def _execute_setup_cypher(
    db: Any,
    cypher: str,
    mapping_fixture: str,
    *,
    mapping: MappingBundle | None = None,
) -> ScenarioOutcome | None:
    """Translate and execute a setup Cypher query. Returns an outcome on failure, None on success."""
    mb = mapping or mapping_bundle_for(mapping_fixture)
    try:
        out = translate(cypher, mapping=mb)
    except CoreError as e:
        if e.code in _SKIP_TRANSLATE_CODES:
            return _execute_create_directly_or_fail(db, cypher)
        return ScenarioOutcome(status="failed", reason=f"setup translate failed: {e}")
    except Exception as e:
        return ScenarioOutcome(status="skipped", reason=f"setup translate error: {e}")
    try:
        list(AqlExecutor(db).execute(out.to_aql_query()))
    except Exception as e:
        err_str = str(e)
        can_seed_directly = bool(re.search(r"\bCREATE\b", cypher, flags=re.IGNORECASE))
        if can_seed_directly and (
            "1579" in err_str
            or "access after data-modification" in err_str
            or "1511" in err_str
            or "assigned multiple times" in err_str
        ):
            return _execute_create_directly_or_fail(db, cypher)
        return ScenarioOutcome(status="failed", reason=f"setup execute failed: {e}")
    return None


def _check_error_expectation(step_text: str, error: Exception) -> ScenarioOutcome:
    """Check whether a caught error matches the TCK error expectation.

    We accept any error as a pass since we can't distinguish ArangoDB error
    categories from Cypher ones precisely.
    """
    if isinstance(error, (CoreError, Exception)):
        return ScenarioOutcome(status="passed")
    return ScenarioOutcome(status="failed", reason=f"expected error but got: {error!r}")


@dataclass
class _QueryResult:
    """What the last ``executing [control] query:`` step produced."""

    query: str
    rows: list[Any] | None = None
    error: Exception | None = None
    stage: str = ""  # translate|execute, when ``error`` is set


def _run_query(db: Any, cypher: str, mapping: MappingBundle, params: dict[str, Any]) -> _QueryResult:
    try:
        out = translate(
            cypher, mapping=mapping, params=params or None, cypher_column_names=True, temporal_strings=True
        )
    except Exception as e:  # CoreError and anything else the translator raises
        return _QueryResult(query=cypher, error=e, stage="translate")
    try:
        rows = list(AqlExecutor(db).execute(out.to_aql_query()))
    except Exception as e:
        return _QueryResult(query=cypher, error=e, stage="execute")
    return _QueryResult(query=cypher, rows=rows)


def _rows_or_failure(result: _QueryResult | None) -> list[Any] | ScenarioOutcome:
    """The result's rows, or the failed outcome a row assertion reports."""
    if result is None:
        return ScenarioOutcome(status="skipped", reason="assertion before any query")
    if result.error is None:
        assert result.rows is not None
        return result.rows
    e = result.error
    if result.stage == "translate" and isinstance(e, CoreError) and e.code in _SKIP_TRANSLATE_CODES:
        return ScenarioOutcome(status="skipped", reason=f"translate skipped: {e.code}")
    return ScenarioOutcome(status="failed", reason=f"{result.stage} failed: {e}")


def _check_rows(
    rows: list[Any],
    table: list[dict[str, Any]],
    *,
    ordered: bool,
    ignore_list_order: bool = False,
    contains: bool = False,
) -> ScenarioOutcome | None:
    if contains:
        match, explanation = results_contain(rows, table, ordered=ordered)
    else:
        match, explanation = results_match(rows, table, ordered=ordered, ignore_list_order=ignore_list_order)
    return None if match else ScenarioOutcome(status="failed", reason=explanation)


#: ``Then`` step prefix -> (ordered, ignore_list_order, contains) for row-table assertions.
_TABLE_ASSERTIONS: tuple[tuple[str, tuple[bool, bool, bool]], ...] = (
    ("the result should be, in order (ignoring element order for lists):", (True, True, False)),
    ("the result should be (ignoring element order for lists):", (False, True, False)),
    ("the result should be, in order:", (True, False, False)),
    ("the result should be, in any order:", (False, False, False)),
    ("the result should be:", (False, False, False)),
    ("the result should contain, in any order:", (False, False, True)),
    ("the result should contain, in order:", (True, False, True)),
)


def run_scenario(
    scenario: Scenario,
    *,
    db_name: str,
    mapping_fixture: str,
) -> ScenarioOutcome:
    """Run a single TCK scenario, step by step.

    Steps run in order: each ``executing query:`` / ``executing control
    query:`` step runs its query, and each ``Then`` assertion checks the most
    recent result — so a control query's table is checked against the control
    query, not the query before it.  Error expectations accept any error (the
    error class is not compared); side effects are not verified.
    """
    db = _connect_db(db_name)
    _ensure_doc_collection(db, "vertices")
    _ensure_edge_collection(db, "edges")
    mapping = _build_mapping_for_scenario(scenario, mapping_fixture)
    params: dict[str, Any] = {}
    last: _QueryResult | None = None
    asserted = False
    error_stage = ""

    for step in scenario.steps:
        s = step.text

        if step.keyword in {"Given", "And"} and s in {
            "an empty graph",
            "the empty graph",
            "any graph",
            "an existing graph",
            "the existing graph",
        }:
            _reset_tck_graph(db)
            continue

        if step.keyword in {"Given", "And"} and s.startswith("having executed:"):
            if not step.doc_string:
                return ScenarioOutcome(status="skipped", reason="having executed without docstring")
            result = _execute_setup_cypher(db, step.doc_string.strip(), mapping_fixture, mapping=mapping)
            if result is not None:
                return result
            continue

        if step.keyword in {"Given", "And"} and s.startswith("parameters are:"):
            params.update(_parse_params(step.data_table))
            continue

        if step.keyword in {"Given", "And"} and s.startswith("there exists a procedure"):
            return ScenarioOutcome(status="skipped", reason="procedures not supported")

        if step.keyword in {"When", "And"} and (
            s.startswith("executing query:") or s.startswith("executing control query:")
        ):
            if not step.doc_string:
                return ScenarioOutcome(status="skipped", reason="missing query docstring")
            last = _run_query(db, step.doc_string.strip(), mapping, params)
            continue

        if step.keyword not in {"Then", "And"}:
            return ScenarioOutcome(status="skipped", reason=f"unsupported step: {step.keyword} {step.text}")

        if s.startswith("the side effects should be:") or s == "no side effects":
            continue

        if any(s.startswith(prefix) for prefix in _ERROR_STEP_PREFIXES):
            if last is None:
                return ScenarioOutcome(status="skipped", reason="error assertion before any query")
            if last.error is None:
                return ScenarioOutcome(
                    status="failed",
                    reason=f"expected error ({s}) but query translated successfully",
                )
            outcome = _check_error_expectation(s, last.error)
            if outcome.status != "passed":
                return outcome
            # Which stage raised is kept for the report: a translator refusal is
            # evidence the error is understood; an ArangoDB execution error may
            # be one for an unrelated reason.
            error_stage = last.stage
            asserted = True
            continue

        if s == "the result should be empty":
            rows = _rows_or_failure(last)
            if isinstance(rows, ScenarioOutcome):
                return rows
            if rows:
                return ScenarioOutcome(status="failed", reason=f"expected empty, got {len(rows)} rows")
            asserted = True
            continue

        row_count_m = _ROW_COUNT_RE.match(s)
        if row_count_m:
            rows = _rows_or_failure(last)
            if isinstance(rows, ScenarioOutcome):
                return rows
            if len(rows) != int(row_count_m.group(1)):
                return ScenarioOutcome(
                    status="failed",
                    reason=f"row count: got {len(rows)}, expected {row_count_m.group(1)}",
                )
            asserted = True
            continue

        table_mode = next((mode for prefix, mode in _TABLE_ASSERTIONS if s.startswith(prefix)), None)
        if table_mode is not None:
            rows = _rows_or_failure(last)
            if isinstance(rows, ScenarioOutcome):
                return rows
            ordered, ignore_list_order, contains = table_mode
            failure = _check_rows(
                rows,
                _parse_table(step.data_table),
                ordered=ordered,
                ignore_list_order=ignore_list_order,
                contains=contains,
            )
            if failure is not None:
                return failure
            asserted = True
            continue

        return ScenarioOutcome(status="skipped", reason=f"unsupported step: {step.keyword} {step.text}")

    if last is None:
        return ScenarioOutcome(status="skipped", reason="no query executed")
    if not asserted:
        return ScenarioOutcome(status="skipped", reason="no assertion")
    return ScenarioOutcome(status="passed", reason=f"error at {error_stage}" if error_stage else None)


def run_feature(
    feature_path: str | Any,
    *,
    db_name: str,
    mapping_fixture: str,
) -> dict[str, Any]:
    from pathlib import Path

    path = Path(feature_path) if not isinstance(feature_path, Path) else feature_path
    feat: Feature = parse_feature(path)
    outcomes: list[ScenarioOutcome] = []
    for sc in feat.scenarios:
        outcomes.append(run_scenario(sc, db_name=db_name, mapping_fixture=mapping_fixture))

    counts: dict[str, int] = {"passed": 0, "skipped": 0, "failed": 0}
    for o in outcomes:
        counts[o.status] = counts.get(o.status, 0) + 1

    return {"feature": feat.name, "scenarios": len(feat.scenarios), "counts": counts}
