"""Validation tests for the 200-case evaluation dataset — task 6.5.

Six category files in evals/datasets/ total 200 independently executable
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
from erp_copilot.tools.candidate_filter import DOMAIN_TOOL_MAP, V6_TOOL_NAMES

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
DATASETS = PROJECT_ROOT / "evals" / "datasets"
KNOWLEDGE = PROJECT_ROOT / "datasets" / "knowledge"

EXPECTED_COUNTS: dict[str, int] = {
    "tool_retrieval_40.json": 40,
    "knowledge_rag_40.json": 40,
    "planning_50.json": 50,
    "recovery_25.json": 25,
    "security_25.json": 25,
    "failure_20.json": 20,
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
    def test_all_six_files_exist(self) -> None:
        for filename in EXPECTED_COUNTS:
            assert (DATASETS / filename).exists(), filename

    def test_each_file_is_valid_json_with_metadata(self) -> None:
        for filename in EXPECTED_COUNTS:
            data = _load(filename)
            assert data["description"]
            assert data["version"]

    def test_six_categories_total_200(self) -> None:
        total = sum(len(_load(f)["cases"]) for f in EXPECTED_COUNTS)
        assert total == 200

    def test_per_file_counts(self) -> None:
        for filename, expected in EXPECTED_COUNTS.items():
            assert len(_load(filename)["cases"]) == expected, filename

    def test_case_ids_unique_within_each_file(self) -> None:
        for filename in EXPECTED_COUNTS:
            ids = _case_ids(filename)
            assert len(ids) == len(set(ids)), f"duplicate case_id in {filename}"


class TestToolRetrieval:
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
            assert set(case) - required <= {"note"}
            assert case["expected_tool"] in V6_TOOL_NAMES, case["case_id"]
            assert set(case["candidate_tools"]) <= V6_TOOL_NAMES, case["case_id"]
            assert case["expected_tool"] in case["candidate_tools"], case["case_id"]
            assert (case["domain"], case["action"]) in known, case["case_id"]

    def test_all_nine_tools_are_expected_by_some_case(self) -> None:
        expected = {c["expected_tool"] for c in _load("tool_retrieval_40.json")["cases"]}
        assert expected == set(V6_TOOL_NAMES)

    def test_hard_negatives_present(self) -> None:
        cases = _load("tool_retrieval_40.json")["cases"]
        hard = [c for c in cases if c["hard_negative"]]
        assert len(hard) >= 8

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
            assert set(case) - required <= {"answer_fact", "category", "note"}
            assert isinstance(case["should_answer"], bool)
            assert isinstance(case["relevant_docs"], list)
            if case["should_answer"]:
                assert case["relevant_docs"], f"{case['case_id']}: answerable but no docs"
                assert "answer_fact" in case, case["case_id"]
                assert set(case["relevant_docs"]) <= doc_ids, case["case_id"]
            else:
                assert case["relevant_docs"] == [], case["case_id"]

    def test_no_answer_cases_present(self) -> None:
        cases = _load("knowledge_rag_40.json")["cases"]
        assert sum(1 for c in cases if not c["should_answer"]) >= 5


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
