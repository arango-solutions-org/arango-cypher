"""``cypher_column_names``: openCypher column names, opt-in (PRD §6.3).

Default output keys an unaliased RETURN column by an inferred short name,
which API, UI and NL clients read; the option names it by the expression as
written. Live behaviour: the TCK harness translates with the option on
(``tests/tck/execution_ledger.tsv``).
"""

from __future__ import annotations

import pytest

from arango_cypher import translate
from arango_cypher.api import clear_translate_cache
from tests.helpers.mapping_fixtures import mapping_bundle_for


@pytest.mark.parametrize("case_id", [f"C{n}" for n in range(1301, 1307)])
def test_cypher_column_names_goldens(corpus_cases, case_id: str):
    case = next(c for c in corpus_cases if c.id == case_id)
    out = translate(
        case.cypher,
        mapping=mapping_bundle_for(case.mapping_fixture),
        params=case.params,
        cypher_column_names=True,
    )
    assert case.expected_aql is not None, f"Golden AQL must be filled for {case_id}"
    assert out.aql.strip() == case.expected_aql.strip()
    assert out.bind_vars == case.expected_bind_vars


def _last_line(q: str, **kw) -> str:
    return translate(q, mapping=mapping_bundle_for("lpg"), **kw).aql.splitlines()[-1].strip()


@pytest.mark.parametrize(
    ("query", "default", "written"),
    [
        (
            "MATCH (n:User) RETURN n.name, count(*)",
            "RETURN {name: name, count: count}",
            'RETURN {"n.name": name, "count(*)": count}',
        ),
        (
            "MATCH (n:User) RETURN n.name, n.age",
            "RETURN {name: n.name, age: n.age}",
            'RETURN {"n.name": n.name, "n.age": n.age}',
        ),
        (
            "MATCH (n:User) RETURN n.name AS who, n.age",
            "RETURN {who: n.name, age: n.age}",
            'RETURN {who: n.name, "n.age": n.age}',
        ),
        ("MATCH (n:User) RETURN n, n.name", "RETURN {n: n, name: n.name}", 'RETURN {n: n, "n.name": n.name}'),
        (
            "RETURN 1 + 2, 'a'",
            "RETURN {expr1: 3, expr2: 'a'}",
            "RETURN {\"1 + 2\": 3, \"'a'\": 'a'}",
        ),
        # a column named by a reserved word is never a bare key (ERR 1501),
        # nor is a key inferred from a folded true / false / null
        ("RETURN true, null", "RETURN {expr1: true, expr2: null}", 'RETURN {"true": true, "null": null}'),
        (
            "WITH 1 AS x, 2 AS d RETURN x > d, x = d",
            "RETURN {expr1: false, expr2: false}",
            'RETURN {"x > d": false, "x = d": false}',
        ),
    ],
)
def test_both_modes(query: str, default: str, written: str):
    assert _last_line(query) == default
    assert _last_line(query, cypher_column_names=False) == default
    assert _last_line(query, cypher_column_names=True) == written


def test_spacing_is_kept_as_written():
    line = _last_line("MATCH (n:User) RETURN n.age  +  1, count( * )", cypher_column_names=True)
    assert '"n.age  +  1"' in line and '"count( * )"' in line


def test_single_unaliased_column_stays_bare():
    assert _last_line("MATCH (n:User) RETURN n.name", cypher_column_names=True) == "RETURN n.name"


def test_optional_match_null_row_uses_the_same_keys():
    aql = translate(
        "OPTIONAL MATCH (n:User) RETURN n.name, n.age",
        mapping=mapping_bundle_for("lpg"),
        cypher_column_names=True,
    ).aql
    # The null row is the projection of null variables, so it has the same keys.
    assert "[{}]" in aql and 'RETURN {"n.name": n.name, "n.age": n.age}' in aql


def test_order_by_and_grouping_still_resolve():
    aql = translate(
        "MATCH (n:User) RETURN n.name, count(*) AS c ORDER BY n.name, c DESC",
        mapping=mapping_bundle_for("lpg"),
        cypher_column_names=True,
    ).aql
    assert "COLLECT name = n.name AGGREGATE" in aql
    assert "SORT" in aql and "name" in aql.split("SORT", 1)[1]


def test_call_subquery_columns_keep_their_compiled_keys():
    """A CALL body's RETURN is read back by the outer query, not shown."""
    q = "MATCH (n:User) CALL { WITH n RETURN n.name AS nm } RETURN nm, n.age"
    assert _last_line(q, cypher_column_names=True).endswith('"n.age": n.age}')


def test_option_is_part_of_the_cache_key():
    clear_translate_cache()
    q = "MATCH (n:User) RETURN n.name, n.age"
    assert _last_line(q) != _last_line(q, cypher_column_names=True)
    assert _last_line(q) == "RETURN {name: n.name, age: n.age}"
