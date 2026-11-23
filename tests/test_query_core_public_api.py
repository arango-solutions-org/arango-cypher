"""arango_cypher uses only arango-query-core's public API.

arango-query-core 0.2.7 dropped ``_get_default_provider`` (a private helper
with no caller inside that library), and every fresh install here then failed
to import ``arango_cypher.nl2cypher``: the pin ``>=0.2.6,<0.3.0`` let it in. A
private name can go in any release, so none may be imported. The NL entry
points resolve their default LLM provider with the public
``get_llm_provider()``, per call.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from arango_cypher.nl2cypher import _aql, _core
from tests.helpers.mapping_fixtures import mapping_bundle_for

_PACKAGE = Path(__file__).resolve().parents[1] / "arango_cypher"


def _private_query_core_uses() -> list[str]:
    found: list[str] = []
    for path in sorted(_PACKAGE.rglob("*.py")):
        if "_antlr" in path.parts:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("arango_query_core"):
                found.extend(
                    f"{path.relative_to(_PACKAGE)}:{node.lineno} {node.module}.{alias.name}"
                    for alias in node.names
                    if alias.name.startswith("_")
                )
            if isinstance(node, ast.Import):
                found.extend(
                    f"{path.relative_to(_PACKAGE)}:{node.lineno} {alias.name}"
                    for alias in node.names
                    if alias.name.startswith("arango_query_core")
                    and any(p.startswith("_") for p in alias.name.split("."))
                )
    return found


def test_the_package_imports_no_private_name_from_arango_query_core() -> None:
    assert _private_query_core_uses() == []


class _ScriptedProvider:
    """Mirrors ``arango_query_core.nl.providers.LLMProvider.generate``."""

    def __init__(self, content: str) -> None:
        self.content = content
        self.calls = 0

    def generate(self, system: str, user: str) -> tuple[str, dict[str, int]]:
        self.calls += 1
        return self.content, {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}


def test_nl_to_cypher_resolves_its_default_provider_through_get_llm_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _ScriptedProvider("```cypher\nMATCH (u:User) RETURN u.name\n```")
    monkeypatch.setattr(_core, "get_llm_provider", lambda: provider)
    result = _core.nl_to_cypher(
        "names of users",
        mapping=mapping_bundle_for("pg"),
        use_fewshot=False,
        use_entity_resolution=False,
    )
    assert provider.calls >= 1
    assert result.method == "llm"
    assert "MATCH (u:User)" in result.cypher


def test_nl_to_cypher_with_no_provider_configured_falls_back_to_rules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_core, "get_llm_provider", lambda: None)
    result = _core.nl_to_cypher(
        "find all users", mapping=mapping_bundle_for("pg"), use_fewshot=False, use_entity_resolution=False
    )
    assert result.method == "rule_based"


def test_nl_to_aql_with_no_provider_configured_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_aql, "get_llm_provider", lambda: None)
    result = _aql.nl_to_aql("names of users", mapping=mapping_bundle_for("pg"))
    assert result.method == "none"
    assert "No LLM provider configured" in result.explanation


def test_default_fewshot_index_without_rank_bm25_is_built_and_finds_no_example(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from arango_query_core.nl import fewshot

    def _no_rank_bm25(examples: list[tuple[str, str]]) -> None:
        raise ImportError("No module named 'rank_bm25'")

    monkeypatch.setattr(fewshot, "BM25Retriever", _no_rank_bm25)
    monkeypatch.setattr(_core, "_DEFAULT_FEWSHOT_INDEX", None)
    monkeypatch.setattr(_core, "_DEFAULT_FEWSHOT_RESOLVED", False)
    index = _core._get_default_fewshot_index()
    assert index is not None
    assert isinstance(index.retriever, _core._NoExamplesRetriever)
    assert index.retrieve("names of users") == []
