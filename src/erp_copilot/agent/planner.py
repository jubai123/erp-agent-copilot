"""Deterministic plan DAG builder — task 4.16 worker integration.

The worker has no LLM client wired, so the build_plan node it injects is a
deterministic planner (ADR "确定性优先，概率兜底"): it maps the classified
intent plus extracted entities onto a Plan DAG over the 25-tool V5 ERP
surface. READ intents emit single steps (or two independent READ steps for a
combined product+supplier query, which execute in parallel); WRITE intents
emit single-step maintenance/delete DAGs and the multi-step order DAGs
(create / cancel / update_status / modify) whose write steps carry the
compensation note and approval flag validate_plan (layer 2) and the policy
gate (layer 3) expect. A query that yields no tool emits EMPTY_PLAN so the
run fails honestly instead of inventing a tool call.

The LLM-driven planner (build_plan.py) stays the default for the interactive
path; this module is the deterministic fallback the worker and the planning
eval runner use.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from erp_copilot.agent.state import (
    AgentState,
    IntentClassification,
    Plan,
    PlanStep,
    StateError,
)
from erp_copilot.domain.enums import ToolRiskLevel

_PRODUCT_BY_NAME = "getProductByName"
_PRODUCT_BY_ID = "getProductById"
_SUBSTITUTES_BY_NAME = "getProductSubstitutesByName"
_SUPPLIERS_BY_REGION = "querySuppliersByDeliveryRegion"
_SUPPLIER_BY_STATUS = "getSupplierByStatus"
_SUPPLIER_BY_NAME = "getSupplierByName"
_SUPPLIER_BY_ID = "getSupplierById"
_ORDER_BY_ID = "getOrderByOrderId"
_ORDERS_BY_SUPPLIER = "getOrdersBySupplierId"
_BY_TIME_RANGE = "getByTimeRange"
_BY_PRODUCT_ID = "getByProductId"
_BY_ORDER_STATUS = "getByOrderStatus"
_CREATE_ORDER = "createOrder"
_UPDATE_ORDER_STATUS = "updateOrderStatus"
_CANCEL_ORDER = "cancelOrder"

_ADD_PRODUCT = "addProduct"
_ADD_SUPPLIERS = "addSuppliers"
_UPDATE_PRODUCT_DESCRIPTION = "updateProductDescription"
_UPDATE_PRODUCT_SUBSTITUTES = "updateProductSubstitutes"
_REMOVE_PRODUCT_BY_NAME = "removeProductByName"
_REMOVE_PRODUCT_BY_ID = "removeProductById"
_DELETE_SUPPLIER_BY_NAME = "deleteSupplierByName"
_DELETE_SUPPLIER_BY_ID = "deleteSupplierById"

_ORDER_WRITE_SCOPE = "order:write"

# Known write-domain tools. The deterministic planner always stamps these as
# WRITE + requires_approval; validate_plan (defence layer 2) shares this set so
# a plan that labels one of them READ is rejected as RISK_DOWNGRADE — the LLM
# planner must not downgrade risk to slip a write past the approval gate.
# Product/supplier maintenance tools (add/update/delete) carry the single
# order:write scope the current coarse scope model exposes; M3 re-classifies
# the four delete tools as DANGEROUS with per-domain scopes.
WRITE_TOOLS: frozenset[str] = frozenset(
    {
        _CREATE_ORDER,
        _UPDATE_ORDER_STATUS,
        _CANCEL_ORDER,
        _ADD_PRODUCT,
        _ADD_SUPPLIERS,
        _UPDATE_PRODUCT_DESCRIPTION,
        _UPDATE_PRODUCT_SUBSTITUTES,
        _REMOVE_PRODUCT_BY_NAME,
        _REMOVE_PRODUCT_BY_ID,
        _DELETE_SUPPLIER_BY_NAME,
        _DELETE_SUPPLIER_BY_ID,
    }
)

# Per-tool success_condition templates (defence layer 4, verify_results).
# Placeholders are formatted with the step's own arguments so the predicate
# pins *what the query asked for* — a completed step whose result does not
# match is a semantic mis-selection, not a success. Set-query tools
# (querySuppliersByDeliveryRegion / getSupplierByStatus) are deliberately
# absent: an empty supplier list is a legitimate answer and the deterministic
# planner cannot mis-select them, so verify stays lazy there.
_SUCCESS_CONDITION_TEMPLATES: dict[str, str] = {
    _PRODUCT_BY_NAME: "response.name == {name!r}",
    _PRODUCT_BY_ID: "response.product_id == {product_id!r}",
    _ORDER_BY_ID: "response.order_id == {order_id!r}",
    # createOrder: amount (not status) stays stable across the simulator and
    # the cloud ERP (cloud order status is an unnormalised field), and a real
    # order always carries a positive amount.
    _CREATE_ORDER: "response.amount > 0",
}


def _condition_for(tool_name: str, arguments: dict[str, Any]) -> str | None:
    """Return the formatted success predicate for *tool_name*, or None."""
    template = _SUCCESS_CONDITION_TEMPLATES.get(tool_name)
    if template is None:
        return None
    return template.format(**arguments)


def _read_step(
    step_id: str,
    tool_name: str,
    description: str,
    arguments: dict[str, Any],
    required_scope: str,
) -> PlanStep:
    return PlanStep(
        step_id=step_id,
        tool_name=tool_name,
        description=description,
        arguments=arguments,
        risk_level=ToolRiskLevel.READ,
        required_scope=required_scope,
        timeout_s=10,
        max_retries=1,
        success_condition=_condition_for(tool_name, arguments),
    )


def _write_step(
    step_id: str,
    tool_name: str,
    description: str,
    arguments: dict[str, Any],
    argument_sources: dict[str, str],
    depends_on: list[str],
    fallback: str,
) -> PlanStep:
    return PlanStep(
        step_id=step_id,
        tool_name=tool_name,
        description=description,
        arguments=arguments,
        argument_sources=argument_sources,
        depends_on=depends_on,
        risk_level=ToolRiskLevel.WRITE,
        required_scope=_ORDER_WRITE_SCOPE,
        requires_approval=True,
        timeout_s=30,
        max_retries=2,
        fallback=fallback,
        success_condition=_condition_for(tool_name, arguments),
    )


def _product_read_step(step_id: str, entities: dict[str, Any]) -> PlanStep:
    """Pick the product READ tool by which entity pins the product."""
    if entities.get("substitute"):
        return _read_step(
            step_id,
            _SUBSTITUTES_BY_NAME,
            description="查询替代品",
            arguments={"name": entities.get("product")},
            required_scope="product:read",
        )
    if entities.get("product_id") is not None:
        return _read_step(
            step_id,
            _PRODUCT_BY_ID,
            description="按 ID 查询商品",
            arguments={"product_id": entities["product_id"]},
            required_scope="product:read",
        )
    return _read_step(
        step_id,
        _PRODUCT_BY_NAME,
        description="按名称查询商品",
        arguments={"name": entities.get("product")},
        required_scope="product:read",
    )


def _supplier_read_step(step_id: str, entities: dict[str, Any]) -> PlanStep:
    """Pick the supplier READ tool by which entity pins the supplier."""
    if entities.get("supplier_id") is not None:
        return _read_step(
            step_id,
            _SUPPLIER_BY_ID,
            description="按 ID 查询供应商",
            arguments={"supplier_id": entities["supplier_id"]},
            required_scope="supplier:read",
        )
    if entities.get("supplier_name"):
        return _read_step(
            step_id,
            _SUPPLIER_BY_NAME,
            description="按名称查询供应商",
            arguments={"name": entities["supplier_name"]},
            required_scope="supplier:read",
        )
    if entities.get("region"):
        return _read_step(
            step_id,
            _SUPPLIERS_BY_REGION,
            description="按配送区域查询供应商",
            arguments={"region": entities["region"]},
            required_scope="supplier:read",
        )
    return _read_step(
        step_id,
        _SUPPLIER_BY_STATUS,
        description="查询可用供应商",
        arguments={"status": "AVAILABLE"},
        required_scope="supplier:read",
    )


def _supplier_create_steps(entities: dict[str, Any]) -> list[PlanStep] | None:
    """addSuppliers step; a missing name yields EMPTY_PLAN, a missing region
    falls through to validate_plan's MISSING_REQUIRED_ARG honest fail."""
    name = entities.get("supplier_name") or entities.get("name")
    if not name:
        return None
    arguments: dict[str, Any] = {"name": name, "status": "AVAILABLE"}
    if entities.get("regions"):
        arguments["regions"] = entities["regions"]
    return [
        _write_step(
            "s1",
            _ADD_SUPPLIERS,
            description="添加供应商",
            arguments=arguments,
            argument_sources={},
            depends_on=[],
            fallback="删除新增供应商以补偿",
        )
    ]


def _supplier_delete_steps(entities: dict[str, Any]) -> list[PlanStep] | None:
    if entities.get("supplier_id") is not None:
        return [
            _write_step(
                "s1",
                _DELETE_SUPPLIER_BY_ID,
                description="按 ID 删除供应商",
                arguments={"supplier_id": entities["supplier_id"]},
                argument_sources={},
                depends_on=[],
                fallback="撤销删除并恢复供应商",
            )
        ]
    name = entities.get("supplier_name")
    if not name:
        return None
    return [
        _write_step(
            "s1",
            _DELETE_SUPPLIER_BY_NAME,
            description="按名称删除供应商",
            arguments={"name": name},
            argument_sources={},
            depends_on=[],
            fallback="撤销删除并恢复供应商",
        )
    ]


def _product_add_steps(entities: dict[str, Any]) -> list[PlanStep] | None:
    """addProduct step; missing price / quantity_in_stock stays honest as a
    MISSING_REQUIRED_ARG gate failure, not a silent partial write."""
    name = entities.get("name") or entities.get("product")
    if not name:
        return None
    arguments: dict[str, Any] = {"name": name}
    if entities.get("price") is not None:
        arguments["price"] = entities["price"]
    if entities.get("stock") is not None:
        arguments["quantity_in_stock"] = entities["stock"]
    return [
        _write_step(
            "s1",
            _ADD_PRODUCT,
            description="添加商品",
            arguments=arguments,
            argument_sources={},
            depends_on=[],
            fallback="删除新增商品以补偿",
        )
    ]


def _product_update_steps(entities: dict[str, Any]) -> list[PlanStep] | None:
    """updateProductDescription / updateProductSubstitutes single-step branch."""
    product_id = entities.get("product_id")
    if product_id is None:
        return None
    description = entities.get("description")
    substitute_name = entities.get("substitute_name")
    if description is None and substitute_name is None:
        return None
    if description is not None:
        return [
            _write_step(
                "s1",
                _UPDATE_PRODUCT_DESCRIPTION,
                description="更新商品描述",
                arguments={"product_id": product_id, "description": description},
                argument_sources={},
                depends_on=[],
                fallback="回滚商品描述到原值",
            )
        ]
    return [
        _write_step(
            "s1",
            _UPDATE_PRODUCT_SUBSTITUTES,
            description="更新商品替代品",
            arguments={"product_id": product_id, "substitute_name": substitute_name},
            argument_sources={},
            depends_on=[],
            fallback="回滚商品替代品到原值",
        )
    ]


def _product_delete_steps(entities: dict[str, Any]) -> list[PlanStep] | None:
    if entities.get("product_id") is not None:
        return [
            _write_step(
                "s1",
                _REMOVE_PRODUCT_BY_ID,
                description="按 ID 删除商品",
                arguments={"product_id": entities["product_id"]},
                argument_sources={},
                depends_on=[],
                fallback="撤销删除并恢复商品",
            )
        ]
    name = entities.get("product") or entities.get("name")
    if not name:
        return None
    return [
        _write_step(
            "s1",
            _REMOVE_PRODUCT_BY_NAME,
            description="按名称删除商品",
            arguments={"name": name},
            argument_sources={},
            depends_on=[],
            fallback="撤销删除并恢复商品",
        )
    ]


def _order_lookup_step(step_id: str, order_id: str) -> PlanStep:
    return _read_step(
        step_id,
        _ORDER_BY_ID,
        description="按订单号查询订单",
        arguments={"order_id": order_id},
        required_scope="order:read",
    )


def _empty_plan_error() -> tuple[Plan, list[StateError]]:
    return Plan(steps=[]), [
        StateError(
            code="EMPTY_PLAN",
            message="无法从意图构造任何可执行步骤（缺少商品名、订单号或供应商意图）",
        )
    ]


def build_plan_from_intent(intent: IntentClassification) -> tuple[Plan, list[StateError]]:
    """Map the classified intent onto a Plan DAG.

    READ intents emit single steps (or two independent READ steps for a
    combined product+supplier query); WRITE intents emit single-step
    maintenance/delete DAGs or the multi-step order DAGs. A step builder that
    finds no usable entity returns None and the planner reports EMPTY_PLAN;
    a step that is emitted with a missing required argument falls through to
    validate_plan's MISSING_REQUIRED_ARG gate instead of writing partial data.
    """
    entities = intent.entities
    steps: list[PlanStep] | None

    if intent.domain == "supplier":
        if intent.action == "create":
            steps = _supplier_create_steps(entities)
        elif intent.action == "delete":
            steps = _supplier_delete_steps(entities)
        else:
            # A supplier query may also carry a product entity (e.g. "查苹果
            # 库存并推荐供应商") — emit both independent READ steps.
            steps = []
            if entities.get("product"):
                steps.append(_product_read_step("s1", entities))
            steps.append(_supplier_read_step("s2" if steps else "s1", entities))
    elif intent.domain == "order":
        steps = _order_dag_steps(intent.action, entities)
    elif intent.action in ("add", "update", "delete"):
        if intent.action == "add":
            steps = _product_add_steps(entities)
        elif intent.action == "update":
            steps = _product_update_steps(entities)
        else:
            steps = _product_delete_steps(entities)
    else:
        # product/query and product/check_stock (and the fallback domain).
        if entities.get("product") or entities.get("product_id") is not None:
            steps = [_product_read_step("s1", entities)]
        else:
            steps = None

    if not steps:
        return _empty_plan_error()
    return Plan(steps=steps, title=intent.action), []


def _order_dag_steps(action: str, entities: dict[str, Any]) -> list[PlanStep] | None:
    """Build the multi-step WRITE DAGs (order domain), or None for EMPTY_PLAN."""
    order_id = entities.get("order_id")

    if action == "query":
        if order_id is None:
            return None
        return [_order_lookup_step("s1", order_id)]

    if action == "query_by_time":
        time_range = entities.get("time_range")
        if time_range is None:
            return None
        start_date, end_date = time_range
        return [
            _read_step(
                "s1",
                _BY_TIME_RANGE,
                description="按时间范围查询订单",
                arguments={"start_date": start_date, "end_date": end_date},
                required_scope="order:read",
            )
        ]

    if action == "query_by_status":
        status = entities.get("status")
        if status is None:
            return None
        return [
            _read_step(
                "s1",
                _BY_ORDER_STATUS,
                description="按状态查询订单",
                arguments={"status": status},
                required_scope="order:read",
            )
        ]

    if action == "query_by_product":
        product_id = entities.get("product_id")
        if product_id is None:
            return None
        return [
            _read_step(
                "s1",
                _BY_PRODUCT_ID,
                description="按商品查询订单",
                arguments={"product_id": product_id},
                required_scope="order:read",
            )
        ]

    if action == "query_by_supplier":
        supplier_id = entities.get("supplier_id")
        if supplier_id is None:
            return None
        return [
            _read_step(
                "s1",
                _ORDERS_BY_SUPPLIER,
                description="按供应商查询订单",
                arguments={"supplier_id": supplier_id},
                required_scope="order:read",
            )
        ]

    if action == "create":
        if not entities.get("product") or not entities.get("region"):
            return None
        product = _product_read_step("s1", entities)
        supplier = _supplier_read_step("s2", entities)
        create = _write_step(
            "s3",
            _CREATE_ORDER,
            description="创建订单",
            arguments={"quantity": entities.get("quantity"), "region": entities["region"]},
            argument_sources={"product_id": "step:s1", "supplier_id": "step:s2"},
            depends_on=["s1", "s2"],
            fallback="取消新建订单以补偿",
        )
        return [product, supplier, create]

    if order_id is None:
        return None

    lookup = _order_lookup_step("s1", order_id)

    if action == "cancel":
        cancel = _write_step(
            "s2",
            _CANCEL_ORDER,
            description="取消订单",
            arguments={},
            argument_sources={"order_id": "step:s1"},
            depends_on=["s1"],
            fallback="重新激活原订单",
        )
        return [lookup, cancel]

    if action == "update_status":
        update = _write_step(
            "s2",
            _UPDATE_ORDER_STATUS,
            description="更新订单状态",
            arguments={"status": entities.get("status")},
            argument_sources={"order_id": "step:s1"},
            depends_on=["s1"],
            fallback="回滚订单状态到原值",
        )
        return [lookup, update]

    if action == "modify":
        cancel = _write_step(
            "s2",
            _CANCEL_ORDER,
            description="取消原订单",
            arguments={},
            argument_sources={"order_id": "step:s1"},
            depends_on=["s1"],
            fallback="重新激活原订单",
        )
        create = _write_step(
            "s3",
            _CREATE_ORDER,
            description="按原订单字段重建订单",
            arguments={"quantity": entities.get("new_quantity") or entities.get("quantity")},
            argument_sources={
                "product_id": "step:s1",
                "supplier_id": "step:s1",
                "region": "step:s1",
            },
            depends_on=["s1", "s2"],
            fallback="取消新建订单以补偿",
        )
        return [lookup, cancel, create]

    return None


def stamp_idempotency_keys(plan: Plan, run_id: str) -> Plan:
    """Stamp a deterministic idempotency key onto every WRITE/DANGEROUS step.

    The key is f"{run_id}:{step_id}": the same run + same step always maps to
    the same key, so a retry or crash-resume replays (never re-applies) the
    write, while different runs get distinct keys. The key lives both on the
    step (for recovery/reconciliation) and in the step's arguments (so the
    executor receives it as the write tool's idempotency_key parameter).
    """
    stamped: list[PlanStep] = []
    for step in plan.steps:
        if step.risk_level in (ToolRiskLevel.WRITE, ToolRiskLevel.DANGEROUS):
            key = f"{run_id}:{step.step_id}"
            step = step.model_copy(
                update={
                    "idempotency_key": key,
                    "arguments": {**step.arguments, "idempotency_key": key},
                }
            )
        stamped.append(step)
    return plan.model_copy(update={"steps": stamped})


def build_deterministic_plan_node() -> Callable[[AgentState], dict[str, Any]]:
    """Build the build_plan LangGraph node using the deterministic planner.

    The worker injects this node in place of the LLM-driven build_plan_node. It
    emits the plan plus the candidate tool set it used, mirroring the
    build_plan node contract so validate_plan can check OUT_OF_CANDIDATES.
    """

    def plan_node(state: AgentState) -> dict[str, Any]:
        if state.intent is None:
            return {"errors": [StateError(code="NO_INTENT", message="build_plan 阶段缺少意图分类")]}
        plan, errors = build_plan_from_intent(state.intent)
        plan = stamp_idempotency_keys(plan, state.run_id)
        updates: dict[str, Any] = {
            "plan": plan,
            "candidate_tools": [step.tool_name for step in plan.steps],
        }
        if errors:
            updates["errors"] = [*state.errors, *errors]
        return updates

    return plan_node
