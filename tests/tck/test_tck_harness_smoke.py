from __future__ import annotations

import os
import textwrap
from pathlib import Path

import pytest

from tests.tck import runner
from tests.tck.gherkin import parse_feature
from tests.tck.normalize import (
    normalize_actual_value,
    normalize_expected_value,
    parse_param_value,
    results_match,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_feature(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "test.feature"
    p.write_text(textwrap.dedent(content), encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# gherkin.py — basic parsing (existing test, expanded)
# ---------------------------------------------------------------------------


def test_tck_feature_parser_smoke():
    p = Path(__file__).resolve().parent / "features" / "sample.feature"
    feat = parse_feature(p)
    assert feat.name
    assert feat.scenarios
    assert feat.scenarios[0].steps


# ---------------------------------------------------------------------------
# gherkin.py — Scenario Outline / Examples expansion
# ---------------------------------------------------------------------------


class TestScenarioOutlineExpansion:
    def test_outline_expands_to_correct_count(self, tmp_path: Path):
        p = _write_feature(
            tmp_path,
            """\
        Feature: Outline test

          Scenario Outline: Return literal
            When executing query:
              \"\"\"
              RETURN <literal>
              \"\"\"
            Then the result should be:
              | value      |
              | <expected> |

          Examples:
            | literal | expected |
            | 1       | 1        |
            | 'foo'   | 'foo'    |
            | true    | true     |
        """,
        )
        feat = parse_feature(p)
        assert len(feat.scenarios) == 3

    def test_outline_substitutes_docstring(self, tmp_path: Path):
        p = _write_feature(
            tmp_path,
            """\
        Feature: Sub test

          Scenario Outline: Query sub
            When executing query:
              \"\"\"
              RETURN <val>
              \"\"\"
            Then the result should be empty

          Examples:
            | val  |
            | 42   |
            | 'hi' |
        """,
        )
        feat = parse_feature(p)
        assert len(feat.scenarios) == 2
        assert "42" in feat.scenarios[0].steps[0].doc_string
        assert "'hi'" in feat.scenarios[1].steps[0].doc_string

    def test_outline_substitutes_table_cells(self, tmp_path: Path):
        p = _write_feature(
            tmp_path,
            """\
        Feature: Table sub test

          Scenario Outline: Table sub
            When executing query:
              \"\"\"
              RETURN 1
              \"\"\"
            Then the result should be:
              | col      |
              | <answer> |

          Examples:
            | answer |
            | 42     |
            | 99     |
        """,
        )
        feat = parse_feature(p)
        assert len(feat.scenarios) == 2
        then_step_0 = feat.scenarios[0].steps[1]
        assert then_step_0.data_table is not None
        assert then_step_0.data_table[1][0] == "42"
        then_step_1 = feat.scenarios[1].steps[1]
        assert then_step_1.data_table is not None
        assert then_step_1.data_table[1][0] == "99"

    def test_outline_name_includes_params(self, tmp_path: Path):
        p = _write_feature(
            tmp_path,
            """\
        Feature: Named outline

          Scenario Outline: Checking <x>
            When executing query:
              \"\"\"
              RETURN <x>
              \"\"\"
            Then the result should be empty

          Examples:
            | x |
            | 1 |
            | 2 |
        """,
        )
        feat = parse_feature(p)
        assert "x=1" in feat.scenarios[0].name
        assert "x=2" in feat.scenarios[1].name

    def test_multiple_examples_tables(self, tmp_path: Path):
        p = _write_feature(
            tmp_path,
            """\
        Feature: Multi examples

          Scenario Outline: Multi
            When executing query:
              \"\"\"
              RETURN <v>
              \"\"\"
            Then the result should be empty

          Examples:
            | v |
            | 1 |

          Examples:
            | v |
            | 2 |
            | 3 |
        """,
        )
        feat = parse_feature(p)
        assert len(feat.scenarios) == 3

    def test_mixed_scenarios_and_outlines(self, tmp_path: Path):
        p = _write_feature(
            tmp_path,
            """\
        Feature: Mixed

          Scenario: Plain
            When executing query:
              \"\"\"
              RETURN 1
              \"\"\"
            Then the result should be empty

          Scenario Outline: Parameterized
            When executing query:
              \"\"\"
              RETURN <v>
              \"\"\"
            Then the result should be empty

          Examples:
            | v |
            | a |
            | b |
        """,
        )
        feat = parse_feature(p)
        assert len(feat.scenarios) == 3
        assert feat.scenarios[0].name == "Plain"
        assert "v=a" in feat.scenarios[1].name

    def test_outline_without_examples_produces_nothing(self, tmp_path: Path):
        p = _write_feature(
            tmp_path,
            """\
        Feature: Empty outline

          Scenario Outline: No examples
            When executing query:
              \"\"\"
              RETURN <v>
              \"\"\"
            Then the result should be empty
        """,
        )
        feat = parse_feature(p)
        assert len(feat.scenarios) == 0


class TestBackground:
    def test_background_steps_prefix_every_scenario(self, tmp_path: Path):
        p = _write_feature(
            tmp_path,
            """\
        Feature: Background test

          Background:
            Given an empty graph
            And having executed:
              \"\"\"
              CREATE (:A {name: 'n0'})
              \"\"\"

          Scenario: [1] First
            When executing query:
              \"\"\"
              MATCH (a:A) RETURN a.name
              \"\"\"
            Then the result should be empty

          Scenario Outline: [2] Second
            When executing query:
              \"\"\"
              RETURN <v>
              \"\"\"
            Then the result should be empty

          Examples:
            | v |
            | 1 |
        """,
        )
        feat = parse_feature(p)
        assert [s.name for s in feat.scenarios] == ["[1] First", "[2] Second [v=1]"]
        for sc in feat.scenarios:
            assert [st.text for st in sc.steps[:2]] == ["an empty graph", "having executed:"]
            assert (sc.steps[1].doc_string or "").strip() == "CREATE (:A {name: 'n0'})"
            assert sc.steps[2].keyword == "When"


# ---------------------------------------------------------------------------
# normalize.py — normalize_expected_value
# ---------------------------------------------------------------------------


class TestNormalizeExpectedValue:
    def test_null(self):
        assert normalize_expected_value("null") is None

    def test_true(self):
        assert normalize_expected_value("true") is True

    def test_false(self):
        assert normalize_expected_value("false") is False

    def test_integer(self):
        assert normalize_expected_value("42") == 42
        assert isinstance(normalize_expected_value("42"), int)

    def test_negative_integer(self):
        assert normalize_expected_value("-7") == -7

    def test_float(self):
        assert normalize_expected_value("3.14") == 3.14

    def test_single_quoted_string(self):
        assert normalize_expected_value("'hello'") == "hello"

    def test_double_quoted_string(self):
        assert normalize_expected_value('"world"') == "world"

    def test_empty_list(self):
        assert normalize_expected_value("[]") == []

    def test_list_of_ints(self):
        assert normalize_expected_value("[1, 2, 3]") == [1, 2, 3]

    def test_nested_list(self):
        assert normalize_expected_value("[[1, 2], [3]]") == [[1, 2], [3]]

    def test_empty_map(self):
        assert normalize_expected_value("{}") == {}

    def test_map(self):
        result = normalize_expected_value("{name: 'Alice', age: 30}")
        assert result == {"name": "Alice", "age": 30}

    def test_node_literal_with_label_and_props(self):
        result = normalize_expected_value("(:Person {name: 'Bob'})")
        assert result["_labels"] == ["Person"]
        assert result["name"] == "Bob"

    def test_node_literal_label_only(self):
        result = normalize_expected_value("(:Animal)")
        assert result["_labels"] == ["Animal"]

    def test_rel_literal(self):
        result = normalize_expected_value("[:KNOWS {since: 2020}]")
        assert result["_type"] == "KNOWS"
        assert result["since"] == 2020

    def test_bare_string_passthrough(self):
        assert normalize_expected_value("hello") == "hello"

    def test_whitespace_stripped(self):
        assert normalize_expected_value("  42  ") == 42

    def test_list_with_strings(self):
        assert normalize_expected_value("['a', 'b']") == ["a", "b"]


# ---------------------------------------------------------------------------
# normalize.py — normalize_actual_value
# ---------------------------------------------------------------------------


class TestNormalizeActualValue:
    def test_strips_meta_keys(self):
        doc = {"_id": "x/1", "_key": "1", "_rev": "abc", "name": "Alice"}
        # A stored document (it has an ``_id``) is a node: unlabeled here.
        assert normalize_actual_value(doc) == {"name": "Alice", "_labels": []}

    def test_float_to_int(self):
        assert normalize_actual_value(3.0) == 3
        assert isinstance(normalize_actual_value(3.0), int)

    def test_nested_dict(self):
        doc = {"addr": {"_rev": "z", "city": "NYC"}}
        result = normalize_actual_value(doc)
        assert result == {"addr": {"city": "NYC"}}

    def test_list_of_dicts(self):
        rows = [{"v": 1, "_key": "a"}, {"v": 2, "_key": "b"}]
        result = normalize_actual_value(rows)
        assert result == [{"v": 1}, {"v": 2}]

    def test_stored_documents_normalize_by_shape(self):
        """Nodes and edges inside lists/maps are recognized by their document shape."""
        node = {"_id": "v/1", "_key": "1", "_rev": "r", "type": "A", "name": "a"}
        edge = {"_id": "e/1", "_key": "1", "_rev": "r", "_from": "v/1", "_to": "v/2", "type": "T"}
        assert normalize_actual_value([node, edge]) == [
            {"_labels": ["A"], "name": "a"},
            {"_type": "T"},
        ]
        assert normalize_actual_value({"n": node}) == {"n": {"_labels": ["A"], "name": "a"}}

    def test_multi_label_node_uses_labels_array(self):
        node = {"_id": "v/1", "type": "A", "labels": ["B", "A"]}
        assert normalize_actual_value(node, as_node=True) == {"_labels": ["A", "B"]}

    def test_path_records_relationship_direction(self):
        path = {
            "nodes": [{"_id": "v/1", "type": "A"}, {"_id": "v/2"}, {"_id": "v/3", "type": "C"}],
            "edges": [
                {"_id": "e/1", "_from": "v/1", "_to": "v/2", "type": "T", "num": 1},
                {"_id": "e/2", "_from": "v/3", "_to": "v/2", "type": "U"},
            ],
        }
        assert normalize_actual_value(path) == normalize_expected_value("<(:A)-[:T {num: 1}]->()<-[:U]-(:C)>")


class TestNormalizeExpectedGraphLiterals:
    def test_multi_label_node(self):
        assert normalize_expected_value("(:A:B:C)") == {"_labels": ["A", "B", "C"]}
        assert normalize_expected_value("(:B :A {name: 'x'})") == {"_labels": ["A", "B"], "name": "x"}

    def test_node_with_nested_map_property(self):
        assert normalize_expected_value("({m: {a: 1}})") == {"_labels": [], "m": {"a": 1}}

    def test_zero_length_path(self):
        assert normalize_expected_value("<({num: 1})>") == {"_path": [{"_labels": [], "num": 1}]}

    def test_path_with_both_directions(self):
        assert normalize_expected_value("<(:A)<-[:T1]-(:B)-[:T2 {k: 'v'}]->()>") == {
            "_path": [
                {"_labels": ["A"]},
                {"_type": "T1", "_dir": "<-"},
                {"_labels": ["B"]},
                {"_type": "T2", "k": "v", "_dir": "->"},
                {"_labels": []},
            ]
        }

    def test_list_of_paths(self):
        assert normalize_expected_value("[<(:A)-[:T]->(:B)>]") == [
            {"_path": [{"_labels": ["A"]}, {"_type": "T", "_dir": "->"}, {"_labels": ["B"]}]}
        ]

    def test_malformed_path_stays_a_string(self):
        assert normalize_expected_value("<(:A)-[:T]-(:B)>") == "<(:A)-[:T]-(:B)>"

    def test_preserves_real_float(self):
        assert normalize_actual_value(3.5) == 3.5

    def test_preserves_none(self):
        assert normalize_actual_value(None) is None

    def test_preserves_string(self):
        assert normalize_actual_value("hello") == "hello"

    def test_preserves_bool(self):
        assert normalize_actual_value(True) is True


# ---------------------------------------------------------------------------
# normalize.py — results_match
# ---------------------------------------------------------------------------


class TestResultsMatch:
    def test_matching_rows(self):
        actual = [{"x": 1}, {"x": 2}]
        expected = [{"x": "1"}, {"x": "2"}]
        match, _ = results_match(actual, expected)
        assert match

    def test_row_count_mismatch(self):
        actual = [{"x": 1}]
        expected = [{"x": "1"}, {"x": "2"}]
        match, reason = results_match(actual, expected)
        assert not match
        assert "row count" in reason

    def test_unordered_match(self):
        actual = [{"x": 2}, {"x": 1}]
        expected = [{"x": "1"}, {"x": "2"}]
        match, _ = results_match(actual, expected, ordered=False)
        assert match

    def test_ordered_mismatch(self):
        actual = [{"x": 2}, {"x": 1}]
        expected = [{"x": "1"}, {"x": "2"}]
        match, _ = results_match(actual, expected, ordered=True)
        assert not match

    def test_strips_arango_meta(self):
        actual = [{"_id": "n/1", "_key": "1", "_rev": "r", "x": 42}]
        expected = [{"x": "42"}]
        match, _ = results_match(actual, expected)
        assert match

    def test_empty_results(self):
        match, _ = results_match([], [])
        assert match

    def test_null_value_match(self):
        actual = [{"x": None}]
        expected = [{"x": "null"}]
        match, _ = results_match(actual, expected)
        assert match

    def test_bool_value_match(self):
        actual = [{"x": True}]
        expected = [{"x": "true"}]
        match, _ = results_match(actual, expected)
        assert match

    def test_float_int_normalization(self):
        actual = [{"x": 5.0}]
        expected = [{"x": "5"}]
        match, _ = results_match(actual, expected)
        assert match


# ---------------------------------------------------------------------------
# normalize.py — parse_param_value
# ---------------------------------------------------------------------------


class TestParseParamValue:
    def test_int(self):
        assert parse_param_value("42") == 42

    def test_float(self):
        assert parse_param_value("3.14") == 3.14

    def test_quoted_string(self):
        assert parse_param_value("'hello'") == "hello"

    def test_bool_true(self):
        assert parse_param_value("true") is True

    def test_bool_false(self):
        assert parse_param_value("false") is False

    def test_null(self):
        assert parse_param_value("null") is None

    def test_list(self):
        assert parse_param_value("[1, 2, 3]") == [1, 2, 3]

    def test_map(self):
        assert parse_param_value("{a: 1}") == {"a": 1}


# ---------------------------------------------------------------------------
# gherkin.py — doc string and data table parsing
# ---------------------------------------------------------------------------


class TestGherkinDocStringAndTable:
    def test_step_with_docstring(self, tmp_path: Path):
        p = _write_feature(
            tmp_path,
            """\
        Feature: Docstring

          Scenario: With docstring
            When executing query:
              \"\"\"
              MATCH (n) RETURN n
              \"\"\"
            Then the result should be empty
        """,
        )
        feat = parse_feature(p)
        when_step = feat.scenarios[0].steps[0]
        assert when_step.doc_string is not None
        assert "MATCH (n) RETURN n" in when_step.doc_string

    def test_step_with_data_table(self, tmp_path: Path):
        p = _write_feature(
            tmp_path,
            """\
        Feature: Table

          Scenario: With table
            Given parameters are:
              | name  | value |
              | x     | 42    |
            When executing query:
              \"\"\"
              RETURN 1
              \"\"\"
            Then the result should be empty
        """,
        )
        feat = parse_feature(p)
        given_step = feat.scenarios[0].steps[0]
        assert given_step.data_table is not None
        assert len(given_step.data_table) == 2
        assert given_step.data_table[0] == ["name", "value"]


# ---------------------------------------------------------------------------
# Setup fallback regression tests
# ---------------------------------------------------------------------------


def test_setup_create_falls_back_for_aql_variable_collision(monkeypatch: pytest.MonkeyPatch):
    class Translated:
        def to_aql_query(self):
            return object()

    class FailingExecutor:
        def __init__(self, _db):
            pass

        def execute(self, _query):
            raise RuntimeError("[HTTP 400][ERR 1511] AQL: variable '_c0' is assigned multiple times")

    seeded: list[str] = []

    monkeypatch.setattr(runner, "translate", lambda *_args, **_kwargs: Translated())
    monkeypatch.setattr(runner, "AqlExecutor", FailingExecutor)
    monkeypatch.setattr(
        runner,
        "_execute_create_directly",
        lambda _db, cypher: seeded.append(cypher) or None,
    )

    outcome = runner._execute_setup_cypher(object(), "CREATE (n:Node {name: 'A'})", "lpg")

    assert outcome is None
    assert seeded == ["CREATE (n:Node {name: 'A'})"]


# ---------------------------------------------------------------------------
# Integration smoke (requires ArangoDB)
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_tck_runner_smoke_integration():
    if os.environ.get("RUN_TCK") != "1":
        pytest.skip("Set RUN_TCK=1 to run TCK harness integration smoke")
    from tests.tck.runner import run_feature

    p = Path(__file__).resolve().parent / "features" / "sample.feature"
    rep = run_feature(p, db_name="tck_smoke_db", mapping_fixture="lpg")
    assert rep["feature"]
    assert rep["scenarios"] >= 1
    assert set(rep["counts"].keys()) == {"passed", "skipped", "failed"}


class TestScenarioInputs:
    def test_parameters_table_has_no_header(self):
        """``Given parameters are:`` rows are ``| name | value |``, every one of them."""
        assert runner._parse_params([["skipAmount", "2"], ["name", "'x'"]]) == {"skipAmount": 2, "name": "x"}

    def test_labels_and_types_with_spaces_and_alternatives(self):
        labels, types = runner._extract_labels_and_types(
            "MATCH (me: Person)--(you :A :B)-[r: T|:U]->() MATCH ()-[:V|W]-()"
        )
        assert labels == {"Person", "A", "B"}
        assert types == {"T", "U", "V", "W"}

    def test_labels_named_only_in_label_predicates(self):
        labels, types = runner._extract_labels_and_types(
            "MATCH (n)-[r:T]-(m) WHERE m:NonExistent RETURN m:X:Y"
        )
        assert labels == {"NonExistent", "X", "Y"}
        assert types == {"T"}

    def test_every_scenario_mapping_stores_multiple_labels(self):
        """SET n:L writes a labels array even when no pattern in the scenario names a label."""
        from tests.tck.gherkin import Scenario, Step

        step = Step(keyword="When", text="executing query:", doc_string="MATCH (n) RETURN n")
        mapping = runner._build_mapping_for_scenario(Scenario(name="x", steps=[step]), "lpg")
        assert all(e.get("labelsField") == "labels" for e in mapping.physical_mapping["entities"].values())
