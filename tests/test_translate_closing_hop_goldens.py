"""Golden AQL for a hop that closes at a bound node checking its neighbours first (WP-44).

The cases translate with the LDBC SF0.1 statistics attached
(``tests/helpers/ldbc_statistics.py``): the filter is driven by them. The
behaviour tests are ``tests/test_translate_closing_hop.py`` and
``tests/integration/test_closing_hop_prefilter_live.py``.
"""

from __future__ import annotations

import dataclasses

import pytest

from arango_cypher import translate
from tests.helpers.ldbc_statistics import SF0_1_STATISTICS
from tests.helpers.mapping_fixtures import mapping_bundle_for


@pytest.mark.parametrize("case_id", ["C1419", "C1420"])
def test_translate_closing_hop_goldens(corpus_cases, case_id: str):
    case = next(c for c in corpus_cases if c.id == case_id)
    bundle = mapping_bundle_for(case.mapping_fixture)
    bundle = dataclasses.replace(bundle, metadata={**bundle.metadata, "statistics": SF0_1_STATISTICS})
    out = translate(case.cypher, mapping=bundle, params=case.params or None)
    assert case.expected_aql is not None
    assert out.aql.strip() == case.expected_aql.strip()
    assert out.bind_vars == case.expected_bind_vars
