"""L1 skill-following scorer.

Measures two metrics over the skill_follow dataset (docs/08 section 3,
ADR decision 2):

- ``follow_rate``: fraction of cases where the judge's decision matches the
  expected decision under the injected L1 constraints.
- ``reject_follow_rate``: follow_rate restricted to cases that must be
  rejected (REJECT) — the safety-critical subset.
- ``false_trigger_rate``: fraction of injected skill instances that the
  intent mapping should NOT have injected for that case.  Zero when the
  deterministic matcher matches the declared expected_skills exactly.
"""

from __future__ import annotations

from collections.abc import Callable

from erp_copilot.retrieval.skill_matcher import match_skills

JudgeFn = Callable[[dict, list[dict]], str]
InjectFn = Callable[[dict], list[dict]]


def _default_inject(case: dict) -> list[dict]:
    return match_skills(case["domain"], case["action"])


def evaluate(
    cases: list[dict],
    judge_fn: JudgeFn,
    inject_fn: InjectFn | None = None,
) -> dict:
    """Score L1 constraint following for *cases*.

    ``judge_fn(case, injected_skills)`` returns the judge's decision
    ("FOLLOW" or "REJECT") for one case, given the injected skill list.
    ``inject_fn`` defaults to the real deterministic matcher; tests pass a
    fake to isolate the scorer.
    """
    if inject_fn is None:
        inject_fn = _default_inject

    per_case: list[dict] = []
    total_false_triggers = 0
    total_injected = 0
    reject_total = 0
    reject_followed = 0

    for case in cases:
        expected = set(case["expected_skills"])
        injected_skills = inject_fn(case)
        injected_ids = [s["skill_id"] for s in injected_skills]

        false_triggers = [sid for sid in injected_ids if sid not in expected]
        total_false_triggers += len(false_triggers)
        total_injected += len(injected_ids)

        decision = judge_fn(case, injected_skills)
        followed = decision == case["expected_decision"]

        if case["expected_decision"] == "REJECT":
            reject_total += 1
            if followed:
                reject_followed += 1

        per_case.append({
            "case_id": case["case_id"],
            "decision": decision,
            "expected_decision": case["expected_decision"],
            "followed": followed,
            "injected_skill_ids": injected_ids,
            "expected_skill_ids": list(case["expected_skills"]),
            "false_triggers": false_triggers,
        })

    num_cases = len(cases)
    follow_rate = sum(1 for pc in per_case if pc["followed"]) / num_cases if num_cases else 0.0
    reject_rate = reject_followed / reject_total if reject_total else 0.0
    false_trigger_rate = total_false_triggers / total_injected if total_injected else 0.0

    return {
        "num_cases": num_cases,
        "follow_rate": follow_rate,
        "reject_follow_rate": reject_rate,
        "false_trigger_rate": false_trigger_rate,
        "per_case": per_case,
    }
