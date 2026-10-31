"""``IN`` precedence and null semantics (PRD §8; grammar ``oC_InExpression``).

Live behaviour: Precedence1/3, List5, Null3 in ``tests/tck/execution_ledger.tsv``.
"""

from __future__ import annotations

from arango_cypher import translate
from arango_cypher._antlr.CypherParser import CypherParser
from arango_cypher._translate_v0.three_valued import compile_in
from arango_cypher.parser import parse_cypher
from tests.helpers.mapping_fixtures import mapping_bundle_for


def _in_operands(expr: str) -> list[str]:
    """The operand texts of the first IN level in ``RETURN expr``."""
    tree = parse_cypher(f"RETURN {expr}").tree

    def find(node):
        if (
            isinstance(node, CypherParser.OC_InExpressionContext)
            and len(node.oC_AddOrSubtractExpression()) > 1
        ):
            return node
        for i in range(node.getChildCount() if hasattr(node, "getChildCount") else 0):
            hit = find(node.getChild(i))
            if hit is not None:
                return hit
        return None

    node = find(tree)
    assert node is not None
    return [t.getText() for t in node.oC_AddOrSubtractExpression()]


def test_subscript_and_slice_bind_tighter_than_in():
    assert _in_operands("3 IN list[0]") == ["3", "list[0]"]
    assert _in_operands("3 IN list[0..1]") == ["3", "list[0..1]"]


def test_additive_binds_tighter_than_in():
    assert _in_operands("[1] + 2 IN [3] + 4") == ["[1] + 2", "[3] + 4"]


def test_in_binds_tighter_than_comparison():
    assert _in_operands("false = true IN [true]") == ["true", "[true]"]


def test_in_values_follow_cypher_null_semantics():
    value = compile_in("@e", "@c", None)
    assert value == (
        "(@c == null ? null : LENGTH(@c) == 0 ? false : @e == null ? null : "
        "(@e IN @c ? true : null IN @c ? null : false))"
    )
    assert compile_in("x", "[1, 2]", None) == "((x == null) ? null : (x IN [1, 2]))"
    assert compile_in("x", "[]", None) == "false"
    assert compile_in("@e", "@c", "false") == (
        "(@c != null AND (LENGTH(@c) == 0 OR (@e != null AND @e NOT IN @c AND null NOT IN @c)))"
    )


def test_computed_operands_are_evaluated_once():
    value = compile_in("f(x)", "g(y)", None)
    assert value.startswith("FIRST(FOR _inl0 IN [f(x)] FOR _inr0 IN [g(y)] RETURN")


def test_where_in_filter_keeps_the_indexable_form():
    aql = translate("MATCH (n:User) WHERE n.name IN $names RETURN n", mapping=mapping_bundle_for("lpg")).aql
    assert "n.name IN @names" in aql and "LENGTH(" not in aql
