"""Unit tests for security/approval.py — task 5.2 decision + resume.

The ApprovalDecisionService flips a PENDING approval request to APPROVED or
DENIED and persists the decided state as the newest checkpoint; resume_run
re-invokes the graph from that checkpoint so a decided run continues — an
APPROVED step executes, a DENIED step is skipped by request_approval's policy
resolution. Tests run against in-memory SQLite + an injected clock, the same
pattern as the 4.12 checkpoint tests.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from erp_copilot.agent.graph import build_agent_graph
from erp_copilot.agent.nodes.execute_steps import build_execute_steps_node
from erp_copilot.agent.nodes.policy_check import build_policy_check_node
from erp_copilot.agent.nodes.validate_plan import ToolSpec, build_validate_plan_node
from erp_copilot.agent.state import (
    AgentState,
    AgentStatus,
    ApprovalRequest,
    ApprovalStatus,
    Plan,
    PlanStep,
    PolicyDecision,
)
from erp_copilot.domain.entities import AgentCheckpoint
from erp_copilot.domain.enums import ToolRiskLevel
from erp_copilot.domain.errors import CopilotError, NotFoundError
from erp_copilot.memory.checkpoint import CheckpointSaver
from erp_copilot.security.approval import ApprovalDecisionService, resume_run
from erp_copilot.tools.tool_result import ToolResult


@pytest.fixture()
def session() -> Iterator[Session]:
    engine = create_engine("sqlite:///:memory:")
    AgentCheckpoint.__table__.create(engine)
    with Session(engine) as db:
        yield db
    engine.dispose()


class _FakeClock:
    def __init__(self) -> None:
        self._now = datetime(2026, 8, 8, tzinfo=UTC)

    @property
    def now(self) -> datetime:
        return self._now

    def __call__(self) -> datetime:
        self._now += timedelta(seconds=1)
        return self._now


def _paused_state(**overrides: object) -> AgentState:
    # The step carries a fallback so the real validate_plan (used by
    # _resume_graph) accepts the plan — a run paused in WAITING_APPROVAL has
    # already passed validation, so re-validating it on resume must too.
    data: dict[str, object] = {
        "run_id": "r1",
        "tenant_id": "t1",
        "user_id": "u1",
        "query": "创建订单",
        "status": AgentStatus.WAITING_APPROVAL,
        "plan": Plan(
            steps=[
                PlanStep(
                    step_id="s1",
                    tool_name="createOrder",
                    description="创建订单",
                    risk_level=ToolRiskLevel.WRITE,
                    required_scope="order:write",
                    fallback="取消创建订单",
                )
            ]
        ),
        "policy_decisions": {"s1": PolicyDecision.REQUIRE_APPROVAL},
        "approvals": [
            ApprovalRequest(
                step_id="s1",
                tool_name="createOrder",
                description="创建订单",
                risk_level=ToolRiskLevel.WRITE,
                required_scope="order:write",
            )
        ],
    }
    data.update(overrides)
    return AgentState(**data)


def _save_paused(session: Session, saver: CheckpointSaver | None = None) -> CheckpointSaver:
    saver = saver or CheckpointSaver(session, clock=_FakeClock())
    saver.save("request_approval", _paused_state())
    return saver


def _service(saver: CheckpointSaver) -> ApprovalDecisionService:
    return ApprovalDecisionService(saver, clock=_FakeClock())


def _resume_graph():
    """A graph that can actually run the resumed WRITE step."""
    validate = build_validate_plan_node(tool_schemas={"createOrder": ToolSpec(name="createOrder")})
    policy = build_policy_check_node(get_scopes=lambda _t, _u: {"order:write"})

    async def fake_executor(_tool_name: str, _arguments: dict[str, object]) -> ToolResult:
        return ToolResult.success(tool_version_id="v1", data={"order_id": "ord123"})

    execute = build_execute_steps_node(executor=fake_executor)
    return build_agent_graph(validate_node=validate, policy_node=policy, execute_node=execute)


class TestDecisionService:
    def test_approve_flips_and_persists(self, session: Session) -> None:
        saver = _save_paused(session)
        decided = _service(saver).decide(
            run_id="r1",
            tenant_id="t1",
            step_id="s1",
            decision=ApprovalStatus.APPROVED,
            decided_by="manager",
        )
        assert decided.status == ApprovalStatus.APPROVED
        assert decided.decided_by == "manager"
        restored = saver.load_latest("r1", "t1")
        assert restored is not None
        assert restored.approvals[0].status == ApprovalStatus.APPROVED

    def test_deny_flips_and_persists(self, session: Session) -> None:
        saver = _save_paused(session)
        decided = _service(saver).decide(
            run_id="r1",
            tenant_id="t1",
            step_id="s1",
            decision=ApprovalStatus.DENIED,
            decided_by="manager",
        )
        assert decided.status == ApprovalStatus.DENIED
        restored = saver.load_latest("r1", "t1")
        assert restored is not None
        assert restored.approvals[0].status == ApprovalStatus.DENIED

    def test_decision_records_who_when_why(self, session: Session) -> None:
        saver = _save_paused(session)
        clock = _FakeClock()
        decided = ApprovalDecisionService(saver, clock=clock).decide(
            run_id="r1",
            tenant_id="t1",
            step_id="s1",
            decision=ApprovalStatus.APPROVED,
            decided_by="manager",
            reason="客户确认可下单",
        )
        # decide() consumed exactly one tick of the injected clock.
        assert decided.decided_at == clock.now
        assert decided.reason == "客户确认可下单"

    def test_decide_keeps_other_pending_untouched(self, session: Session) -> None:
        saver = CheckpointSaver(session, clock=_FakeClock())
        saver.save(
            "request_approval",
            _paused_state(
                plan=Plan(
                    steps=[
                        PlanStep(
                            step_id="s1",
                            tool_name="createOrder",
                            risk_level=ToolRiskLevel.WRITE,
                            required_scope="order:write",
                            fallback="取消创建订单",
                        ),
                        PlanStep(
                            step_id="s2",
                            tool_name="createOrder",
                            risk_level=ToolRiskLevel.WRITE,
                            required_scope="order:write",
                            fallback="取消创建订单",
                        ),
                    ]
                ),
                policy_decisions={
                    "s1": PolicyDecision.REQUIRE_APPROVAL,
                    "s2": PolicyDecision.REQUIRE_APPROVAL,
                },
                approvals=[
                    ApprovalRequest(
                        step_id="s1",
                        tool_name="createOrder",
                        risk_level=ToolRiskLevel.WRITE,
                        required_scope="order:write",
                    ),
                    ApprovalRequest(
                        step_id="s2",
                        tool_name="createOrder",
                        risk_level=ToolRiskLevel.WRITE,
                        required_scope="order:write",
                    ),
                ],
            ),
        )
        _service(saver).decide(
            run_id="r1",
            tenant_id="t1",
            step_id="s2",
            decision=ApprovalStatus.APPROVED,
            decided_by="manager",
        )
        restored = saver.load_latest("r1", "t1")
        assert restored is not None
        # Deciding s2 leaves s1's record pending.
        assert restored.approvals[0].status == ApprovalStatus.PENDING
        assert restored.approvals[1].status == ApprovalStatus.APPROVED

    def test_reject_when_run_not_waiting(self, session: Session) -> None:
        saver = CheckpointSaver(session, clock=_FakeClock())
        saver.save("execute_ready_steps", _paused_state(status=AgentStatus.EXECUTING))
        with pytest.raises(CopilotError) as exc_info:
            _service(saver).decide(
                run_id="r1",
                tenant_id="t1",
                step_id="s1",
                decision=ApprovalStatus.APPROVED,
                decided_by="manager",
            )
        assert exc_info.value.code == "RUN_NOT_WAITING_APPROVAL"

    def test_reject_when_no_checkpoint(self, session: Session) -> None:
        saver = CheckpointSaver(session)
        with pytest.raises(NotFoundError) as exc_info:
            _service(saver).decide(
                run_id="r1",
                tenant_id="t1",
                step_id="s1",
                decision=ApprovalStatus.APPROVED,
                decided_by="manager",
            )
        assert exc_info.value.code == "CHECKPOINT_NOT_FOUND"

    def test_reject_when_step_not_found(self, session: Session) -> None:
        saver = _save_paused(session)
        with pytest.raises(NotFoundError) as exc_info:
            _service(saver).decide(
                run_id="r1",
                tenant_id="t1",
                step_id="ghost",
                decision=ApprovalStatus.APPROVED,
                decided_by="manager",
            )
        assert exc_info.value.code == "APPROVAL_NOT_FOUND"

    def test_reject_when_already_decided(self, session: Session) -> None:
        saver = CheckpointSaver(session, clock=_FakeClock())
        decided = ApprovalRequest(
            step_id="s1",
            tool_name="createOrder",
            risk_level=ToolRiskLevel.WRITE,
            status=ApprovalStatus.APPROVED,
        )
        saver.save(
            "approval_decision",
            _paused_state(approvals=[decided]),
        )
        with pytest.raises(CopilotError) as exc_info:
            _service(saver).decide(
                run_id="r1",
                tenant_id="t1",
                step_id="s1",
                decision=ApprovalStatus.APPROVED,
                decided_by="manager",
            )
        assert exc_info.value.code == "APPROVAL_ALREADY_DECIDED"

    def test_reject_invalid_decision_value(self, session: Session) -> None:
        saver = _save_paused(session)
        with pytest.raises(CopilotError) as exc_info:
            _service(saver).decide(
                run_id="r1",
                tenant_id="t1",
                step_id="s1",
                decision=ApprovalStatus.PENDING,
                decided_by="manager",
            )
        assert exc_info.value.code == "APPROVAL_DECISION_INVALID"

    def test_tenant_isolated_decision(self, session: Session) -> None:
        _save_paused(session)  # saved under tenant t1
        saver = CheckpointSaver(session)
        with pytest.raises(NotFoundError) as exc_info:
            _service(saver).decide(
                run_id="r1",
                tenant_id="t2",
                step_id="s1",
                decision=ApprovalStatus.APPROVED,
                decided_by="manager",
            )
        assert exc_info.value.code == "CHECKPOINT_NOT_FOUND"


class TestResumeRun:
    def test_resume_run_without_checkpoint_raises(self, session: Session) -> None:
        saver = CheckpointSaver(session)
        with pytest.raises(NotFoundError) as exc_info:
            asyncio.run(resume_run(_resume_graph(), saver, run_id="r1", tenant_id="t1"))
        assert exc_info.value.code == "CHECKPOINT_NOT_FOUND"

    def test_resume_after_approve_executes_step(self, session: Session) -> None:
        saver = _save_paused(session)
        _service(saver).decide(
            run_id="r1",
            tenant_id="t1",
            step_id="s1",
            decision=ApprovalStatus.APPROVED,
            decided_by="manager",
        )
        result = asyncio.run(resume_run(_resume_graph(), saver, run_id="r1", tenant_id="t1"))
        assert result["step_results"]["s1"].status == "completed"
        assert result["step_results"]["s1"].data["order_id"] == "ord123"
        assert result["status"] == "succeeded"

    def test_resume_after_deny_skips_step(self, session: Session) -> None:
        saver = _save_paused(session)
        _service(saver).decide(
            run_id="r1",
            tenant_id="t1",
            step_id="s1",
            decision=ApprovalStatus.DENIED,
            decided_by="manager",
            reason="风控拒绝",
        )
        result = asyncio.run(resume_run(_resume_graph(), saver, run_id="r1", tenant_id="t1"))
        assert result["step_results"]["s1"].status == "skipped"
        assert result["status"] == "succeeded"
