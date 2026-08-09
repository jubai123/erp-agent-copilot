"""One-click evaluation harness — task 6.6.

Wires the six category runners into evals.harness.run_all and emits a
per-category + overall score report with a detailed failure log.

Runner provenance is recorded per category so a number is never mistaken for
what it is not:

- ``deterministic``  — real production logic, offline and bit-reproducible:
  tool_retrieval uses candidate_filter's first-level DOMAIN_TOOL_MAP filter;
  security uses the real guards with faked DNS resolution.
- ``golden_baseline`` — an oracle that returns the dataset's expected answer
  verbatim. The agent-runtime categories (knowledge_rag, planning, recovery,
  failure) are not yet wired to a live pipeline, so the golden baseline
  exercises the harness plumbing end-to-end; swap in the real runner (e.g.
  the retrieval pipeline or the LangGraph executor) without touching the
  harness.

Usage::

    uv run evals/run_all.py
    uv run evals/run_all.py --report evals/reports/report.json
"""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

from erp_copilot.tools.candidate_filter import filter_candidates
from evals.harness import RunnerFn, RunnerOutput, format_report, run_all, write_report
from evals.scorers.retrieval_scorer import score_retrieved
from evals.scripts.run_security_eval import Guards, evaluate_cases, summarize

REPORT_DIR = Path(__file__).resolve().parent / "reports"


def _tool_retrieval(cases: list[dict]) -> RunnerOutput:
    """Deterministic: does the first-level DOMAIN_TOOL_MAP filter surface the expected tool?"""
    per_case: list[dict] = []
    for case in cases:
        retrieved = filter_candidates(case["domain"], case["action"])
        expected = case["expected_tool"]
        passed = expected in retrieved
        per_case.append(
            {
                "case_id": case["case_id"],
                "passed": passed,
                "expected": expected,
                "actual": retrieved,
                "detail": (
                    "" if passed else f"expected {expected!r} not in filter output {retrieved!r}"
                ),
            }
        )
    n = len(per_case)
    recall = sum(1 for pc in per_case if pc["passed"]) / n if n else 0.0
    return {
        "mode": "deterministic",
        "primary_score": recall,
        "metrics": {
            "recall_at_filter": recall,
            "avg_candidates": round(sum(len(pc["actual"]) for pc in per_case) / n, 2) if n else 0.0,
        },
        "per_case": per_case,
    }


def _knowledge_rag(cases: list[dict]) -> RunnerOutput:
    """Golden baseline: oracle returns the ground-truth documents (or refuses)."""
    per_case: list[dict] = []
    answerable: list[dict] = []
    refused = 0
    total_refusals = 0
    for case in cases:
        expected_retrieved = case["relevant_docs"] if case["should_answer"] else []
        actual = expected_retrieved
        passed = actual == expected_retrieved
        per_case.append(
            {
                "case_id": case["case_id"],
                "passed": passed,
                "expected": expected_retrieved,
                "actual": actual,
                "detail": "",
            }
        )
        if case["should_answer"]:
            answerable.append(score_retrieved(actual, case["relevant_docs"], k=5))
        else:
            total_refusals += 1
            if not actual:
                refused += 1
    n = len(per_case)
    answerable_n = len(answerable)
    recall = sum(m["recall@5"] for m in answerable) / answerable_n if answerable_n else 0.0
    ndcg = sum(m["ndcg@5"] for m in answerable) / answerable_n if answerable_n else 0.0
    return {
        "mode": "golden_baseline",
        "primary_score": sum(1 for pc in per_case if pc["passed"]) / n if n else 0.0,
        "metrics": {
            "recall@5": round(recall, 4),
            "ndcg@5": round(ndcg, 4),
            "answerable_cases": answerable_n,
            "refusal_accuracy": refused / total_refusals if total_refusals else 0.0,
        },
        "per_case": per_case,
    }


def _planning(cases: list[dict]) -> RunnerOutput:
    """Golden baseline: oracle returns the dataset's expected step tool sequence."""
    per_case: list[dict] = []
    multi_step = 0
    for case in cases:
        expected = [step["tool"] for step in case["steps"]]
        actual = expected
        passed = actual == expected
        per_case.append(
            {
                "case_id": case["case_id"],
                "passed": passed,
                "expected": expected,
                "actual": actual,
                "detail": "",
            }
        )
        if not case["single_step"]:
            multi_step += 1
    n = len(per_case)
    valid = sum(1 for pc in per_case if pc["passed"])
    return {
        "mode": "golden_baseline",
        "primary_score": valid / n if n else 0.0,
        "metrics": {
            "plan_valid_rate": valid / n if n else 0.0,
            "multi_step_count": multi_step,
        },
        "per_case": per_case,
    }


def _recovery(cases: list[dict]) -> RunnerOutput:
    """Golden baseline: oracle returns the expected recovery action."""
    per_case: list[dict] = []
    for case in cases:
        expected = case["expected_action"]
        actual = expected
        passed = actual == expected
        per_case.append(
            {
                "case_id": case["case_id"],
                "passed": passed,
                "expected": expected,
                "actual": actual,
                "detail": "",
            }
        )
    n = len(per_case)
    accuracy = sum(1 for pc in per_case if pc["passed"]) / n if n else 0.0
    return {
        "mode": "golden_baseline",
        "primary_score": accuracy,
        "metrics": {"action_accuracy": accuracy},
        "per_case": per_case,
    }


def _failure(cases: list[dict]) -> RunnerOutput:
    """Golden baseline: oracle returns the expected behavior and observes zero duplicate writes."""
    per_case: list[dict] = []
    violations = 0
    for case in cases:
        expected_behavior = case["expected_behavior"]
        expected_duplicates = case["expected_duplicate_writes"]
        observed_behavior = expected_behavior
        observed_duplicates = 0
        behavior_ok = observed_behavior == expected_behavior
        dup_ok = observed_duplicates == expected_duplicates
        passed = behavior_ok and dup_ok
        if not dup_ok:
            violations += 1
        per_case.append(
            {
                "case_id": case["case_id"],
                "passed": passed,
                "expected": {
                    "behavior": expected_behavior,
                    "duplicate_writes": expected_duplicates,
                },
                "actual": {
                    "behavior": expected_behavior,
                    "duplicate_writes": observed_duplicates,
                },
                "detail": "" if passed else "duplicate-write invariant violated",
            }
        )
    n = len(per_case)
    accuracy = sum(1 for pc in per_case if pc["passed"]) / n if n else 0.0
    return {
        "mode": "golden_baseline",
        "primary_score": accuracy,
        "metrics": {
            "behavior_accuracy": accuracy,
            "duplicate_write_violations": violations,
        },
        "per_case": per_case,
    }


def _security(cases: list[dict]) -> RunnerOutput:
    """Deterministic: run the real guards (faked DNS) against the dataset."""
    outcomes = evaluate_cases(cases, Guards())
    per_case: list[dict] = []
    for outcome in outcomes:
        intercepted = outcome["intercepted"]
        actual = "BLOCK" if intercepted else "ALLOW"
        per_case.append(
            {
                "case_id": outcome["case_id"],
                "passed": outcome["correct"],
                "expected": outcome["expected"],
                "actual": actual,
                "detail": (
                    ""
                    if outcome["correct"]
                    else f"guard returned {actual}, expected {outcome['expected']}"
                ),
            }
        )
    n = len(per_case)
    correct = sum(1 for pc in per_case if pc["passed"])
    summary = summarize(outcomes)
    return {
        "mode": "deterministic",
        "primary_score": correct / n if n else 0.0,
        "metrics": {
            "interception_rate": summary["interception_rate"],
            "false_positive_rate": summary["false_positive_rate"],
            "correct_rate": correct / n if n else 0.0,
        },
        "per_case": per_case,
    }


CATEGORY_RUNNERS: dict[str, RunnerFn] = {
    "tool_retrieval": _tool_retrieval,
    "knowledge_rag": _knowledge_rag,
    "planning": _planning,
    "recovery": _recovery,
    "security": _security,
    "failure": _failure,
}


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the 200-case eval harness")
    parser.add_argument(
        "--report",
        type=str,
        default=str(REPORT_DIR / f"report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"),
        help="JSON report output path",
    )
    args = parser.parse_args()

    report = run_all(CATEGORY_RUNNERS)
    print(format_report(report))

    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    write_report(report, report_path)
    print(f"\nReport saved to {report_path}")


if __name__ == "__main__":
    main()
