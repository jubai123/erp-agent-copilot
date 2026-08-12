"""Unit tests for the eval harness framework — task 6.6.

The harness (evals/harness.py) is the runner-agnostic orchestration layer:
load the five dataset files, run each category through an injected runner,
and aggregate per-category + overall scores with a detailed failure log.
These tests drive the framework with deterministic fake runners (no LLM, no
DB, no network); the real five runners are exercised in test_run_all.py.
"""

from __future__ import annotations

import json

import pytest

from evals.harness import (
    DATASET_SPECS,
    RunnerFn,
    collect_failures,
    format_report,
    load_cases,
    overall_score,
    run_all,
    run_category,
    write_report,
)

SPEC_COUNTS = {
    "tool_retrieval": 40,
    "knowledge_rag": 40,
    "planning": 50,
    "recover_or_replan": 20,
    "security": 25,
}


def _fake_runner(primary: float, fails: int = 0) -> RunnerFn:
    def runner(cases: list[dict]):
        per_case = []
        for index, case in enumerate(cases):
            failed = index < fails
            per_case.append(
                {
                    "case_id": case["case_id"],
                    "passed": not failed,
                    "expected": "x",
                    "actual": "y" if failed else "x",
                    "detail": "boom" if failed else "",
                }
            )
        return {
            "mode": "fake",
            "primary_score": primary,
            "metrics": {"mean": primary},
            "per_case": per_case,
        }

    return runner


class TestDatasets:
    def test_specs_cover_five_files(self) -> None:
        assert len(DATASET_SPECS) == 5
        assert dict(DATASET_SPECS).keys() == set(SPEC_COUNTS)

    def test_load_cases_loads_spec_file(self) -> None:
        filename = dict(DATASET_SPECS)["recover_or_replan"]
        cases = load_cases(filename)
        assert len(cases) == SPEC_COUNTS["recover_or_replan"]

    def test_total_cases_is_175(self) -> None:
        total = sum(len(load_cases(filename)) for _, filename in DATASET_SPECS)
        assert total == 175


class TestRunCategory:
    def test_fills_category_and_num_cases(self) -> None:
        cases = [{"case_id": "a"}, {"case_id": "b"}]
        result = run_category("planning", cases, _fake_runner(0.5))
        assert result["category"] == "planning"
        assert result["num_cases"] == 2
        assert result["primary_score"] == 0.5
        assert result["mode"] == "fake"

    def test_requires_primary_score(self) -> None:
        def bad(cases: list[dict]) -> dict:
            return {"mode": "fake", "metrics": {}, "per_case": []}

        with pytest.raises(ValueError, match="primary_score"):
            run_category("planning", [], bad)

    def test_requires_per_case(self) -> None:
        def bad(cases: list[dict]) -> dict:
            return {"mode": "fake", "primary_score": 1.0, "metrics": {}}

        with pytest.raises(ValueError, match="per_case"):
            run_category("planning", [], bad)


class TestRunAll:
    def test_runs_all_five_categories(self) -> None:
        runners = {category: _fake_runner(0.8) for category, _ in DATASET_SPECS}
        report = run_all(runners)
        assert set(report["categories"]) == {category for category, _ in DATASET_SPECS}
        assert report["summary"]["total_cases"] == 175
        assert report["summary"]["categories"] == 5

    def test_runner_error_is_isolated(self) -> None:
        def boom(cases: list[dict]) -> None:
            raise RuntimeError("kaboom")

        runners = {category: _fake_runner(0.9) for category, _ in DATASET_SPECS}
        runners["recover_or_replan"] = boom
        report = run_all(runners)
        assert report["categories"]["recover_or_replan"]["mode"] == "error"
        assert report["errors"] == {"recover_or_replan": "RuntimeError: kaboom"}
        # Other four categories still score; the errored category counts as 0.
        assert report["summary"]["overall_score"] == pytest.approx(0.9 * 155 / 175)

    def test_overall_weighted_by_case_count(self) -> None:
        categories = {
            "tool_retrieval": {
                "category": "tool_retrieval",
                "mode": "fake",
                "num_cases": 40,
                "primary_score": 1.0,
                "metrics": {},
                "per_case": [],
            },
            "security": {
                "category": "security",
                "mode": "fake",
                "num_cases": 25,
                "primary_score": 0.0,
                "metrics": {},
                "per_case": [],
            },
        }
        assert overall_score(categories) == pytest.approx(40 * 1.0 / 65)


class TestFailuresAndReport:
    def test_collect_failures_flattens(self) -> None:
        categories = {
            "tool_retrieval": {
                "category": "tool_retrieval",
                "mode": "fake",
                "num_cases": 1,
                "primary_score": 0.0,
                "metrics": {},
                "per_case": [
                    {
                        "case_id": "tool-001",
                        "passed": False,
                        "expected": "getProductByName",
                        "actual": "getProductById",
                        "detail": "wrong tool",
                    }
                ],
            }
        }
        failures = collect_failures(categories)
        assert failures == [
            {
                "category": "tool_retrieval",
                "case_id": "tool-001",
                "expected": "getProductByName",
                "actual": "getProductById",
                "detail": "wrong tool",
            }
        ]

    def test_format_report_contains_scores_and_failures(self) -> None:
        report = {
            "summary": {
                "total_cases": 2,
                "categories": 1,
                "failed_cases": 1,
                "overall_score": 0.5,
            },
            "categories": {
                "tool_retrieval": {
                    "category": "tool_retrieval",
                    "mode": "fake",
                    "num_cases": 2,
                    "primary_score": 0.5,
                    "metrics": {},
                    "per_case": [
                        {
                            "case_id": "tool-001",
                            "passed": True,
                            "expected": "x",
                            "actual": "x",
                            "detail": "",
                        },
                        {
                            "case_id": "tool-002",
                            "passed": False,
                            "expected": "x",
                            "actual": "y",
                            "detail": "wrong",
                        },
                    ],
                }
            },
            "failures": [
                {
                    "category": "tool_retrieval",
                    "case_id": "tool-002",
                    "expected": "x",
                    "actual": "y",
                    "detail": "wrong",
                }
            ],
            "errors": {},
        }
        text = format_report(report)
        assert "tool_retrieval" in text
        assert "50.00%" in text
        assert "tool-002" in text
        assert "wrong" in text

    def test_write_report_roundtrip(self, tmp_path) -> None:
        report = {
            "summary": {
                "total_cases": 1,
                "categories": 1,
                "failed_cases": 0,
                "overall_score": 1.0,
            },
            "categories": {},
            "failures": [],
            "errors": {},
        }
        path = tmp_path / "report.json"
        write_report(report, path)
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["summary"]["overall_score"] == 1.0
