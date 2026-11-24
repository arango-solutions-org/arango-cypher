"""Compile-time evaluation of constant expressions (``const_eval``).

Each case is a place where Cypher and AQL (or Python) disagree, so folding is
what makes the answer right — or a guard that folding stays out of the way.
"""

from __future__ import annotations

import math

import pytest

from arango_cypher import translate
from arango_cypher._translate_v0 import cypher_values as cv
from arango_cypher._translate_v0.const_eval import Evaluator
from arango_cypher._translate_v0.cypher_values import CypherEvalError, NotConstant
from arango_cypher._translate_v0.literals import cypher_string_value
from arango_cypher.parser import parse_cypher
from tests.helpers.mapping_fixtures import mapping_bundle_for


def _eval(expr: str, env: dict | None = None):
    tree = parse_cypher(f"RETURN {expr} AS v")
    item = tree.tree  # type: ignore[attr-defined]
    stack = [item]
    while stack:
        node = stack.pop()
        if type(node).__name__ == "OC_ExpressionContext":
            return Evaluator(env or {}).eval(node)
        stack.extend(reversed(getattr(node, "children", None) or []))
    raise AssertionError("no expression")


@pytest.mark.parametrize(
    ("expr", "expected"),
    [
        ("[1, null] = [1, null]", None),
        ("[1, 2] = [1, null]", None),
        ("[1] = [1, null]", False),
        ("{a: null} = {a: null}", None),
        ("1 = '1'", False),
        ("1 = 1.0", True),
        ("1 < '1'", None),
        ("0.0 / 0.0 = 0.0 / 0.0", False),
        ("0.0 / 0.0 > 1", False),
        ("[1, 2] < [1, 3]", True),
        ("1 < 2 < 3", True),
        ("3 IN [1, null]", None),
        ("1 IN [1, null]", True),
        ("null IN []", False),
        ("7 / 2", 3),
        ("-7 / 2", -3),
        ("-7 % 3", -1),
        ("2 ^ 3", 8.0),
        ("'a' + 1", "a1"),
        ("'a' + 1.0", "a1.0"),
        ("[1] + 2", [1, 2]),
        ("[1] + null", None),
        ("[1] + [null]", [1, None]),
        ("toString(1e20)", "1.0E20"),
        ("toString(0.001)", "0.001"),
        ("toInteger('42.9')", 42),
        ("toBoolean('TRUE')", True),
        ("[1, 2, 3][-1]", 3),
        ("[1, 2, 3][5]", None),
        ("[1, 2, 3][1..]", [2, 3]),
        ("[1, 2, 3][null..2]", None),
        ("[x IN [1, 2, 3] WHERE x > 1 | x * 10]", [20, 30]),
        ("all(x IN [1, null] WHERE x > 0)", None),
        ("any(x IN [1, null] WHERE x > 0)", True),
        ("none(x IN [] WHERE x > 0)", True),
        ("single(x IN [1, 2] WHERE x > 0)", False),
        ("reduce(acc = 0, x IN [1, 2, 3] | acc + x)", 6),
        ("CASE 2 WHEN 1 THEN 'a' WHEN 2 THEN 'b' END", "b"),
        ("CASE WHEN false THEN 1 / 0 ELSE 0 END", 0),
        ("-9223372036854775808", -(2**63)),
        ("0x1F", 31),
        ("'abc' STARTS WITH 'a'", True),
        ("1 STARTS WITH 'a'", None),
        ("null IS NULL", True),
    ],
)
def test_cypher_semantics(expr: str, expected) -> None:
    got = _eval(expr)
    assert got == expected, (expr, got)
    assert type(got) is type(expected), (expr, got)


def test_variables_come_from_the_environment() -> None:
    assert _eval("x + 1", {"x": 41}) == 42
    with pytest.raises(NotConstant):
        _eval("y + 1")


@pytest.mark.parametrize("expr", ["rand()", "count(*)", "count(1)", "$p", "n.prop", "timestamp()"])
def test_data_dependent_expressions_are_not_constant(expr: str) -> None:
    with pytest.raises(NotConstant):
        _eval(expr)


@pytest.mark.parametrize(
    ("expr", "cypher_class"),
    [
        ("1 / 0", "DivisionByZero"),
        ("9223372036854775807 + 1", "IntegerOverflow"),
        ("toInteger([1])", "InvalidArgumentType"),
        ("range(1, 2, 0)", "NumberOutOfRange"),
        ("1 AND true", "InvalidArgumentType"),
    ],
)
def test_errors_carry_the_cypher_class(expr: str, cypher_class: str) -> None:
    with pytest.raises(CypherEvalError) as info:
        _eval(expr)
    assert info.value.cypher_class == cypher_class


def test_nan_is_not_folded_into_aql() -> None:
    # JSON has no NaN, so the value stays an AQL expression.
    assert math.isnan(_eval("0.0 / 0.0"))
    out = translate("RETURN 0.0 / 0.0 AS v", mapping=mapping_bundle_for("lpg"))
    assert "cv0" not in out.bind_vars


def test_string_escapes() -> None:
    assert cypher_string_value(r"'a\tb\\c\'d'") == "a\tb\\c'd"
    assert cypher_string_value(r"'A'") == "A"


class TestTranslation:
    mapping = mapping_bundle_for("lpg")

    def test_folded_string_goes_through_a_bind_variable(self) -> None:
        out = translate("RETURN 'a' + 'b' AS s", mapping=self.mapping)
        assert "@cv0" in out.aql and out.bind_vars["cv0"] == "ab"

    def test_plain_literals_are_not_rewritten(self) -> None:
        out = translate("UNWIND [1, 2] AS x RETURN x", mapping=self.mapping)
        assert "[1,2]" in out.aql and not out.bind_vars

    def test_with_constants_fold_downstream(self) -> None:
        out = translate("WITH [1, 2, 3] AS l RETURN size(l) AS s", mapping=self.mapping)
        assert "s: 3" in out.aql

    def test_translation_cache_keys_on_folding(self) -> None:
        # A translation cached with folding on must not answer one with it
        # off (the arithmetic goldens failed after test_corpus_smoke).
        from arango_cypher._translate_v0.const_eval import constant_folding

        folded = translate("RETURN 'a' + 'b' AS s", mapping=self.mapping)
        with constant_folding(False):
            lowered = translate("RETURN 'a' + 'b' AS s", mapping=self.mapping)
        assert "@cv0" in folded.aql and "CONCAT" in lowered.aql

    def test_unaliased_folded_literal_is_not_used_as_a_bare_key(self) -> None:
        # ``x > d`` folds to ``false``; a bare ``false`` key is AQL ERR 1501.
        out = translate("WITH 1 AS x, 2 AS d RETURN x > d, x = d, x < d", mapping=self.mapping)
        returned = out.aql.rsplit("RETURN", 1)[1]
        assert "false:" not in returned and "true:" not in returned
        assert "expr1: false" in returned and "expr3: true" in returned

    def test_error_is_deferred_to_evaluation(self) -> None:
        out = translate("RETURN 1 / 0 AS x", mapping=self.mapping)
        assert "FAIL(@cv0)" in out.aql

    def test_comprehension_variable_is_not_the_with_constant(self) -> None:
        # x inside the comprehension is the loop variable, not WITH's x.
        out = translate(
            "MATCH (n:User) WITH n, 10 AS x RETURN [x IN n.scores | x + 1] AS s",
            mapping=self.mapping,
        )
        assert "11" not in out.aql
