"""Golden AQL for reading a pattern from the end an index finds (``pattern_reversal``, WP-44).

The behaviour tests are ``tests/test_translate_pattern_reversal.py`` and
``tests/integration/test_anchor_selection_live.py``.
"""

from __future__ import annotations

import pytest

from arango_cypher import translate
from tests.helpers.mapping_fixtures import mapping_bundle_for


@pytest.mark.parametrize("case_id", ["C1415", "C1416"])
def test_translate_anchor_selection_goldens(corpus_cases, case_id: str):
    case = next(c for c in corpus_cases if c.id == case_id)
    out = translate(case.cypher, mapping=mapping_bundle_for(case.mapping_fixture), params=case.params or None)
    assert case.expected_aql is not None
    assert out.aql.strip() == case.expected_aql.strip()
    assert out.bind_vars == case.expected_bind_vars
