"""Unit tests for evals/llm_planner_eval.py — LLM planner tool-selection eval.

The eval answers "大模型能否精准选定合适的 ERP API" by driving the real LLM
plan node (build_plan_node) over the planning_50 cases and scoring tool
selection against the dataset's expected steps. score_plan is the pure
per-case scorer (a plan in, a measurement out); evaluate_cases wires the real
classify → LLM plan → validate node chain and aggregates. Tests inject a fake
llm_complete so both run deterministically — no LLM, no network.
"""

from __future__ import annotations

import json
from collections import Counter

import pytest

from erp_copilot.agent.nodes.validate_plan import ToolSpec, validate_plan
from erp_copilot.agent.planner import WRITE_TOOLS
from erp_copilot.agent.state import Plan, PlanStep, PlanValidation
from erp_copilot.domain.enums import ToolRiskLevel
from evals.harness import DATASET_DIR, load_cases
from evals.llm_planner_eval import (
    evaluate_cases,
    heldout_case_ids,
    load_planning_cases,
    score_plan,
    validate_split_args,
)

TOOLS: dict[str, ToolSpec] = {
    "getProductByName": ToolSpec(name="getProductByName", required_params=["name"]),
    "getProductById": ToolSpec(name="getProductById", required_params=["id"]),
    "getOrderByOrderId": ToolSpec(name="getOrderByOrderId", required_params=["order_id"]),
    "querySuppliersByDeliveryRegion": ToolSpec(
        name="querySuppliersByDeliveryRegion", required_params=["region"]
    ),
    "getSupplierByStatus": ToolSpec(name="getSupplierByStatus", required_params=["status"]),
    "createOrder": ToolSpec(
        name="createOrder",
        required_params=["product_id", "supplier_id", "quantity", "region"],
    ),
}


def _case(tools: list[str], *, case_id: str = "plan-t", query: str = "苹果多少钱") -> dict:
    return {
        "case_id": case_id,
        "query": query,
        "single_step": len(tools) == 1,
        "steps": [{"tool": tool, "params": {}} for tool in tools],
    }


def _plan(*tools: str) -> Plan:
    return Plan(
        steps=[
            PlanStep(step_id=str(i), tool_name=tool, arguments={"name": "苹果"})
            for i, tool in enumerate(tools, start=1)
        ]
    )


def _read_plan(*tools: str) -> tuple[Plan, PlanValidation]:
    plan = _plan(*tools)
    return plan, validate_plan(plan, TOOLS)


class TestScorePlan:
    def test_correct_single_step_scores_full(self) -> None:
        plan, validation = _read_plan("getProductByName")
        s = score_plan(_case(["getProductByName"]), plan, validation)
        assert s["tool_seq_exact"] is True
        assert s["tool_set_exact"] is True
        assert s["tool_coverage"] == 1.0
        assert s["contract_valid"] is True
        assert s["passed"] is True

    def test_wrong_tool_breaks_set(self) -> None:
        plan, validation = _read_plan("getProductById")
        s = score_plan(_case(["getProductByName"]), plan, validation)
        assert s["tool_set_exact"] is False
        assert s["tool_coverage"] == 0.0
        assert s["passed"] is False

    def test_extra_tool_keeps_coverage_but_breaks_set(self) -> None:
        # All expected tools present (coverage 1.0) but an extra tool makes the
        # set wrong — selection is a superset, not exact.
        plan, validation = _read_plan("getProductByName", "getProductById")
        s = score_plan(_case(["getProductByName"]), plan, validation)
        assert s["tool_coverage"] == 1.0
        assert s["tool_set_exact"] is False
        assert s["passed"] is False

    def test_missing_tool_lowers_coverage(self) -> None:
        plan, validation = _read_plan("getProductByName")
        s = score_plan(_case(["getProductByName", "getOrderByOrderId"]), plan, validation)
        assert s["tool_coverage"] == 0.5
        assert s["tool_set_exact"] is False
        assert s["passed"] is False

    def test_order_mismatch_breaks_seq_not_set(self) -> None:
        # Same two tools, swapped order: selection (set) is right, orchestration
        # (sequence) is wrong. Both steps carry valid args so the plan is
        # contract-valid — passed stays True (selection + contract both hold).
        plan = Plan(
            steps=[
                PlanStep(step_id="1", tool_name="getOrderByOrderId", arguments={"order_id": "x"}),
                PlanStep(step_id="2", tool_name="getProductByName", arguments={"name": "苹果"}),
            ]
        )
        validation = validate_plan(plan, TOOLS)
        s = score_plan(_case(["getProductByName", "getOrderByOrderId"]), plan, validation)
        assert s["tool_seq_exact"] is False
        assert s["tool_set_exact"] is True
        assert s["contract_valid"] is True
        assert s["passed"] is True

    def test_none_plan_scores_zero(self) -> None:
        s = score_plan(_case(["getProductByName"]), None, None)
        assert s["parse_success"] is False
        assert s["tool_seq_exact"] is False
        assert s["tool_set_exact"] is False
        assert s["tool_coverage"] == 0.0
        assert s["contract_valid"] is False
        assert s["passed"] is False

    def test_correct_selection_with_contract_failure_is_not_passed(self) -> None:
        # createOrder labeled READ trips the RISK_DOWNGRADE guard: the LLM
        # picked the right tool (set-exact) but the plan fails the deterministic
        # gate — selection and contract validity are independent dimensions.
        plan = Plan(
            steps=[
                PlanStep(
                    step_id="1",
                    tool_name="createOrder",
                    arguments={
                        "product_id": 1,
                        "supplier_id": 2,
                        "quantity": 5,
                        "region": "上海",
                    },
                    risk_level=ToolRiskLevel.READ,
                )
            ]
        )
        validation = validate_plan(plan, TOOLS)
        assert validation.is_valid is False
        s = score_plan(_case(["createOrder"]), plan, validation)
        assert s["tool_set_exact"] is True
        assert s["contract_valid"] is False
        assert s["validation_errors"] == ["RISK_DOWNGRADE"]
        assert s["passed"] is False


class TestEvaluateCases:
    def test_runs_llm_chain_and_aggregates(self) -> None:
        responses = iter(
            [
                # correct
                (
                    '{"steps": [{"step_id": "1", "tool_name": "getProductByName",'
                    ' "arguments": {"name": "苹果"}, "risk_level": "READ"}]}'
                ),
                # valid but wrong tool (getProductById is in product/query candidates)
                (
                    '{"steps": [{"step_id": "1", "tool_name": "getProductById",'
                    ' "arguments": {"id": 1}, "risk_level": "READ"}]}'
                ),
                # unparseable
                "this is not json",
            ]
        )

        def fake_llm(_prompt: str) -> str:
            return next(responses)

        cases = [
            _case(["getProductByName"], case_id="plan-a"),
            _case(["getProductByName"], case_id="plan-b"),
            _case(["getProductByName"], case_id="plan-c"),
        ]
        out = evaluate_cases(
            cases,
            llm_complete=fake_llm,
            available_tools=set(TOOLS),
            tool_schemas=TOOLS,
        )
        assert out["mode"] == "llm_planner"
        assert out["primary_score"] == 1 / 3  # 选型准确率 (set-level)
        assert out["metrics"]["tool_set_exact_rate"] == 1 / 3
        assert out["metrics"]["tool_seq_exact_rate"] == 1 / 3
        assert out["metrics"]["tool_coverage"] == 1 / 3
        assert out["metrics"]["contract_valid_rate"] == 2 / 3
        assert out["metrics"]["parse_success_rate"] == 2 / 3
        by_id = {pc["case_id"]: pc for pc in out["per_case"]}
        assert by_id["plan-a"]["passed"] is True
        assert by_id["plan-b"]["passed"] is False
        assert by_id["plan-b"]["contract_valid"] is True  # wrong tool, valid plan
        assert by_id["plan-c"]["parse_success"] is False

    def test_single_multi_breakdown(self) -> None:
        responses = iter(
            [
                # single step: product query
                (
                    '{"steps": [{"step_id": "1", "tool_name": "getProductByName",'
                    ' "arguments": {"name": "苹果"}, "risk_level": "READ"}]}'
                ),
                # multi step: order/create — the whole chain is in candidates
                (
                    '{"steps": ['
                    ' {"step_id": "1", "tool_name": "getProductByName",'
                    '  "arguments": {"name": "苹果"}, "risk_level": "READ"},'
                    ' {"step_id": "2", "tool_name": "querySuppliersByDeliveryRegion",'
                    '  "arguments": {"region": "上海"}, "risk_level": "READ"},'
                    ' {"step_id": "3", "tool_name": "createOrder",'
                    '  "arguments": {"product_id": 1, "supplier_id": 2,'
                    '   "quantity": 5, "region": "上海"}, "risk_level": "WRITE",'
                    '  "fallback": "取消新建订单以补偿"}]}'
                ),
            ]
        )

        def fake_llm(_prompt: str) -> str:
            return next(responses)

        cases = [
            _case(["getProductByName"], case_id="s1"),
            _case(
                ["getProductByName", "querySuppliersByDeliveryRegion", "createOrder"],
                case_id="m1",
                query="帮我在上海下一单 10 KG 苹果",
            ),
        ]
        out = evaluate_cases(
            cases,
            llm_complete=fake_llm,
            available_tools=set(TOOLS),
            tool_schemas=TOOLS,
        )
        assert out["metrics"]["single_step_set_acc"] == 1.0
        assert out["metrics"]["multi_step_set_acc"] == 1.0
        assert out["metrics"]["multi_step_count"] == 1

    def test_prompt_carries_fixed_policy_rules(self) -> None:
        # The fixed-policy rules (baseline failure classes: createOrder labeled
        # READ; supplier two-choice over-selection) now live in the production
        # SYSTEM_PROMPT and must reach the LLM by default. The risk table is
        # derived from planner.WRITE_TOOLS — the same set the RISK_DOWNGRADE
        # gate checks — so the prompt and the guard cannot drift apart.
        captured: list[str] = []

        def fake_llm(prompt: str) -> str:
            captured.append(prompt)
            return '{"steps": []}'

        evaluate_cases(
            [_case(["getProductByName"], case_id="p1")],
            llm_complete=fake_llm,
            available_tools=set(TOOLS),
            tool_schemas=TOOLS,
        )
        prompt = captured[0]
        assert "严禁标成 READ" in prompt
        assert "供应商查询二选一" in prompt
        assert "getSupplierByStatus" in prompt
        for tool in WRITE_TOOLS:
            assert tool in prompt


class TestHeldoutSplit:
    """P3 held-out discipline: a frozen ~20% clean set, never touched by tuning."""

    def test_heldout_is_20_percent_of_planning_200(self) -> None:
        cases = load_cases("planning_200.json")
        assert len(heldout_case_ids(cases)) == 40  # 20% of 200

    def test_heldout_is_stratified_across_all_four_layers(self) -> None:
        cases = load_cases("planning_200.json")
        by_id = {c["case_id"]: c for c in cases}
        counts = Counter(
            (by_id[cid]["single_step"], by_id[cid].get("difficulty"))
            for cid in heldout_case_ids(cases)
        )
        assert counts == {
            (True, "easy"): 10,
            (True, "hard"): 10,
            (False, "easy"): 10,
            (False, "hard"): 10,
        }

    def test_heldout_is_deterministic(self) -> None:
        cases = load_cases("planning_200.json")
        assert heldout_case_ids(cases) == heldout_case_ids(cases)

    def test_frozen_manifest_matches_splitter_output(self) -> None:
        """The checked-in manifest is the boundary that prompt tuning must not
        cross; it must equal what the splitter produces on the current dataset,
        or the frozen/observed split have silently drifted."""
        manifest = json.loads(
            (DATASET_DIR / "planning_heldout.json").read_text(encoding="utf-8")
        )
        assert manifest["case_ids"] == heldout_case_ids(load_cases("planning_200.json"))


class TestLoadPlanningCases:
    def test_heldout_and_dev_partition_planning_200(self) -> None:
        heldout = load_planning_cases("heldout")
        dev = load_planning_cases("dev")
        heldout_ids = {c["case_id"] for c in heldout}
        dev_ids = {c["case_id"] for c in dev}
        assert len(heldout) == 40
        assert len(dev) == 160
        assert heldout_ids.isdisjoint(dev_ids)
        assert heldout_ids | dev_ids == {c["case_id"] for c in load_cases("planning_200.json")}

    def test_all_returns_every_case(self) -> None:
        assert len(load_planning_cases("all")) == 200


class TestValidateSplitArgs:
    """Prompt overrides are refused on the frozen held-out set."""

    def test_refuses_prompt_override_on_heldout(self) -> None:
        with pytest.raises(ValueError, match="held-out"):
            validate_split_args("heldout", "tuned prompt v1")

    def test_allows_prompt_override_on_dev_and_all(self) -> None:
        validate_split_args("dev", "tuned prompt v1")
        validate_split_args("all", "tuned prompt v1")

    def test_heldout_with_production_prompt_is_fine(self) -> None:
        validate_split_args("heldout", None)
