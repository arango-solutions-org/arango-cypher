"""openCypher string literals reach AQL with the escapes AQL reads differently
rewritten. The live check is in ``tests/integration/test_write_clauses_live.py``."""

from __future__ import annotations

import re

import pytest
from arango_query_core import CoreError

from arango_cypher._translate_v0.literals import _aql_string_from_cypher, cypher_string_value


@pytest.mark.parametrize(
    ("cypher", "aql"),
    [
        ("'plain'", "'plain'"),
        ("'it\\'s'", "'it\\'s'"),
        ("'\\N\\T\\B\\F\\R'", "'\\n\\t\\b\\f\\r'"),
        ("'\\u00e9'", "'\\u00e9'"),
        ("'\\U000000e9'", "'\\u00e9'"),
        ("'\\U0001F600'", "'\\ud83d\\ude00'"),
        ("'\\U00000022'", "'\\u0022'"),
        ("'\\\\U0001F600'", "'\\\\U0001F600'"),
        ('"a\\\\"', '"a\\\\"'),
    ],
)
def test_escapes_aql_reads_differently_are_rewritten(cypher: str, aql: str) -> None:
    assert _aql_string_from_cypher(cypher) == aql


def test_a_code_point_past_unicode_is_refused() -> None:
    with pytest.raises(CoreError, match="not a Unicode code point"):
        _aql_string_from_cypher("'\\U00110000'")


@pytest.mark.parametrize(
    ("cypher", "aql"),
    [
        # \U takes four hex digits when eight do not follow (the grammar admits
        # both); AQL has no \U and read it as the letter U.
        ("'\\U00e9x'", "'\\u00e9x'"),
        # \u always takes four.
        ("'\\u00412'", "'\\u00412'"),
        ("'\\uD83D\\uDE00'", "'\\uD83D\\uDE00'"),
    ],
)
def test_four_digit_escapes_reach_aql_as_four_digit_escapes(cypher: str, aql: str) -> None:
    assert _aql_string_from_cypher(cypher) == aql


@pytest.mark.parametrize(
    ("cypher", "value"),
    [
        ("'plain'", "plain"),
        ("'a\\tb\\\\c\\'d'", "a\tb\\c'd"),
        ("'\\N\\T\\B\\F\\R'", "\n\t\b\f\r"),
        ("'\\u00e9'", "é"),
        ("'\\U00e9x'", "éx"),
        ("'\\U000000e9'", "é"),
        ("'\\u00412'", "A2"),
        ("'\\U0001F600'", "\U0001f600"),
        ("'\\uD83D\\uDE00'", "\U0001f600"),
        ("'\\uD83D\\UDE00'", "\U0001f600"),
        ("'\\\\U0001F600'", "\\U0001F600"),
    ],
)
def test_the_value_of_a_literal(cypher: str, value: str) -> None:
    assert cypher_string_value(cypher) == value


@pytest.mark.parametrize(
    ("cypher", "why"),
    [
        ("'\\U00110000'", "past U+10FFFF"),
        ("'\\UFFFFFFFF'", "past U+10FFFF"),
        ("'\\uD800'", "a high surrogate with no low surrogate after it"),
        ("'\\uD800x'", "a high surrogate with no low surrogate after it"),
        ("'\\uD800\\uD800'", "a high surrogate with no low surrogate after it"),
        ("'\\uDC00'", "a surrogate without its other half"),
        ("'\\U0000D83D\\uDE00'", "a surrogate without its other half"),
    ],
)
def test_what_is_not_a_character_is_refused_by_both(cypher: str, why: str) -> None:
    for decode in (cypher_string_value, _aql_string_from_cypher):
        with pytest.raises(CoreError, match=re.escape(why)) as exc:
            decode(cypher)
        assert exc.value.code == "UNSUPPORTED"
