"""Integration tests for the approve/deny API route — task 5.2 backfill.

POST /v1/runs/{run_id}/approve flips a PENDING approval to APPROVED/DENIED,
writes an audit_logs row, then resumes the run from its checkpoint. Since the
route was wired to the shared worker graph (real nodes + idempotent writes), a
resumed run actually executes against the ERP simulator and the terminal state
is persisted onto the Run/RunStep rows — FAILED/EXPIRED outcomes route through
the human-intervention queue (task 5.9).

Tests run against the dedicated test database (tests/conftest.py). The paused
checkpoint is seeded directly because POST /v1/runs cannot carry a WRITE query
(the route only passes product_name), so create_run alone never pauses for
approval.
"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from apps.erp_simulator.data.orders import get_by_idempotency_key
from apps.erp_simulator.data.products import PRODUCT_BY_NAME
from erp_copilot.agent.state import (
    AgentState,
    AgentStatus,
    ApprovalRequest,
)
from erp_copilot.application.failure_queue import FailureQueue
from erp_copilot.domain.entities import (
    AuditLog,
    IdempotencyRecord,
    Role,
    RoleScope,
    Run,
    RunEvent,
    Tenant,
    User,
    UserRole,
)
from erp_copilot.domain.enums import ToolRiskLevel
from erp_copilot.infrastructure.database import get_session
from erp_copilot.memory.checkpoint import CheckpointSaver

# The same WRITE query the worker tests use: the deterministic planner maps it
# to a create-order DAG (s1 product read, s2 supplier read, s3 createOrder), so
# approving s3 resumes the run through a real idempotent write.
_WRITE_QUERY = "帮我在上海下一单 1 KG 苹果"


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


def _make_writer_user(session: Session, tenant_id: str) -> str:
    """Create an active user whose scopes let the resumed create-order DAG run.

    On resume the graph re-runs policy_check against the checkpoint's user_id
    (docs/07 §8), so the acting user must exist in the role graph holding
    product:read, supplier:read and order:write — otherwise every scoped step is
    policy-denied and the resumed run completes as a silent no-op.
    """
    role = Role(tenant_id=tenant_id, name="writer")
    session.add(role)
    session.flush()
    for resource, action in [("product", "read"), ("supplier", "read"), ("order", "write")]:
        session.add(RoleScope(role_id=role.id, resource=resource, action=action))
    user = User(
        tenant_id=tenant_id,
        email="writer@example.com",
        hashed_password="x",
        is_active=True,
    )
    session.add(user)
    session.flush()
    session.add(UserRole(user_id=user.id, role_id=role.id))
    session.commit()
    return user.id


def _create_paused_run(tenant_id: str) -> str:
    """Create a Run paused in WAITING_APPROVAL and return its ID."""
    session = get_session()
    try:
        run = Run(tenant_id=tenant_id, title="审批测试", status="WAITING_APPROVAL")
        session.add(run)
        session.commit()
        return str(run.id)
    finally:
        session.close()


def _save_paused_checkpoint(
    run_id: str,
    tenant_id: str,
    *,
    query: str = _WRITE_QUERY,
    step_id: str = "s3",
    waiting: bool = True,
) -> None:
    """Save a checkpoint paused at request_approval with one PENDING write step.

    The plan/policy are deliberately omitted: on resume the graph re-runs
    classify -> build_plan (deterministic) and regenerates the create-order DAG,
    so the seed only needs to carry the query and the pending approval record
    the decision API flips.
    """
    session = get_session()
    try:
        user_id = _make_writer_user(session, tenant_id)
        CheckpointSaver(session).save(
            "request_approval",
            AgentState(
                run_id=run_id,
                tenant_id=tenant_id,
                user_id=user_id,
                query=query,
                status=AgentStatus.WAITING_APPROVAL if waiting else AgentStatus.EXECUTING,
                approvals=[
                    ApprovalRequest(
                        step_id=step_id,
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


def _run_status_events(run_id: str) -> list[str]:
    """Return RUN_STATUS event payload statuses for *run_id* in order."""
    session = get_session()
    try:
        events = (
            session.query(RunEvent)
            .filter_by(run_id=run_id, event_type="RUN_STATUS")
            .order_by(RunEvent.sequence.asc())
            .all()
        )
        return [json.loads(event.payload)["status"] for event in events]
    finally:
        session.close()


class TestApproveRun:
    """Acceptance: approve resumes the run through the real graph and persists."""

    def test_approve_executes_write_end_to_end(self) -> None:
        from apps.api.main import create_app

        tenant_id = _create_tenant("Approve Write", "approve-write")
        run_id = _create_paused_run(tenant_id)
        _save_paused_checkpoint(run_id, tenant_id)
        product = PRODUCT_BY_NAME["苹果"]
        original_stock = product.quantity_in_stock
        client = TestClient(create_app())
        try:
            response = client.post(
                f"/v1/runs/{run_id}/approve",
                json={
                    "step_id": "s3",
                    "decision": "APPROVE",
                    "decided_by": "manager",
                    "reason": "已核对产品与数量",
                },
            )

            assert response.status_code == 200
            data = response.json()
            assert data["run_id"] == run_id
            assert data["step_id"] == "s3"
            assert data["decision"] == "approved"
            assert data["decided_by"] == "manager"
            assert data["run_status"] == "succeeded"

            session = get_session()
            try:
                log = session.query(AuditLog).filter_by(resource=f"run:{run_id}/step:s3").first()
                assert log is not None
                assert log.actor == "manager"
                assert log.action == "approval.approved"
                assert log.result == "approved"

                run = session.query(Run).filter_by(id=run_id).first()
                assert run is not None
                assert run.status == "COMPLETED"
                assert run.completed_at is not None
                assert {step.step_index: step.status for step in run.steps} == {
                    1: "COMPLETED",
                    2: "COMPLETED",
                    3: "COMPLETED",
                }

                record = (
                    session.query(IdempotencyRecord)
                    .filter_by(idempotency_key=f"{run_id}:s3")
                    .first()
                )
                assert record is not None
                assert record.status == "COMPLETED"
            finally:
                session.close()

            order = get_by_idempotency_key(f"{run_id}:s3")
            assert order is not None
            assert order.status == "CREATED"
            assert order.quantity == 1

            # The resume path shares the worker's status-event sink, so the SSE
            # stream shows the resumed run reach its terminal status.
            assert _run_status_events(run_id)[-1] == "succeeded"
        finally:
            product.quantity_in_stock = original_stock

    def test_deny_skips_write_and_completes(self) -> None:
        from apps.api.main import create_app

        tenant_id = _create_tenant("Deny Write", "deny-write")
        run_id = _create_paused_run(tenant_id)
        _save_paused_checkpoint(run_id, tenant_id)
        client = TestClient(create_app())

        response = client.post(
            f"/v1/runs/{run_id}/approve",
            json={
                "step_id": "s3",
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
            log = session.query(AuditLog).filter_by(resource=f"run:{run_id}/step:s3").first()
            assert log is not None
            assert log.action == "approval.denied"
            assert log.actor == "risk"

            run = session.query(Run).filter_by(id=run_id).first()
            assert run is not None
            assert run.status == "COMPLETED"
            # The denied WRITE step is skipped; the READ lookups still ran.
            assert {step.step_index: step.status for step in run.steps} == {
                1: "COMPLETED",
                2: "COMPLETED",
                3: "SKIPPED",
            }
            order = get_by_idempotency_key(f"{run_id}:s3")
            assert order is None
        finally:
            session.close()

    def test_failed_resume_enters_human_queue(self) -> None:
        from apps.api.main import create_app

        # "创建订单" carries no product/region entity, so on resume the
        # deterministic planner emits EMPTY_PLAN and the run fails into the
        # human-intervention queue (task 5.9) instead of completing.
        tenant_id = _create_tenant("Approve Fail", "approve-fail")
        run_id = _create_paused_run(tenant_id)
        _save_paused_checkpoint(run_id, tenant_id, query="创建订单", step_id="s1")
        client = TestClient(create_app())

        response = client.post(
            f"/v1/runs/{run_id}/approve",
            json={"step_id": "s1", "decision": "APPROVE", "decided_by": "manager"},
        )

        assert response.status_code == 200
        assert response.json()["run_status"] == "failed"

        session = get_session()
        try:
            run = session.query(Run).filter_by(id=run_id).first()
            assert run is not None
            assert run.status == "FAILED"
            assert run.failure_code == "EMPTY_PLAN"
            assert run.failure_reason
            assert run.suggested_action

            event = (
                session.query(RunEvent).filter_by(run_id=run_id, event_type="RUN_FAILED").first()
            )
            assert event is not None
            assert json.loads(event.payload)["error_code"] == "EMPTY_PLAN"

            queue = FailureQueue(session).list_needing_intervention(tenant_id)
            assert [q.id for q in queue] == [run_id]
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
        run_id = _create_paused_run(tenant_id)
        _save_paused_checkpoint(run_id, tenant_id, waiting=False)
        client = TestClient(create_app())

        response = client.post(
            f"/v1/runs/{run_id}/approve",
            json={"step_id": "s3", "decision": "APPROVE", "decided_by": "manager"},
        )
        assert response.status_code == 409
