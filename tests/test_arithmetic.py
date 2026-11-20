"""Unit tests for the arithmetic helpers (``_translate_v0.arithmetic``)."""

from __future__ import annotations

import pytest
from arango_query_core import MappingResolver

from arango_cypher._antlr.CypherParser import CypherParser
from arango_cypher._translate_v0.arithmetic import (
    INT,
    LIST,
    NUMBER,
    STRING,
    _static_type,
    additive_chain_propagates_null,
    compile_addition,
    declared_property_type,
    raw_additive_chain,
    raw_arithmetic,
)
from arango_cypher._translate_v0.three_valued import _is_obvious_non_null
from arango_cypher.parser import parse_cypher
from tests.helpers.mapping_fixtures import mapping_bundle_for


def _return_expression(expr: str) -> CypherParser.OC_ExpressionContext:
    tree = parse_cypher(f"RETURN {expr} AS x").tree
    found: list[CypherParser.OC_ExpressionContext] = []

    def walk(node: object) -> None:
        if isinstance(node, CypherParser.OC_ExpressionContext):
            found.append(node)
            return
        for i in range(node.getChildCount()):  # type: ignore[attr-defined]
            child = node.getChild(i)  # type: ignore[attr-defined]
            if hasattr(child, "getChildCount"):
                walk(child)

    walk(tree)
    return found[0]


@pytest.mark.parametrize(
    ("expr", "expected"),
    [
        ("1", INT),
        ("0x1F", INT),
        ("1.5", "float"),
        ("1e3", "float"),
        ("'a'", STRING),
        ("[1]", LIST),
        ("{a: 1}", "map"),
        ("null", "null"),
        ("true", "bool"),
        ("1 + 2", INT),
        ("1 + 2.0", NUMBER),
        ("'a' + 1", STRING),
        ("[1] + 'a'", LIST),
        ("x * 2", NUMBER),
        ("2 * 3", INT),
        ("-x", NUMBER),
        ("-2", INT),
        ("2 ^ 2", "float"),
        ("count(x)", INT),
        ("toString(x)", STRING),
        ("collect(x)", LIST),
        ("toFloat(x)", "float"),
        ("x", None),
        ("x.y", None),
        ("coalesce(x, 1)", None),
        ("x IN [1]", "bool"),
        ("x[0]", None),
        ("x[0..1]", LIST),
        ("(1 + 2)", INT),
        ("x = 1", "bool"),
    ],
)
def test_static_type(expr: str, expected: str | None) -> None:
    assert _static_type(_return_expression(expr)) == expected


def test_declared_property_type_reads_an_unambiguous_mapping_type() -> None:
    resolver = MappingResolver(mapping_bundle_for("ldbc_pg"))
    assert declared_property_type("length", resolver) == NUMBER
    assert declared_property_type("firstName", resolver) == STRING
    assert declared_property_type("noSuchProperty", resolver) is None
    assert declared_property_type("length", None) is None
    # Undeclared in the untyped fixture.
    assert declared_property_type("age", MappingResolver(mapping_bundle_for("pg"))) is None


@pytest.mark.parametrize(
    ("types", "ops", "expected"),
    [
        ([INT, INT], ["+"], True),
        ([STRING, INT], ["+"], True),
        ([INT, None], ["-"], True),
        ([INT, None], ["+"], False),
        ([LIST, INT], ["+"], False),
        ([INT, INT, STRING], ["+", "+"], True),
        ([STRING, "bool"], ["+"], False),
    ],
)
def test_additive_chain_propagates_null(types: list[str | None], ops: list[str], expected: bool) -> None:
    assert additive_chain_propagates_null(types, ops) is expected


def test_raw_additive_chain_evaluates_left_to_right() -> None:
    assert raw_additive_chain(["1", "2", "'a'"], [INT, INT, STRING], ["+", "+"]) == "CONCAT((1 + 2), 'a')"
    assert raw_additive_chain(["'a'", "1", "2"], [STRING, INT, INT], ["+", "+"]) == "CONCAT('a', 1, 2)"
    assert raw_additive_chain(["a", "1", "2"], [INT, INT, INT], ["+", "-"]) == "((a + 1) - 2)"


def test_raw_arithmetic_truncates_integer_division() -> None:
    assert raw_arithmetic("7", "/", "2", INT, INT) == "((7 - 7 % 2) / 2)"
    assert raw_arithmetic("7.0", "/", "2", "float", INT) == "(7.0 / 2)"
    assert raw_arithmetic("a", "*", "b", None, None) == "(a * b)"


@pytest.mark.parametrize(
    ("left", "lt", "right", "rt", "expected"),
    [
        ("[1]", LIST, "[2]", LIST, "APPEND([1], [2])"),
        # A null operand makes + null, lists included (Neo4j 5.26).
        ("[1]", LIST, "null", "null", "null"),
        ("n.l", LIST, "1", INT, "((n.l == null) ? null : PUSH(n.l, 1))"),
        ("1", INT, "n.l", LIST, "((n.l == null) ? null : UNSHIFT(n.l, 1))"),
        ("null", "null", "1", INT, "null"),
        ("true", "bool", "1", INT, "null"),
        (
            "1",
            INT,
            "x",
            None,
            "(IS_NUMBER(x) ? (1 + x) : (IS_STRING(x) ? CONCAT(1, x) : (IS_ARRAY(x) ? UNSHIFT(x, 1) : null)))",
        ),
        ("x", None, "'a'", STRING, "(IS_ARRAY(x) ? PUSH(x, 'a') : ((x == null) ? null : CONCAT(x, 'a')))"),
    ],
)
def test_compile_addition(left: str, lt: str | None, right: str, rt: str | None, expected: str) -> None:
    assert compile_addition(left, lt, right, rt) == expected


def test_compile_addition_dispatches_on_two_unknown_types() -> None:
    aql = compile_addition("a", None, "b", None)
    assert aql.startswith("((IS_NUMBER(a) AND IS_NUMBER(b)) ? (a + b)")
    assert "CONCAT(a, b)" in aql and "APPEND(a, b)" in aql


@pytest.mark.parametrize(
    ("expr", "expected"),
    [
        ("[1, 2]", True),
        ("{a: 1}", True),
        ("['a]', 'b']", True),
        ("[1] + [2]", False),
        ("([1])[0]", False),
        ("[1][0]", False),
        ("x", False),
    ],
)
def test_is_obvious_non_null_recognises_list_and_map_literals(expr: str, expected: bool) -> None:
    assert _is_obvious_non_null(expr) is expected


@pytest.mark.parametrize(
    ("expr", "expected"),
    [
        ("LENGTH(x)", True),
        ("LENGTH((FOR a IN b RETURN a))", True),
        ('LENGTH(")")', True),  # a parenthesis in a string does not close the call
        ("LENGTH(x) + y", False),
        ("LENGTH(a) == LENGTH(b)", False),
        ("LENGTHS(x)", False),
    ],
)
def test_is_obvious_non_null_recognises_a_whole_length_call(expr: str, expected: bool) -> None:
    # LENGTH(null) is 0: a guard on the call never fires.
    assert _is_obvious_non_null(expr) is expected


def test_arithmetic_over_size_evaluates_each_comprehension_once() -> None:
    """The null guard repeated every operand, so each subquery ran twice (LDBC BI8)."""
    from arango_cypher import translate

    aql = translate(
        "MATCH (t:Tag {id: 1}), (p:Person {id: 2}) "
        "RETURN 100 * size([(t)<-[i:HAS_INTEREST]-(p) | i]) + size([(p)-[:KNOWS]-(f) | f]) AS s",
        mapping=mapping_bundle_for("ldbc_pg"),
    ).aql
    assert aql.count("FOR i IN") == 1 and aql.count("FOR f, ") == 1, aql
    assert "== null" not in aql.splitlines()[-1], aql
