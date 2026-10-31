"""``TranslateOptions.temporal_strings``: temporal values held as strings are
read at run time in AQL (``_translate_v0/temporal_strings.py``).

What the AQL computes is checked on ArangoDB against the Python temporal
library in ``tests/integration/test_temporal_strings_live.py``; these tests
pin where the option applies and that it changes nothing when off.
"""

from __future__ import annotations

import pytest

from arango_cypher import translate
from arango_cypher._translate_v0 import temporal_strings as ts
from arango_cypher._translate_v0.const_eval import constant_folding
from tests.helpers.mapping_fixtures import mapping_bundle_for

MAPPING = mapping_bundle_for("lpg")

QUERIES = [
    "MATCH (v:User) WITH v.date AS d RETURN d.year AS y, d.offsetMinutes AS o",
    "MATCH (v:User) RETURN v.since + v.span AS s, v.span * 2 AS p, v.span / 2 AS q",
    "MATCH (v:User) WITH v, v.at AS t ORDER BY t RETURN v",
    "MATCH (v:User) WHERE v.at < v.until RETURN v",
    "MATCH (v:User) RETURN v.at + duration({minutes: 6}) AS t",
]


def _aql(query: str, **options: bool) -> str:
    return translate(query, mapping=MAPPING, **options).aql


@pytest.mark.parametrize("query", QUERIES)
def test_off_by_default(query: str) -> None:
    """Without the option nothing of it is emitted: the AQL is the default's."""
    assert _aql(query) == _aql(query, temporal_strings=False)
    assert "_tv0" not in _aql(query)


@pytest.mark.parametrize("query", QUERIES)
def test_on_reads_strings_at_run_time(query: str) -> None:
    aql = _aql(query, temporal_strings=True)
    assert "LET _tv0 = " in aql
    assert aql != _aql(query)


def test_option_is_part_of_the_cache_key() -> None:
    query = QUERIES[0]
    assert _aql(query, temporal_strings=True) != _aql(query, temporal_strings=False)


@pytest.mark.parametrize(
    "query",
    [
        # statically numeric operands keep their arithmetic
        "MATCH (v:User) RETURN v.age + 1 AS a, 2 * 3 AS b",
        # a node attribute that is not an accessor name
        "MATCH (v:User) RETURN v.name AS n",
        # an ordering on a number
        "MATCH (v:User) RETURN v.age AS a ORDER BY size(v.name)",
    ],
)
def test_not_applied_where_types_rule_it_out(query: str) -> None:
    with constant_folding(False):
        assert "_tv" not in _aql(query, temporal_strings=True)


def test_bindings_are_fresh_per_translation() -> None:
    """Each translation numbers its bindings from 0, so the AQL is stable."""
    query = QUERIES[0]
    first = _aql(query, temporal_strings=True)
    from arango_cypher.api import clear_translate_cache

    clear_translate_cache()
    assert _aql(query, temporal_strings=True) == first
    names = [part.split(" ")[0] for part in first.split("LET _tv")[1:]]
    assert len(names) == len(set(names)), "a binding name is declared twice"


def test_accessor_reads_maps_and_nodes_as_before() -> None:
    aql = ts.accessor("x", "year")
    assert aql.endswith(": _tv0.year))")
    with pytest.raises(ValueError):
        ts.accessor("x", "fortnight")


def test_snippets_never_raise_in_an_arm() -> None:
    """AQL evaluates both arms of a ternary, so no snippet may call FAIL()."""
    snippets = [ts.accessor("x", name) for name in sorted(ts.ACCESSORS)] + [
        ts.additive("a", "+", "b", "null"),
        ts.additive("a", "-", "b", "null"),
        ts.multiplicative("a", "*", "b", "null"),
        ts.multiplicative("a", "/", "b", "null"),
        ts.sort_key("a", "a"),
        ts.ordering("a", "<", "b", "false"),
    ]
    assert not any("FAIL(" in s for s in snippets)


def test_ordering_respects_a_negated_predicate() -> None:
    assert "!(" in ts.ordering("a", "<", "b", "fallback", negated=True)
    assert "!(" not in ts.ordering("a", "<", "b", "fallback")


def test_temporal_tck_shapes_translate() -> None:
    """The TCK's run-time temporal shapes translate with the option."""
    for query in (
        "MATCH (v:User) WITH v.date AS d RETURN d.year, d.weekYear, d.nanosecond, d.timezone, d.epochMillis",
        "MATCH (dur:User), (dur2:Note) RETURN dur.date + dur2.date AS sum, dur.date - dur2.date AS diff",
        "MATCH (d:User) RETURN d.date * 0.5 AS prod, d.date / 2 AS div",
        "UNWIND ['10:35-08:00', '12:31:14+01:00'] AS times WITH times ORDER BY times ASC LIMIT 3 RETURN times",
        "MATCH (a:User) WITH a ORDER BY a.time + duration({minutes: 6}) LIMIT 3 RETURN a",
    ):
        translate(query, mapping=mapping_bundle_for("lpg"), temporal_strings=True, cypher_column_names=True)
