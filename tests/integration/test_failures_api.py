"""Integration tests for the human-intervention failure queue API (task 5.9).

GET /v1/failures lists a tenant's queued failures — FAILED runs with a recorded
reason, most recently failed first. POST /v1/failures/{run_id}/resolve settles
one ambiguous write per the operator's decision, requeues the run, and
dispatches the worker so it resumes from its checkpoint. With Celery eager
(tests/integration/conftest.py) the dispatch runs synchronously, so a
confirmed-applied resolve drives the run through recovery + idempotent replay to
COMPLETED — the write is never re-executed.

Tests run against the dedicated test database (tests/conftest.py). The failed
run and its mid-execution checkpoint are seeded directly: the checkpoint mirrors
what the worker leaves when a write crashes between begin() and complete() —
plan present, s3 already approved, nothing executed yet, the write record
PENDING.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from apps.erp_simulator.data.orders import get_by_idempotency_key
from erp_copilot.agent.nodes.classify_intent import classify_intent
from erp_copilot.agent.planner import build_deterministic_plan_node
from erp_copilot.agent.state import (
    AgentState,
    AgentStatus,
    ApprovalRequest,
    ApprovalStatus,
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
from erp_copilot.infrastructure.database import get_session
from erp_copilot.memory.checkpoint import CheckpointSaver

# The same WRITE query the worker tests use: the deterministic planner maps it
# to a create-order DAG (s1 product read, s2 supplier read, s3 createOrder), so
# resolving s3 replays the write instead of re-invoking the tool.
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
    """Create an active user holding the scopes a resumed create-order DAG needs.

    On resume the graph re-runs policy_check against the checkpoint's user_id
    (docs/07 §8), so the acting user must exist in the role graph holding
    product:read, supplier:read and order:write — and the resolve API route
    requires order:write at the boundary — otherwise the request is 403.
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


def _seed_reconciliation_run(tenant_id: str, user_id: str | None) -> str:
    """Create a run failed for an uncertain write and return its ID.

    Seeds the three pieces the resolve flow touches: a FAILED Run carrying the
    RECOVERY_RECONCILIATION_REQUIRED detail (so it sits in the intervention
    queue), a mid-execution checkpoint (plan + s3 approved), and a PENDING
    idempotency record for the write. s3's success_condition is dropped so a
    replayed "{}" — the operator confirmed applied, but the exact payload is
    unknown — verifies cleanly on resume.
    """
    session = get_session()
    try:
        run = Run(
            tenant_id=tenant_id,
            title="对账测试",
            status="FAILED",
            failure_code="RECOVERY_RECONCILIATION_REQUIRED",
            failure_reason="写步骤 s3 执行状态不确定",
            suggested_action="人工确认外部订单状态后再继续",
            completed_at=datetime.now(UTC),
        )
        session.add(run)
        session.commit()
        run_id = str(run.id)

        plan = build_deterministic_plan_node()(
            AgentState(
                run_id=run_id,
                tenant_id=tenant_id,
                query=_WRITE_QUERY,
                intent=classify_intent(_WRITE_QUERY),
            )
        )["plan"]
        s3 = next(step for step in plan.steps if step.step_id == "s3")
        plan = plan.model_copy(
            update={
                "steps": [
                    step.model_copy(update={"success_condition": None})
                    if step.step_id == "s3"
                    else step
                    for step in plan.steps
                ]
            }
        )
        CheckpointSaver(session).save(
            "execute_ready_steps",
            AgentState(
                run_id=run_id,
                tenant_id=tenant_id,
                user_id=user_id,
                query=_WRITE_QUERY,
                status=AgentStatus.EXECUTING,
                plan=plan,
                approvals=[
                    ApprovalRequest(
                        step_id=s3.step_id,
                        tool_name=s3.tool_name,
                        description=s3.description,
                        risk_level=s3.risk_level,
                        required_scope=s3.required_scope,
                        status=ApprovalStatus.APPROVED,
                        decided_by="tester",
                    )
                ],
            ),
        )
        session.add(
            IdempotencyRecord(
                tenant_id=tenant_id,
                run_id=run_id,
                step_id="s3",
                idempotency_key=f"{run_id}:s3",
                status="PENDING",
                request_payload="{}",
            )
        )
        session.commit()
        return run_id
    finally:
        session.close()


class TestListFailures:
    """Acceptance: the queue is queryable per tenant with the diagnostics."""

    def test_lists_tenant_queue_with_diagnostics(self) -> None:
        from apps.api.main import create_app

        tenant_id = _create_tenant("Failures List", "failures-list")
        run_id = _seed_reconciliation_run(tenant_id, None)
        client = TestClient(create_app())

        response = client.get("/v1/failures", headers={"X-Tenant-ID": tenant_id})

        assert response.status_code == 200
        items = response.json()["items"]
        assert [item["run_id"] for item in items] == [run_id]
        item = items[0]
        assert item["tenant_id"] == tenant_id
        assert item["title"] == "对账测试"
        assert item["failure_code"] == "RECOVERY_RECONCILIATION_REQUIRED"
        assert item["failure_reason"]
        assert item["suggested_action"]
        assert item["failed_at"] is not None

    def test_tenant_scoped_and_excludes_settled(self) -> None:
        from apps.api.main import create_app

        tenant_a = _create_tenant("Failures A", "failures-a")
        tenant_b = _create_tenant("Failures B", "failures-b")
        failed_a = _seed_reconciliation_run(tenant_a, None)
        _seed_reconciliation_run(tenant_b, None)
        session = get_session()
        try:
            session.add(
                Run(
                    tenant_id=tenant_a,
                    title="已完成",
                    status="COMPLETED",
                )
            )
            session.add(Run(tenant_id=tenant_a, title="无原因失败", status="FAILED"))
            session.commit()
        finally:
            session.close()
        client = TestClient(create_app())

        response = client.get("/v1/failures", headers={"X-Tenant-ID": tenant_a})

        assert response.status_code == 200
        assert [item["run_id"] for item in response.json()["items"]] == [failed_a]


class TestResolve:
    """Acceptance: resolve settles the write and closes the loop."""

    def _writer(self, tenant_id: str) -> str:
        session = get_session()
        try:
            return _make_writer_user(session, tenant_id)
        finally:
            session.close()

    def test_confirmed_applied_settles_record_and_run_completes(self) -> None:
        from apps.api.main import create_app

        tenant_id = _create_tenant("Resolve Applied", "resolve-applied")
        user_id = self._writer(tenant_id)
        run_id = _seed_reconciliation_run(tenant_id, user_id)
        client = TestClient(create_app())

        response = client.post(
            f"/v1/failures/{run_id}/resolve",
            json={
                "step_id": "s3",
                "decision": "confirmed_applied",
                "decided_by": "operator@example.com",
                "reason": "已与 ERP 管理员确认订单已创建",
            },
            headers={"X-Tenant-ID": tenant_id, "X-User-ID": user_id},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["run_id"] == run_id
        assert data["step_id"] == "s3"
        assert data["decision"] == "confirmed_applied"
        assert data["decided_by"] == "operator@example.com"
        assert data["status"] == "QUEUED"

        session = get_session()
        try:
            record = (
                session.query(IdempotencyRecord).filter_by(idempotency_key=f"{run_id}:s3").one()
            )
            assert record.status == "COMPLETED"
            assert record.result_payload == "{}"

            event = (
                session.query(RunEvent).filter_by(run_id=run_id, event_type="RUN_RESOLVED").one()
            )
            payload = json.loads(event.payload)
            assert payload["step_id"] == "s3"
            assert payload["decision"] == "confirmed_applied"
            assert payload["decided_by"] == "operator@example.com"

            # The eager worker dispatch resumed the run from the checkpoint and
            # completed it — the confirmed-applied write was replayed, not
            # re-executed, so no order was created in the simulator.
            run = session.query(Run).filter_by(id=run_id).one()
            assert run.status == "COMPLETED"
            assert run.failure_code is None
            assert run.failure_reason is None
            assert get_by_idempotency_key(f"{run_id}:s3") is None
        finally:
            session.close()

    def test_confirmed_not_applied_marks_record_failed(self) -> None:
        from apps.api.main import create_app

        tenant_id = _create_tenant("Resolve NotApplied", "resolve-notapplied")
        user_id = self._writer(tenant_id)
        run_id = _seed_reconciliation_run(tenant_id, user_id)
        client = TestClient(create_app())

        response = client.post(
            f"/v1/failures/{run_id}/resolve",
            json={
                "step_id": "s3",
                "decision": "confirmed_not_applied",
                "decided_by": "operator@example.com",
            },
            headers={"X-Tenant-ID": tenant_id, "X-User-ID": user_id},
        )

        assert response.status_code == 200
        assert response.json()["decision"] == "confirmed_not_applied"
        session = get_session()
        try:
            # The eager worker dispatch resumed the run, and a confirmed-not-
            # applied record means the write may be safely retried — the
            # resumed graph re-executed createOrder and completed the run.
            run = session.query(Run).filter_by(id=run_id).one()
            assert run.status == "COMPLETED"
            assert run.failure_code is None
            record = (
                session.query(IdempotencyRecord).filter_by(idempotency_key=f"{run_id}:s3").one()
            )
            assert record.status == "COMPLETED"
            assert get_by_idempotency_key(f"{run_id}:s3") is not None
        finally:
            session.close()

    def test_resolve_requires_write_scope(self) -> None:
        from apps.api.main import create_app

        tenant_id = _create_tenant("Resolve NoScope", "resolve-noscope")
        run_id = _seed_reconciliation_run(tenant_id, None)
        client = TestClient(create_app())

        response = client.post(
            f"/v1/failures/{run_id}/resolve",
            json={"step_id": "s3", "decision": "confirmed_applied", "decided_by": "op"},
            headers={"X-Tenant-ID": tenant_id},  # no X-User-ID -> no scopes
        )

        assert response.status_code == 403

    def test_resolve_cross_tenant_returns_403(self) -> None:
        from apps.api.main import create_app

        tenant_a = _create_tenant("Resolve A", "resolve-a")
        tenant_b = _create_tenant("Resolve B", "resolve-b")
        run_id = _seed_reconciliation_run(tenant_a, None)
        user_b = self._writer(tenant_b)
        client = TestClient(create_app())

        response = client.post(
            f"/v1/failures/{run_id}/resolve",
            json={"step_id": "s3", "decision": "confirmed_applied", "decided_by": "op"},
            headers={"X-Tenant-ID": tenant_b, "X-User-ID": user_b},
        )

        assert response.status_code == 403

    def test_resolve_unknown_run_returns_404(self) -> None:
        from apps.api.main import create_app

        tenant_id = _create_tenant("Resolve Ghost", "resolve-ghost")
        user_id = self._writer(tenant_id)
        client = TestClient(create_app())

        response = client.post(
            "/v1/failures/ghost/resolve",
            json={"step_id": "s3", "decision": "confirmed_applied", "decided_by": "op"},
            headers={"X-Tenant-ID": tenant_id, "X-User-ID": user_id},
        )

        assert response.status_code == 404

    def test_resolve_run_not_in_queue_returns_409(self) -> None:
        from apps.api.main import create_app

        tenant_id = _create_tenant("Resolve Settled", "resolve-settled")
        user_id = self._writer(tenant_id)
        session = get_session()
        try:
            run = Run(tenant_id=tenant_id, title="已完成", status="COMPLETED")
            session.add(run)
            session.commit()
            run_id = str(run.id)
        finally:
            session.close()
        client = TestClient(create_app())

        response = client.post(
            f"/v1/failures/{run_id}/resolve",
            json={"step_id": "s3", "decision": "confirmed_applied", "decided_by": "op"},
            headers={"X-Tenant-ID": tenant_id, "X-User-ID": user_id},
        )

        assert response.status_code == 409
