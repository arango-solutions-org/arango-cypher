from __future__ import annotations

import pytest

from arango_cypher import translate
from tests.helpers.mapping_fixtures import mapping_bundle_for


@pytest.mark.parametrize("case_id", ["C720", "C721", "C1417", "C1418"])
def test_translate_relationship_uniqueness_goldens(corpus_cases, case_id: str):
    case = next(c for c in corpus_cases if c.id == case_id)
    mapping = mapping_bundle_for(case.mapping_fixture)

    out = translate(case.cypher, mapping=mapping, params=case.params)

    assert case.expected_aql is not None, f"Golden AQL must be filled for {case_id}"
    assert out.aql.strip() == case.expected_aql.strip()
    assert out.bind_vars == case.expected_bind_vars
