#!/usr/bin/env python3
"""Measure parse, translation, execution, and TCK-assertion outcomes.

This command requires a live ArangoDB configured with the same ``ARANGO_*``
environment variables used by the TCK tests.  It deliberately treats the TCK
expected result as the equivalence oracle; it does not claim a per-scenario
Neo4j comparison.  The separately marked ``cross`` suites provide that
reference-engine comparison for checked-in Movies and Northwind corpora.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

from arango_query_core import CoreError

from arango_cypher import translate_plan
from arango_cypher.parser import parse_cypher
from tests.tck.analyze_coverage import (
    _get_main_query,
    _scenario_expects_error,
    _scenario_steps_supported,
    scenario_id,
)
from tests.tck.gherkin import parse_feature
from tests.tck.procedures import scenario_params, scenario_registry
from tests.tck.runner import _build_mapping_for_scenario, harness_translate_options, run_scenario

_FEATURES_DIR = Path(__file__).resolve().parent / "features"


def _empty_counts() -> Counter[str]:
    return Counter(
        {
            "total": 0,
            "parseable": 0,
            "translatable": 0,
            "correct_rejections": 0,
            "executed": 0,
            "tck_assertion_passed": 0,
            "skipped": 0,
            "translation_failures": 0,
            "execution_failures": 0,
        }
    )


def analyze_execution(*, db_name: str, mapping_fixture: str, only: str | None = None) -> dict[str, Any]:
    """Run every harness-compatible TCK scenario and return staged metrics.

    The result's ``scenarios`` maps each scenario id to ``(outcome, reason)``;
    outcomes are ``passed``, ``correct_rejection``, ``skipped``,
    ``translation_failure`` and ``execution_failure``.  ``only`` restricts the
    run to categories starting with that prefix (e.g. ``clauses/match``).
    """
    records: dict[str, tuple[str, str]] = {}
    totals = _empty_counts()
    categories: dict[str, Counter[str]] = {}
    skipped: Counter[str] = Counter()
    failures: Counter[str] = Counter()

    for feature_path in sorted(_FEATURES_DIR.rglob("*.feature")):
        if feature_path.name == "sample.feature":
            continue
        category = str(feature_path.relative_to(_FEATURES_DIR).parent)
        if only and not category.startswith(only):
            continue
        counts = categories.setdefault(category, _empty_counts())
        seen_names: Counter[str] = Counter()
        for scenario in parse_feature(feature_path).scenarios:
            seen_names[scenario.name] += 1
            sid = scenario_id(category, feature_path.stem, scenario.name, seen_names[scenario.name])
            for counter in (totals, counts):
                counter["total"] += 1

            steps_ok, reason = _scenario_steps_supported(scenario)
            if not steps_ok:
                for counter in (totals, counts):
                    counter["skipped"] += 1
                skipped[reason] += 1
                records[sid] = ("skipped", reason)
                continue

            query = _get_main_query(scenario)
            if not query:
                for counter in (totals, counts):
                    counter["skipped"] += 1
                skipped["no query"] += 1
                records[sid] = ("skipped", "no query")
                continue

            expects_error = _scenario_expects_error(scenario)
            try:
                parse_cypher(query)
            except CoreError:
                if expects_error:
                    for counter in (totals, counts):
                        counter["correct_rejections"] += 1
                    records[sid] = ("correct_rejection", "parse")
                    continue
                for counter in (totals, counts):
                    counter["translation_failures"] += 1
                failures["parse rejected"] += 1
                records[sid] = ("translation_failure", "parse rejected")
                continue

            for counter in (totals, counts):
                counter["parseable"] += 1

            try:
                translate_plan(
                    query,
                    mapping=_build_mapping_for_scenario(scenario, mapping_fixture),
                    params=scenario_params(scenario) or None,
                    registry=scenario_registry(scenario),
                    **harness_translate_options(),
                )
            except CoreError as exc:
                if expects_error:
                    for counter in (totals, counts):
                        counter["correct_rejections"] += 1
                    records[sid] = ("correct_rejection", "translate")
                    continue
                for counter in (totals, counts):
                    counter["translation_failures"] += 1
                failures["translation rejected"] += 1
                records[sid] = ("translation_failure", f"{exc.code}: {exc}")
                continue

            for counter in (totals, counts):
                counter["translatable"] += 1

            outcome = run_scenario(scenario, db_name=db_name, mapping_fixture=mapping_fixture)
            if outcome.status == "skipped":
                for counter in (totals, counts):
                    counter["skipped"] += 1
                skipped[outcome.reason or "execution skipped"] += 1
                records[sid] = ("skipped", outcome.reason or "execution skipped")
            elif outcome.status == "passed":
                for counter in (totals, counts):
                    counter["executed"] += 1
                    counter["tck_assertion_passed"] += 1
                records[sid] = ("passed", outcome.reason or "")
            else:
                for counter in (totals, counts):
                    counter["executed"] += 1
                    counter["execution_failures"] += 1
                failures[outcome.reason or "execution failed"] += 1
                records[sid] = ("execution_failure", outcome.reason or "execution failed")

    return {
        "measurement": {
            "kind": "arango_tck_execution",
            "equivalence_oracle": "TCK expected rows and error assertions",
            "reference_engine_comparison": "not per-scenario; run pytest -m cross separately",
        },
        "totals": dict(totals),
        "categories": {category: dict(counts) for category, counts in sorted(categories.items())},
        "skip_reasons": dict(sorted(skipped.items())),
        "failure_reasons": dict(sorted(failures.items())),
        "scenarios": dict(sorted(records.items())),
    }


def write_tsv(scenarios: dict[str, tuple[str, str]], path: Path) -> None:
    """Write ``outcome<TAB>scenario_id<TAB>reason`` lines, one per scenario."""
    lines = []
    for sid, (outcome, reason) in sorted(scenarios.items()):
        flat = " ".join(reason.split())[:400]
        lines.append(f"{outcome}\t{sid}\t{flat}\n")
    path.write_text("".join(lines), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-name", default="tck_coverage_db")
    parser.add_argument("--mapping-fixture", default="lpg")
    parser.add_argument("--only", default=None, help="Category prefix, e.g. clauses/match.")
    parser.add_argument("--tsv", type=Path, default=None, help="Also write per-scenario outcomes here.")
    args = parser.parse_args()
    result = analyze_execution(db_name=args.db_name, mapping_fixture=args.mapping_fixture, only=args.only)
    if args.tsv is not None:
        write_tsv(result["scenarios"], args.tsv)
    result.pop("scenarios")
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
