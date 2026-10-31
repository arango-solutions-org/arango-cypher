"""Unit tests for the semantic analysis pass (``_translate_v0.semantic_check``).

Every rejection is a query Neo4j rejects too (the openCypher TCK names the
error), and every acceptance is a query Neo4j runs: the pass must never turn
away a valid query, so the positive cases matter as much as the negative ones.
"""

from __future__ import annotations

import pytest
from arango_query_core import CoreError

from arango_cypher import translate
from arango_cypher._translate_v0.semantic_check import check_semantics
from arango_cypher._translate_v0.semantic_errors import (
    SEMANTIC_ERROR_CODE,
    TYPE_ERROR_CODE,
    CypherSemanticError,
)
from arango_cypher.parser import parse_cypher
from tests.helpers.mapping_fixtures import mapping_bundle_for


def _check(cypher: str) -> None:
    check_semantics(parse_cypher(cypher).tree)


def _rejection(cypher: str) -> CypherSemanticError:
    with pytest.raises(CypherSemanticError) as info:
        _check(cypher)
    return info.value


# -- rejections: (query, openCypher error detail, code) -----------------------

REJECTED = [
    # Variables bound twice (VariableAlreadyBound).
    ("MATCH (p)-[]-() MATCH p = ()-[]-() RETURN p", "VariableAlreadyBound", SEMANTIC_ERROR_CODE),
    ("MATCH ()-[p]-() MATCH p = ()-[]-() RETURN p", "VariableAlreadyBound", SEMANTIC_ERROR_CODE),
    ("MATCH (p)-[]-(), p = ()-[]-() RETURN p", "VariableAlreadyBound", SEMANTIC_ERROR_CODE),
    ("MATCH (a) CREATE (a)", "VariableAlreadyBound", SEMANTIC_ERROR_CODE),
    ("MATCH (a) CREATE (a {name: 'foo'}) RETURN a", "VariableAlreadyBound", SEMANTIC_ERROR_CODE),
    ("CREATE (n:Foo)-[:T1]->(), (n:Bar)-[:T2]->()", "VariableAlreadyBound", SEMANTIC_ERROR_CODE),
    ("MATCH ()-[r]->() CREATE ()-[r]->()", "VariableAlreadyBound", SEMANTIC_ERROR_CODE),
    ("MATCH (a) MERGE (a)", "VariableAlreadyBound", SEMANTIC_ERROR_CODE),
    ("CREATE (a:Foo) MERGE (a)-[r:KNOWS]->(a:Bar)", "VariableAlreadyBound", SEMANTIC_ERROR_CODE),
    ("MATCH (a)-[r]->(b) MERGE (a)-[r]->(b)", "VariableAlreadyBound", SEMANTIC_ERROR_CODE),
    ("WITH [1] AS x UNWIND [2] AS x RETURN x", "VariableAlreadyBound", SEMANTIC_ERROR_CODE),
    # One name, two kinds of value (VariableTypeConflict).
    ("MATCH ()-[r]-() MATCH (r) RETURN r", "VariableTypeConflict", SEMANTIC_ERROR_CODE),
    ("MATCH ()-[r*]-()-[]-(r) RETURN r", "VariableTypeConflict", SEMANTIC_ERROR_CODE),
    ("MATCH r = ()-[]-(), (r) RETURN r", "VariableTypeConflict", SEMANTIC_ERROR_CODE),
    ("MATCH r = ()-[p*]-() MATCH ()-[r]-() RETURN r", "VariableTypeConflict", SEMANTIC_ERROR_CODE),
    ("MATCH (a) MATCH ()-[a]->() RETURN a", "VariableTypeConflict", SEMANTIC_ERROR_CODE),
    ("MATCH (n) WITH [n] AS users MATCH (users)-->(m) RETURN m", "VariableTypeConflict", SEMANTIC_ERROR_CODE),
    ("WITH 1 AS x MATCH (x) RETURN x", "VariableTypeConflict", SEMANTIC_ERROR_CODE),
    # A pattern predicate introducing a variable (UndefinedVariable).
    ("MATCH (n) WHERE (n)-[r]->() RETURN n", "UndefinedVariable", SEMANTIC_ERROR_CODE),
    ("MATCH (n) WHERE (n)-->(m) RETURN n", "UndefinedVariable", SEMANTIC_ERROR_CODE),
    # Values of the wrong type (InvalidArgumentType).
    ("RETURN NOT 0", "InvalidArgumentType", SEMANTIC_ERROR_CODE),
    ("RETURN NOT 'true'", "InvalidArgumentType", SEMANTIC_ERROR_CODE),
    ("RETURN NOT [true]", "InvalidArgumentType", SEMANTIC_ERROR_CODE),
    ("RETURN NOT {a: true}", "InvalidArgumentType", SEMANTIC_ERROR_CODE),
    ("RETURN 123 AND true", "InvalidArgumentType", SEMANTIC_ERROR_CODE),
    ("RETURN true OR 'foo'", "InvalidArgumentType", SEMANTIC_ERROR_CODE),
    ("RETURN true XOR {}", "InvalidArgumentType", SEMANTIC_ERROR_CODE),
    ("MATCH (n) WHERE (n) RETURN n", "InvalidArgumentType", SEMANTIC_ERROR_CODE),
    ("MATCH (n) WHERE 1 RETURN n", "InvalidArgumentType", SEMANTIC_ERROR_CODE),
    ("WITH 123 AS x RETURN x.num", "InvalidArgumentType", SEMANTIC_ERROR_CODE),
    ("WITH 'string' AS x RETURN x.num", "InvalidArgumentType", SEMANTIC_ERROR_CODE),
    ("WITH [123, true] AS x RETURN x.num", "InvalidArgumentType", SEMANTIC_ERROR_CODE),
    (
        "MATCH (n) MATCH r = (n)-[*]->() WHERE r.name = 'apa' RETURN r",
        "InvalidArgumentType",
        SEMANTIC_ERROR_CODE,
    ),
    ("WITH true AS list, 0 AS idx RETURN list[idx]", "InvalidArgumentType", SEMANTIC_ERROR_CODE),
    ("WITH '1' AS list, 0 AS idx RETURN list[idx]", "InvalidArgumentType", SEMANTIC_ERROR_CODE),
    ("WITH [1, 2] AS list, 4.7 AS idx RETURN list[idx]", "InvalidArgumentType", SEMANTIC_ERROR_CODE),
    ("WITH [1, 2] AS list, {x: 3} AS idx RETURN list[idx]", "InvalidArgumentType", SEMANTIC_ERROR_CODE),
    ("RETURN 1 IN 123", "InvalidArgumentType", SEMANTIC_ERROR_CODE),
    ("RETURN 1 IN 'foo'", "InvalidArgumentType", SEMANTIC_ERROR_CODE),
    ("RETURN 1 IN {x: []}", "InvalidArgumentType", SEMANTIC_ERROR_CODE),
    ("RETURN 'a' % 2", "InvalidArgumentType", SEMANTIC_ERROR_CODE),
    ("RETURN true - 1", "InvalidArgumentType", SEMANTIC_ERROR_CODE),
    ("RETURN [1] * 2", "InvalidArgumentType", SEMANTIC_ERROR_CODE),
    ("RETURN true + 1", "InvalidArgumentType", SEMANTIC_ERROR_CODE),
    ("RETURN -'a'", "InvalidArgumentType", SEMANTIC_ERROR_CODE),
    ("RETURN none(x IN ['Clara'] WHERE x % 2 = 0) AS r", "InvalidArgumentType", SEMANTIC_ERROR_CODE),
    ("RETURN all(x IN [false, true] WHERE x % 2 = 0) AS r", "InvalidArgumentType", SEMANTIC_ERROR_CODE),
    ("MATCH (r) RETURN type(r)", "InvalidArgumentType", SEMANTIC_ERROR_CODE),
    ("MATCH (n) RETURN length(n)", "InvalidArgumentType", SEMANTIC_ERROR_CODE),
    ("MATCH ()-[r]->() RETURN length(r)", "InvalidArgumentType", SEMANTIC_ERROR_CODE),
    ("MATCH p = (a)-[*]->(b) RETURN size(p)", "InvalidArgumentType", SEMANTIC_ERROR_CODE),
    ("RETURN properties(1)", "InvalidArgumentType", SEMANTIC_ERROR_CODE),
    ("RETURN properties([true, false])", "InvalidArgumentType", SEMANTIC_ERROR_CODE),
    ("MATCH ()-[r]->() RETURN labels(r)", "InvalidArgumentType", SEMANTIC_ERROR_CODE),
    ("MATCH (n) WITH [n, 1] AS list RETURN labels(list[1]) AS l", "InvalidArgumentType", SEMANTIC_ERROR_CODE),
    ("MATCH (n) RETURN n SKIP 1.5", "InvalidArgumentType", SEMANTIC_ERROR_CODE),
    ("MATCH (n) RETURN n LIMIT 1.7", "InvalidArgumentType", SEMANTIC_ERROR_CODE),
    ("RETURN {a: 1}[0]", "MapElementAccessByNonString", SEMANTIC_ERROR_CODE),
    # SKIP / LIMIT below zero; literals out of range.
    ("MATCH (n) RETURN n SKIP -1", "NegativeIntegerArgument", SEMANTIC_ERROR_CODE),
    ("MATCH (n) RETURN n LIMIT -2", "NegativeIntegerArgument", SEMANTIC_ERROR_CODE),
    ("RETURN 9223372036854775808 AS literal", "IntegerOverflow", SEMANTIC_ERROR_CODE),
    ("RETURN -9223372036854775809 AS literal", "IntegerOverflow", SEMANTIC_ERROR_CODE),
    ("RETURN 0x8000000000000000 AS literal", "IntegerOverflow", SEMANTIC_ERROR_CODE),
    ("RETURN 1.34E999", "FloatingPointOverflow", SEMANTIC_ERROR_CODE),
    # Aggregating expressions mentioning what is not a simple grouping key.
    (
        "MATCH (me)--(you) RETURN me.age + count(you.age)",
        "AmbiguousAggregationExpression",
        SEMANTIC_ERROR_CODE,
    ),
    (
        "MATCH (me)--(you) RETURN me.age + you.age, me.age + you.age + count(*)",
        "AmbiguousAggregationExpression",
        SEMANTIC_ERROR_CODE,
    ),
    (
        "MATCH (me)--(you) WITH me.age + count(you.age) AS agg RETURN *",
        "AmbiguousAggregationExpression",
        SEMANTIC_ERROR_CODE,
    ),
    (
        "MATCH (me)--(you) WITH me.age + you.age AS grp, me.age + you.age + count(*) AS agg RETURN *",
        "AmbiguousAggregationExpression",
        SEMANTIC_ERROR_CODE,
    ),
    (
        "MATCH (me)--(you) RETURN me, you.age + count(*) AS x",
        "AmbiguousAggregationExpression",
        SEMANTIC_ERROR_CODE,
    ),
    ("MATCH (n) RETURN size(n.list) + count(*) AS x", "AmbiguousAggregationExpression", SEMANTIC_ERROR_CODE),
    (
        "MATCH (me)--(you) RETURN me.age + you.age, count(*) AS cnt ORDER BY me.age + you.age + count(*)",
        "AmbiguousAggregationExpression",
        SEMANTIC_ERROR_CODE,
    ),
    (
        "MATCH (me)--(you) WITH me.age + you.age, count(*) AS cnt ORDER BY me.age + you.age + count(*) RETURN *",
        "AmbiguousAggregationExpression",
        SEMANTIC_ERROR_CODE,
    ),
    (
        "MATCH (me)--(you) RETURN count(you.age) AS agg ORDER BY me.age + count(you.age)",
        "UndefinedVariable",
        SEMANTIC_ERROR_CODE,
    ),
    # Projections and UNION.
    ("RETURN 1 AS a, 2 AS a", "ColumnNameConflict", SEMANTIC_ERROR_CODE),
    ("WITH 1 AS a, 2 AS a RETURN a", "ColumnNameConflict", SEMANTIC_ERROR_CODE),
    ("MATCH (a) WITH a, count(*) RETURN a", "NoExpressionAlias", SEMANTIC_ERROR_CODE),
    ("MATCH (a) WITH a.name RETURN 1", "NoExpressionAlias", SEMANTIC_ERROR_CODE),
    ("RETURN 1 AS a UNION RETURN 2 AS b", "DifferentColumnsInUnion", SEMANTIC_ERROR_CODE),
    ("RETURN 1 AS a UNION ALL RETURN 2 AS b", "DifferentColumnsInUnion", SEMANTIC_ERROR_CODE),
    (
        "RETURN 1 AS a UNION RETURN 2 AS a UNION ALL RETURN 3 AS a",
        "InvalidClauseComposition",
        SEMANTIC_ERROR_CODE,
    ),
    # Errors Neo4j raises while it runs the query, shown here by a literal.
    ("RETURN range(2, 8, 0)", "NumberOutOfRange", TYPE_ERROR_CODE),
    ("RETURN range(0, 1, 1.1)", "InvalidArgumentType", TYPE_ERROR_CODE),
    ("RETURN range(true, 1, 1)", "InvalidArgumentType", TYPE_ERROR_CODE),
    ("RETURN range('xyz', 1, 1)", "InvalidArgumentType", TYPE_ERROR_CODE),
    (
        "MATCH p = (n)-[r:T]->() RETURN [x IN [true, []] | toBoolean(x)] AS l",
        "InvalidArgumentValue",
        TYPE_ERROR_CODE,
    ),
    (
        "MATCH p = (n)-[r:T]->() RETURN [x IN [true, 1.0] | toBoolean(x)] AS l",
        "InvalidArgumentValue",
        TYPE_ERROR_CODE,
    ),
    (
        "MATCH p = (n)-[r:T]->() RETURN [x IN [1, n] | toInteger(x)] AS l",
        "InvalidArgumentValue",
        TYPE_ERROR_CODE,
    ),
    (
        "MATCH p = (n)-[r:T]->() RETURN [x IN [1.0, true] | toFloat(x)] AS l",
        "InvalidArgumentValue",
        TYPE_ERROR_CODE,
    ),
    (
        "MATCH p = (n)-[r:T]->() RETURN [x IN [1, '', p] | toString(x)] AS l",
        "InvalidArgumentValue",
        TYPE_ERROR_CODE,
    ),
    ("MATCH (n)-[r:T]->() RETURN [x IN [r, 0] | type(x)] AS l", "InvalidArgumentValue", TYPE_ERROR_CODE),
    # A pattern expression used as a value (UnexpectedSyntax, Neo4j 5).
    ("MATCH (a) RETURN size((a)-->())", "UnexpectedSyntax", SEMANTIC_ERROR_CODE),
    ("MATCH (a), (b) RETURN size((a)-[:REL]->(b))", "UnexpectedSyntax", SEMANTIC_ERROR_CODE),
    ("MATCH (n) RETURN (n)-[]->()", "UnexpectedSyntax", SEMANTIC_ERROR_CODE),
    ("MATCH (n) WITH (n)-[]->() AS x RETURN x", "UnexpectedSyntax", SEMANTIC_ERROR_CODE),
    ("MATCH (n) SET n.prop = head(nodes(head((n)-[:REL]->()))).foo", "UnexpectedSyntax", SEMANTIC_ERROR_CODE),
    ("MATCH (n) WHERE size((n)-->()) > 1 RETURN n", "UnexpectedSyntax", SEMANTIC_ERROR_CODE),
    ("MATCH (n) WHERE (n)-->() = true RETURN n", "UnexpectedSyntax", SEMANTIC_ERROR_CODE),
    ("MATCH (n) RETURN CASE WHEN true THEN (n)-->() END AS c", "UnexpectedSyntax", SEMANTIC_ERROR_CODE),
    ("MATCH (n) RETURN [(n)-->()] AS l", "UnexpectedSyntax", SEMANTIC_ERROR_CODE),
]


@pytest.mark.parametrize(("cypher", "detail", "code"), REJECTED)
def test_invalid_query_is_rejected_with_its_error_class(cypher: str, detail: str, code: str) -> None:
    error = _rejection(cypher)
    assert error.neo4j_error == detail
    assert error.code == code
    assert str(error).startswith(f"{detail}: ")


# -- acceptances: queries Neo4j runs ------------------------------------------

ACCEPTED = [
    # Re-using a bound node or relationship in MATCH is a join.
    "MATCH (a) MATCH (a)-->(b) RETURN b",
    "MATCH ()-[r]->() MATCH ()-[r]->() RETURN r",
    "MATCH (a {name: 'x'}) MATCH (a:Label {age: 1}) RETURN a",
    "MATCH (a)-->(b), (b)-->(c) RETURN a, c",
    "MATCH p = (a)-->(b) RETURN nodes(p), relationships(p), length(p)",
    "MATCH p = shortestPath((a)-[*]-(b)) RETURN p",
    "MATCH ()-[r*]->() UNWIND r AS rel RETURN type(rel)",
    "MATCH (n) WITH collect(n) AS ns UNWIND ns AS m MATCH (m)-->() RETURN m",
    "MATCH (n) WITH head(collect(n)) AS m MATCH (m)-->(o) RETURN o",
    "MATCH (n) WITH n.friend AS f MATCH (f) RETURN f",
    "UNWIND $nodes AS x MATCH (x) RETURN x",
    # CREATE / MERGE may use a bound node bare, as a relationship's end.
    "MATCH (a) CREATE (a)-[:R]->(b) RETURN b",
    "CREATE (a:Foo), (a)-[:R]->(:Bar)",
    "CREATE (a)-[:R]->(a)",
    "CREATE (a:Foo) CREATE (a)-[:R]->(b)",
    "MATCH (a), (b) MERGE (a)-[r:R]->(b) ON CREATE SET r.x = 1 ON MATCH SET r.y = 2",
    "FOREACH (x IN [1, 2] | CREATE (:N {v: x}))",
    # Pattern predicates over bound variables; subqueries and comprehensions.
    "MATCH (n) WHERE (n)-->() RETURN n",
    "MATCH (n), (m) WHERE (n)-[:R]->(m) RETURN n",
    "MATCH (n) WHERE NOT (n)-[:R]->() AND n.x > 1 RETURN n",
    "MATCH (n) WHERE EXISTS { MATCH (n)-->(m) WHERE m.x = 1 } RETURN n",
    "MATCH (n) RETURN COUNT { MATCH (n)-->() } AS c",
    "MATCH (n) RETURN [(n)-->(m) | m.name] AS names",
    "MATCH (n) RETURN [p = (n)-->() | length(p)] AS lengths",
    "MATCH (n) CALL { WITH n MATCH (n)-->(m) RETURN m } RETURN n, m",
    "MATCH (p) CALL { MATCH p = ()-->() RETURN count(p) AS c } RETURN p, c",
    "CALL db.labels() YIELD label RETURN label",
    # Booleans, literals and arithmetic Neo4j accepts.
    "RETURN NOT null, NOT true, NOT (1 = 1)",
    "MATCH (n) WHERE n.flag RETURN n",
    "MATCH (n) WHERE n:Label RETURN n",
    "RETURN -9223372036854775808 AS m, 9223372036854775807 AS n, 0x7FFFFFFFFFFFFFFF AS h",
    "RETURN 1.5e300 AS big, -0.0 AS z",
    "RETURN 'a' + 1, [1] + 2, 1 + 2.5, null + 1, 'a' + 'b'",
    "RETURN 2 * 3.5, 10 % 3, 2 ^ 8, -(1), null % 2",
    "RETURN date('2020-01-01').year, duration('P1D') * 2",
    "WITH {a: 1} AS m RETURN m.a, m['a']",
    "WITH [1, 2, 3] AS l RETURN l[0], l[-1], l[1..2], 2 IN l",
    "RETURN 3 IN [[1, 2, 3]][0] AS r",
    "WITH [[1, 2, 3]] AS list RETURN 3 IN list[0] AS r",
    "MATCH (n) WITH n.list AS l RETURN l[0], l.x",
    "MATCH (n) RETURN n['name'], n[$key]",
    "RETURN 1 IN null, null IN [1]",
    # Functions over values of the right kind.
    "MATCH (a)-[r]->(b) RETURN type(r), startNode(r), endNode(r), labels(a), properties(r), keys(a)",
    "RETURN size([1, 2]), size('abc'), length('abc')",
    "RETURN range(0, 10, 2), range(0, $n), range(10, 0, -1)",
    "RETURN [x IN [1, 2] | toString(x)], [x IN ['1', 2.5] | toInteger(x)]",
    "RETURN [x IN [1, 'a'] WHERE x = 1 | x % 2] AS filtered",
    "RETURN all(x IN [1, 2] WHERE x % 2 = 0), any(x IN [] WHERE x)",
    "RETURN reduce(acc = 0, x IN [1, 2] | acc + x) AS total",
    "RETURN toBoolean(1), toBoolean('true'), toInteger(true), toFloat('1.5'), toString(1.5)",
    # Aggregating expressions over simple grouping keys, literals, parameters.
    "MATCH (person) RETURN $age + avg(person.age) - 1000",
    "MATCH (person) WITH $age + avg(person.age) - 1000 AS agg RETURN *",
    "MATCH (me)--(you) WITH me.age AS age, you RETURN age, age + count(you.age)",
    "MATCH (me)--(you) RETURN me.age, me.age + count(you.age)",
    "MATCH (me)--(you) WITH me.age AS age, me.age + count(you.age) AS agg RETURN *",
    "MATCH (n) RETURN n, count(n) + n.prop AS x",
    "MATCH (n) RETURN n.x, n.x + count(*) AS y",
    "MATCH (n) RETURN n.a.b, n.a.b + count(*) AS y",
    "MATCH (n) WITH *, count(*) AS c RETURN c",
    "MATCH (me)-[r1]->()<-[r2]-(you) RETURN me, you, sum(r1.times / (1 + r2.times)) AS s",
    "MATCH (n) RETURN n.k AS k, [x IN n.list | x] + collect(n.v) AS mixed",
    "MATCH (me)--(you) RETURN me.age AS age, count(you.age) AS cnt ORDER BY age, age + count(you.age)",
    "MATCH (me)--(you) RETURN me.age AS age, count(you.age) AS cnt ORDER BY me.age + count(you.age)",
    "MATCH (me)--(you) WITH me.age AS age, count(you.age) AS cnt ORDER BY cnt + count(*) RETURN age",
    "MATCH (person) RETURN avg(person.age) AS a ORDER BY $age + avg(person.age) - 1000",
    "MATCH (n) RETURN n.x + 1 AS k, count(*) AS c ORDER BY k",
    # Projections.
    "MATCH (n) WITH n, count(*) AS c RETURN n, c",
    "MATCH (n) WITH * RETURN n",
    "MATCH (n) RETURN n.name, n.age",
    "WITH 1 AS a RETURN a AS b, a AS c",
    "MATCH (n) RETURN n SKIP 1 LIMIT 10",
    "MATCH (n) RETURN n SKIP $s LIMIT $l",
    "UNWIND [1, 2] AS x RETURN x ORDER BY x DESC",
    "RETURN 1 AS a UNION RETURN 2 AS a",
    "RETURN 1 AS a, 2 AS b UNION ALL RETURN 3 AS b, 4 AS a",
    # Columns named by their expression text are not compared (golden C051).
    "MATCH (n) RETURN n.id, n.id",
    "MATCH (n) RETURN n.id UNION MATCH (m) RETURN m.id",
    "MATCH (n) CALL { WITH n RETURN n.x, n AS m } MATCH (m)-->() RETURN m",
    "MATCH (n) DELETE n",
    "MATCH (n) SET n.x = 1, n:Label REMOVE n.y",
    # Pattern expressions as predicates, and their replacements as values.
    "MATCH (n) WHERE (n)-->() RETURN n",
    "MATCH (n) WHERE NOT (n)-->() AND n.x > 1 OR (n)<--() RETURN n",
    "MATCH (n) WHERE ((n)-->()) RETURN n",
    "MATCH (n) RETURN exists((n)-->()) AS e",
    "MATCH (n) RETURN CASE WHEN (n)-->() THEN 1 ELSE 0 END AS c",
    "MATCH (n) RETURN [x IN [n] WHERE (x)-->()] AS l",
    "MATCH (n) RETURN COUNT { (n)-->() } AS c, EXISTS { (n)-->() } AS e",
    "MATCH (n) RETURN [p = (n)-->() | p] AS paths",
]


@pytest.mark.parametrize("cypher", ACCEPTED)
def test_valid_query_passes(cypher: str) -> None:
    _check(cypher)


# -- the hook in the translator -----------------------------------------------


def test_translate_raises_the_semantic_error_not_unsupported() -> None:
    with pytest.raises(CoreError) as info:
        translate("MATCH ()-[r]-() MATCH (r) RETURN r", mapping=mapping_bundle_for("lpg"))
    assert isinstance(info.value, CypherSemanticError)
    assert info.value.code == SEMANTIC_ERROR_CODE
    assert "VariableTypeConflict" in str(info.value)


def test_pattern_expression_message_names_the_replacements() -> None:
    message = str(_rejection("MATCH (d) RETURN size((d)--()) AS degree"))
    assert message.startswith("UnexpectedSyntax")
    assert "COUNT { (d)--() }" in message and "EXISTS { (d)--() }" in message


def test_count_subquery_replacement_translates() -> None:
    out = translate("MATCH (d:User) RETURN COUNT { (d)--() } AS degree", mapping=mapping_bundle_for("lpg"))
    assert "LENGTH(FOR" in out.aql


def test_translate_still_translates_a_valid_query() -> None:
    out = translate("MATCH (a)-->(b) MATCH (a)-->(c) RETURN b, c", mapping=mapping_bundle_for("lpg"))
    assert "RETURN" in out.aql


def test_deeply_nested_literals_do_not_exhaust_the_stack() -> None:
    _check("RETURN " + "[" * 60 + "]" * 60 + " AS deep")
    _check("RETURN " + "{a: " * 60 + "1" + "}" * 60 + " AS deep")


def test_error_codes_are_restricted_to_the_two_semantic_codes() -> None:
    with pytest.raises(ValueError):
        CypherSemanticError("VariableAlreadyBound", "x", code="UNSUPPORTED")
