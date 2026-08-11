"""Deterministic plan DAG builder — task 4.16 worker integration.

The worker has no LLM client wired, so the build_plan node it injects is a
deterministic planner (ADR "确定性优先，概率兜底"): it maps the classified
intent plus extracted entities onto a small READ-only Plan DAG against the ERP
simulator's product/supplier tools. A combined query like "查苹果库存并推荐
供应商" (task 4.16 acceptance) yields two independent READ steps that execute
in parallel. A query that yields neither a product nor a supplier intent
reports EMPTY_PLAN so the run fails honestly instead of inventing a tool call.

The LLM-driven planner (build_plan.py) stays the default for the interactive
path; this module is the deterministic fallback the worker uses until a real
LLM is wired into the task.
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

_PRODUCT_TOOL = "getProductByName"
_SUPPLIER_TOOL = "getSupplierByStatus"


def _product_step(product: str) -> PlanStep:
    return PlanStep(
        step_id="s1",
        tool_name=_PRODUCT_TOOL,
        description=f"查询 {product} 库存",
        arguments={"name": product},
        risk_level=ToolRiskLevel.READ,
        required_scope="product:read",
        success_condition=f"response.name == '{product}'",
        timeout_s=10,
        max_retries=1,
    )


def _supplier_step() -> PlanStep:
    return PlanStep(
        step_id="s2",
        tool_name=_SUPPLIER_TOOL,
        description="查询可用供应商",
        arguments={"status": "AVAILABLE"},
        risk_level=ToolRiskLevel.READ,
        required_scope="supplier:read",
        timeout_s=10,
        max_retries=1,
    )


def build_plan_from_intent(intent: IntentClassification) -> tuple[Plan, list[StateError]]:
    """Map the classified intent onto a READ-only Plan DAG.

    Returns (plan, errors); errors is non-empty only when the intent carries
    neither a product entity nor a supplier domain — the planner then has
    nothing honest to execute and reports EMPTY_PLAN.
    """
    steps: list[PlanStep] = []
    product = intent.entities.get("product")
    if isinstance(product, str):
        steps.append(_product_step(product))
    if intent.domain == "supplier":
        steps.append(_supplier_step())
    if not steps:
        return Plan(steps=[]), [
            StateError(
                code="EMPTY_PLAN",
                message="无法从意图构造任何可执行步骤（缺少商品名或供应商意图）",
            )
        ]
    return Plan(steps=steps, title=intent.action), []


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
        updates: dict[str, Any] = {
            "plan": plan,
            "candidate_tools": [step.tool_name for step in plan.steps],
        }
        if errors:
            updates["errors"] = [*state.errors, *errors]
        return updates

    return plan_node
