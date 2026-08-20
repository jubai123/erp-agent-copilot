"""Validation tests for the L1 rule-following evaluation dataset.

The dataset drives two metrics (docs/08 section 3):
- constraint follow rate: judge follows the injected constraint.
- false-trigger rate: rules injected that were NOT expected.

So each case must declare the exact rule set the intent mapping injects,
and that set must match intent_rule_map.yaml — otherwise the false-trigger
metric is meaningless.
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
DATASET_PATH = PROJECT_ROOT / "evals" / "datasets" / "rule_follow_20.json"
RULES_PATH = PROJECT_ROOT / "datasets" / "knowledge" / "rules" / "rules.yaml"
INTENT_MAP_PATH = PROJECT_ROOT / "datasets" / "knowledge" / "rules" / "intent_rule_map.yaml"

VALID_DECISIONS = {"REJECT", "FOLLOW"}


def _load_dataset() -> dict:
    return json.loads(DATASET_PATH.read_text(encoding="utf-8"))


def _load_intent_map() -> dict[tuple[str, str], list[str]]:
    raw = yaml.safe_load(INTENT_MAP_PATH.read_text(encoding="utf-8"))
    return {(e["domain"], e["action"]): e["rules"] for e in raw["intents"]}


def _load_rule_ids() -> set[str]:
    raw = yaml.safe_load(RULES_PATH.read_text(encoding="utf-8"))
    return {item["rule_id"] for item in raw}


class TestDatasetStructure:
    def test_dataset_file_exists(self) -> None:
        assert DATASET_PATH.exists()

    def test_has_exactly_20_cases(self) -> None:
        cases = _load_dataset()["cases"]
        assert len(cases) == 20

    def test_case_ids_unique(self) -> None:
        cases = _load_dataset()["cases"]
        ids = [c["case_id"] for c in cases]
        assert len(ids) == len(set(ids))

    def test_every_case_has_required_fields(self) -> None:
        required = {
            "case_id",
            "query",
            "domain",
            "action",
            "expected_rules",
            "constraint",
            "expected_decision",
            "note",
        }
        for case in _load_dataset()["cases"]:
            assert required <= set(case), f"missing fields in {case.get('case_id')}"

    def test_decision_values_valid(self) -> None:
        for case in _load_dataset()["cases"]:
            assert case["expected_decision"] in VALID_DECISIONS, (
                f"{case['case_id']}: invalid decision {case['expected_decision']}"
            )


class TestDatasetConsistency:
    def test_expected_rules_exist_in_rules_yaml(self) -> None:
        known = _load_rule_ids()
        for case in _load_dataset()["cases"]:
            unknown = [r for r in case["expected_rules"] if r not in known]
            assert not unknown, f"{case['case_id']}: unknown rules {unknown}"

    def test_expected_rules_match_intent_map_exactly(self) -> None:
        """The declared rule set must equal what the deterministic matcher
        injects for that (domain, action) — otherwise false-trigger rate
        cannot be computed."""
        intent_map = _load_intent_map()
        for case in _load_dataset()["cases"]:
            key = (case["domain"], case["action"])
            assert key in intent_map, f"{case['case_id']}: intent {key} not in intent map"
            assert sorted(case["expected_rules"]) == sorted(intent_map[key]), (
                f"{case['case_id']}: declared rules differ from intent map"
            )

    def test_includes_mandatory_reject_scenarios(self) -> None:
        """Task 3.12: must contain illegal state transitions and illegal
        region values that must be rejected."""
        rejects = [c for c in _load_dataset()["cases"] if c["expected_decision"] == "REJECT"]
        assert len(rejects) >= 10
        assert any("状态转换" in c["constraint"] for c in rejects), (
            "no illegal state transition case"
        )
        assert any("region" in c["constraint"] or "枚举" in c["constraint"] for c in rejects), (
            "no illegal region case"
        )


class TestRuleScorerContract:
    """Contract the scorer must implement (written first — scorer is next)."""

    def test_expected_metric_names(self) -> None:
        # Document the metric names the scorer will output so the dataset
        # and scorer stay in sync.
        cases = _load_dataset()["cases"]
        from evals.scorers import rule_scorer  # noqa: F401

        report = rule_scorer.evaluate(cases, judge_fn=lambda case, rules: "FOLLOW")
        assert "follow_rate" in report
        assert "false_trigger_rate" in report
