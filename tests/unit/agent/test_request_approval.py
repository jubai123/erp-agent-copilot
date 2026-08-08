"""Unit tests for the request_approval node — task 5.2.

The node sits between policy_check and execute_ready_steps. It turns
REQUIRE_APPROVAL policy decisions into pending ApprovalRequest records and
pauses the run in WAITING_APPROVAL. On a later re-invoke (checkpoint resume
after a human approves), the node sees the APPROVED records, creates nothing
new, and the graph routes on to execution.

The node is a pure function of AgentState: it has no injected dependencies and
returns the state updates. Approval decisions (approve/deny) and the skip logic
for denied steps arrive with task 5.2's decision API.
"""

from __future__ import annotations

from erp_copilot.agent.nodes.request_approval import request_approval_node
from erp_copilot.agent.state import (
    AgentState,
    AgentStatus,
    ApprovalRequest,
    ApprovalStatus,
    Plan,
    PlanStep,
    PolicyDecision,
)
from erp_copilot.domain.enums import ToolRiskLevel


def _state(**overrides: object) -> AgentState:
    base: dict[str, object] = {"run_id": "r1", "tenant_id": "t1", "query": "创建订单"}
    base.update(overrides)
    return AgentState(**base)  # type: ignore[arg-type]


def _write_step(step_id: str = "s1") -> PlanStep:
    return PlanStep(
        step_id=step_id,
        tool_name="createOrder",
        description="创建订单",
        risk_level=ToolRiskLevel.WRITE,
        required_scope="order:write",
    )


def _read_step(step_id: str = "s1") -> PlanStep:
    return PlanStep(
        step_id=step_id,
        tool_name="getProductById",
        description="查询商品",
        risk_level=ToolRiskLevel.READ,
        required_scope="product:read",
    )


def _approval(
    step_id: str = "s1",
    status: ApprovalStatus = ApprovalStatus.APPROVED,
) -> ApprovalRequest:
    return ApprovalRequest(
        step_id=step_id,
        tool_name="createOrder",
        description="创建订单",
        risk_level=ToolRiskLevel.WRITE,
        required_scope="order:write",
        status=status,
    )


class TestNodePureFunction:
    def test_no_plan_returns_empty(self) -> None:
        assert request_approval_node(_state()) == {}

    def test_no_require_approval_steps_returns_empty(self) -> None:
        state = _state(
            plan=Plan(steps=[_read_step()]),
            policy_decisions={"s1": PolicyDecision.ALLOW},
        )
        assert request_approval_node(state) == {}

    def test_denied_step_not_awaited(self) -> None:
        state = _state(
            plan=Plan(steps=[_write_step()]),
            policy_decisions={"s1": PolicyDecision.DENY},
        )
        assert request_approval_node(state) == {}

    def test_creates_pending_request_with_full_context(self) -> None:
        state = _state(
            plan=Plan(steps=[_write_step()]),
            policy_decisions={"s1": PolicyDecision.REQUIRE_APPROVAL},
        )
        updates = request_approval_node(state)
        assert updates["status"] == AgentStatus.WAITING_APPROVAL
        [req] = updates["approvals"]
        assert req.step_id == "s1"
        assert req.tool_name == "createOrder"
        assert req.description == "创建订单"
        assert req.risk_level == ToolRiskLevel.WRITE
        assert req.required_scope == "order:write"
        assert req.status == ApprovalStatus.PENDING

    def test_multiple_require_approval_steps_recorded(self) -> None:
        state = _state(
            plan=Plan(steps=[_write_step("s1"), _write_step("s2")]),
            policy_decisions={
                "s1": PolicyDecision.REQUIRE_APPROVAL,
                "s2": PolicyDecision.REQUIRE_APPROVAL,
            },
        )
        updates = request_approval_node(state)
        assert {r.step_id for r in updates["approvals"]} == {"s1", "s2"}
        assert updates["status"] == AgentStatus.WAITING_APPROVAL

    def test_mixed_decisions_only_await_require_approval(self) -> None:
        state = _state(
            plan=Plan(steps=[_read_step("s1"), _write_step("s2")]),
            policy_decisions={
                "s1": PolicyDecision.ALLOW,
                "s2": PolicyDecision.REQUIRE_APPROVAL,
            },
        )
        updates = request_approval_node(state)
        assert [r.step_id for r in updates["approvals"]] == ["s2"]

    def test_read_step_with_requires_approval_flag_goes_to_approval(self) -> None:
        step = PlanStep(
            step_id="s1",
            tool_name="getProductById",
            description="导出客户清单",
            risk_level=ToolRiskLevel.READ,
            requires_approval=True,
        )
        state = _state(
            plan=Plan(steps=[step]),
            policy_decisions={"s1": PolicyDecision.REQUIRE_APPROVAL},
        )
        updates = request_approval_node(state)
        assert updates["status"] == AgentStatus.WAITING_APPROVAL
        assert updates["approvals"][0].tool_name == "getProductById"


class TestResumeIdempotency:
    def test_reinvoke_does_not_duplicate_requests(self) -> None:
        state = _state(
            plan=Plan(steps=[_write_step()]),
            policy_decisions={"s1": PolicyDecision.REQUIRE_APPROVAL},
        )
        first = request_approval_node(state)
        resumed = _state(
            plan=Plan(steps=[_write_step()]),
            policy_decisions={"s1": PolicyDecision.REQUIRE_APPROVAL},
            approvals=first["approvals"],
        )
        second = request_approval_node(resumed)
        # Nothing new appended — the record already exists.
        assert second.get("approvals") is None
        # Still awaiting because the record is still PENDING.
        assert second["status"] == AgentStatus.WAITING_APPROVAL

    def test_pending_request_keeps_run_waiting(self) -> None:
        state = _state(
            plan=Plan(steps=[_write_step()]),
            policy_decisions={"s1": PolicyDecision.REQUIRE_APPROVAL},
            approvals=[_approval(status=ApprovalStatus.PENDING)],
        )
        updates = request_approval_node(state)
        assert updates["status"] == AgentStatus.WAITING_APPROVAL

    def test_approved_request_releases_run(self) -> None:
        state = _state(
            plan=Plan(steps=[_write_step()]),
            policy_decisions={"s1": PolicyDecision.REQUIRE_APPROVAL},
            approvals=[_approval(status=ApprovalStatus.APPROVED)],
        )
        assert request_approval_node(state) == {}
