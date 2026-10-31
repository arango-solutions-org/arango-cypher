"""Table-backed procedures and the procedure invocation protocol
(``arango_cypher.procedures``, ``_translate_v0/calls.py``).

What the AQL returns is checked on ArangoDB in
``tests/integration/test_procedures_live.py`` and by the TCK's
``clauses/call`` scenarios.
"""

from __future__ import annotations

from typing import Any

import pytest
from arango_query_core import CoreError, ExtensionPolicy, ExtensionRegistry

from arango_cypher import translate
from arango_cypher._translate_v0.semantic_errors import CypherSemanticError
from arango_cypher.procedures import (
    CompiledProcedure,
    ProcedureField,
    ProcedureInvocation,
    ProcedureSignature,
    TableProcedure,
    parse_signature,
    register_procedure,
    register_table_procedure,
)
from tests.helpers.mapping_fixtures import mapping_bundle_for

MAPPING = mapping_bundle_for("lpg")
CITIES = "test.my.proc(name :: STRING?, id :: INTEGER?) :: (city :: STRING?, country_code :: INTEGER?)"
ROWS = [
    {"name": "Stefan", "id": 1, "city": "Berlin", "country_code": 49},
    {"name": "Stefan", "id": 2, "city": "München", "country_code": 49},
    {"name": "Petra", "id": 1, "city": "London", "country_code": 44},
]


@pytest.fixture()
def registry() -> ExtensionRegistry:
    r = ExtensionRegistry(policy=ExtensionPolicy(enabled=True))
    register_table_procedure(r, CITIES, ROWS)
    register_table_procedure(r, "test.doNothing() :: ()", [])
    register_table_procedure(r, "test.labels() :: (label :: STRING?)", [{"label": "A"}, {"label": "B"}])
    return r


def _translate(query: str, registry: ExtensionRegistry, **kw: Any) -> Any:
    return translate(query, mapping=MAPPING, registry=registry, **kw)


def _rejected(query: str, registry: ExtensionRegistry, detail: str, **kw: Any) -> None:
    with pytest.raises(CypherSemanticError) as info:
        _translate(query, registry, **kw)
    assert info.value.neo4j_error == detail, str(info.value)


# --- signatures ------------------------------------------------------------------------


def test_parse_signature() -> None:
    name, sig = parse_signature(CITIES)
    assert name == "test.my.proc"
    assert sig.inputs == (ProcedureField("name", "STRING"), ProcedureField("id", "INTEGER"))
    assert sig.output_names == ("city", "country_code")
    assert not sig.void
    assert parse_signature("test.doNothing() :: ()")[1].void
    _, listy = parse_signature("p(xs :: LIST? OF STRING?, n :: INTEGER) :: (a :: ANY?) :")
    assert listy.inputs[0].type == "LIST OF STRING" and not listy.inputs[1].nullable


@pytest.mark.parametrize("text", ["p", "p(x INTEGER) :: ()", "p(x :: WIDGET?) :: ()", "p() :: (a)"])
def test_bad_signatures(text: str) -> None:
    with pytest.raises(ValueError):
        parse_signature(text)


@pytest.mark.parametrize(
    ("field", "static", "accepted"),
    [
        (ProcedureField("x", "INTEGER"), "INTEGER", True),
        (ProcedureField("x", "INTEGER"), "BOOLEAN", False),
        (ProcedureField("x", "INTEGER"), "FLOAT", False),
        (ProcedureField("x", "FLOAT"), "INTEGER", True),
        (ProcedureField("x", "NUMBER"), "FLOAT", True),
        (ProcedureField("x", "STRING"), None, True),
        (ProcedureField("x", "STRING"), "NULL", True),
        (ProcedureField("x", "STRING", nullable=False), "NULL", False),
        (ProcedureField("x", "ANY"), "MAP", True),
    ],
)
def test_argument_types(field: ProcedureField, static: str | None, accepted: bool) -> None:
    assert field.accepts(static) is accepted


def test_table_rows_must_match_the_signature() -> None:
    _, sig = parse_signature(CITIES)
    with pytest.raises(ValueError, match="missing"):
        TableProcedure(sig, [{"name": "x", "id": 1, "city": "y"}])
    with pytest.raises(ValueError, match="unknown"):
        TableProcedure(sig, [{**ROWS[0], "extra": 1}])
    with pytest.raises(ValueError, match="void"):
        TableProcedure(ProcedureSignature(), [{}])
    with pytest.raises(ValueError, match="must differ"):
        TableProcedure(ProcedureSignature((ProcedureField("a"),), (ProcedureField("a"),)), [])


# --- translation -------------------------------------------------------------------------


def test_standalone_call_returns_every_output(registry: ExtensionRegistry) -> None:
    out = _translate("CALL test.my.proc('Stefan', 1)", registry)
    assert out.bind_vars["procRows0"] == ROWS
    assert (
        'FILTER _proc_row["name"] == (@' in out.aql or "FILTER _proc_row[\"name\"] == ('Stefan')" in out.aql
    )
    assert out.aql.splitlines()[-1].strip() == "RETURN {city: city, country_code: country_code}"


def test_standalone_yield_star_and_rename(registry: ExtensionRegistry) -> None:
    star = _translate("CALL test.my.proc('Stefan', 1) YIELD *", registry).aql
    assert star.splitlines()[-1].strip() == "RETURN {city: city, country_code: country_code}"
    renamed = _translate("CALL test.my.proc('Stefan', 1) YIELD city AS town", registry).aql
    assert (
        'LET town = _call_row["city"]' in renamed
        and renamed.splitlines()[-1].strip() == "RETURN {town: town}"
    )


def test_implicit_arguments_come_from_parameters(registry: ExtensionRegistry) -> None:
    out = _translate("CALL test.my.proc", registry, params={"name": "Stefan", "id": 1})
    assert '_proc_row["name"] == (@name)' in out.aql and '_proc_row["id"] == (@id)' in out.aql
    _rejected("CALL test.my.proc", registry, "MissingParameter", params={"name": "Stefan"})


def test_void_procedure(registry: ExtensionRegistry) -> None:
    assert _translate("CALL test.doNothing()", registry).aql.startswith("FOR _call_row IN []")
    in_query = _translate("MATCH (n:User) CALL test.doNothing() RETURN n", registry).aql
    assert "_call_row" not in in_query and "procRows" not in in_query
    _rejected("MATCH (n:User) CALL test.doNothing() YIELD x RETURN n", registry, "UndefinedVariable")


def test_in_query_call_with_where(registry: ExtensionRegistry) -> None:
    out = _translate(
        "CALL test.my.proc('Stefan', 1) YIELD city, country_code WHERE country_code > 40 RETURN city",
        registry,
    )
    assert "LET city = " in out.aql and "FILTER " in out.aql


def test_call_after_with_binds_fresh_names(registry: ExtensionRegistry) -> None:
    """A name a WITH dropped can be yielded again (TCK Call6 [1])."""
    aql = _translate(
        "CALL test.labels() YIELD label WITH count(*) AS c CALL test.labels() YIELD label RETURN *",
        registry,
    ).aql
    assert "LET label = " in aql and "LET label_1 = " in aql


@pytest.mark.parametrize(
    ("query", "detail"),
    [
        ("CALL test.my.proc('Dobby')", "InvalidNumberOfArguments"),
        ("CALL test.my.proc('Dobby', 1, 2) YIELD city RETURN city", "InvalidNumberOfArguments"),
        ("CALL test.my.proc(true, 1)", "InvalidArgumentType"),
        ("CALL test.my.proc('a', 1.5) YIELD city RETURN city", "InvalidArgumentType"),
        ("CALL test.missing()", "ProcedureNotFound"),
        ("CALL test.missing() YIELD out RETURN out", "ProcedureNotFound"),
        ("CALL test.my.proc('Stefan', 1) YIELD *  RETURN city", "UnexpectedSyntax"),
        ("CALL test.my.proc('Stefan', 1) YIELD town RETURN town", "UndefinedVariable"),
        # An in-query call of a procedure with outputs must YIELD them (Neo4j
        # reports the RETURN of an unbound name as UndefinedVariable).
        ("CALL test.my.proc('Stefan', 1) RETURN city", "UnexpectedSyntax"),
        ("WITH 'Hi' AS label CALL test.labels() YIELD label RETURN *", "VariableAlreadyBound"),
        ("CALL test.my.proc('a', 1) YIELD city, country_code AS city RETURN city", "VariableAlreadyBound"),
        ("MATCH (n:User) CALL test.my.proc(count(n), 1) YIELD city RETURN city", "InvalidAggregation"),
    ],
)
def test_neo4j_rejections(registry: ExtensionRegistry, query: str, detail: str) -> None:
    _rejected(query, registry, detail)


def test_without_a_registry_a_procedure_is_unsupported() -> None:
    with pytest.raises(CoreError) as info:
        translate("CALL test.my.proc('Stefan', 1)", mapping=MAPPING)
    assert info.value.code == "UNSUPPORTED"


def test_a_compiler_must_follow_the_protocol() -> None:
    registry = ExtensionRegistry(policy=ExtensionPolicy(enabled=True))
    registry.register_procedure("test.bad", lambda invocation, bind_vars: "FOR x IN [] RETURN x")
    with pytest.raises(CoreError) as info:
        _translate("CALL test.bad()", registry)
    assert info.value.code == "INVALID_EXTENSION"


def test_custom_compiler_receives_the_invocation() -> None:
    seen: list[ProcedureInvocation] = []

    def compiler(invocation: ProcedureInvocation, bind_vars: dict[str, Any]) -> CompiledProcedure:
        seen.append(invocation)
        return CompiledProcedure(
            aql=f"[{{x: {invocation.args[0] if invocation.args else 0}}}]", columns=("x",)
        )

    registry = ExtensionRegistry(policy=ExtensionPolicy(enabled=True))
    register_procedure(registry, "my.echo", compiler)
    out = _translate("CALL my.echo(41 + 1) YIELD x RETURN x", registry)
    assert seen[0].name == "my.echo" and seen[0].arg_types == ("INTEGER",) and not seen[0].standalone
    assert "LET x = " in out.aql


def test_arango_namespace_is_reserved() -> None:
    registry = ExtensionRegistry(policy=ExtensionPolicy(enabled=True))
    with pytest.raises(CoreError):
        register_table_procedure(registry, "arango.mine() :: (a :: INTEGER?)", [])
