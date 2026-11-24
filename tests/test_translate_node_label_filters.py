"""A node's labels filter the same way in every MATCH compiler.

Nine compilers each carried their own copy of the label dispatch (a ``LABEL``
type test, a ``MULTI_LABEL`` labels-array test, the extra labels of a
multi-label node, a ``COLLECTION`` node's extra labels); they now call
``_node_label_filters``. Each test asks for the same node in every position.
"""

from __future__ import annotations

import copy
import re

import pytest
from arango_query_core import CoreError, MappingBundle

from arango_cypher import translate
from tests.helpers.mapping_fixtures import mapping_bundle_for

#: The node ``x`` in each compiler's position.
POSITIONS = {
    "first MATCH": "MATCH (x{labels}) RETURN x",
    "hop target": "MATCH (a:User)-[:FOLLOWS]->(x{labels}) RETURN x",
    "comma part": "MATCH (a:User), (x{labels}) RETURN x",
    "MATCH after WITH": "MATCH (a:User) WITH a MATCH (x{labels}) RETURN x",
    "hop after WITH": "MATCH (a:User) WITH a MATCH (a)-[:FOLLOWS]->(x{labels}) RETURN a, x",
    "EXISTS": "MATCH (a:User) WHERE EXISTS {{ (x{labels}) }} RETURN a",
    "before a write": "MATCH (a:User)-[:FOLLOWS]->(x{labels}) SET a.seen = true",
}


def _aql(position: str, labels: str, mapping: str | MappingBundle) -> tuple[str, dict]:
    bundle = mapping_bundle_for(mapping) if isinstance(mapping, str) else mapping
    q = translate(POSITIONS[position].format(labels=labels), mapping=bundle)
    return q.aql, q.bind_vars


@pytest.mark.parametrize("position", list(POSITIONS))
def test_a_label_is_a_type_test(position: str) -> None:
    aql, bv = _aql(position, ":Doc", "lpg")
    tests = re.findall(r"\w+\[@(\w+)\] == @(\w+)", aql)
    assert any(bv[f] == "type" and bv[v] == "Doc" for f, v in tests), aql


@pytest.mark.parametrize("position", list(POSITIONS))
def test_a_second_label_is_tested_too(position: str) -> None:
    aql, _ = _aql(position, ":User:Doc", "lpg_labels")
    assert re.search(r'"User" IN \w+\.labels', aql), aql


@pytest.mark.parametrize("position", list(POSITIONS))
def test_a_label_mapping_without_a_type_value_fails_closed(position: str) -> None:
    raw = mapping_bundle_for("lpg")
    physical = copy.deepcopy(raw.physical_mapping)
    del physical["entities"]["Doc"]["typeValue"]
    broken = MappingBundle(
        conceptual_schema=raw.conceptual_schema,
        physical_mapping=physical,
        metadata=raw.metadata,
        source=raw.source,
    )
    # Bound as null, the test would be ``x[@typeField] == null``: every document
    # without the field.
    with pytest.raises(CoreError) as exc:
        _aql(position, ":Doc", broken)
    assert exc.value.code == "INVALID_MAPPING"
