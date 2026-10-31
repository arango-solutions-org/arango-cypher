"""``x[i]`` run-time TypeErrors and ``IN`` precedence (PRD §8).

Live behaviour: List1 [7]/[9], List5, Map2 [6]-[8] in
``tests/tck/execution_ledger.tsv``.
"""

from __future__ import annotations

import pytest

from arango_cypher import translate
from arango_cypher._translate_v0.arithmetic import INT, LIST, MAP, STRING
from arango_cypher._translate_v0.subscripts import compile_subscript
from tests.helpers.mapping_fixtures import mapping_bundle_for


@pytest.mark.parametrize("case_id", [f"C{n}" for n in range(1411, 1415)])
def test_subscript_goldens(corpus_cases, case_id: str):
    case = next(c for c in corpus_cases if c.id == case_id)
    out = translate(case.cypher, mapping=mapping_bundle_for(case.mapping_fixture), params=case.params or None)
    assert case.expected_aql is not None, f"Golden AQL must be filled for {case_id}"
    assert out.aql.strip() == case.expected_aql.strip()
    assert out.bind_vars == case.expected_bind_vars


def test_statically_well_typed_access_is_plain():
    assert compile_subscript("l", "0", LIST, INT) == "(l)[0]"
    assert compile_subscript("m", "'k'", MAP, STRING) == "(m)['k']"


def test_unknown_types_are_guarded_inline_for_plain_references():
    out = compile_subscript("x", "@i", None, None)
    assert out.startswith("(x == null OR @i == null ? null : IS_ARRAY(x)")
    assert "FAIL(" in out and "FOR " not in out


def test_nested_guards_use_distinct_names():
    inner = compile_subscript("f(x)", "1", None, INT)
    outer = compile_subscript(inner, "2", None, INT)
    assert "FOR _sb0 IN" in outer and "FOR _sb1 IN" in outer


def test_positional_parameters_bind_by_number():
    """``$1`` is AQL's ``@1`` (bind names may be all digits); it used to be refused."""
    out = translate(
        "MATCH (n:User) WHERE n.id = $1 RETURN n", mapping=mapping_bundle_for("lpg"), params={"1": 7}
    )
    assert "@1" in out.aql and out.bind_vars["1"] == 7


def test_only_optional_matches_project_the_null_row():
    """``OPTIONAL MATCH (n) RETURN n.x IS NULL``: the RETURN sees n = null."""
    aql = translate(
        "OPTIONAL MATCH (n:User) RETURN n.x IS NULL AS missing", mapping=mapping_bundle_for("lpg")
    ).aql
    assert "[{}]" in aql and "LET n = _opt_row.n" in aql
    assert aql.rstrip().endswith("RETURN {missing: (n.x == null)}")


def test_in_binds_looser_than_subscript_and_slice():
    for q in ("WITH $l AS list RETURN 3 IN list[0] AS r", "WITH $l AS list RETURN 3 IN list[0..1] AS r"):
        aql = translate(q, mapping=mapping_bundle_for("lpg"), params={"l": [[1, 2, 3]]}).aql
        assert " IN " in aql and "cannot index" not in aql
