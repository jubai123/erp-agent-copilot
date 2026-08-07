"""Unit tests for evals/scorers/skill_scorer.py — L1 constraint follow rate
and false-trigger rate (docs/08 section 3, ADR decision 2)."""

from __future__ import annotations

import pytest

from evals.scorers.skill_scorer import evaluate

_CASES = [
    {
        "case_id": "c1",
        "domain": "order",
        "action": "update_status",
        "expected_skills": ["order-state-machine", "approval-policy"],
        "expected_decision": "REJECT",
    },
    {
        "case_id": "c2",
        "domain": "product",
        "action": "check_stock",
        "expected_skills": ["product-fields", "scenario-awareness"],
        "expected_decision": "FOLLOW",
    },
    {
        "case_id": "c3",
        "domain": "order",
        "action": "cancel",
        "expected_skills": ["order-state-machine", "approval-policy"],
        "expected_decision": "REJECT",
    },
]


def _fake_inject(case: dict) -> list[dict]:
    return [{"skill_id": s} for s in case["expected_skills"]]


class TestFollowRate:
    def test_perfect_following(self) -> None:
        judge = lambda case, skills: case["expected_decision"]  # noqa: E731
        report = evaluate(_CASES, judge_fn=judge, inject_fn=_fake_inject)
        assert report["follow_rate"] == 1.0
        assert report["reject_follow_rate"] == 1.0
        assert report["false_trigger_rate"] == 0.0

    def test_half_following(self) -> None:
        decisions = {"c1": "FOLLOW", "c2": "FOLLOW", "c3": "REJECT"}
        judge = lambda case, skills: decisions[case["case_id"]]  # noqa: E731
        report = evaluate(_CASES, judge_fn=judge, inject_fn=_fake_inject)
        assert report["follow_rate"] == pytest.approx(2 / 3)
        assert report["reject_follow_rate"] == pytest.approx(1 / 2)
        assert report["num_cases"] == 3

    def test_none_following(self) -> None:
        judge = lambda case, skills: "FOLLOW" if case["expected_decision"] == "REJECT" else "REJECT"  # noqa: E731
        report = evaluate(_CASES, judge_fn=judge, inject_fn=_fake_inject)
        assert report["follow_rate"] == 0.0
        assert report["reject_follow_rate"] == 0.0

    def test_empty_cases(self) -> None:
        report = evaluate([], judge_fn=lambda c, s: "FOLLOW", inject_fn=_fake_inject)
        assert report["follow_rate"] == 0.0
        assert report["false_trigger_rate"] == 0.0
        assert report["num_cases"] == 0


class TestFalseTriggerRate:
    def test_extra_injected_skill_counts_as_false_trigger(self) -> None:
        def bad_inject(case: dict) -> list[dict]:
            return [{"skill_id": s} for s in case["expected_skills"]] + [
                {"skill_id": "unexpected-skill"}
            ]

        judge = lambda c, s: c["expected_decision"]  # noqa: E731
        report = evaluate(_CASES, judge_fn=judge, inject_fn=bad_inject)
        # 3 cases × 1 extra trigger out of 3 cases × 3 skills (2+2+2 = 6) + 3 = 9 injected
        assert report["false_trigger_rate"] == pytest.approx(3 / 9)

    def test_wrong_skill_injected_counts_as_false_trigger(self) -> None:
        def wrong_inject(case: dict) -> list[dict]:
            skills = list(case["expected_skills"])
            skills[0] = "wrong-skill"
            return [{"skill_id": s} for s in skills]

        judge = lambda c, s: c["expected_decision"]  # noqa: E731
        report = evaluate(_CASES, judge_fn=judge, inject_fn=wrong_inject)
        assert report["false_trigger_rate"] == pytest.approx(3 / 6)

    def test_no_injection_has_zero_rate(self) -> None:
        judge = lambda c, s: c["expected_decision"]  # noqa: E731
        report = evaluate(_CASES, judge_fn=judge, inject_fn=lambda c: [])
        assert report["false_trigger_rate"] == 0.0

    def test_default_inject_fn_uses_real_matcher(self) -> None:
        """With the real skill_matcher, declared expected_skills must produce
        zero false triggers (consistency guaranteed by the dataset test)."""
        import json
        from pathlib import Path

        dataset = (
            Path(__file__).resolve().parent.parent.parent.parent
            / "evals" / "datasets" / "skill_follow_20.json"
        )
        cases = json.loads(dataset.read_text(encoding="utf-8"))["cases"]
        report = evaluate(cases, judge_fn=lambda c, s: c["expected_decision"])
        assert report["num_cases"] == 20
        assert report["false_trigger_rate"] == 0.0


class TestPerCaseDetails:
    def test_per_case_contains_judge_skills(self) -> None:
        judge = lambda case, skills: case["expected_decision"]  # noqa: E731
        report = evaluate(_CASES, judge_fn=judge, inject_fn=_fake_inject)
        assert len(report["per_case"]) == 3
        first = report["per_case"][0]
        assert first["case_id"] == "c1"
        assert first["followed"] is True
        assert first["injected_skill_ids"] == ["order-state-machine", "approval-policy"]
        assert first["expected_skill_ids"] == ["order-state-machine", "approval-policy"]
