"""Goldens for the UNWIND, OPTIONAL MATCH and write fixes
(``tests/fixtures/cases/unwind_optional_writes.yml``). The behaviour is pinned
by ``tests/test_translate_unwind_optional_writes.py`` and run on ArangoDB by
``tests/integration/test_unwind_optional_writes_live.py``."""

from __future__ import annotations

import pytest

from arango_cypher import translate
from tests.helpers.mapping_fixtures import mapping_bundle_for


@pytest.mark.parametrize("case_id", ["C1421", "C1422", "C1423", "C1424"])
def test_translate_unwind_optional_writes_goldens(corpus_cases, case_id: str) -> None:
    case = next(c for c in corpus_cases if c.id == case_id)
    out = translate(case.cypher, mapping=mapping_bundle_for(case.mapping_fixture), params=case.params)

    assert case.expected_aql is not None, f"Golden AQL must be filled for {case_id}"
    assert out.aql.strip() == case.expected_aql.strip()
    assert out.bind_vars == case.expected_bind_vars
