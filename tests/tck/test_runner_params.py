"""``parameters are:`` tables in the TCK harness (``tests/tck/runner.py``)."""

from __future__ import annotations

import pytest

from tests.tck.runner import _parse_params


def test_every_row_is_a_parameter() -> None:
    # No header row: reading one dropped the first parameter of every scenario.
    assert _parse_params([["elt", "null"], ["coll", "[1, 2, 3, null]"]]) == {
        "elt": None,
        "coll": [1, 2, 3, None],
    }


def test_single_parameter() -> None:
    assert _parse_params([["idx", "1"]]) == {"idx": 1}


def test_malformed_row_is_an_error() -> None:
    with pytest.raises(ValueError, match="name | value"):
        _parse_params([["a", "1", "2"]])


def test_harness_turns_on_the_opt_in_options(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.tck.runner import harness_translate_options

    monkeypatch.delenv("TCK_DEFAULT_OPTIONS", raising=False)
    assert harness_translate_options() == {"cypher_column_names": True, "temporal_strings": True}


def test_default_options_mode_turns_them_off(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.tck.runner import harness_translate_options

    monkeypatch.setenv("TCK_DEFAULT_OPTIONS", "1")
    assert harness_translate_options() == {}
