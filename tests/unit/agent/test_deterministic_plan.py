"""Deterministic planner coverage against the planning eval dataset — TDD.

These tests drive the *real* planning chain (classify_intent →
build_plan_from_intent) over every case in evals/datasets/planning_50.json and
assert the produced tool sequence matches the dataset's expected steps. They
pin the 9-tool dispatch matrix that replaces the golden-baseline oracle in the
eval harness: product/supplier/order single-step reads plus the multi-step
write DAGs (create / cancel / update_status / modify) with parameter threading.

Before the planner extension these tests fail for 40 of 50 cases (the planner
only emitted getProductByName/getSupplierByStatus); the dataset is the
specification the deterministic planner must satisfy.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from erp_copilot.agent.nodes.classify_intent import classify_intent
from erp_copilot.agent.planner import build_deterministic_plan_node, build_plan_from_intent
from erp_copilot.agent.state import AgentState, Plan
from erp_copilot.domain.enums import ToolRiskLevel

DATASET = Path(__file__).resolve().parents[3] / "evals" / "datasets" / "planning_50.json"
CASES = json.loads(DATASET.read_text(encoding="utf-8"))["cases"]


def _tools_for(query: str) -> list[str]:
    """Tool-name sequence the real planner produces for *query*."""
    intent = classify_intent(query)
    plan, errors = build_plan_from_intent(intent)
    return [step.tool_name for step in plan.steps] if not errors else []


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["case_id"])
def test_planner_produces_expected_tool_sequence(case: dict) -> None:
    expected = [step["tool"] for step in case["steps"]]
    assert _tools_for(case["query"]) == expected


class TestIntentRouting:
    """Pin the intent→tool mapping that the multi-step DAGs depend on."""

    @pytest.mark.parametrize(
        ("query", "expected"),
        [
            ("苹果多少钱", ["getProductByName"]),
            ("商品 4 号的信息", ["getProductById"]),
            ("苹果的替代品", ["getProductSubstitutesByName"]),
            ("上海有哪些供应商", ["querySuppliersByDeliveryRegion"]),
            ("可用的供应商", ["getSupplierByStatus"]),
            ("查订单 3f2a9c1d", ["getOrderByOrderId"]),
            (
                "帮我在上海下一单 10 KG 苹果",
                ["getProductByName", "querySuppliersByDeliveryRegion", "createOrder"],
            ),
            ("取消订单 3f2a9c1d", ["getOrderByOrderId", "cancelOrder"]),
            ("把订单 a1b2c3 改为已发货", ["getOrderByOrderId", "updateOrderStatus"]),
            (
                "改单：把订单 3f2a9c1d 数量从 10 改成 20",
                ["getOrderByOrderId", "cancelOrder", "createOrder"],
            ),
        ],
    )
    def test_routes_each_intent_to_expected_tools(self, query: str, expected: list[str]) -> None:
        assert _tools_for(query) == expected


class TestWritePlanShape:
    """The multi-step DAGs carry the structural guarantees validate_plan needs."""

    def test_create_order_is_three_step_dag_with_threaded_args(self) -> None:
        intent = classify_intent("帮我在上海下一单 10 KG 苹果")
        plan, errors = build_plan_from_intent(intent)
        assert errors == []
        assert [s.tool_name for s in plan.steps] == [
            "getProductByName",
            "querySuppliersByDeliveryRegion",
            "createOrder",
        ]
        s1, s2, s3 = plan.steps
        assert s3.depends_on == ["s1", "s2"]
        assert s3.argument_sources.get("product_id") == "step:s1"
        assert s3.argument_sources.get("supplier_id") == "step:s2"
        # WRITE steps carry the compensation note and approval flag the
        # validator (layer 2) and the policy gate (layer 3) expect.
        assert s3.risk_level == ToolRiskLevel.WRITE
        assert s3.fallback is not None
        assert s3.requires_approval is True

    def test_cancel_order_threads_order_id_from_lookup(self) -> None:
        intent = classify_intent("取消订单 3f2a9c1d")
        plan, errors = build_plan_from_intent(intent)
        assert errors == []
        s1, s2 = plan.steps
        assert s2.tool_name == "cancelOrder"
        assert s2.argument_sources.get("order_id") == "step:s1"
        assert s2.depends_on == ["s1"]

    def test_update_status_uses_extracted_target_state(self) -> None:
        intent = classify_intent("把订单 a1b2c3 改为已发货")
        plan, errors = build_plan_from_intent(intent)
        assert errors == []
        s2 = plan.steps[1]
        assert s2.tool_name == "updateOrderStatus"
        assert s2.arguments.get("status") == "SHIPPED"

    def test_empty_intent_still_reports_empty_plan(self) -> None:
        intent = classify_intent("查询库存")
        plan, errors = build_plan_from_intent(intent)
        assert plan.steps == []
        assert any(e.code == "EMPTY_PLAN" for e in errors)


class TestIdempotencyKeyStamping:
    """Write DAGs get a deterministic run-scoped idempotency key (task: write path).

    The pure build_plan_from_intent cannot stamp keys (no run_id); the
    deterministic plan *node* can — it stamps f"{run_id}:{step_id}" on every
    WRITE/DANGEROUS step, both on the step (for recovery/reconciliation) and in
    its arguments (so the executor receives it as the write tool's
    idempotency_key parameter).
    """

    def _stamped_plan(self, query: str, run_id: str = "run-1") -> Plan:
        node = build_deterministic_plan_node()
        state = AgentState(
            run_id=run_id,
            tenant_id="t1",
            query=query,
            intent=classify_intent(query),
        )
        return node(state)["plan"]

    def test_write_step_carries_run_scoped_idempotency_key(self) -> None:
        plan = self._stamped_plan("帮我在上海下一单 10 KG 苹果", run_id="run-1")
        s3 = plan.steps[-1]
        assert s3.risk_level == ToolRiskLevel.WRITE
        assert s3.idempotency_key == "run-1:s3"
        assert s3.arguments["idempotency_key"] == "run-1:s3"

    def test_read_steps_keep_no_idempotency_key(self) -> None:
        plan = self._stamped_plan("帮我在上海下一单 10 KG 苹果", run_id="run-1")
        for step in plan.steps[:-1]:
            assert step.risk_level == ToolRiskLevel.READ
            assert step.idempotency_key is None

    def test_keys_differ_across_runs(self) -> None:
        plan_a = self._stamped_plan("帮我在上海下一单 10 KG 苹果", run_id="run-a")
        plan_b = self._stamped_plan("帮我在上海下一单 10 KG 苹果", run_id="run-b")
        assert plan_a.steps[-1].idempotency_key != plan_b.steps[-1].idempotency_key
