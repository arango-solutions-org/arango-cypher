from __future__ import annotations

import pytest
from arango_query_core import CoreError

from arango_cypher import translate
from tests.helpers.mapping_fixtures import mapping_bundle_for


@pytest.mark.parametrize(
    "cypher,code,msg_sub",
    [
        # MultiPartQuery / WITH guardrails
        # (Note: leading WITH-constant / UNWIND computational pipelines like
        # `WITH 1 AS x RETURN x` are now supported — see
        # test_translate_computational_pipeline.py.)
        (
            # WITH → SET is supported; SET on a computed (non-document) WITH
            # projection still fails closed.
            "MATCH (n:User)\nWITH n.id AS id\nSET id = 1\nRETURN id",
            "NOT_IMPLEMENTED",
            "not a MATCH-bound document variable",
        ),
        (
            # UNWIND after WITH now compiles (test_translate_optional_match_rows);
            # an in-query CALL there still fails closed.
            "MATCH (n:User)\nWITH n\nCALL db.labels() YIELD label\nRETURN n, label",
            "NOT_IMPLEMENTED",
            "Only MATCH and UNWIND are supported after WITH",
        ),
        # Relationship type guardrails. Untyped relationships (``-->`` /
        # ``-[]->`` / ``-[r]->``) are now supported when a single edge
        # collection can be inferred (see test_translate_untyped_rel_goldens),
        # but multi-type edges remain unsupported.
        # Expression compiler unsupported shapes
        (
            "MATCH (n:User) WHERE unknownFunc(n.age) = 1 RETURN n",
            "UNSUPPORTED",
            "Unsupported function",
        ),
        (
            "MATCH (n:User) WHERE size([1,2], [3]) = 2 RETURN n",
            "UNSUPPORTED",
            "size expects 1 arg",
        ),
        (
            "MATCH (n:User) RETURN toUpper(n.city, n.name)",
            "UNSUPPORTED",
            "toUpper expects 1 arg",
        ),
        (
            "MATCH (n:User) RETURN toLower(n.city, n.name)",
            "UNSUPPORTED",
            "toLower expects 1 arg",
        ),
        (
            'MATCH (n:User) WHERE arango.foo(n.city) = "x" RETURN n',
            "EXTENSIONS_DISABLED",
            "arango.* extension",
        ),
        # Inline node properties guardrails
        (
            "MATCH (n:User $props) RETURN n",
            "NOT_IMPLEMENTED",
            "Parameterized node properties are not supported",
        ),
        # Multi-pattern MATCH fail-fast branches
        (
            "MATCH (u:User)-[r:FOLLOWS]->(v:User), (x:User)-[r:FOLLOWS]->(y:User) RETURN u",
            "NOT_IMPLEMENTED",
            "Shared relationship variables",
        ),
    ],
)
def test_translate_fail_fast_errors_extra(cypher: str, code: str, msg_sub: str):
    mapping = mapping_bundle_for("pg")
    with pytest.raises(CoreError) as e:
        translate(cypher, mapping=mapping)
    assert e.value.code == code
    assert msg_sub.lower() in str(e.value).lower()
