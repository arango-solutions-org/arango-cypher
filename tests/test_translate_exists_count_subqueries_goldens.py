"""Goldens for EXISTS { } and COUNT { } bodies compiled by the shared MATCH
compiler (``tests/fixtures/cases/exists_count_subqueries.yml``). The behaviour
is pinned by ``tests/test_translate_exists_count_subqueries.py`` and run on
ArangoDB by ``tests/integration/test_exists_count_subqueries_live.py``."""

from __future__ import annotations

import pytest

from arango_cypher import translate
from tests.helpers.mapping_fixtures import mapping_bundle_for


@pytest.mark.parametrize("case_id", ["C1425", "C1426", "C1427", "C1428"])
def test_translate_exists_count_subqueries_goldens(corpus_cases, case_id: str) -> None:
    case = next(c for c in corpus_cases if c.id == case_id)
    out = translate(case.cypher, mapping=mapping_bundle_for(case.mapping_fixture), params=case.params)

    assert case.expected_aql is not None, f"Golden AQL must be filled for {case_id}"
    assert out.aql.strip() == case.expected_aql.strip()
    assert out.bind_vars == case.expected_bind_vars
