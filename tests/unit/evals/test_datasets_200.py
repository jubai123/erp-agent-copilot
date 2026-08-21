"""Validation tests for the evaluation dataset files — task 6.5.

Five category files in evals/datasets/ total 205 independently executable
cases (docs/08 section 2). These tests assert the count, per-category
schema, enumerated values, and grounding of every case against the
authoritative sources: V6_TOOL_NAMES / DOMAIN_TOOL_MAP
(src/erp_copilot/tools/candidate_filter.py), the knowledge-base
frontmatter IDs, and the ERP Simulator seed data. No LLM and no network.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from apps.erp_simulator.data.products import SEED_PRODUCTS
from apps.erp_simulator.data.suppliers import SEED_SUPPLIERS
from erp_copilot.tools.candidate_filter import (
    DOMAIN_TOOL_MAP,
    V6_TOOL_NAMES,
    filter_candidates,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
DATASETS = PROJECT_ROOT / "evals" / "datasets"
KNOWLEDGE = PROJECT_ROOT / "datasets" / "knowledge"

EXPECTED_COUNTS: dict[str, int] = {
    "tool_retrieval_40.json": 56,
    "knowledge_rag_40.json": 40,
    "planning_50.json": 64,
    "recover_or_replan_20.json": 20,
    "security_25.json": 25,
}

REGIONS = {"上海", "南京", "北京", "天津", "广州", "深圳", "成都", "重庆", "西安", "兰州"}
PRODUCT_NAMES = {p.name for p in SEED_PRODUCTS}
SUPPLIER_NAMES = {s.name for s in SEED_SUPPLIERS}


def _load(filename: str) -> dict:
    return json.loads((DATASETS / filename).read_text(encoding="utf-8"))


def _knowledge_doc_ids() -> set[str]:
    ids: set[str] = set()
    for path in KNOWLEDGE.rglob("*.md"):
        match = re.search(r"^document_id:\s*(\S+)", path.read_text(encoding="utf-8"), re.M)
        if match:
            ids.add(match.group(1))
    return ids


def _case_ids(filename: str) -> list[str]:
    return [c["case_id"] for c in _load(filename)["cases"]]


class TestAggregate:
    def test_all_five_files_exist(self) -> None:
        for filename in EXPECTED_COUNTS:
            assert (DATASETS / filename).exists(), filename

    def test_each_file_is_valid_json_with_metadata(self) -> None:
        for filename in EXPECTED_COUNTS:
            data = _load(filename)
            assert data["description"]
            assert data["version"]

    def test_five_categories_total_205(self) -> None:
        total = sum(len(_load(f)["cases"]) for f in EXPECTED_COUNTS)
        assert total == 205

    def test_per_file_counts(self) -> None:
        for filename, expected in EXPECTED_COUNTS.items():
            assert len(_load(filename)["cases"]) == expected, filename

    def test_case_ids_unique_within_each_file(self) -> None:
        for filename in EXPECTED_COUNTS:
            ids = _case_ids(filename)
            assert len(ids) == len(set(ids)), f"duplicate case_id in {filename}"


class TestToolRetrieval:
    _WRITE_TOOLS = {"createOrder", "updateOrderStatus", "cancelOrder"}

    def test_schema_and_grounding(self) -> None:
        known = DOMAIN_TOOL_MAP
        for case in _load("tool_retrieval_40.json")["cases"]:
            required = {
                "case_id",
                "query",
                "domain",
                "action",
                "expected_tool",
                "candidate_tools",
                "hard_negative",
            }
            assert required <= set(case), case["case_id"]
            assert set(case) - required <= {"note", "confusion_tool"}
            assert case["expected_tool"] in V6_TOOL_NAMES, case["case_id"]
            assert set(case["candidate_tools"]) <= V6_TOOL_NAMES, case["case_id"]
            assert case["expected_tool"] in case["candidate_tools"], case["case_id"]
            assert (case["domain"], case["action"]) in known, case["case_id"]

    def test_expected_tools_cover_whole_registry(self) -> None:
        """Every registered tool has at least one retrieval eval case.

        tool_retrieval_56 covers all 25 tools in V6_TOOL_NAMES — the M5
        invariant "每个注册工具都有评测用例" restored after the 25-tool V5
        alignment.
        """
        expected = {c["expected_tool"] for c in _load("tool_retrieval_40.json")["cases"]}
        assert expected == set(V6_TOOL_NAMES)

    def test_hard_negatives_present(self) -> None:
        cases = _load("tool_retrieval_40.json")["cases"]
        hard = [c for c in cases if c["hard_negative"]]
        assert len(hard) >= 8

    def test_hard_negative_cases_name_confusion_tool(self) -> None:
        """Every trap names the tool it would be misrouted to; non-trap cases
        carry no confusion_tool."""
        for case in _load("tool_retrieval_40.json")["cases"]:
            if case["hard_negative"]:
                assert case.get("confusion_tool"), case["case_id"]
            else:
                assert "confusion_tool" not in case, case["case_id"]

    def test_confusion_tool_registered_and_distinct(self) -> None:
        for case in _load("tool_retrieval_40.json")["cases"]:
            if not case["hard_negative"]:
                continue
            confusion = case["confusion_tool"]
            assert confusion in V6_TOOL_NAMES, case["case_id"]
            assert confusion != case["expected_tool"], case["case_id"]

    def test_confusion_tool_grounded_in_cross_intent(self) -> None:
        """The trap must be real: some OTHER (domain, action) intent surfaces the
        confusion tool, so a misroute would actually select it."""
        for case in _load("tool_retrieval_40.json")["cases"]:
            if not case["hard_negative"]:
                continue
            confusion = case["confusion_tool"]
            others = [
                filter_candidates(domain, action)
                for (domain, action) in DOMAIN_TOOL_MAP
                if (domain, action) != (case["domain"], case["action"])
            ]
            assert any(confusion in cands for cands in others), (
                f"{case['case_id']}: confusion {confusion} reachable only from "
                "the case's own intent"
            )

    def test_read_hard_negative_confusion_is_write_tool(self) -> None:
        """A read-intent (query/check_stock) trap asserts the query is NOT routed
        to a write tool — so its confusion_tool must be a write tool."""
        for case in _load("tool_retrieval_40.json")["cases"]:
            if case["hard_negative"] and case["action"] in {"query", "check_stock"}:
                assert case["confusion_tool"] in self._WRITE_TOOLS, case["case_id"]

    def test_read_intents_never_expect_write_tool(self) -> None:
        """A read intent (query/check_stock) must not have a write tool as
        its primary expected tool — otherwise Recall@5 is meaningless."""
        for case in _load("tool_retrieval_40.json")["cases"]:
            if case["domain"] == "order" and case["action"] in {"query", "cancel", "modify"}:
                continue
            if case["action"] in {"query", "check_stock"}:
                assert not case["expected_tool"].startswith(("create", "update", "cancel")), case[
                    "case_id"
                ]


class TestKnowledgeRag:
    def test_schema(self) -> None:
        doc_ids = _knowledge_doc_ids()
        assert doc_ids, "knowledge base has no frontmatter ids"
        for case in _load("knowledge_rag_40.json")["cases"]:
            required = {"case_id", "query", "relevant_docs", "should_answer"}
            assert required <= set(case), case["case_id"]
            assert set(case) - required <= {"answer_fact", "category", "note", "hard_negative_docs"}
            assert isinstance(case["should_answer"], bool)
            assert isinstance(case["relevant_docs"], list)
            if case["should_answer"]:
                assert case["relevant_docs"], f"{case['case_id']}: answerable but no docs"
                assert "answer_fact" in case, case["case_id"]
                assert set(case["relevant_docs"]) <= doc_ids, case["case_id"]
                assert "hard_negative_docs" in case, case["case_id"]
                assert isinstance(case["hard_negative_docs"], list)
                assert case["hard_negative_docs"], f"{case['case_id']}: no hard negatives"
                assert set(case["hard_negative_docs"]) <= doc_ids, case["case_id"]
                assert not set(case["hard_negative_docs"]) & set(case["relevant_docs"]), (
                    f"{case['case_id']}: hard_negative overlaps relevant_docs"
                )
            else:
                assert case["relevant_docs"] == [], case["case_id"]
                assert "hard_negative_docs" not in case, case["case_id"]

    def test_no_answer_cases_present(self) -> None:
        cases = _load("knowledge_rag_40.json")["cases"]
        assert sum(1 for c in cases if not c["should_answer"]) >= 5

    def test_all_answerable_have_hard_negatives(self) -> None:
        cases = _load("knowledge_rag_40.json")["cases"]
        answerable = [c for c in cases if c["should_answer"]]
        assert answerable, "no answerable cases"
        missing = [c["case_id"] for c in answerable if not c["hard_negative_docs"]]
        assert not missing, f"answerable cases missing hard negatives: {missing}"

    def test_hard_negatives_disjoint_from_relevant(self) -> None:
        for case in _load("knowledge_rag_40.json")["cases"]:
            if not case["should_answer"]:
                continue
            relevant = set(case["relevant_docs"])
            hard = set(case["hard_negative_docs"])
            assert not hard & relevant, f"{case['case_id']}: {hard & relevant} also relevant"


class TestPlanning:
    def test_schema_and_tool_grounding(self) -> None:
        for case in _load("planning_50.json")["cases"]:
            required = {"case_id", "query", "steps", "single_step", "required_params"}
            assert required <= set(case), case["case_id"]
            assert set(case) - required <= {"note"}
            assert isinstance(case["single_step"], bool)
            assert len(case["steps"]) >= 1, case["case_id"]
            assert case["single_step"] == (len(case["steps"]) == 1), case["case_id"]
            for step in case["steps"]:
                assert step["tool"] in V6_TOOL_NAMES, f"{case['case_id']}: {step['tool']}"
                assert isinstance(step["params"], dict), case["case_id"]

    def test_dependency_refs_point_backward(self) -> None:
        for case in _load("planning_50.json")["cases"]:
            for index, step in enumerate(case["steps"], start=1):
                for value in step["params"].values():
                    if isinstance(value, str):
                        for ref in re.findall(r"\$(\d+)\.", value):
                            assert int(ref) < index, (
                                f"{case['case_id']} step {index}: ref ${ref} not backward"
                            )

    def test_multi_step_cases_present(self) -> None:
        cases = _load("planning_50.json")["cases"]
        assert sum(1 for c in cases if not c["single_step"]) >= 15

    def test_first_step_has_no_forward_dependency(self) -> None:
        for case in _load("planning_50.json")["cases"]:
            first = case["steps"][0]
            for value in first["params"].values():
                assert not isinstance(value, str) or "$" not in value, case["case_id"]


class TestRecovery:
    _ACTIONS = {"ask_missing", "confirm_conflict", "retry", "reject"}

    def test_schema_and_actions(self) -> None:
        for case in _load("recovery_25.json")["cases"]:
            required = {"case_id", "query", "missing_params", "conflict", "expected_action"}
            assert required <= set(case), case["case_id"]
            assert set(case) - required <= {"expected_params", "note"}
            assert case["expected_action"] in self._ACTIONS, case["case_id"]
            assert isinstance(case["missing_params"], list), case["case_id"]

    def test_action_coherence(self) -> None:
        for case in _load("recovery_25.json")["cases"]:
            if case["expected_action"] == "ask_missing":
                assert case["missing_params"], case["case_id"]
                assert case["conflict"] is None, case["case_id"]
            elif case["expected_action"] in {"confirm_conflict", "reject"}:
                assert case["conflict"], case["case_id"]
            else:  # retry
                assert "幂等" in case["query"] or "重试" in case["query"], case["case_id"]

    def test_every_action_type_covered(self) -> None:
        actions = {c["expected_action"] for c in _load("recovery_25.json")["cases"]}
        assert actions == self._ACTIONS


class TestFailure:
    _SCENARIOS = {
        "worker_crash",
        "tool_timeout",
        "tool_5xx",
        "tool_429",
        "user_cancel",
        "deadline",
        "reconciliation",
    }
    _BEHAVIORS = {
        "retry",
        "fail_fast_notify",
        "cancel_run",
        "recover_resume",
        "reconcile_no_duplicate",
    }

    def test_schema_and_enums(self) -> None:
        for case in _load("failure_20.json")["cases"]:
            required = {
                "case_id",
                "query",
                "scenario",
                "expected_behavior",
                "expected_duplicate_writes",
            }
            assert required <= set(case), case["case_id"]
            assert set(case) - required <= {"note"}
            assert case["scenario"] in self._SCENARIOS, case["case_id"]
            assert case["expected_behavior"] in self._BEHAVIORS, case["case_id"]
            assert isinstance(case["expected_duplicate_writes"], int), case["case_id"]
            assert case["expected_duplicate_writes"] >= 0, case["case_id"]

    def test_all_scenarios_covered(self) -> None:
        scenarios = {c["scenario"] for c in _load("failure_20.json")["cases"]}
        assert scenarios == self._SCENARIOS

    def test_worker_recovery_never_duplicates_writes(self) -> None:
        for case in _load("failure_20.json")["cases"]:
            if case["scenario"] == "worker_crash":
                assert case["expected_duplicate_writes"] == 0, case["case_id"]


class TestRecoverOrReplan:
    _STATUSES = {
        "QUEUED",
        "PLANNING",
        "WAITING_APPROVAL",
        "EXECUTING",
        "VERIFYING",
        "RETRYING",
        "REPLANNING",
        "SUCCEEDED",
        "FAILED",
        "CANCELLED",
        "EXPIRED",
    }
    _RISK_LEVELS = {"READ", "WRITE", "DANGEROUS"}
    _STEP_STATUSES = {"COMPLETED", "FAILED"}
    _GIVE_UP_CODES = {
        "RECOVERY_REQUIRES_HUMAN",
        "RECOVERY_GIVE_UP",
        "WRITE_OUTCOME_AMBIGUOUS",
        "RETRY_BUDGET_EXHAUSTED",
        "REPLAN_BUDGET_EXHAUSTED",
    }

    def test_seed_and_expected_schema(self) -> None:
        for case in _load("recover_or_replan_20.json")["cases"]:
            seed = case["seed"]
            required_seed = {"status", "retry_count", "replan_count", "errors"}
            assert required_seed <= set(seed), case["case_id"]
            assert set(seed) - required_seed <= {"plan", "step_results"}
            assert seed["status"] in self._STATUSES, case["case_id"]
            assert isinstance(seed["retry_count"], int) and seed["retry_count"] >= 0
            assert isinstance(seed["replan_count"], int) and seed["replan_count"] >= 0
            assert isinstance(seed["errors"], list)
            for err in seed["errors"]:
                assert err["code"] and err["message"]
            if seed["plan"] is not None:
                for step in seed["plan"]["steps"]:
                    assert step["step_id"] and step["tool_name"]
                    assert step["risk_level"] in self._RISK_LEVELS
            for sr in seed.get("step_results", []):
                assert sr["step_id"]
                assert sr["status"] in self._STEP_STATUSES

            expected = case["expected"]
            assert set(expected) == {
                "status",
                "retry_count",
                "replan_count",
                "error_code",
                "errors_cleared",
            }, case["case_id"]
            assert expected["status"] in self._STATUSES, case["case_id"]
            assert isinstance(expected["errors_cleared"], bool)
            assert expected["error_code"] is None or expected["error_code"] in self._GIVE_UP_CODES

    def test_step_results_reference_plan_steps(self) -> None:
        """recover_or_replan looks up a plan step's result by step_id
        (_failed_ambiguous_write), so every result must map to a plan step."""
        for case in _load("recover_or_replan_20.json")["cases"]:
            seed = case["seed"]
            if seed["plan"] is None or not seed.get("step_results"):
                continue
            plan_ids = {s["step_id"] for s in seed["plan"]["steps"]}
            result_ids = {sr["step_id"] for sr in seed["step_results"]}
            assert plan_ids == result_ids, case["case_id"]

    def test_every_give_up_code_covered(self) -> None:
        codes = {
            c["expected"]["error_code"]
            for c in _load("recover_or_replan_20.json")["cases"]
            if c["expected"]["error_code"] is not None
        }
        assert codes == self._GIVE_UP_CODES

    def test_every_continuation_branch_covered(self) -> None:
        cases = _load("recover_or_replan_20.json")["cases"]
        retried = any(
            c["seed"]["status"] == "RETRYING" and c["expected"]["status"] == "EXECUTING"
            for c in cases
        )
        replanned = any(
            c["seed"]["status"] in {"REPLANNING", "PLANNING"}
            and c["expected"]["status"] == "PLANNING"
            for c in cases
        )
        noop = any(
            c["seed"]["status"] not in {"RETRYING", "REPLANNING", "PLANNING"}
            and c["expected"]["status"] == c["seed"]["status"]
            for c in cases
        )
        assert retried and replanned and noop


class TestSecurity:
    _VALID_LAYERS = {"injection_guard", "ssrf_guard", "redactor"}

    def test_schema(self) -> None:
        for case in _load("security_25.json")["cases"]:
            required = {"case_id", "attack_type", "target_layer", "input", "expected"}
            assert required <= set(case), case["case_id"]
            assert set(case) - required <= {"note"}
            assert case["expected"] in {"BLOCK", "ALLOW"}
            assert case["target_layer"] in self._VALID_LAYERS
            assert isinstance(case["input"], str) and case["input"]


class TestDomainGrounding:
    """Seed-data grounding: planning step params must reference real entities."""

    def test_planning_step_params_grounded_in_seed_data(self) -> None:
        for case in _load("planning_50.json")["cases"]:
            for step in case["steps"]:
                params = step["params"]
                if step["tool"] == "getProductByName":
                    assert params["name"] in PRODUCT_NAMES, (
                        f"{case['case_id']}: unknown product {params['name']}"
                    )
                if step["tool"] == "querySuppliersByDeliveryRegion":
                    assert params["region"] in REGIONS, (
                        f"{case['case_id']}: unknown region {params['region']}"
                    )
                if step["tool"] == "createOrder":
                    assert isinstance(params["quantity"], int) and params["quantity"] > 0, case[
                        "case_id"
                    ]
                    region = params.get("region")
                    # skip $N. dependency references; validate only literals
                    if region is not None and isinstance(region, str) and "$" not in region:
                        assert region in REGIONS, case["case_id"]

    def test_seed_catalog_sizes_match_grounded_datasets(self) -> None:
        assert len(SEED_PRODUCTS) == 6
        assert len(SUPPLIER_NAMES) == 5
