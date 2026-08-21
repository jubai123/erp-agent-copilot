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
            ("已发货的订单有哪些", ["getByOrderStatus"]),
            ("查询2023年1月1日至2023年1月31日之间的订单", ["getByTimeRange"]),
            ("商品 3 号的订单", ["getByProductId"]),
            ("供应商 3 号的订单", ["getOrdersBySupplierId"]),
            ("添加供应商 旧物流，覆盖上海", ["addSuppliers"]),
            ("删除供应商 旧物流", ["deleteSupplierByName"]),
            ("添加名称为西瓜的商品 价格10元 库存100", ["addProduct"]),
            ("修改商品 3 号描述为鲜甜多汁", ["updateProductDescription"]),
            ("把商品 4 号的替代品改为西瓜", ["updateProductSubstitutes"]),
            ("删除商品 4 号", ["removeProductById"]),
            ("删除商品 苹果", ["removeProductByName"]),
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


class TestWriteMaintenancePlanShape:
    """V5-aligned write intents emit WRITE + approval-gated steps (M0).

    Maintenance/deletes are single-step DAGs; a step emitted with a missing
    required argument (e.g. addSuppliers without regions) is deliberately left
    for validate_plan's MISSING_REQUIRED_ARG gate rather than written partial.
    """

    def test_add_supplier_carries_regions_and_status(self) -> None:
        intent = classify_intent("添加供应商 旧物流，覆盖上海")
        plan, errors = build_plan_from_intent(intent)
        assert errors == []
        step = plan.steps[0]
        assert step.tool_name == "addSuppliers"
        assert step.risk_level == ToolRiskLevel.WRITE
        assert step.requires_approval is True
        assert step.arguments["name"] == "旧物流"
        assert step.arguments["regions"] == ["上海"]
        assert step.arguments["status"] == "AVAILABLE"

    def test_add_supplier_without_region_stays_honest(self) -> None:
        intent = classify_intent("添加供应商 旧物流")
        plan, errors = build_plan_from_intent(intent)
        assert errors == []
        step = plan.steps[0]
        assert step.tool_name == "addSuppliers"
        assert "regions" not in step.arguments

    def test_add_supplier_without_name_is_empty_plan(self) -> None:
        intent = classify_intent("添加供应商")
        plan, errors = build_plan_from_intent(intent)
        assert plan.steps == []
        assert any(e.code == "EMPTY_PLAN" for e in errors)

    def test_add_product_carries_price_and_stock(self) -> None:
        intent = classify_intent("添加名称为西瓜的商品 价格10元 库存100")
        plan, errors = build_plan_from_intent(intent)
        assert errors == []
        step = plan.steps[0]
        assert step.tool_name == "addProduct"
        assert step.risk_level == ToolRiskLevel.WRITE
        assert step.requires_approval is True
        assert step.arguments == {"name": "西瓜", "price": 10.0, "quantity_in_stock": 100}

    def test_update_description_targets_product_by_id(self) -> None:
        intent = classify_intent("修改商品 3 号描述为鲜甜多汁")
        plan, errors = build_plan_from_intent(intent)
        assert errors == []
        step = plan.steps[0]
        assert step.tool_name == "updateProductDescription"
        assert step.arguments == {"product_id": 3, "description": "鲜甜多汁"}

    def test_update_substitutes_targets_product_by_id(self) -> None:
        intent = classify_intent("把商品 4 号的替代品改为西瓜")
        plan, errors = build_plan_from_intent(intent)
        assert errors == []
        step = plan.steps[0]
        assert step.tool_name == "updateProductSubstitutes"
        assert step.arguments == {"product_id": 4, "substitute_name": "西瓜"}

    def test_delete_by_id_and_by_name(self) -> None:
        plan, _ = build_plan_from_intent(classify_intent("删除商品 4 号"))
        assert plan.steps[0].tool_name == "removeProductById"
        plan, _ = build_plan_from_intent(classify_intent("删除商品 苹果"))
        assert plan.steps[0].tool_name == "removeProductByName"
        plan, _ = build_plan_from_intent(classify_intent("删除供应商 5 号"))
        assert plan.steps[0].tool_name == "deleteSupplierById"

    def test_delete_steps_are_dangerous_and_approval_gated(self) -> None:
        # Delete tools are DANGEROUS (not merely WRITE): the risk feeds the
        # approval/audit surface and validate_plan guards any downgrade below
        # DANGEROUS. They keep the coarse order:write scope so existing RBAC
        # grants (which hold order:write, not per-domain delete scopes) still
        # reach the approval gate instead of being DENYed.
        for query, expected in [
            ("删除商品 4 号", "removeProductById"),
            ("删除商品 苹果", "removeProductByName"),
            ("删除供应商 5 号", "deleteSupplierById"),
            ("删除供应商 旧物流", "deleteSupplierByName"),
        ]:
            plan, errors = build_plan_from_intent(classify_intent(query))
            assert errors == []
            step = plan.steps[0]
            assert step.tool_name == expected
            assert step.risk_level == ToolRiskLevel.DANGEROUS
            assert step.requires_approval is True
            assert step.fallback is not None

    def test_order_query_subintents_are_read_steps(self) -> None:
        for query, tool, expected in [
            ("已发货的订单有哪些", "getByOrderStatus", {"status": "SHIPPED"}),
            (
                "查询2023年1月1日至2023年1月31日之间的订单",
                "getByTimeRange",
                {"start_date": "2023-01-01", "end_date": "2023-01-31"},
            ),
            ("商品 3 号的订单", "getByProductId", {"product_id": 3}),
            ("供应商 3 号的订单", "getOrdersBySupplierId", {"supplier_id": 3}),
        ]:
            intent = classify_intent(query)
            plan, errors = build_plan_from_intent(intent)
            assert errors == [], query
            step = plan.steps[0]
            assert step.tool_name == tool, query
            assert step.risk_level == ToolRiskLevel.READ, query
            assert step.arguments == expected, query


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


class TestSuccessCondition:
    """Deterministic plan steps carry success predicates so the verify node
    (defence layer 4) is active on the worker production path — semantic
    mis-selection is caught instead of silently passing."""

    def test_product_name_step_pins_the_queried_name(self) -> None:
        intent = classify_intent("苹果多少钱")
        plan, errors = build_plan_from_intent(intent)
        assert errors == []
        assert plan.steps[0].success_condition == "response.name == '苹果'"

    def test_product_by_id_step_pins_the_id(self) -> None:
        intent = classify_intent("商品 4 号的信息")
        plan, errors = build_plan_from_intent(intent)
        assert errors == []
        assert plan.steps[0].success_condition == "response.product_id == 4"

    def test_order_lookup_step_pins_the_order_id(self) -> None:
        intent = classify_intent("查订单 3f2a9c1d")
        plan, errors = build_plan_from_intent(intent)
        assert errors == []
        assert plan.steps[0].success_condition == "response.order_id == '3f2a9c1d'"

    def test_create_order_step_pins_a_real_order_amount(self) -> None:
        intent = classify_intent("帮我在上海下一单 10 KG 苹果")
        plan, errors = build_plan_from_intent(intent)
        assert errors == []
        s1, _s2, s3 = plan.steps
        assert s1.tool_name == "getProductByName"
        assert s1.success_condition == "response.name == '苹果'"
        assert s3.tool_name == "createOrder"
        assert s3.success_condition == "response.amount > 0"

    def test_supplier_set_query_steps_stay_lazy(self) -> None:
        # Set-query tools get no predicate: an empty supplier list is a
        # legitimate business answer, and the deterministic planner cannot
        # mis-select the supplier tool.
        intent = classify_intent("上海有哪些供应商")
        plan, errors = build_plan_from_intent(intent)
        assert errors == []
        assert plan.steps
        for step in plan.steps:
            assert step.success_condition is None
