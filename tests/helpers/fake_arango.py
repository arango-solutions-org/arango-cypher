"""python-arango test doubles shared by the query-mining tests.

Signatures mirror the real ones — ``AQL.execute(query, count, batch_size,
..., bind_vars, ..., max_runtime)``, ``AQL.explain(query, all_plans,
max_plans, opt_rules, bind_vars)``, ``Cursor.close(ignore_missing)``,
``StandardCollection.all(skip, limit)`` — and errors are real
``ArangoServerError`` subclasses populated the way
``BaseConnection.prep_response`` populates them, so a driver change breaks the
tests instead of passing them.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from typing import Any

from arango.request import Request
from arango.response import Response


def server_error(cls: type, code: int, num: int, message: str) -> Exception:
    """A real python-arango server error, populated as BaseConnection.prep_response does."""
    body = {"error": True, "code": code, "errorNum": num, "errorMessage": message}
    resp = Response("post", "http://db/_api/cursor", {}, code, "error", json.dumps(body))
    resp.body = body
    resp.error_code = num
    resp.error_message = message
    resp.is_success = False
    return cls(resp, Request("post", "/_api/cursor"))


class FakeCursor:
    """Mirrors ``arango.cursor.Cursor``: an iterator (``__iter__`` returns
    itself, ``__next__``/``next()`` hand out the rows), so ``next(cursor)``
    works as it does on a real cursor."""

    def __init__(self, rows: list[Any]):
        self._rows = rows
        self._it = iter(rows)
        self.closed = False

    def __iter__(self) -> FakeCursor:
        return self

    def __next__(self) -> Any:
        return next(self._it)

    def next(self) -> Any:
        return next(self._it)

    def close(self, ignore_missing: bool = False) -> bool | None:
        self.closed = True
        return True


class FakeCollection:
    """The slice of ``StandardCollection`` the mining code uses, backed by a dict."""

    def __init__(self, docs: list[dict[str, Any]]):
        self._docs = docs

    def all(self, skip: int | None = None, limit: int | None = None) -> list[dict[str, Any]]:
        return list(self._docs)

    def insert(
        self,
        document: dict[str, Any],
        return_new: bool = False,
        sync: bool | None = None,
        silent: bool = False,
        overwrite: bool = False,
        **_more: Any,
    ) -> dict[str, Any]:
        key = document.get("_key")
        existing = [d for d in self._docs if d.get("_key") == key]
        if existing and not overwrite:
            raise KeyError(f"unique constraint violated: {key}")
        self._docs[:] = [d for d in self._docs if d.get("_key") != key] + [dict(document)]
        return {"_key": key}

    def has(
        self, document: str | dict[str, Any], rev: str | None = None, check_rev: bool = True, **_more: Any
    ) -> bool:
        key = document if isinstance(document, str) else document.get("_key")
        return any(d.get("_key") == key for d in self._docs)

    def delete(
        self,
        document: str | dict[str, Any],
        rev: str | None = None,
        check_rev: bool = True,
        ignore_missing: bool = False,
        **_more: Any,
    ) -> bool:
        key = document if isinstance(document, str) else document.get("_key")
        before = len(self._docs)
        self._docs[:] = [d for d in self._docs if d.get("_key") != key]
        if len(self._docs) == before and not ignore_missing:
            raise KeyError(f"document not found: {key}")
        return len(self._docs) != before

    def get(self, document: str | dict[str, Any], **_more: Any) -> dict[str, Any] | None:
        key = document if isinstance(document, str) else document.get("_key")
        return next((dict(d) for d in self._docs if d.get("_key") == key), None)

    def import_bulk(
        self,
        documents: list[dict[str, Any]],
        halt_on_error: bool = True,
        details: bool = True,
        from_prefix: str | None = None,
        to_prefix: str | None = None,
        overwrite: bool | None = None,
        on_duplicate: str | None = None,
        sync: bool | None = None,
        batch_size: int | None = None,
    ) -> dict[str, Any]:
        """Mirrors ``StandardCollection.import_bulk``: with ``halt_on_error`` the
        server rejects the whole request on a duplicate ``_key`` and the driver
        raises ``DocumentInsertError``; otherwise the duplicate is counted."""
        from arango.exceptions import DocumentInsertError

        existing = {d.get("_key") for d in self._docs if "_key" in d}
        dupes = [i for i, d in enumerate(documents) if "_key" in d and d["_key"] in existing]
        if dupes and halt_on_error:
            raise server_error(DocumentInsertError, 409, 1210, "unique constraint violated")
        msgs = [
            f"at position {i}: creating document failed with error 'unique constraint violated'"
            for i in dupes
        ]
        created = [dict(d) for i, d in enumerate(documents) if i not in set(dupes)]
        self._docs.extend(created)
        out: dict[str, Any] = {
            "created": len(created),
            "errors": len(dupes),
            "empty": 0,
            "updated": 0,
            "ignored": 0,
        }
        if details:
            out["details"] = msgs
        return out


Handler = Callable[[str, dict[str, Any]], list[Any]]


class FakeAQL:
    def __init__(self, handler: Handler, plan_nodes: Callable[[str], list[str]]):
        self._handler = handler
        self._plan_nodes = plan_nodes
        self.executed: list[tuple[str, dict[str, Any], float | None]] = []

    def execute(
        self,
        query: str,
        count: bool = False,
        batch_size: int | None = None,
        ttl: int | None = None,
        bind_vars: dict[str, Any] | None = None,
        full_count: bool | None = None,
        max_plans: int | None = None,
        optimizer_rules: list[str] | None = None,
        cache: bool | None = None,
        memory_limit: int = 0,
        fail_on_warning: bool | None = None,
        profile: bool | None = None,
        max_transaction_size: int | None = None,
        max_warning_count: int | None = None,
        intermediate_commit_count: int | None = None,
        intermediate_commit_size: int | None = None,
        satellite_sync_wait: int | None = None,
        stream: bool | None = None,
        skip_inaccessible_cols: bool | None = None,
        max_runtime: float | None = None,
        **_more: Any,
    ) -> FakeCursor:
        self.executed.append((query, dict(bind_vars or {}), max_runtime))
        return FakeCursor(self._handler(query, dict(bind_vars or {})))

    def explain(
        self,
        query: str,
        all_plans: bool = False,
        max_plans: int | None = None,
        opt_rules: list[str] | None = None,
        bind_vars: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {"nodes": [{"type": t} for t in self._plan_nodes(query)]}


class FakeGraph:
    """``Graph.properties()`` as python-arango returns it (snake_case keys)."""

    def __init__(self, name: str, edge_definitions: list[dict[str, Any]]):
        self._name = name
        self._edge_definitions = edge_definitions

    def properties(self) -> dict[str, Any]:
        return {
            "name": self._name,
            "edge_definitions": list(self._edge_definitions),
            "orphan_collections": [],
        }


class FakeDb:
    def __init__(
        self,
        collections: dict[str, list[dict[str, Any]]] | None = None,
        *,
        handler: Handler | None = None,
        plan_nodes: Callable[[str], list[str]] | None = None,
        name: str = "IAM",
        graphs: dict[str, list[dict[str, Any]]] | None = None,
    ):
        self._collections = collections or {}
        self._name = name
        self._graphs = graphs or {}
        self.created: list[str] = []
        self.aql = FakeAQL(
            handler or (lambda q, b: []), plan_nodes or (lambda q: ["SingletonNode", "ReturnNode"])
        )

    @property
    def name(self) -> str:
        return self._name

    def has_collection(self, name: str) -> bool:
        return name in self._collections

    def collection(self, name: str) -> FakeCollection:
        return FakeCollection(self._collections[name])

    def create_collection(
        self, name: str, sync: bool = False, system: bool = False, **_more: Any
    ) -> FakeCollection:
        if name in self._collections:
            raise KeyError(f"duplicate name: {name}")
        self._collections[name] = []
        self.created.append(name)
        return FakeCollection(self._collections[name])

    def graph(self, name: str) -> FakeGraph:
        return FakeGraph(name, self._graphs[name])
