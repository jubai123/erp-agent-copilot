"""Unit tests for evals/run_engineering_metrics.py — engineering metrics collectors.

Each collector is exercised with injected fakes (harness report, fault runner,
e2e driver, subprocess runner, temp code dirs) so the tests are deterministic
and offline. The gap semantics are asserted honestly: node_span/llm_call zero
call sites report FAIL, a missing pytest-cov reports NOT_CONFIGURED — neither
is papered over as a pass.
"""

from __future__ import annotations

from pathlib import Path

from erp_copilot.observability.metrics import create_metrics
from evals.harness import DATASET_SPECS
from evals.run_engineering_metrics import (
    FAIL,
    MEASURED,
    NOT_CONFIGURED,
    PASS,
    SKIPPED,
    build_report,
    collect_e2e_runtime,
    collect_observability,
    collect_process,
    collect_quality,
    format_report,
)


def _category(score: float, metrics: dict[str, object] | None = None) -> dict[str, object]:
    return {"primary_score": score, "metrics": metrics or {}, "per_case": []}


def _harness_report(
    *,
    overall: float = 0.98,
    errors: dict[str, str] | None = None,
) -> dict[str, object]:
    categories: dict[str, dict[str, object]] = {}
    for category, _ in DATASET_SPECS:
        if errors and category in errors:
            continue
        metrics: dict[str, object] = {}
        if category == "security":
            metrics = {
                "interception_rate": 1.0,
                "false_positive_rate": 0.0,
                "correct_rate": 1.0,
            }
        categories[category] = _category(0.99, metrics)
    return {
        "summary": {
            "total_cases": 175,
            "categories": len(categories),
            "failed_cases": 2,
            "overall_score": overall,
        },
        "categories": categories,
        "failures": [],
        "errors": errors or {},
    }


def _fault_report(passed: int, total: int = 3) -> dict[str, object]:
    return {
        "total": total,
        "passed": passed,
        "scenarios": [],
        "failures": [] if passed == total else [{"name": "x", "passed": False, "detail": ""}],
    }


class TestCollectQuality:
    def test_aggregates_harness_and_fault_scores(self) -> None:
        result = collect_quality(
            run_harness=lambda: _harness_report(overall=0.98),
            run_faults=lambda: _fault_report(passed=3),
        )
        assert result["group"] == "quality"
        assert result["status"] == PASS
        names = {m["name"]: m for m in result["metrics"]}
        assert names["eval_overall_score"]["value"] == 0.98
        assert names["eval_overall_score"]["status"] == MEASURED
        assert names["fault_injection_pass_rate"]["value"] == 1.0
        assert names["fault_injection_pass_rate"]["status"] == PASS
        assert names["security_interception_rate"]["value"] == 1.0
        assert names["security_false_positive_rate"]["value"] == 0.0

    def test_db_dependent_category_marks_skipped(self) -> None:
        result = collect_quality(
            run_harness=lambda: _harness_report(errors={"knowledge_rag": "OperationalError: down"}),
            run_faults=lambda: _fault_report(passed=3),
        )
        names = {m["name"]: m for m in result["metrics"]}
        assert names["category:knowledge_rag"]["status"] == SKIPPED
        assert "OperationalError" in names["category:knowledge_rag"]["evidence"]
        assert result["status"] == PASS  # SKIPPED does not fail the group

    def test_fault_failure_marks_group_fail(self) -> None:
        result = collect_quality(
            run_harness=lambda: _harness_report(),
            run_faults=lambda: _fault_report(passed=2, total=3),
        )
        names = {m["name"]: m for m in result["metrics"]}
        assert names["fault_injection_pass_rate"]["status"] == FAIL
        assert result["status"] == FAIL


class TestCollectE2ERuntime:
    def test_translates_measurements(self) -> None:
        result = collect_e2e_runtime(
            driver=lambda: {
                "happy_path_success": True,
                "timeout_detected": True,
                "phase_latency_ms": {"plan": 12.0, "execute": 40.0, "verify": 8.0},
            }
        )
        assert result["status"] == PASS
        names = {m["name"]: m for m in result["metrics"]}
        assert names["happy_path_success"]["status"] == PASS
        assert names["timeout_detected"]["status"] == PASS
        assert names["phase_latency_plan_ms"]["value"] == 12.0
        assert names["execution_success_rate"]["value"] == 1.0

    def test_timeout_failure_expected_keeps_success_rate_pass(self) -> None:
        # The timeout scenario is deliberately expected to fail; reaching FAILED
        # is a success, so the driver stays green instead of reading 0.5.
        result = collect_e2e_runtime(
            driver=lambda: {
                "happy_path_success": True,
                "timeout_detected": True,
                "phase_latency_ms": {"plan": 1.0, "execute": 2.0, "verify": 3.0},
            }
        )
        names = {m["name"]: m for m in result["metrics"]}
        assert names["execution_success_rate"]["value"] == 1.0
        assert names["execution_success_rate"]["status"] == PASS

    def test_unexpected_terminal_state_fails_success_rate(self) -> None:
        result = collect_e2e_runtime(
            driver=lambda: {
                "happy_path_success": True,
                "timeout_detected": False,
                "phase_latency_ms": {},
            }
        )
        names = {m["name"]: m for m in result["metrics"]}
        assert names["execution_success_rate"]["value"] == 0.5
        assert names["execution_success_rate"]["status"] == FAIL

    def test_reports_honest_gaps_even_when_driver_succeeds(self) -> None:
        result = collect_e2e_runtime(driver=lambda: {"happy_path_success": True})
        names = {m["name"]: m for m in result["metrics"]}
        assert names["retry_rate"]["status"] == NOT_CONFIGURED
        assert names["recovery_rate"]["status"] == NOT_CONFIGURED

    def test_driver_failure_marks_group_skipped_but_keeps_gaps(self) -> None:
        def _boom() -> dict[str, object]:
            raise RuntimeError("database unreachable")

        result = collect_e2e_runtime(driver=_boom)
        assert result["status"] == SKIPPED
        names = {m["name"]: m for m in result["metrics"]}
        assert names["happy_path_success"]["status"] == SKIPPED
        assert names["retry_rate"]["status"] == NOT_CONFIGURED


class TestCollectObservability:
    def test_metric_families_present_from_real_registry(self) -> None:
        result = collect_observability(metrics=create_metrics())
        names = {m["name"]: m for m in result["metrics"]}
        assert names["metric_families_present"]["status"] == PASS
        assert names["phase_latency_phase_label"]["status"] == PASS

    def test_zero_call_sites_reported_as_gap(self, tmp_path: Path) -> None:
        (tmp_path / "plain.py").write_text("def f():\n    return 1\n", encoding="utf-8")
        result = collect_observability(
            code_dirs=[tmp_path],
            metrics_route_file=tmp_path / "routes" / "metrics.py",
        )
        names = {m["name"]: m for m in result["metrics"]}
        assert names["node_span_call_sites"]["value"] == 0
        assert names["node_span_call_sites"]["status"] == FAIL
        assert names["llm_call_call_sites"]["status"] == FAIL
        assert result["status"] == FAIL

    def test_call_sites_found_when_wired(self, tmp_path: Path) -> None:
        (tmp_path / "wired.py").write_text(
            "@node_span\ndef step():\n    result = llm_call(prompt='x')\n    return result\n",
            encoding="utf-8",
        )
        result = collect_observability(
            code_dirs=[tmp_path],
            metrics_route_file=tmp_path / "routes" / "metrics.py",
        )
        names = {m["name"]: m for m in result["metrics"]}
        assert names["node_span_call_sites"]["value"] == 1
        assert names["node_span_call_sites"]["status"] == PASS
        assert names["llm_call_call_sites"]["value"] == 1
        assert names["llm_call_call_sites"]["status"] == PASS

    def test_missing_metrics_route_flagged(self, tmp_path: Path) -> None:
        result = collect_observability(
            code_dirs=[tmp_path],
            metrics_route_file=tmp_path / "nonexistent.py",
        )
        names = {m["name"]: m for m in result["metrics"]}
        assert names["metrics_route_registered"]["status"] == FAIL


class TestCollectProcess:
    def test_code_scale_from_temp_dirs(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        src.mkdir()
        (src / "a.py").write_text("def a():\n    return 1\n\n\n", encoding="utf-8")
        (src / "b.py").write_text("x = 1\n", encoding="utf-8")
        tests = tmp_path / "tests"
        tests.mkdir()
        (tests / "test_x.py").write_text("def test_a():\n    assert True\n", encoding="utf-8")
        result = collect_process(
            code_dirs={"src": src},
            tests_dir=tests,
            run_cmd=lambda cmd: 0,
            coverage_available=False,
        )
        names = {m["name"]: m for m in result["metrics"]}
        assert names["code_files_src"]["value"] == 2
        assert names["code_loc_src"]["value"] == 3  # a.py: def+return=2, b.py: x=1
        assert names["test_functions_count"]["value"] == 1

    def test_test_count_skips_non_test_defs(self, tmp_path: Path) -> None:
        tests = tmp_path / "tests"
        tests.mkdir()
        (tests / "helper.py").write_text(
            "def helper():\n    return 1\n\ndef test_real():\n    pass\n", encoding="utf-8"
        )
        result = collect_process(
            code_dirs={"src": tmp_path},
            tests_dir=tests,
            run_cmd=lambda cmd: 0,
            coverage_available=False,
        )
        names = {m["name"]: m for m in result["metrics"]}
        assert names["test_functions_count"]["value"] == 1

    def test_coverage_not_configured_when_pytest_cov_missing(self, tmp_path: Path) -> None:
        result = collect_process(
            code_dirs={"src": tmp_path},
            tests_dir=tmp_path,
            run_cmd=lambda cmd: 0,
            coverage_available=False,
        )
        names = {m["name"]: m for m in result["metrics"]}
        assert names["line_coverage_percent"]["status"] == NOT_CONFIGURED

    def test_coverage_pass_above_threshold(self, tmp_path: Path) -> None:
        result = collect_process(
            code_dirs={"src": tmp_path},
            tests_dir=tmp_path,
            run_cmd=lambda cmd: 0,
            coverage_available=True,
            coverage_runner=lambda: {"percent_covered": 85.0},
        )
        names = {m["name"]: m for m in result["metrics"]}
        assert names["line_coverage_percent"]["value"] == 85.0
        assert names["line_coverage_percent"]["status"] == PASS

    def test_mypy_ruff_fail_on_nonzero_return(self, tmp_path: Path) -> None:
        def _run(cmd: list[str]) -> int:
            return 0 if "mypy" in cmd else 1

        result = collect_process(
            code_dirs={"src": tmp_path},
            tests_dir=tmp_path,
            run_cmd=_run,
            coverage_available=False,
        )
        names = {m["name"]: m for m in result["metrics"]}
        assert names["mypy_clean"]["status"] == PASS
        assert names["ruff_clean"]["status"] == FAIL


class TestBuildReport:
    def test_schema_and_overall_status(self, tmp_path: Path) -> None:
        def _all_pass() -> dict[str, object]:
            return {"group": "g", "status": PASS, "metrics": []}

        def _one_fail() -> dict[str, object]:
            return {
                "group": "g",
                "status": FAIL,
                "metrics": [
                    {
                        "name": "x",
                        "value": 0,
                        "unit": "",
                        "acceptance": "≥1",
                        "status": FAIL,
                        "evidence": "",
                    }
                ],
            }

        report = build_report(collectors={"ok": _all_pass})
        assert set(report["groups"]) == {"ok"}
        assert report["overall_status"] == PASS
        assert report["gaps"] == []

        report = build_report(collectors={"ok": _one_fail})
        assert report["overall_status"] == FAIL
        assert any(g["name"] == "x" for g in report["gaps"])

    def test_collector_exception_marks_group_skipped(self) -> None:
        def _boom() -> dict[str, object]:
            raise RuntimeError("boom")

        report = build_report(collectors={"broken": _boom})
        assert report["groups"]["broken"]["status"] == SKIPPED
        assert report["overall_status"] == SKIPPED  # a crashed group stops overall at SKIPPED

    def test_format_report_lists_groups_and_gaps(self) -> None:
        report = build_report(
            collectors={
                "quality": lambda: {
                    "group": "quality",
                    "status": FAIL,
                    "metrics": [
                        {
                            "name": "node_span_call_sites",
                            "value": 0,
                            "unit": "",
                            "acceptance": "≥1",
                            "status": FAIL,
                            "evidence": "生产代码未接线",
                        }
                    ],
                }
            }
        )
        text = format_report(report)
        assert "quality" in text
        assert "node_span_call_sites" in text
        assert "overall" in text.lower()
