"""Integration tests for Run creation and query endpoints."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from apps.erp_simulator.data.orders import get_by_idempotency_key
from apps.erp_simulator.data.products import PRODUCT_BY_NAME
from erp_copilot.agent.state import (
    AgentState,
    AgentStatus,
    ApprovalRequest,
    Plan,
    PlanStep,
    PolicyDecision,
)
from erp_copilot.domain.entities import (
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


def _parse_sse(body: str) -> list[dict[str, str]]:
    """Parse a raw SSE body into {id, event, data} frames."""
    frames: list[dict[str, str]] = []
    for block in body.split("\n\n"):
        if not block.strip():
            continue
        frame: dict[str, str] = {}
        for line in block.split("\n"):
            if line.startswith("id: "):
                frame["id"] = line[len("id: ") :]
            elif line.startswith("event: "):
                frame["event"] = line[len("event: ") :]
            elif line.startswith("data: "):
                frame["data"] = line[len("data: ") :]
        if frame:
            frames.append(frame)
    return frames


class TestRunEvents:
    """Acceptance: GET /v1/runs/{run_id}/events streams the run's event log."""

    def test_stream_replays_lifecycle_in_order(self) -> None:
        from apps.api.main import create_app

        tenant_id = _create_tenant("Events", "events-test")
        client = TestClient(create_app())
        run_id = client.post("/v1/runs", json={"tenant_id": tenant_id, "title": "events"}).json()[
            "run_id"
        ]

        with client.stream("GET", f"/v1/runs/{run_id}/events") as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            body = "".join(response.iter_text())
        frames = _parse_sse(body)

        assert frames[0]["event"] == "RUN_CREATED"
        statuses = [
            json.loads(frame["data"])["status"]
            for frame in frames
            if frame["event"] == "RUN_STATUS"
        ]
        assert statuses[0] == "planning"
        assert statuses[-1] == "succeeded"
        assert (
            statuses.index("planning") < statuses.index("executing") < statuses.index("succeeded")
        )

    def test_reconnect_with_last_event_id_resumes_stream(self) -> None:
        from apps.api.main import create_app

        tenant_id = _create_tenant("Events Resume", "events-resume")
        client = TestClient(create_app())
        run_id = client.post("/v1/runs", json={"tenant_id": tenant_id, "title": "resume"}).json()[
            "run_id"
        ]

        with client.stream("GET", f"/v1/runs/{run_id}/events") as response:
            first = _parse_sse("".join(response.iter_text()))
        created_id = next(frame["id"] for frame in first if frame["event"] == "RUN_CREATED")

        with client.stream(
            "GET",
            f"/v1/runs/{run_id}/events",
            headers={"Last-Event-ID": created_id},
        ) as response:
            resumed = _parse_sse("".join(response.iter_text()))

        assert resumed
        assert all(frame["event"] == "RUN_STATUS" for frame in resumed)
        assert "RUN_CREATED" not in {frame["event"] for frame in resumed}

    def test_events_404_for_missing_run(self) -> None:
        from apps.api.main import create_app

        client = TestClient(create_app())
        response = client.get("/v1/runs/ghost/events")
        assert response.status_code == 404


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


def _make_writer_user(tenant_id: str) -> str:
    """Create an active writer-scoped user in *tenant_id*; return its id.

    DB-driven RBAC (docs/06 §4) resolves the acting user's scopes from the role
    graph, so a WRITE create_run only pauses for approval when the caller's
    user_id holds product:read, supplier:read and order:write.
    """
    session = get_session()
    try:
        role = Role(tenant_id=tenant_id, name="writer")
        session.add(role)
        session.flush()
        for resource, action in [
            ("product", "read"),
            ("supplier", "read"),
            ("order", "write"),
        ]:
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

    def test_create_run_with_unknown_user_returns_422(self) -> None:
        from apps.api.main import create_app

        # Scopes resolve from the role graph by user_id, so an unknown/inactive
        # user would silently run as no-scope — the boundary rejects it instead.
        tenant_id = _create_tenant("Unknown User", "unknown-user")
        client = TestClient(create_app())

        response = client.post(
            "/v1/runs",
            json={"tenant_id": tenant_id, "title": "x", "user_id": "ghost-user"},
        )

        assert response.status_code == 422

    def test_create_run_write_query_pauses_then_approve_completes(self) -> None:
        from apps.api.main import create_app

        # A WRITE query ("下一单") can only pause for approval if create_run
        # actually threads the query through to the worker — before the query
        # field existed, POST /v1/runs could only ever emit product reads and
        # the write path was unreachable from the API. Approving s3 resumes the
        # graph and the order lands exactly once.
        tenant_id = _create_tenant("Write Query", "write-query")
        user_id = _make_writer_user(tenant_id)
        product = PRODUCT_BY_NAME["苹果"]
        original_stock = product.quantity_in_stock
        client = TestClient(create_app())
        try:
            create_resp = client.post(
                "/v1/runs",
                json={
                    "tenant_id": tenant_id,
                    "title": "下单",
                    "query": "帮我在上海下一单 1 KG 苹果",
                    "user_id": user_id,
                },
            )

            assert create_resp.status_code == 202
            run_id = create_resp.json()["run_id"]
            assert create_resp.json()["status"] == "WAITING_APPROVAL"

            approve_resp = client.post(
                f"/v1/runs/{run_id}/approve",
                json={"step_id": "s3", "decision": "APPROVE", "decided_by": "manager"},
            )
            assert approve_resp.status_code == 200
            assert approve_resp.json()["run_status"] == "succeeded"

            session = get_session()
            try:
                run = session.query(Run).filter_by(id=run_id).first()
                assert run is not None
                assert run.status == "COMPLETED"
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
        finally:
            product.quantity_in_stock = original_stock


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
