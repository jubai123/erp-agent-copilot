"""Integration tests for Run creation and query endpoints."""

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
from erp_copilot.domain.entities import Run, RunEvent, Tenant
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


class TestCreateRun:
    """Acceptance: POST /v1/runs creates a new Run."""

    def test_create_run_returns_202_and_run_id(self) -> None:
        from apps.api.main import create_app

        tenant_id = _create_tenant("Run Test", "run-test")
        client = TestClient(create_app())

        response = client.post(
            "/v1/runs",
            json={"tenant_id": tenant_id, "title": "Test run"},
        )

        assert response.status_code == 202
        data = response.json()
        assert "run_id" in data
        assert data["status"] == "COMPLETED"

    def test_create_run_default_title(self) -> None:
        from apps.api.main import create_app

        tenant_id = _create_tenant("Run Default", "run-default")
        client = TestClient(create_app())

        response = client.post(
            "/v1/runs",
            json={"tenant_id": tenant_id},
        )

        assert response.status_code == 202
        data = response.json()
        assert data["run_id"]
        assert data["status"] == "COMPLETED"


class TestGetRun:
    """Acceptance: GET /v1/runs/{run_id} returns Run status."""

    def test_get_existing_run(self) -> None:
        from apps.api.main import create_app

        tenant_id = _create_tenant("Get Run", "get-run")
        client = TestClient(create_app())

        create_resp = client.post(
            "/v1/runs",
            json={"tenant_id": tenant_id, "title": "My run"},
        )
        run_id = create_resp.json()["run_id"]

        response = client.get(f"/v1/runs/{run_id}")

        assert response.status_code == 200
        data = response.json()
        assert data["run_id"] == run_id
        assert data["status"] == "COMPLETED"
        assert data["title"] == "My run"

    def test_get_nonexistent_run_returns_404(self) -> None:
        from apps.api.main import create_app

        client = TestClient(create_app())

        response = client.get("/v1/runs/nonexistent-id")

        assert response.status_code == 404
        assert "detail" in response.json()


class TestCancelRun:
    """Acceptance: POST /v1/runs/{run_id}/cancel stops a non-terminal run."""

    def test_cancel_queued_run(self) -> None:
        from apps.api.main import create_app

        tenant_id = _create_tenant("Cancel Queue", "cancel-queue")
        run_id = _create_queued_run(tenant_id)
        client = TestClient(create_app())

        response = client.post(f"/v1/runs/{run_id}/cancel")

        assert response.status_code == 200
        assert response.json()["status"] == "CANCELLED"
        session = get_session()
        try:
            run = session.query(Run).filter_by(id=run_id).first()
            assert run is not None
            assert run.status == "CANCELLED"
            assert run.completed_at is not None
            event = session.query(RunEvent).filter_by(run_id=run_id).first()
            assert event is not None
            assert event.event_type == "RUN_CANCELLED"
        finally:
            session.close()

    def test_cancel_completed_run_conflicts(self) -> None:
        from apps.api.main import create_app

        tenant_id = _create_tenant("Cancel Done", "cancel-done")
        client = TestClient(create_app())
        create_resp = client.post("/v1/runs", json={"tenant_id": tenant_id, "title": "done"})
        run_id = create_resp.json()["run_id"]

        response = client.post(f"/v1/runs/{run_id}/cancel")

        assert response.status_code == 409

    def test_cancel_nonexistent_run_returns_404(self) -> None:
        from apps.api.main import create_app

        client = TestClient(create_app())

        response = client.post("/v1/runs/ghost/cancel")

        assert response.status_code == 404

    def test_cancel_paused_run_blocks_resume(self) -> None:
        from apps.api.main import create_app

        tenant_id = _create_tenant("Cancel Paused", "cancel-paused")
        run_id = _create_paused_run(tenant_id)
        client = TestClient(create_app())

        cancel_resp = client.post(f"/v1/runs/{run_id}/cancel")
        assert cancel_resp.status_code == 200

        # The CANCELLED checkpoint supersedes the paused state, so a late
        # approve must conflict instead of resuming a cancelled run.
        approve_resp = client.post(
            f"/v1/runs/{run_id}/approve",
            json={"step_id": "s1", "decision": "APPROVE", "decided_by": "manager"},
        )
        assert approve_resp.status_code == 409


def _create_queued_run(tenant_id: str) -> str:
    """Create a Run the worker has not picked up yet."""
    session = get_session()
    try:
        run = Run(tenant_id=tenant_id, title="queued", status="QUEUED")
        session.add(run)
        session.commit()
        return str(run.id)
    finally:
        session.close()


def _create_paused_run(tenant_id: str) -> str:
    """Create a Run paused in WAITING_APPROVAL with a checkpoint (task 5.2)."""
    session = get_session()
    try:
        run = Run(tenant_id=tenant_id, title="paused", status="WAITING_APPROVAL")
        session.add(run)
        session.commit()
        run_id = str(run.id)
    finally:
        session.close()

    session = get_session()
    try:
        CheckpointSaver(session).save(
            "request_approval",
            AgentState(
                run_id=run_id,
                tenant_id=tenant_id,
                user_id="u1",
                query="创建订单",
                status=AgentStatus.WAITING_APPROVAL,
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
    return run_id
