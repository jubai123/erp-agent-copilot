"""Eval harness framework — task 6.6.

Runner-agnostic orchestration: load the five dataset files, run each category
through an injected runner function, and aggregate per-category + overall
scores with a detailed failure log. The framework knows nothing about how a
category is scored — runners return a normalized RunnerOutput and the harness
handles loading, counting, aggregation, and reporting.

The real five runners live in evals/run_all.py; tests inject fake runners here
so the framework is exercised deterministically (no LLM, no DB, no network).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol, TypedDict

DATASET_DIR = Path(__file__).resolve().parent / "datasets"

# Ordered like docs/08 section 2.
DATASET_SPECS: tuple[tuple[str, str], ...] = (
    ("tool_retrieval", "tool_retrieval_40.json"),
    ("knowledge_rag", "knowledge_rag_40.json"),
    ("planning", "planning_50.json"),
    ("recover_or_replan", "recover_or_replan_20.json"),
    ("security", "security_25.json"),
)


class PerCaseResult(TypedDict):
    case_id: str
    passed: bool
    expected: object
    actual: object
    detail: str


class RunnerOutput(TypedDict):
    mode: str
    primary_score: float
    metrics: dict[str, object]
    per_case: list[PerCaseResult]


class CategoryResult(RunnerOutput):
    category: str
    num_cases: int


class RunnerFn(Protocol):
    def __call__(self, cases: list[dict]) -> RunnerOutput: ...


def load_cases(filename: str) -> list[dict]:
    """Load a dataset file's cases."""
    data = json.loads((DATASET_DIR / filename).read_text(encoding="utf-8"))
    return data["cases"]


def run_category(category: str, cases: list[dict], runner: RunnerFn) -> CategoryResult:
    """Run *runner* over *cases* and normalize into a CategoryResult."""
    output = runner(cases)
    if "primary_score" not in output:
        raise ValueError(f"runner for {category} returned no primary_score")
    if "per_case" not in output:
        raise ValueError(f"runner for {category} returned no per_case")
    return {
        "category": category,
        "num_cases": len(cases),
        "mode": output["mode"],
        "primary_score": output["primary_score"],
        "metrics": output["metrics"],
        "per_case": output["per_case"],
    }


def overall_score(categories: dict[str, CategoryResult]) -> float:
    """Weighted mean of category primary scores by case count."""
    total_weight = sum(c["num_cases"] for c in categories.values())
    if total_weight == 0:
        return 0.0
    return sum(c["primary_score"] * c["num_cases"] for c in categories.values()) / total_weight


def collect_failures(categories: dict[str, CategoryResult]) -> list[dict[str, object]]:
    """Flatten per-case failures into the report's detailed failure log."""
    failures: list[dict[str, object]] = []
    for category, result in categories.items():
        for pc in result["per_case"]:
            if not pc["passed"]:
                failures.append(
                    {
                        "category": category,
                        "case_id": pc["case_id"],
                        "expected": pc["expected"],
                        "actual": pc["actual"],
                        "detail": pc["detail"],
                    }
                )
    return failures


def run_all(runners: dict[str, RunnerFn]) -> dict[str, object]:
    """Run every category through its runner and build the report.

    A failing category is recorded with mode "error" and a zero primary score
    (and reported under ``errors``) so one broken runner never aborts the rest.
    """
    categories: dict[str, CategoryResult] = {}
    errors: dict[str, str] = {}
    for category, filename in DATASET_SPECS:
        cases = load_cases(filename)
        try:
            categories[category] = run_category(category, cases, runners[category])
        except Exception as exc:
            categories[category] = {
                "category": category,
                "mode": "error",
                "num_cases": len(cases),
                "primary_score": 0.0,
                "metrics": {},
                "per_case": [],
            }
            errors[category] = f"{type(exc).__name__}: {exc}"
    failures = collect_failures(categories)
    return {
        "summary": {
            "total_cases": sum(c["num_cases"] for c in categories.values()),
            "categories": len(categories),
            "failed_cases": len(failures),
            "overall_score": overall_score(categories),
        },
        "categories": categories,
        "failures": failures,
        "errors": errors,
    }


def format_report(report: dict[str, object]) -> str:
    """Render the report as a human-readable text table plus failure log."""
    summary = report["summary"]
    categories = report["categories"]
    failures = report["failures"]
    errors = report["errors"]
    lines = [
        "=" * 60,
        "Eval Harness Report",
        "=" * 60,
        f"Total cases : {summary['total_cases']}  ({summary['categories']} categories)",
        f"Overall     : {summary['overall_score']:.2%}",
        f"Failures    : {summary['failed_cases']}",
        "",
    ]
    for category, _ in DATASET_SPECS:
        result = categories.get(category)
        if result is None:
            continue
        lines.append(
            f"  {category:<15} {result['primary_score']:>7.2%}  "
            f"({result['num_cases']:>3} cases, {result['mode']})"
        )
    if errors:
        lines.append("")
        lines.append("Runner errors:")
        for category, message in errors.items():
            lines.append(f"  [{category}] {message}")
    if failures:
        lines.append("")
        lines.append(f"Failed cases ({len(failures)}):")
        for failure in failures:
            lines.append(
                f"  [{failure['category']}] {failure['case_id']} "
                f"expected={failure['expected']!r} actual={failure['actual']!r} {failure['detail']}"
            )
    else:
        lines.append("")
        lines.append("No failed cases.")
    lines.append("=" * 60)
    return "\n".join(lines)


def write_report(report: dict[str, object], path: Path) -> None:
    """Persist the report as JSON (Chinese text preserved)."""
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
