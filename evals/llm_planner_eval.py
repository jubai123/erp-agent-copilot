"""LLM planner tool-selection eval — real DeepSeek over planning_50.

Answers "大模型能否精准选定合适的 ERP API" with a number: drive the real LLM
plan node (build_plan_node) over the planning_50 cases and score how often the
chosen tool set matches the dataset's expected steps. The headline metric is
``tool_set_exact`` (选型准确率); ``tool_seq_exact`` (选型+顺序)、
``tool_coverage`` (是否漏选)、``contract_valid`` (计划是否通过确定性校验) and
``parse_success`` are reported alongside so "选对工具" is never conflated with
"正确调用" — the whole point of the 确定性优先 ADR.

The runner (``evaluate_cases``) is injectable: ``llm_complete`` is a plain
callable, so tests substitute a stub; ``main()`` wires the real OpenAI-compatible
client from Settings and writes a harness-format report. It is deliberately NOT
part of evals/run_all — it needs a real LLM key and network and is stochastic.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from erp_copilot.agent.nodes.build_plan import build_plan_node
from erp_copilot.agent.nodes.classify_intent import classify_intent_node
from erp_copilot.agent.nodes.validate_plan import ToolSpec, build_validate_plan_node
from erp_copilot.agent.state import AgentState, Plan, PlanValidation
from evals.harness import (
    RunnerOutput,
    collect_failures,
    load_cases,
    run_category,
    write_report,
)

if TYPE_CHECKING:
    from erp_copilot.infrastructure.config import Settings

REPORT_DIR = Path(__file__).resolve().parent / "reports"


def _failure_detail(case: dict, actual: list[str], validation_errors: list[str]) -> str:
    expected = [step["tool"] for step in case["steps"]]
    if not actual:
        return "LLM 输出未能解析为 Plan"
    if set(actual) != set(expected):
        return f"选定工具 {set(actual)!r} != 期望 {set(expected)!r}"
    if actual != expected:
        return f"工具顺序 {actual!r} != 期望 {expected!r}"
    if validation_errors:
        return f"工具选对但计划未通过确定性校验: {validation_errors!r}"
    return ""


def score_plan(case: dict, plan: Plan | None, validation: PlanValidation | None) -> dict:
    """Measure one LLM-produced plan against the case's expected steps.

    Returns a per-case measurement dict consumed by evaluate_cases. ``passed``
    means the LLM selected the right tool set AND the plan survives the
    deterministic gate (validate_plan) — a correctly selected write tool can
    still be labeled READ and fail the RISK_DOWNGRADE gate, and that case must
    not count as passed. ``tool_set_exact`` (选型准确率) is reported separately
    so selection and contract validity stay independent dimensions.
    """
    expected = [step["tool"] for step in case["steps"]]
    actual = [step.tool_name for step in plan.steps] if plan is not None else []
    expected_set = set(expected)
    actual_set = set(actual)
    tool_coverage = (
        len(expected_set & actual_set) / len(expected_set)
        if plan is not None and expected_set
        else 0.0
    )
    tool_seq_exact = plan is not None and actual == expected
    tool_set_exact = plan is not None and actual_set == expected_set
    contract_valid = validation is not None and validation.is_valid
    validation_errors = [e.code for e in validation.errors] if validation is not None else []
    return {
        "case_id": case["case_id"],
        "expected": expected,
        "actual": actual,
        "single_step": bool(case["single_step"]),
        "parse_success": plan is not None,
        "tool_seq_exact": tool_seq_exact,
        "tool_set_exact": tool_set_exact,
        "tool_coverage": tool_coverage,
        "contract_valid": contract_valid,
        "validation_errors": validation_errors,
        "passed": tool_set_exact and contract_valid,
        "detail": _failure_detail(case, actual, validation_errors),
    }


def evaluate_cases(
    cases: list[dict],
    *,
    llm_complete: Callable[[str], str],
    available_tools: set[str],
    tool_schemas: dict[str, ToolSpec],
) -> RunnerOutput:
    """Run the real classify → LLM plan → validate chain and aggregate scores.

    Mirrors the app's LLM-planner wiring (build_plan_node + validate_plan with
    injected schemas and candidate-filtered tool set) so the eval measures the
    same configuration the interactive path uses, including all four
    deterministic guards and the RISK_DOWNGRADE gate.
    """
    plan_node = build_plan_node(
        llm_complete=llm_complete,
        available_tools=available_tools,
        tool_schemas=tool_schemas,
    )
    validate_node = build_validate_plan_node(tool_schemas=tool_schemas)

    per_case: list[dict] = []
    for case in cases:
        state = AgentState(
            run_id=f"llm-eval-{case['case_id']}", tenant_id="eval", query=case["query"]
        )
        state = state.model_copy(update=classify_intent_node(state))
        state = state.model_copy(update=plan_node(state))
        state = state.model_copy(update=validate_node(state))
        per_case.append(score_plan(case, state.plan, state.plan_validation))

    n = len(per_case)

    def rate(key: str) -> float:
        return sum(1 for pc in per_case if pc[key]) / n if n else 0.0

    def mean(key: str) -> float:
        return sum(pc[key] for pc in per_case) / n if n else 0.0

    def set_acc(items: list[dict]) -> float:
        return sum(1 for pc in items if pc["tool_set_exact"]) / len(items) if items else 0.0

    def rate_with(error_code: str) -> float:
        return sum(1 for pc in per_case if error_code in pc["validation_errors"]) / n if n else 0.0

    single = [pc for pc in per_case if pc["single_step"]]
    multi = [pc for pc in per_case if not pc["single_step"]]
    return {
        "mode": "llm_planner",
        "primary_score": rate("tool_set_exact"),
        "metrics": {
            "tool_set_exact_rate": rate("tool_set_exact"),
            "tool_seq_exact_rate": rate("tool_seq_exact"),
            "tool_coverage": mean("tool_coverage"),
            "contract_valid_rate": rate("contract_valid"),
            "parse_success_rate": rate("parse_success"),
            "single_step_set_acc": set_acc(single),
            "multi_step_set_acc": set_acc(multi),
            "multi_step_count": len(multi),
            "risk_downgrade_rate": rate_with("RISK_DOWNGRADE"),
        },
        "per_case": per_case,
    }


def build_real_llm_complete(settings: Settings) -> Callable[[str], str]:
    """OpenAI-compatible chat client (DeepSeek) backed by Settings."""
    from openai import OpenAI

    client = OpenAI(
        api_key=settings.llm_api_key.get_secret_value(),
        base_url=settings.llm_base_url,
    )

    def llm_complete(prompt: str) -> str:
        resp = client.chat.completions.create(
            model=settings.llm_model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
        )
        return resp.choices[0].message.content or ""

    return llm_complete


def _print_summary(category: dict, num_cases: int) -> None:
    print(f"LLM planner eval over planning_50 ({num_cases} cases, mode={category['mode']})")
    print(f"  选型准确率 (tool_set_exact): {category['primary_score']:.2%}")
    for key, value in category["metrics"].items():
        rendered = str(value) if isinstance(value, int) else f"{value:.2%}"
        print(f"  {key}: {rendered}")
    failed = sum(1 for pc in category["per_case"] if not pc["passed"])
    print(f"  未通过 (选型错或契约不过): {failed}")
    for pc in category["per_case"]:
        if not pc["passed"]:
            print(
                f"    - {pc['case_id']} expected={pc['expected']} "
                f"actual={pc['actual']} {pc['detail']}"
            )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="LLM planner tool-selection eval over planning_50 (real LLM)"
    )
    parser.add_argument("--report", type=str, default="", help="JSON report output path")
    args = parser.parse_args()

    from apps.worker.graph_builder import WORKER_TOOL_SCHEMAS
    from erp_copilot.infrastructure.config import Settings

    settings = Settings()
    llm_complete = build_real_llm_complete(settings)
    cases = load_cases("planning_50.json")
    category = run_category(
        "planning_llm",
        cases,
        lambda c: evaluate_cases(
            c,
            llm_complete=llm_complete,
            available_tools=set(WORKER_TOOL_SCHEMAS),
            tool_schemas=WORKER_TOOL_SCHEMAS,
        ),
    )
    report = {
        "summary": {
            "total_cases": len(cases),
            "categories": 1,
            "failed_cases": sum(1 for pc in category["per_case"] if not pc["passed"]),
            "overall_score": category["primary_score"],
        },
        "categories": {"planning_llm": category},
        "failures": collect_failures({"planning_llm": category}),
        "errors": {},
    }
    _print_summary(category, len(cases))
    path = (
        Path(args.report)
        if args.report
        else REPORT_DIR / f"report_llm_planner_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    write_report(report, path)
    print(f"\nReport saved to {path}")


if __name__ == "__main__":
    main()
