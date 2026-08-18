"""Agent routing eval — the three-layer funnel router's boundary, measured.

Answers "query 应落三层漏斗的哪一层" via the production router
``route_query_layer`` (erp_copilot.agent.routing) — the single source of truth
for the funnel decision. ``observe_layer`` maps its answer to the 2×2 vocabulary
("tier1" / "route_up"); ``observe_layer_exact`` keeps the precise layer so the
report also shows tier2-vs-tier3 assignment quality. The 2×2 matrix then scores
the boundary:

- expected tier1 + served with the exact expected tool sequence → PASS
- expected tier1 + routed up → FAIL (coverage_gap: the deterministic layer
  refuses a query it should serve)
- expected tier2/3 + routed up → PASS (honest up-throw)
- expected tier2/3 + served by tier1 → FAIL (overgrab: the deterministic layer
  grabs a query outside its competence — 危险边界)

The headline metric is ``routing_accuracy``; ``tier1_hit_rate`` /
``tier1_correct_rate`` / ``route_up_rate`` / ``overgrab_rate`` /
``coverage_gap_rate`` / ``layer_assign_correct_rate`` / ``layer_counts`` are
reported alongside. Over-grabs are FAILed and listed in the report so the
funnel's honesty is measurable, not assumed.

Honest caveat: the router's trigger vocabulary is hand-authored against this
50-case oracle — the eval measures boundary behaviour on the authored dataset,
not real-world recall. That is exactly the "know your boundary" evidence, not a
claim of general router quality.

Standalone on purpose: it is NOT one of run_all's five categories, so the
documented "175 条五大分类" numbers stay untouched. Purely deterministic — no
LLM, no DB, no network — so the unit tests run the real router over the full
dataset.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from erp_copilot.agent.nodes.classify_intent import classify_intent
from erp_copilot.agent.planner import build_plan_from_intent
from erp_copilot.agent.routing import Layer, route_query_layer
from evals.harness import (
    RunnerOutput,
    collect_failures,
    load_cases,
    run_category,
    write_report,
)

REPORT_DIR = Path(__file__).resolve().parent / "reports"


def observe_layer(query: str) -> str:
    """Route signal: "tier1" if the funnel router serves the query at tier1.

    Delegates to the production router — the single source of truth for the
    funnel decision. Anything the router routes above tier1 (EMPTY_PLAN,
    beyond-grammar complexity, uncovered region) maps to "route_up".
    """
    return "tier1" if route_query_layer(query) == "tier1" else "route_up"


def observe_layer_exact(query: str) -> Layer:
    """The precise funnel layer the router assigns: tier1 / tier2 / tier3."""
    return route_query_layer(query)


def observe_tools(query: str) -> list[str]:
    """The tool sequence the deterministic chain would execute (empty if none).

    Kept as the raw chain observation — for tier1 hits it is the expected-tools
    oracle; for route-up cases it shows what tier1 *would* have grabbed.
    """
    intent = classify_intent(query)
    plan, _ = build_plan_from_intent(intent)
    return [step.tool_name for step in plan.steps] if plan is not None else []


def score_case(case: dict) -> dict:
    """Score one case against the 2×2 routing matrix.

    Returns a harness PerCaseResult-shaped dict: passed, expected, actual and a
    human detail. expected/actual carry {"layer", "tools"} so the report shows
    exactly what was expected vs. what the deterministic layer grabbed.
    """
    case_id = case["case_id"]
    expected_layer = case["expected_layer"]
    layer = route_query_layer(case["query"])
    observed = "tier1" if layer == "tier1" else "route_up"
    tools = observe_tools(case["query"])
    expected = {"layer": expected_layer, "tools": case.get("expected_tools", [])}
    actual = {
        "layer": observed,
        "tools": tools if observed == "tier1" else [],
        "exact_layer": layer,
    }
    if observed != "tier1":
        actual["would_grab"] = tools

    if expected_layer == "tier1":
        if observed != "tier1":
            return {
                "case_id": case_id,
                "passed": False,
                "expected": expected,
                "actual": actual,
                "detail": "coverage_gap: 期望 tier1 服务却被上抛",
            }
        if tools == case.get("expected_tools"):
            return {
                "case_id": case_id,
                "passed": True,
                "expected": expected,
                "actual": actual,
                "detail": "tier1 命中且工具序列正确",
            }
        return {
            "case_id": case_id,
            "passed": False,
            "expected": expected,
            "actual": actual,
            "detail": f"tier1 命中但工具序列错误 {tools}",
        }

    # tier2 / tier3 — must route up.
    if observed == "route_up":
        return {
            "case_id": case_id,
            "passed": True,
            "expected": expected,
            "actual": actual,
            "detail": "诚实上抛",
        }
    return {
        "case_id": case_id,
        "passed": False,
        "expected": expected,
        "actual": actual,
        "detail": f"overgrab: 确定性层越界抓取 {tools}",
    }


def evaluate_cases(cases: list[dict]) -> RunnerOutput:
    """Run the deterministic chain over all cases and aggregate the boundary."""
    per_case = [score_case(case) for case in cases]
    n = len(per_case)
    if n == 0:
        return {"mode": "agent_routing", "primary_score": 0.0, "metrics": {}, "per_case": []}

    def rate(cond: Callable[[dict], bool]) -> float:
        return sum(1 for pc in per_case if cond(pc)) / n

    tier1_grabbed = sum(1 for pc in per_case if pc["actual"]["layer"] == "tier1")
    tier1_correct = sum(1 for pc in per_case if pc["passed"] and pc["actual"]["layer"] == "tier1")
    overgrabs = sum(1 for pc in per_case if pc["detail"].startswith("overgrab"))
    coverage_gaps = sum(1 for pc in per_case if pc["detail"].startswith("coverage_gap"))
    route_ups = sum(1 for pc in per_case if pc["actual"]["layer"] == "route_up")
    layer_assign_correct = sum(
        1
        for pc in per_case
        if pc["actual"]["layer"] == "route_up"
        and pc["actual"]["exact_layer"] == pc["expected"]["layer"]
    )
    layer_counts = {
        layer: sum(1 for c in cases if c["expected_layer"] == layer)
        for layer in ("tier1", "tier2", "tier3")
    }
    return {
        "mode": "agent_routing",
        "primary_score": rate(lambda pc: pc["passed"]),
        "metrics": {
            "routing_accuracy": rate(lambda pc: pc["passed"]),
            "tier1_hit_rate": tier1_grabbed / n,
            "tier1_correct_rate": tier1_correct / tier1_grabbed if tier1_grabbed else 0.0,
            "route_up_rate": rate(lambda pc: pc["actual"]["layer"] == "route_up"),
            "overgrab_rate": overgrabs / n,
            "coverage_gap_rate": coverage_gaps / n,
            "layer_assign_correct_rate": layer_assign_correct / route_ups if route_ups else 0.0,
            "layer_counts": layer_counts,
        },
        "per_case": per_case,
    }


def _print_summary(category: dict, num_cases: int) -> None:
    print(f"Agent routing eval over agent_routing_50 ({num_cases} cases, mode={category['mode']})")
    print(f"  路由准确率 (routing_accuracy): {category['primary_score']:.2%}")
    for key, value in category["metrics"].items():
        rendered = str(value) if isinstance(value, (int, dict)) else f"{value:.2%}"
        print(f"  {key}: {rendered}")
    failed = sum(1 for pc in category["per_case"] if not pc["passed"])
    print(f"  越界抓取/覆盖缺口 (FAIL): {failed}")
    for pc in category["per_case"]:
        if not pc["passed"]:
            print(
                f"    - {pc['case_id']} expected={pc['expected']} "
                f"actual={pc['actual']} {pc['detail']}"
            )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Agent routing eval — deterministic tier-1 boundary over agent_routing_50"
    )
    parser.add_argument("--report", type=str, default="", help="JSON report output path")
    args = parser.parse_args()

    cases = load_cases("agent_routing_50.json")
    category = run_category("agent_routing", cases, evaluate_cases)
    report = {
        "summary": {
            "total_cases": len(cases),
            "categories": 1,
            "failed_cases": sum(1 for pc in category["per_case"] if not pc["passed"]),
            "overall_score": category["primary_score"],
        },
        "categories": {"agent_routing": category},
        "failures": collect_failures({"agent_routing": category}),
        "errors": {},
    }
    _print_summary(category, len(cases))
    path = (
        Path(args.report)
        if args.report
        else REPORT_DIR / f"report_agent_routing_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    write_report(report, path)
    print(f"\nReport saved to {path}")


if __name__ == "__main__":
    main()
