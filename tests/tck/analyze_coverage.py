#!/usr/bin/env python3
"""Measure TCK parse and translation coverage without a database.

Parses all .feature files, extracts the main Cypher query from each scenario,
checks step compatibility with the harness, and attempts to translate.

Reports:
  - Total scenarios (full + core-minus-temporal-and-call)
  - Parseable, translatable, and correctly rejected scenarios
  - Skipped categories and reasons
  - Projected translation pass rate (full and core)

This is deliberately a dry-run measurement.  ``execute`` and
``semantically_equivalent`` outcomes require the live TCK harness and are
reported by ``tests/tck/analyze_execution.py``.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from arango_query_core import CoreError

from arango_cypher import translate_plan
from arango_cypher.parser import parse_cypher
from tests.tck.gherkin import Scenario, Step, parse_feature
from tests.tck.procedures import PROCEDURE_STEP, scenario_params, scenario_registry
from tests.tck.runner import _build_mapping_for_scenario  # noqa: PLC2701

_FEATURES_DIR = Path(__file__).resolve().parent / "features"

_OUT_OF_SCOPE_CATS = frozenset(
    {
        "expressions/temporal",
        "clauses/call",
    }
)

_ERROR_STEP_PREFIXES = (
    "a SyntaxError should be raised",
    "a TypeError should be raised",
    "a SemanticError should be raised",
    "a ParameterMissing error should be raised",
    "a ParameterMissing should be raised",
    "a ArgumentError should be raised",
    "a EntityNotFound should be raised",
    "an error should be raised",
    "a ProcedureError should be raised",
    "a ConstraintVerification",
)

_ROW_COUNT_RE = re.compile(r"the result should have (\d+) rows?")

_SUPPORTED_STEP_TEXTS = {
    "an empty graph",
    "the empty graph",
    "any graph",
    "an existing graph",
    "the existing graph",
    "the result should be empty",
    "no side effects",
}


def _step_is_supported(step: Step) -> tuple[bool, str]:
    """Check if the harness can handle a given step (keyword-agnostic)."""
    s = step.text

    if s in _SUPPORTED_STEP_TEXTS:
        return True, ""

    if s.startswith("having executed:"):
        return True, ""
    if s.startswith("parameters are:"):
        return True, ""
    if s.startswith("executing query:"):
        return True, ""
    if s.startswith("executing control query:"):
        return True, ""
    if s.startswith("the result should be, in order:"):
        return True, ""
    if s.startswith("the result should be, in any order:"):
        return True, ""
    if s.startswith("the result should be (ignoring element order for lists):"):
        return True, ""
    if s.startswith("the result should be, in order (ignoring element order for lists):"):
        return True, ""
    if s.startswith("the result should be:"):
        return True, ""
    if s.startswith("the result should contain"):
        return True, ""
    if s.startswith("the side effects should be:"):
        return True, ""
    if _ROW_COUNT_RE.match(s):
        return True, ""

    for prefix in _ERROR_STEP_PREFIXES:
        if s.startswith(prefix):
            return True, ""

    if s.startswith(PROCEDURE_STEP):
        return True, ""  # a table-backed procedure (tests/tck/procedures.py)

    return False, f"unsupported: {s[:60]}"


def _scenario_steps_supported(sc: Scenario) -> tuple[bool, str]:
    for step in sc.steps:
        ok, reason = _step_is_supported(step)
        if not ok:
            return False, reason
    return True, ""


def _scenario_expects_error(sc: Scenario) -> bool:
    for step in sc.steps:
        # "a SyntaxError …", "an error …", and "a ParameterMissing should be raised …".
        if step.text.startswith(_ERROR_STEP_PREFIXES):
            return True
    return False


def _get_main_query(sc: Scenario) -> str | None:
    for step in sc.steps:
        if "executing query:" in step.text:
            if step.doc_string:
                return step.doc_string.strip()
    return None


def _counter_dict(counter: Counter[str]) -> dict[str, int]:
    return dict(sorted(counter.items()))


def scenario_id(category: str, feature_stem: str, scenario_name: str, ordinal: int) -> str:
    """Stable identifier for one TCK scenario.

    Scenario-outline rows expand to several scenarios sharing a name, so an
    ordinal disambiguates them.  The id is the ledger's primary key and must not
    change between runs, or the conformance ratchet reports phantom churn.
    """
    base = f"{category}/{feature_stem}/{scenario_name}"
    return base if ordinal == 1 else f"{base}#{ordinal}"


def analyze(*, collect_scenarios: bool = False) -> dict[str, Any]:
    """Return structured dry-run coverage metrics for the bundled TCK.

    With ``collect_scenarios`` the result also carries a ``scenarios`` mapping of
    scenario id to outcome, which is what ``tests/tck/ledger.py`` writes to the
    committed conformance ledger.  It is opt-in so the default payload (and the
    ``--json`` output consumed by CI) stays unchanged.
    """
    records: dict[str, str] = {}
    full_total = 0
    core_total = 0
    full_passable = 0
    core_passable = 0
    full_parseable = 0
    core_parseable = 0
    full_translatable = 0
    core_translatable = 0
    full_correct_rejections = 0
    core_correct_rejections = 0

    harness_skip_reasons: Counter = Counter()
    translate_fail_reasons: Counter = Counter()
    by_category: dict[str, Counter] = {}

    for feat_file in sorted(_FEATURES_DIR.rglob("*.feature")):
        if feat_file.name == "sample.feature":
            continue
        rel = feat_file.relative_to(_FEATURES_DIR)
        category = str(rel.parent)
        is_core = category not in _OUT_OF_SCOPE_CATS
        if category not in by_category:
            by_category[category] = Counter()

        feat = parse_feature(feat_file)
        seen_names: Counter = Counter()
        for sc in feat.scenarios:
            seen_names[sc.name] += 1
            sid = scenario_id(category, feat_file.stem, sc.name, seen_names[sc.name])

            def _rec(outcome: str, _sid: str = sid) -> None:
                if collect_scenarios:
                    records[_sid] = outcome

            full_total += 1
            by_category[category]["total"] += 1
            if is_core:
                core_total += 1

            steps_ok, step_reason = _scenario_steps_supported(sc)
            if not steps_ok:
                harness_skip_reasons[step_reason] += 1
                by_category[category]["harness_skip"] += 1
                _rec("harness_skip")
                continue

            query = _get_main_query(sc)
            if not query:
                by_category[category]["no_query"] += 1
                _rec("no_query")
                continue

            expects_error = _scenario_expects_error(sc)

            try:
                parse_cypher(query)
            except CoreError as e:
                by_category[category]["parse_rejected"] += 1
                if expects_error:
                    full_passable += 1
                    full_correct_rejections += 1
                    by_category[category]["correct_rejection"] += 1
                    if is_core:
                        core_passable += 1
                        core_correct_rejections += 1
                    _rec("correct_rejection")
                    continue
                translate_fail_reasons[str(e)[:60]] += 1
                by_category[category]["translate_fail"] += 1
                _rec("parse_rejected")
                continue

            full_parseable += 1
            by_category[category]["parseable"] += 1
            if is_core:
                core_parseable += 1

            try:
                mapping = _build_mapping_for_scenario(sc, "lpg")
                translate_plan(
                    query, mapping=mapping, params=scenario_params(sc) or None, registry=scenario_registry(sc)
                )
                full_passable += 1
                full_translatable += 1
                by_category[category]["translatable"] += 1
                if is_core:
                    core_passable += 1
                    core_translatable += 1
                _rec("translatable")
            except CoreError as e:
                if expects_error:
                    full_passable += 1
                    full_correct_rejections += 1
                    by_category[category]["correct_rejection"] += 1
                    if is_core:
                        core_passable += 1
                        core_correct_rejections += 1
                    _rec("correct_rejection")
                else:
                    translate_fail_reasons[str(e)[:60]] += 1
                    by_category[category]["translate_fail"] += 1
                    _rec("translate_fail")
            except Exception:
                if expects_error:
                    full_passable += 1
                    full_correct_rejections += 1
                    by_category[category]["correct_rejection"] += 1
                    if is_core:
                        core_passable += 1
                        core_correct_rejections += 1
                    _rec("correct_rejection")
                else:
                    by_category[category]["translate_fail"] += 1
                    _rec("translate_fail")

    full_rate = (full_passable / full_total * 100) if full_total else 0
    core_rate = (core_passable / core_total * 100) if core_total else 0
    categories = {
        category: {
            "total": counts["total"],
            "parseable": counts["parseable"],
            "translatable": counts["translatable"],
            "correct_rejections": counts["correct_rejection"],
            "passable": counts["translatable"] + counts["correct_rejection"],
            "harness_skips": counts["harness_skip"],
            "translation_failures": counts["translate_fail"],
        }
        for category, counts in sorted(by_category.items())
    }

    return {
        "measurement": {
            "kind": "translation_dry_run",
            "execution_available": False,
            "equivalence_available": False,
            "core_exclusions": sorted(_OUT_OF_SCOPE_CATS),
        },
        "full": {
            "total": full_total,
            "parseable": full_parseable,
            "translatable": full_translatable,
            "correct_rejections": full_correct_rejections,
            "passable": full_passable,
            "pass_rate": full_rate,
        },
        "core": {
            "total": core_total,
            "parseable": core_parseable,
            "translatable": core_translatable,
            "correct_rejections": core_correct_rejections,
            "passable": core_passable,
            "pass_rate": core_rate,
        },
        "harness_skip_reasons": _counter_dict(harness_skip_reasons),
        "translation_failure_reasons": _counter_dict(translate_fail_reasons),
        "categories": categories,
        **({"scenarios": dict(sorted(records.items()))} if collect_scenarios else {}),
    }


def _print_text_report(metrics: dict[str, Any]) -> None:
    full = metrics["full"]
    core = metrics["core"]
    print("=" * 72)
    print("TCK DRY-RUN COVERAGE ANALYSIS")
    print("=" * 72)
    print()
    print(f"FULL TCK (all {full['total']} scenarios):")
    print(f"  Parseable:         {full['parseable']:5d} / {full['total']}")
    print(f"  Translatable:      {full['translatable']:5d} / {full['total']}")
    print(f"  Correct rejections:{full['correct_rejections']:5d} / {full['total']}")
    print(f"  Passable:          {full['passable']:5d} / {full['total']}")
    print(f"  Pass rate:         {full['pass_rate']:5.1f}%")
    print()
    print(
        "CORE TCK (excl. "
        + "+".join(metrics["measurement"]["core_exclusions"])
        + f" — {core['total']} scenarios):"
    )
    print(f"  Parseable:         {core['parseable']:5d} / {core['total']}")
    print(f"  Translatable:      {core['translatable']:5d} / {core['total']}")
    print(f"  Correct rejections:{core['correct_rejections']:5d} / {core['total']}")
    print(f"  Passable:          {core['passable']:5d} / {core['total']}")
    print(f"  Pass rate:         {core['pass_rate']:5.1f}%")
    print()

    print("-" * 72)
    print("TOP HARNESS SKIP REASONS")
    print("-" * 72)
    for reason, count in sorted(
        metrics["harness_skip_reasons"].items(), key=lambda item: item[1], reverse=True
    )[:10]:
        print(f"  {count:4d}  {reason}")
    print()

    print("-" * 72)
    print("TOP TRANSLATE FAILURE REASONS")
    print("-" * 72)
    for reason, count in sorted(
        metrics["translation_failure_reasons"].items(), key=lambda item: item[1], reverse=True
    )[:15]:
        print(f"  {count:4d}  {reason}")
    print()

    print("-" * 72)
    print("BREAKDOWN BY CATEGORY")
    print("-" * 72)
    for cat, category in metrics["categories"].items():
        total = category["total"]
        passable = category["passable"]
        r = (passable / total * 100) if total else 0
        oos = " [OUT OF SCOPE]" if cat in _OUT_OF_SCOPE_CATS else ""
        print(f"  {cat:45s}  {passable:3d}/{total:3d}  ({r:5.1f}%){oos}")
    print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="Emit structured JSON instead of text.")
    args = parser.parse_args()
    metrics = analyze()
    if args.json:
        print(json.dumps(metrics, indent=2, sort_keys=True))
    else:
        _print_text_report(metrics)
