"""Deterministic plan DAG builder — task 4.16 worker integration.

The worker has no LLM client wired, so the build_plan node it injects is a
deterministic planner (ADR "确定性优先，概率兜底"): it maps the classified
intent plus extracted entities onto a Plan DAG against the ERP simulator's
9 tools. READ intents emit single steps (or two independent READ steps for a
combined product+supplier query, which execute in parallel); WRITE intents
emit multi-step DAGs (create / cancel / update_status / modify) whose write
steps carry the compensation note and approval flag validate_plan (layer 2)
and the policy gate (layer 3) expect. A query that yields no tool emits
EMPTY_PLAN so the run fails honestly instead of inventing a tool call.

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
_ORDER_BY_ID = "getOrderByOrderId"
_CREATE_ORDER = "createOrder"
_UPDATE_ORDER_STATUS = "updateOrderStatus"
_CANCEL_ORDER = "cancelOrder"

_ORDER_WRITE_SCOPE = "order:write"


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
    """Pick the supplier READ tool by whether a delivery region is named."""
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

    Returns (plan, errors); errors is non-empty only when the intent carries
    neither a product entity nor a supplier domain nor an order id — the
    planner then has nothing honest to execute and reports EMPTY_PLAN.
    """
    entities = intent.entities
    steps: list[PlanStep] | None

    if intent.domain == "supplier":
        # A supplier intent may also carry a product entity (e.g. "查苹果库存
        # 并推荐供应商") — emit both independent READ steps.
        steps = []
        if entities.get("product"):
            steps.append(_product_read_step("s1", entities))
        steps.append(_supplier_read_step("s2" if steps else "s1", entities))
    elif intent.domain == "order":
        steps = _order_dag_steps(intent.action, entities)
        if steps is None:
            return _empty_plan_error()
    else:
        # product/query and product/check_stock (and the fallback domain).
        if entities.get("product") or entities.get("product_id") is not None:
            steps = [_product_read_step("s1", entities)]
        else:
            return _empty_plan_error()

    assert steps is not None
    return Plan(steps=steps, title=intent.action), []


def _order_dag_steps(action: str, entities: dict[str, Any]) -> list[PlanStep] | None:
    """Build the multi-step WRITE DAGs (order domain), or None for EMPTY_PLAN."""
    order_id = entities.get("order_id")

    if action == "query":
        if order_id is None:
            return None
        return [_order_lookup_step("s1", order_id)]

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


def _stamp_idempotency_keys(plan: Plan, run_id: str) -> Plan:
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
        plan = _stamp_idempotency_keys(plan, state.run_id)
        updates: dict[str, Any] = {
            "plan": plan,
            "candidate_tools": [step.tool_name for step in plan.steps],
        }
        if errors:
            updates["errors"] = [*state.errors, *errors]
        return updates

    return plan_node
