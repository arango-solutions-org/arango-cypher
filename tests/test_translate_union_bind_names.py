"""UNION bind-variable naming: what the golden cases cannot express.

Each UNION branch is translated on its own and conflicting internal names are
renamed afterwards (see ``_merge_bind_vars``). A *user* parameter must never be
renamed — that would rebind the user's own reference to an internal value, a
silent wrong result — so a name clash is refused with a clear error. Internal
names avoid the user's parameters when they are picked, so the refusal is the
guard behind that.
"""

from __future__ import annotations

import pytest
from arango_query_core import CoreError

from arango_cypher._translate_v0.bind_names import (
    _merge_bind_vars,
    _refuse_overwritten_params,
    _rename_bind_references,
)
from arango_cypher.api import translate
from tests.helpers.mapping_fixtures import mapping_bundle_for

CLASH = (
    "MATCH (n:User) WHERE n.name = $typeValue RETURN n.name AS x "
    "UNION MATCH (n:Doc) WHERE n.name = $typeValue RETURN n.name AS x"
)


@pytest.mark.parametrize("fixture", ["lpg", "hybrid"])
def test_a_user_parameter_named_like_an_internal_one_keeps_its_value(fixture: str) -> None:
    # The label tests pick bind names of their own (``_node_label_filters``),
    # so the user's $typeValue is neither overwritten nor renamed.
    t = translate(CLASH, mapping=mapping_bundle_for(fixture), params={"typeValue": "vdoc"})
    assert t.bind_vars["typeValue"] == "vdoc"
    assert t.aql.count("n.name == @typeValue)") == 2
    assert "== @typeValue\n" not in t.aql  # no type test reads the user's parameter
    assert {v for k, v in t.bind_vars.items() if k.startswith("typeValue2")} <= {"User", "Doc"}


def test_an_internal_bind_that_overwrote_a_user_parameter_is_refused() -> None:
    with pytest.raises(CoreError, match=r"Parameter \$typeValue has the same name as a bind variable"):
        _refuse_overwritten_params({"typeValue": "vdoc"}, {"typeValue": "User"})


def test_a_union_branch_binding_a_user_parameter_name_is_refused() -> None:
    with pytest.raises(CoreError, match=r"Parameter \$typeValue has the same name as a bind variable"):
        _merge_bind_vars(
            {"typeValue": "vdoc"},
            {"typeValue": "Doc"},
            "FILTER n.type == @typeValue",
            branch_index=1,
            user_params=frozenset({"typeValue"}),
        )


def test_a_shared_user_parameter_with_one_value_stays_one_bind_variable() -> None:
    q = "MATCH (n:User) WHERE n.name = $q RETURN n.name AS x UNION MATCH (n:Person) WHERE n.name = $q RETURN n.name AS x"
    t = translate(q, mapping=mapping_bundle_for("pg"), params={"q": "alice"})
    assert t.bind_vars["q"] == "alice"
    assert t.aql.count("n.name == @q") == 2
    assert not any(k.startswith("q_u") for k in t.bind_vars)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("FOR n IN @@collection RETURN n", "FOR n IN @@collection_u1 RETURN n"),
        ("FILTER n.name == 'a @@collection b'", "FILTER n.name == 'a @@collection b'"),
        ('FILTER n.name == "x @@collection"', 'FILTER n.name == "x @@collection"'),
        ("FILTER n.name == 'it\\'s @@collection'", "FILTER n.name == 'it\\'s @@collection'"),
        ("RETURN n.`@@collection`", "RETURN n.`@@collection`"),
        ("RETURN n.´@@collection´", "RETURN n.´@@collection´"),
        ("FOR n IN @@collection2 RETURN n", "FOR n IN @@collection2 RETURN n"),
        (
            "FOR n IN @@collection FILTER n.a == 'q' RETURN 'x'",
            "FOR n IN @@collection_u1 FILTER n.a == 'q' RETURN 'x'",
        ),
    ],
)
def test_renaming_skips_quoted_text_and_other_names(text: str, expected: str) -> None:
    assert _rename_bind_references(text, "@collection", "@collection_u1") == expected


def test_a_plain_key_does_not_match_inside_a_collection_reference() -> None:
    assert _rename_bind_references(
        "FOR n IN @@collection FILTER n.c == @collection", "collection", "c_u1"
    ) == ("FOR n IN @@collection FILTER n.c == @c_u1")


def test_an_escaped_backtick_does_not_end_a_quoted_name() -> None:
    # AQL reads `a\` @@collection` as one name; only the reference after it is real.
    text = "RETURN n.`a\\` @@collection`, @@collection"
    assert _rename_bind_references(text, "@collection", "@collection_u1") == (
        "RETURN n.`a\\` @@collection`, @@collection_u1"
    )


def test_an_unterminated_quote_runs_to_the_end_in_one_pass() -> None:
    # Every quote below starts a string that never closes. Before the closing
    # quote was optional, each one restarted a scan to the end of the text:
    # quadratic in its length, about 10^8 steps here.
    tail = "\\' @@collection" * 10_000
    assert _rename_bind_references("@@collection " + tail, "@collection", "@collection_u1") == (
        "@@collection_u1 " + tail
    )


def test_a_branch_picks_its_own_names_around_a_user_parameter() -> None:
    q = (
        "MATCH (n:User)-[r]->(m) RETURN n.name AS x "
        "UNION MATCH (n:Doc) WHERE n.name = $vCollection RETURN n.name AS x"
    )
    t = translate(q, mapping=mapping_bundle_for("lpg"), params={"vCollection": "alice"})
    assert t.bind_vars["vCollection"] == "alice"
    assert "IS_SAME_COLLECTION(@vCollection2, m)" in t.aql
    assert t.bind_vars["vCollection2"] == "vertices"


class TestSingleQueryParameterClash:
    """The emitters bind some values under fixed names (``@collection``);
    outside a UNION a user parameter of the same name used to be overwritten
    in place. A label's type test picks a name of its own."""

    Q = "MATCH (n:User) WHERE n.name = $typeValue RETURN n.name AS x"

    def test_a_parameter_named_like_a_type_bind_keeps_its_value(self) -> None:
        t = translate(self.Q, mapping=mapping_bundle_for("lpg"), params={"typeValue": "alice"})
        assert t.bind_vars["typeValue"] == "alice"
        assert t.bind_vars["typeValue2"] == "User"
        assert "n[@typeField] == @typeValue2" in t.aql

    def test_a_parameter_holding_the_same_value_is_harmless(self) -> None:
        t = translate(self.Q, mapping=mapping_bundle_for("lpg"), params={"typeValue": "User"})
        assert t.bind_vars["typeValue"] == "User"

    def test_a_collection_parameter_is_named_without_a_dollar(self) -> None:
        with pytest.raises(CoreError, match=r"Parameter '@collection' has the same name"):
            translate(
                "MATCH (n:User) RETURN n.name AS x",
                mapping=mapping_bundle_for("pg"),
                params={"@collection": "docs"},
            )
