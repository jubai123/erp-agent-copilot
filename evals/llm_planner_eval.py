"""LLM planner tool-selection eval — real DeepSeek over planning_200.

Answers "大模型能否精准选定合适的 ERP API" with a number: drive the real LLM
plan node (build_plan_node) over the planning_200 cases and score how often the
chosen tool set matches the dataset's expected steps. The headline metric is
``tool_set_exact`` (选型准确率); ``tool_seq_exact`` (选型+顺序)、
``tool_coverage`` (是否漏选)、``contract_valid`` (计划是否通过确定性校验) and
``parse_success`` are reported alongside so "选对工具" is never conflated with
"正确调用" — the whole point of the 确定性优先 ADR.

Task 7.10 stratified the dataset from 50 to 200 cases into four layers
(single/multi × easy/hard, 50 each), so the report now carries a per-layer
score with a 95% Wilson confidence interval (each layer ≥ 30 makes the
interval meaningful), plus a drift section that compares the run against the
50-case baseline report on the *overlapping* case subset and alarms when
``tool_set_exact`` drops more than 0.02 — the acceptance gate for model drift.

The runner (``evaluate_cases``) is injectable: ``llm_complete`` is a plain
callable, so tests substitute a stub; ``main()`` wires the real OpenAI-compatible
client from Settings and writes a harness-format report. It is deliberately NOT
part of evals/run_all — it needs a real LLM key and network and is stochastic.
"""

from __future__ import annotations

import argparse
import json
import math
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from erp_copilot.agent.nodes.build_plan import SYSTEM_PROMPT, build_plan_node
from erp_copilot.agent.nodes.classify_intent import classify_intent_node
from erp_copilot.agent.nodes.validate_plan import ToolSpec, build_validate_plan_node
from erp_copilot.agent.state import AgentState, Plan, PlanValidation
from evals.harness import DATASET_DIR, RunnerOutput, collect_failures, load_cases, write_report

REPORT_DIR = Path(__file__).resolve().parent / "reports"

# Task 7.10: the stratified 200-case dataset replaces planning_50 for this eval.
DATASET_FILE = "planning_200.json"
# The pre-expansion 50-case run this eval must not silently regress against.
BASELINE_REPORT = REPORT_DIR / "report_llm_planner_deepseek_real_20260818.json"
# Acceptance gate: tool_set_exact dropping more than this below baseline alarms.
DRIFT_ALARM_THRESHOLD = 0.02
# 95% two-sided z for the Wilson score interval.
CONFIDENCE_Z = 1.96

# P3 held-out discipline (docs/08 §2.2): ~20% (40) of planning_200 is frozen as
# the clean evaluation set. Prompt tuning iterates on the dev complement; the
# reported headline always comes from heldout — the frozen manifest is the
# single source of truth for what tuning must never touch.
HELDOUT_FRACTION = 0.2
HELDOUT_MANIFEST = "planning_heldout.json"
HELDOUT_SPLITS = ("heldout", "dev", "all")


def wilson_ci(rate: float, n: int, z: float = CONFIDENCE_Z) -> tuple[float, float]:
    """95% Wilson score interval for a proportion, as (lower, upper).

    A proportion's normal-approximation interval is unreliable near 0/1 and for
    small n; Wilson's score interval handles both (it is what statistical
    calculators call the "exact" CI). n=50 at p=1.0 yields ~[0.955, 1.0], an
    honest "+/- ~4.4%" rather than a false zero-width certainty.
    """
    if n <= 0:
        return (0.0, 0.0)
    p = max(0.0, min(1.0, rate))
    z2 = z * z
    center = (p + z2 / (2 * n)) / (1 + z2 / n)
    half = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n)) / (1 + z2 / n)
    return (max(0.0, center - half), min(1.0, center + half))


def heldout_case_ids(cases: list[dict]) -> list[str]:
    """Deterministically select ~20% of *cases* as held-out, stratified.

    planning_200 is four strata of 50 (single/multi × easy/hard). Taking every
    5th case within each stratum (sorted by case_id) yields 10 per stratum =
    40 total = 20%. No RNG, so the split is reproducible and the frozen
    manifest can be verified against it on every run.
    """
    by_layer: dict[tuple[bool, str], list[dict]] = {}
    for case in cases:
        layer = (bool(case["single_step"]), str(case.get("difficulty", "easy")))
        by_layer.setdefault(layer, []).append(case)
    selected: list[str] = []
    for layer in sorted(by_layer):
        ordered = sorted(by_layer[layer], key=lambda c: c["case_id"])
        selected.extend(c["case_id"] for i, c in enumerate(ordered) if i % 5 == 0)
    return sorted(selected)


def _load_heldout_ids() -> list[str]:
    data = json.loads((DATASET_DIR / HELDOUT_MANIFEST).read_text(encoding="utf-8"))
    return data["case_ids"]


def load_planning_cases(split: str, *, cases: list[dict] | None = None) -> list[dict]:
    """Load planning_200 cases restricted to *split*.

    heldout = the frozen manifest's case_ids; dev = the complement; all =
    everything. The manifest is the boundary prompt tuning must not cross — the
    reported (clean) number always comes from heldout, tuning from dev.
    """
    all_cases = cases if cases is not None else load_cases(DATASET_FILE)
    if split == "all":
        return all_cases
    heldout = set(_load_heldout_ids())
    if split == "heldout":
        return [c for c in all_cases if c["case_id"] in heldout]
    return [c for c in all_cases if c["case_id"] not in heldout]  # dev


def validate_split_args(split: str, system: str | None) -> None:
    """Refuse prompt overrides on the frozen held-out set.

    The held-out report must measure the production prompt — tuning a prompt on
    the held-out cases would burn the clean set (docs/08 §2.2). Tuning runs on
    dev, which is what the reported number is compared against.
    """
    if split == "heldout" and system is not None:
        raise ValueError(
            "held-out 评测集只使用生产 SYSTEM_PROMPT；prompt 调优请用 --split dev"
        )


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
        "difficulty": case.get("difficulty", "easy"),
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
    system: str = SYSTEM_PROMPT,
) -> RunnerOutput:
    """Run the real classify → LLM plan → validate chain and aggregate scores.

    Mirrors the app's LLM-planner wiring (build_plan_node + validate_plan with
    injected schemas and candidate-filtered tool set) so the eval measures the
    same configuration the interactive path uses, including all four
    deterministic guards and the RISK_DOWNGRADE gate. ``system`` lets the eval
    A/B prompt variants against the production SYSTEM_PROMPT.
    """
    plan_node = build_plan_node(
        llm_complete=llm_complete,
        available_tools=available_tools,
        tool_schemas=tool_schemas,
        system=system,
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
    layers = _layer_metrics(per_case)
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
        "layers": layers,
    }


def _layer_metrics(per_case: list[dict]) -> dict[str, dict[str, float]]:
    """Stratify per-case scores into single/multi × easy/hard and add Wilson CIs.

    Task 7.10: the four layers (each ≥ 30 cases by dataset construction) get
    their own score ± 95% confidence interval so a per-layer regression is
    visible instead of being averaged into the headline number.
    """
    grouped: dict[str, list[dict]] = {}
    for pc in per_case:
        layer = f"{'single' if pc['single_step'] else 'multi'}_{pc['difficulty']}"
        grouped.setdefault(layer, []).append(pc)
    layers: dict[str, dict[str, float]] = {}
    for layer, items in sorted(grouped.items()):
        n = len(items)

        # Defaults bind the loop variables so the closure is safe to call.
        def rate(key: str, items: list[dict] = items, n: int = n) -> float:
            return sum(1 for pc in items if pc[key]) / n if n else 0.0

        lo, hi = wilson_ci(rate("tool_set_exact"), n)
        layers[layer] = {
            "num_cases": n,
            "tool_set_exact_rate": rate("tool_set_exact"),
            "ci_lower": lo,
            "ci_upper": hi,
            "ci_half_width": (hi - lo) / 2,
            "tool_seq_exact_rate": rate("tool_seq_exact"),
            "contract_valid_rate": rate("contract_valid"),
            "parse_success_rate": rate("parse_success"),
        }
    return layers


_DRIFT_METRIC_KEYS = (
    "tool_set_exact_rate",
    "tool_seq_exact_rate",
    "contract_valid_rate",
    "parse_success_rate",
)


def load_baseline_report(path: Path = BASELINE_REPORT) -> dict[str, object] | None:
    """Load the baseline report's per-case drift flags.

    Returns None when the file is absent (first run / fresh checkout) so main()
    reports "no baseline" instead of crashing. The drift gate needs per-case
    flags so both sides can be restricted to the same overlap subset — the
    full-set aggregates alone would compare like with unlike (the held-out
    false-alarm). Flags are keyed by the drift metric names ("tool_set_exact_rate", ...).
    """
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    per_case = data.get("categories", {}).get("planning_llm", {}).get("per_case", [])
    if not per_case:
        return None
    per_case_flags = {
        pc["case_id"]: {
            key: bool(pc.get(key.removesuffix("_rate"))) for key in _DRIFT_METRIC_KEYS
        }
        for pc in per_case
    }
    return {"case_ids": set(per_case_flags), "per_case_flags": per_case_flags}


def baseline_overlap_metrics(
    per_case: list[dict],
    baseline: dict[str, object] | None,
) -> dict[str, float]:
    """Headline rates on the cases the current run shares with the baseline.

    Both the current run and the baseline are restricted to the same overlap
    subset, so a harder case mix in a held-out split cannot masquerade as model
    drift. (Previously only the current side was restricted; the baseline side
    used its full-set aggregate, which fabricated a drop whenever the current
    run was a subset.) ``current_<metric>`` and ``baseline_<metric>`` are
    returned alongside the overlap size.
    """
    if baseline is None:
        return {"num_cases": 0}
    baseline_case_ids: set[str] = baseline["case_ids"]
    subset = [pc for pc in per_case if pc["case_id"] in baseline_case_ids]
    n = len(subset)
    result: dict[str, float] = {"num_cases": n}
    if n == 0:
        return result
    baseline_flags: dict[str, dict[str, bool]] = baseline["per_case_flags"]
    for key in _DRIFT_METRIC_KEYS:
        field = key.removesuffix("_rate")
        result[f"current_{key}"] = sum(1 for pc in subset if pc[field]) / n
        result[f"baseline_{key}"] = sum(
            1 for pc in subset if baseline_flags[pc["case_id"]][key]
        ) / n
    return result


def compute_drift(
    current_overlap: dict[str, float],
    baseline: dict[str, object] | None,
    threshold: float = DRIFT_ALARM_THRESHOLD,
) -> dict[str, object]:
    """Delta of current vs baseline headline rates plus the alarm flag.

    The alarm is the task 7.10 acceptance gate: ``tool_set_exact`` falling more
    than *threshold* (0.02) below the baseline on the shared case subset.
    Companion-metric deltas are reported but never alarm.
    """
    if baseline is None or current_overlap.get("num_cases", 0) == 0:
        return {"status": "no_baseline", "alarm": False, "message": "基线报告缺失或无可比子集"}
    deltas = {
        key: round(
            current_overlap[f"current_{key}"] - current_overlap[f"baseline_{key}"], 4
        )
        for key in _DRIFT_METRIC_KEYS
        if f"current_{key}" in current_overlap and f"baseline_{key}" in current_overlap
    }
    set_delta = deltas.get("tool_set_exact_rate", 0.0)
    alarm = set_delta < -threshold
    return {
        "status": "ok" if not alarm else "drift",
        "alarm": alarm,
        "threshold": threshold,
        "delta": deltas,
        "tool_set_exact_delta": set_delta,
        "message": (
            f"tool_set_exact 相对基线跌 {abs(set_delta):.2%}（超过阈值 {threshold:.0%}，告警）"
            if alarm
            else f"tool_set_exact 相对基线变化 {set_delta:+.2%}（阈值 {threshold:.0%}）"
        ),
    }


def _print_summary(category: dict, num_cases: int) -> None:
    split = category.get("split", "all")
    print(
        f"LLM planner eval over {DATASET_FILE} ({num_cases} cases, "
        f"split={split}, mode={category['mode']})"
    )
    print(f"  选型准确率 (tool_set_exact): {category['primary_score']:.2%}")
    for key, value in category["metrics"].items():
        rendered = str(value) if isinstance(value, int) else f"{value:.2%}"
        print(f"  {key}: {rendered}")
    print("  分层 (score ± 95% Wilson CI):")
    for layer, metrics in category.get("layers", {}).items():
        print(
            f"    {layer:<12} {metrics['tool_set_exact_rate']:.2%} ± "
            f"{metrics['ci_half_width']:.2%}  (n={metrics['num_cases']})"
        )
    drift = category.get("drift")
    if drift is not None:
        flag = "⚠ DRIFT" if drift["alarm"] else "ok"
        print(f"  漂移检测 [{flag}]: {drift.get('message', '')}")
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
        description=f"LLM planner tool-selection eval over {DATASET_FILE} (real LLM)"
    )
    parser.add_argument("--report", type=str, default="", help="JSON report output path")
    parser.add_argument(
        "--baseline",
        type=str,
        default="",
        help=f"baseline report path (default {BASELINE_REPORT.name})",
    )
    parser.add_argument(
        "--split",
        choices=HELDOUT_SPLITS,
        default="heldout",
        help=(
            "评测子集：heldout=冻结 20%（默认，报告只用它）；dev=调优集"
            "（prompt A/B 只在此跑）；all=全量 200"
        ),
    )
    parser.add_argument(
        "--system",
        type=str,
        default=None,
        help="覆盖 SYSTEM_PROMPT 做 A/B 调优（仅允许 --split dev/all；heldout 只测生产 prompt）",
    )
    args = parser.parse_args()

    try:
        validate_split_args(args.split, args.system)
    except ValueError as exc:
        parser.error(str(exc))

    from apps.worker.graph_builder import WORKER_TOOL_SCHEMAS
    from apps.worker.llm_planner import build_real_llm_complete
    from erp_copilot.infrastructure.config import Settings

    settings = Settings()
    llm_complete = build_real_llm_complete(settings)
    cases = load_planning_cases(args.split)
    output = evaluate_cases(
        cases,
        llm_complete=llm_complete,
        available_tools=set(WORKER_TOOL_SCHEMAS),
        tool_schemas=WORKER_TOOL_SCHEMAS,
        system=args.system if args.system is not None else SYSTEM_PROMPT,
    )
    # run_category would drop evaluate_cases' extra keys, so build the category
    # dict directly and attach the task 7.10 strata + drift sections.
    baseline_path = Path(args.baseline) if args.baseline else BASELINE_REPORT
    baseline = load_baseline_report(baseline_path)
    overlap = baseline_overlap_metrics(output["per_case"], baseline) if baseline else {}
    category = {
        "category": "planning_llm",
        "num_cases": len(cases),
        "mode": output["mode"],
        "split": args.split,
        "primary_score": output["primary_score"],
        "metrics": output["metrics"],
        "per_case": output["per_case"],
        "layers": output["layers"],
        "drift": compute_drift(overlap, baseline),
    }
    report = {
        "summary": {
            "total_cases": len(cases),
            "categories": 1,
            "failed_cases": sum(1 for pc in output["per_case"] if not pc["passed"]),
            "overall_score": output["primary_score"],
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
