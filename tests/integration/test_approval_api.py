"""Integration tests for the approve/deny API route — task 5.2 backfill.

POST /v1/runs/{run_id}/approve flips a PENDING approval to APPROVED/DENIED,
writes an audit_logs row, then resumes the run from its checkpoint. Tests run
against the dedicated test database (tests/conftest.py) with the agent graph
built by the route itself (default no-op nodes; real node composition lands
with the worker wiring task).
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from erp_copilot.agent.state import (
    AgentState,
    AgentStatus,
    ApprovalRequest,
    Plan,
    PlanStep,
    PolicyDecision,
)
from erp_copilot.domain.entities import AuditLog, Run, Tenant
from erp_copilot.domain.enums import ToolRiskLevel
from erp_copilot.infrastructure.database import get_session
from erp_copilot.memory.checkpoint import CheckpointSaver


def _create_tenant(name: str, slug: str) -> str:
    """Create a tenant in the database and return its ID."""
    session = get_session()
    try:
        tenant = Tenant(name=name, slug=slug)
        session.add(tenant)
        session.commit()
        return str(tenant.id)
    finally:
        session.close()


def _create_run(tenant_id: str) -> str:
    """Create a Run paused in WAITING_APPROVAL and return its ID."""
    session = get_session()
    try:
        run = Run(tenant_id=tenant_id, title="审批测试", status="WAITING_APPROVAL")
        session.add(run)
        session.commit()
        return str(run.id)
    finally:
        session.close()


def _save_paused_checkpoint(run_id: str, tenant_id: str, *, waiting: bool) -> None:
    """Save a checkpoint with a single WRITE step awaiting approval."""
    session = get_session()
    try:
        CheckpointSaver(session).save(
            "request_approval",
            AgentState(
                run_id=run_id,
                tenant_id=tenant_id,
                user_id="u1",
                query="创建订单",
                status=AgentStatus.WAITING_APPROVAL if waiting else AgentStatus.EXECUTING,
                plan=Plan(
                    steps=[
                        PlanStep(
                            step_id="s1",
                            tool_name="createOrder",
                            description="创建订单",
                            risk_level=ToolRiskLevel.WRITE,
                            required_scope="order:write",
                        )
                    ]
                ),
                policy_decisions={"s1": PolicyDecision.REQUIRE_APPROVAL},
                approvals=[
                    ApprovalRequest(
                        step_id="s1",
                        tool_name="createOrder",
                        description="创建订单",
                        risk_level=ToolRiskLevel.WRITE,
                        required_scope="order:write",
                    )
                ],
            ),
        )
    finally:
        session.close()


class TestApproveRun:
    """Acceptance: POST /v1/runs/{run_id}/approve continues the run + audits."""

    def test_approve_continues_and_writes_audit_log(self) -> None:
        from apps.api.main import create_app

        tenant_id = _create_tenant("Approve Run", "approve-run")
        run_id = _create_run(tenant_id)
        _save_paused_checkpoint(run_id, tenant_id, waiting=True)
        client = TestClient(create_app())

        response = client.post(
            f"/v1/runs/{run_id}/approve",
            json={
                "step_id": "s1",
                "decision": "APPROVE",
                "decided_by": "manager",
                "reason": "已核对产品与数量",
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["run_id"] == run_id
        assert data["step_id"] == "s1"
        assert data["decision"] == "approved"
        assert data["decided_by"] == "manager"
        assert data["run_status"] == "succeeded"

        session = get_session()
        try:
            log = session.query(AuditLog).filter_by(resource=f"run:{run_id}/step:s1").first()
            assert log is not None
            assert log.actor == "manager"
            assert log.action == "approval.approved"
            assert log.result == "approved"
        finally:
            session.close()

    def test_deny_continues_and_writes_audit_log(self) -> None:
        from apps.api.main import create_app

        tenant_id = _create_tenant("Deny Run", "deny-run")
        run_id = _create_run(tenant_id)
        _save_paused_checkpoint(run_id, tenant_id, waiting=True)
        client = TestClient(create_app())

        response = client.post(
            f"/v1/runs/{run_id}/approve",
            json={
                "step_id": "s1",
                "decision": "DENY",
                "decided_by": "risk",
                "reason": "风控拒绝",
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["decision"] == "denied"
        assert data["run_status"] == "succeeded"

        session = get_session()
        try:
            log = session.query(AuditLog).filter_by(resource=f"run:{run_id}/step:s1").first()
            assert log is not None
            assert log.action == "approval.denied"
            assert log.actor == "risk"
        finally:
            session.close()

    def test_unknown_run_returns_404(self) -> None:
        from apps.api.main import create_app

        client = TestClient(create_app())
        response = client.post(
            "/v1/runs/ghost/approve",
            json={"step_id": "s1", "decision": "APPROVE", "decided_by": "manager"},
        )
        assert response.status_code == 404

    def test_run_not_waiting_for_approval_returns_409(self) -> None:
        from apps.api.main import create_app

        tenant_id = _create_tenant("Busy Run", "busy-run")
        run_id = _create_run(tenant_id)
        _save_paused_checkpoint(run_id, tenant_id, waiting=False)
        client = TestClient(create_app())

        response = client.post(
            f"/v1/runs/{run_id}/approve",
            json={"step_id": "s1", "decision": "APPROVE", "decided_by": "manager"},
        )
        assert response.status_code == 409
