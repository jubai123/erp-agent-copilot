"""Unit tests for agent/state.py — the Typed AgentState (task 4.1).

The state is the strong-typed object flowing through LangGraph nodes. It
must be JSON-serializable so it can be checkpointed (task 4.12) and must
validate strictly so a wayward LLM plan (task 4.6) cannot silently corrupt
the run.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from erp_copilot.agent.state import (
    AgentState,
    AgentStatus,
    IntentClassification,
    Plan,
    PlanStep,
    RetrievedDocument,
    StateError,
    StepResult,
)
from erp_copilot.domain.enums import StepStatus, ToolRiskLevel

PLAN_STEP_CONTRACT = {
    "step_id": "check_recent_changes",
    "description": "查询发往上海的可用物流供应商",
    "depends_on": [],
    "tool_name": "querySuppliersByDeliveryRegion",
    "arguments": {"region": "上海"},
    "argument_sources": {"region": "user_query"},
    "risk_level": "READ",
    "required_scope": "supplier:read",
    "requires_approval": False,
    "timeout_s": 10,
    "max_retries": 2,
    "idempotency_key": None,
    "expected_output_schema": "ChangeList",
    "success_condition": "response.status == 'ok' and len(response.suppliers) > 0",
    "fallback": "ask_for_alternate_delivery_region",
}


class TestAgentStatus:
    def test_has_all_runtime_states(self) -> None:
        expected = {
            "queued",
            "planning",
            "waiting_approval",
            "executing",
            "verifying",
            "retrying",
            "replanning",
            "succeeded",
            "failed",
            "cancelled",
            "expired",
        }
        assert {s.value for s in AgentStatus} == expected

    def test_is_str_enum(self) -> None:
        # StrEnum.__str__ returns the value, unlike a plain Enum.
        assert str(AgentStatus.QUEUED) == "queued"
        assert AgentStatus.QUEUED.value == "queued"


class TestIntentClassification:
    def test_defaults(self) -> None:
        intent = IntentClassification(domain="product", action="query")
        assert intent.risk_level == ToolRiskLevel.READ
        assert intent.entities == {}

    def test_constructs_with_values(self) -> None:
        intent = IntentClassification(
            domain="order",
            action="create",
            risk_level=ToolRiskLevel.WRITE,
            entities={"product": "苹果", "quantity": 10},
        )
        assert intent.entities["product"] == "苹果"


class TestPlanStep:
    def test_requires_step_id_and_tool_name(self) -> None:
        with pytest.raises(ValidationError):
            PlanStep()  # type: ignore[call-arg]
        with pytest.raises(ValidationError):
            PlanStep(step_id="s1")  # type: ignore[call-arg]

    def test_defaults(self) -> None:
        step = PlanStep(step_id="s1", tool_name="getProductByName")
        assert step.depends_on == []
        assert step.arguments == {}
        assert step.argument_sources == {}
        assert step.risk_level == ToolRiskLevel.READ
        assert step.requires_approval is False
        assert step.max_retries == 0
        assert step.required_scope is None

    def test_accepts_full_contract_from_docs(self) -> None:
        step = PlanStep.model_validate(PLAN_STEP_CONTRACT)
        assert step.step_id == "check_recent_changes"
        assert step.tool_name == "querySuppliersByDeliveryRegion"
        assert step.arguments == {"region": "上海"}
        assert step.argument_sources == {"region": "user_query"}
        assert step.risk_level == ToolRiskLevel.READ
        assert step.timeout_s == 10
        assert step.max_retries == 2
        assert step.success_condition is not None
        assert step.fallback == "ask_for_alternate_delivery_region"

    def test_risk_level_accepts_uppercase(self) -> None:
        assert (
            PlanStep(
                step_id="s1",
                tool_name="t",
                risk_level="WRITE",  # type: ignore[arg-type]
            ).risk_level
            == ToolRiskLevel.WRITE
        )

    def test_risk_level_maps_admin_to_dangerous(self) -> None:
        step = PlanStep(
            step_id="s1",
            tool_name="t",
            risk_level="ADMIN",  # type: ignore[arg-type]
        )
        assert step.risk_level == ToolRiskLevel.DANGEROUS

    def test_rejects_unknown_risk_level(self) -> None:
        with pytest.raises(ValidationError):
            PlanStep(step_id="s1", tool_name="t", risk_level="EXPLODE")  # type: ignore[arg-type]


class TestPlan:
    def test_holds_steps_in_order(self) -> None:
        steps = [
            PlanStep(step_id="s1", tool_name="getProductByName"),
            PlanStep(step_id="s2", tool_name="createOrder"),
        ]
        plan = Plan(steps=steps, title="采购流程")
        assert plan.title == "采购流程"
        assert [s.step_id for s in plan.steps] == ["s1", "s2"]

    def test_title_optional(self) -> None:
        plan = Plan(steps=[])
        assert plan.title == ""


class TestStepResult:
    def test_defaults(self) -> None:
        result = StepResult(step_id="s1", status=StepStatus.COMPLETED)
        assert result.data is None
        assert result.error_code is None
        assert result.is_retryable is False

    def test_constructs_with_error(self) -> None:
        result = StepResult(
            step_id="s1",
            status=StepStatus.FAILED,
            error_code="TIMEOUT",
            error_message="connect reset",
            is_retryable=True,
        )
        assert result.is_retryable is True


class TestAgentState:
    def test_requires_identifiers(self) -> None:
        with pytest.raises(ValidationError):
            AgentState()  # type: ignore[call-arg]
        with pytest.raises(ValidationError):
            AgentState(run_id="r1", tenant_id="t1")  # type: ignore[call-arg]

    def test_defaults(self) -> None:
        state = AgentState(run_id="r1", tenant_id="t1", query="查苹果库存")
        assert state.status == AgentStatus.QUEUED
        assert state.intent is None
        assert state.plan is None
        assert state.retrieved_context == []
        assert state.candidate_tools == []
        assert state.step_results == {}
        assert state.errors == []
        assert state.retry_count == 0
        assert state.replan_count == 0
        assert state.user_id is None

    def test_json_round_trip_preserves_everything(self) -> None:
        state = AgentState(
            run_id="run-1",
            tenant_id="tenant-1",
            user_id="user-1",
            query="查苹果库存并推荐供应商",
            status=AgentStatus.PLANNING,
            intent=IntentClassification(
                domain="order",
                action="create",
                risk_level=ToolRiskLevel.WRITE,
                entities={"product": "苹果"},
            ),
            active_rules=[{"rule_id": "order-state-machine"}],
            retrieved_context=[
                RetrievedDocument(
                    content="库存规则",
                    source="orders.md",
                    section_path=["库存"],
                    score=0.92,
                )
            ],
            candidate_tools=["getProductByName", "createOrder"],
            plan=Plan(steps=[PlanStep(step_id="s1", tool_name="getProductByName")]),
            current_step_id="s1",
            step_results={
                "s1": StepResult(
                    step_id="s1",
                    status=StepStatus.COMPLETED,
                    data={"product": "苹果", "stock": 100},
                )
            },
            errors=[StateError(code="VALIDATION_ERROR", message="bad step")],
            retry_count=1,
            replan_count=2,
            started_at=datetime(2026, 8, 7, 10, 30, tzinfo=UTC),
            deadline_at=datetime(2026, 8, 7, 10, 35, tzinfo=UTC),
        )

        restored = AgentState.model_validate_json(state.model_dump_json())

        assert restored == state
        assert restored.model_dump(mode="json") == state.model_dump(mode="json")

    def test_datetime_serialized_to_iso(self) -> None:
        state = AgentState(
            run_id="r1",
            tenant_id="t1",
            query="q",
            started_at=datetime(2026, 8, 7, 10, 30, tzinfo=UTC),
        )
        dumped = state.model_dump(mode="json")
        assert dumped["started_at"] == "2026-08-07T10:30:00Z"

    def test_unknown_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            AgentState(
                run_id="r1",
                tenant_id="t1",
                query="q",
                typo_field="boom",  # type: ignore[call-arg]
            )

    def test_default_lists_are_not_shared(self) -> None:
        a = AgentState(run_id="r1", tenant_id="t1", query="q")
        b = AgentState(run_id="r2", tenant_id="t2", query="q2")
        a.errors.append(StateError(code="X", message="m"))
        assert b.errors == []
