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

import erp_copilot.security.approval as approval_mod
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
from erp_copilot.domain.entities import AgentCheckpoint, AuditLog
from erp_copilot.domain.enums import ToolRiskLevel
from erp_copilot.domain.errors import CopilotError, NotFoundError
from erp_copilot.memory.checkpoint import CheckpointSaver
from erp_copilot.observability.metrics import create_metrics, generate_latest
from erp_copilot.security.approval import (
    ApprovalDecisionService,
    decide_and_resume,
    record_audit_log,
    resume_run,
)
from erp_copilot.tools.tool_result import ToolResult


@pytest.fixture()
def session() -> Iterator[Session]:
    engine = create_engine("sqlite:///:memory:")
    AgentCheckpoint.__table__.create(engine)
    AuditLog.__table__.create(engine)
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


class TestAuditLog:
    def test_record_audit_log_persists_all_columns(self, session: Session) -> None:
        entry = record_audit_log(
            session,
            actor="manager",
            resource="run:r1/step:s1",
            action="approval.approved",
            result="approved",
            ip="127.0.0.1",
            trace_id="tr-123",
        )
        row = session.get(AuditLog, entry.id)
        assert row is not None
        assert row.actor == "manager"
        assert row.resource == "run:r1/step:s1"
        assert row.action == "approval.approved"
        assert row.result == "approved"
        assert row.ip == "127.0.0.1"
        assert row.trace_id == "tr-123"
        assert row.created_at is not None

    def test_record_audit_log_ip_and_trace_optional(self, session: Session) -> None:
        entry = record_audit_log(session, actor="a", resource="r", action="x", result="y")
        row = session.get(AuditLog, entry.id)
        assert row is not None
        assert row.ip is None
        assert row.trace_id is None

    def test_created_at_is_immutable(self, session: Session) -> None:
        # created_at is default-only (no onupdate): auditing keeps one
        # immutable timestamp even if the row is later touched.
        entry = record_audit_log(session, actor="a", resource="r", action="x", result="y")
        original = session.get(AuditLog, entry.id)
        original.result = "changed"
        session.commit()
        after = session.get(AuditLog, entry.id)
        assert after.created_at == original.created_at


class TestDecideAndResume:
    def test_approve_decides_audits_and_resumes(self, session: Session) -> None:
        saver = _save_paused(session)
        decided, result = asyncio.run(
            decide_and_resume(
                _resume_graph(),
                saver,
                session,
                run_id="r1",
                tenant_id="t1",
                step_id="s1",
                decision=ApprovalStatus.APPROVED,
                decided_by="manager",
                reason="已核对",
                ip="127.0.0.1",
                trace_id="tr-1",
            )
        )
        assert decided.status == ApprovalStatus.APPROVED
        assert decided.decided_by == "manager"
        assert result["status"] == "succeeded"
        logs = session.query(AuditLog).all()
        assert len(logs) == 1
        assert logs[0].action == "approval.approved"
        assert logs[0].result == "approved"
        assert logs[0].resource == "run:r1/step:s1"
        assert logs[0].actor == "manager"
        assert logs[0].ip == "127.0.0.1"
        assert logs[0].trace_id == "tr-1"

    def test_deny_decides_audits_and_skips(self, session: Session) -> None:
        saver = _save_paused(session)
        decided, result = asyncio.run(
            decide_and_resume(
                _resume_graph(),
                saver,
                session,
                run_id="r1",
                tenant_id="t1",
                step_id="s1",
                decision=ApprovalStatus.DENIED,
                decided_by="risk",
            )
        )
        assert decided.status == ApprovalStatus.DENIED
        assert result["step_results"]["s1"].status == "skipped"
        assert result["status"] == "succeeded"
        logs = session.query(AuditLog).all()
        assert len(logs) == 1
        assert logs[0].action == "approval.denied"
        assert logs[0].result == "denied"
        assert logs[0].ip is None


class TestApprovalMetrics:
    """Task 7.4: deciding a request incs the matching outcome label.

    decide() flips exactly one PENDING record (APPROVAL_ALREADY_DECIDED guards
    double decisions), and the inc fires only after the checkpoint save succeeds
    — a failed persist produces neither a record nor a count, so metrics and the
    audit trail agree. The module-level METRICS is monkeypatched per test.
    """

    def _wired(self, monkeypatch: pytest.MonkeyPatch) -> object:
        metrics = create_metrics()
        monkeypatch.setattr(approval_mod, "METRICS", metrics)
        return metrics

    def test_approve_increments_approved_outcome(
        self, session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        metrics = self._wired(monkeypatch)
        saver = _save_paused(session)
        _service(saver).decide(
            run_id="r1",
            tenant_id="t1",
            step_id="s1",
            decision=ApprovalStatus.APPROVED,
            decided_by="ops",
        )
        text = generate_latest(metrics)
        assert 'erp_approval_requests_total{outcome="approved"} 1.0' in text
        # A label value never observed emits no series line — only the touched
        # outcome appears in the exposition text.
        assert 'outcome="denied"' not in text

    def test_denied_increments_denied_outcome(
        self, session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        metrics = self._wired(monkeypatch)
        saver = _save_paused(session)
        _service(saver).decide(
            run_id="r1",
            tenant_id="t1",
            step_id="s1",
            decision=ApprovalStatus.DENIED,
            decided_by="risk",
        )
        text = generate_latest(metrics)
        assert 'erp_approval_requests_total{outcome="denied"} 1.0' in text
        assert 'outcome="approved"' not in text
