"""Integration tests for Prometheus run-lifecycle wiring (task 6.4 gap).

The collectors (metrics.py) exist but were never wired into the run path.
These tests assert the wiring at the real choke points: create_run counts a
created run, persist_run counts a completed/failed run, and the worker graph
observes plan/execute/verify phase latency. Counters live on the module
singleton and accumulate across tests in one process, so every assertion is a
delta read before/after the action instead of an absolute value.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from erp_copilot.domain.entities import (
    Role,
    RoleScope,
    Tenant,
    User,
    UserRole,
)
from erp_copilot.infrastructure.database import get_session
from erp_copilot.observability.metrics import METRICS, generate_latest


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


def _make_user(tenant_id: str, scopes: list[tuple[str, str]]) -> str:
    """Create an active user in *tenant_id* with *scopes*; return its id.

    DB-driven RBAC (docs/06 §4) resolves the acting user's scopes from the role
    graph, so a test that expects real tool execution must provision a user
    holding the plan's required scopes — otherwise every scoped step is
    policy-denied and the run completes as a silent no-op.
    """
    session = get_session()
    try:
        role = Role(tenant_id=tenant_id, name="tester")
        session.add(role)
        session.flush()
        for resource, action in scopes:
            session.add(RoleScope(role_id=role.id, resource=resource, action=action))
        user = User(
            tenant_id=tenant_id,
            email=f"user-{role.id}@example.com",
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


def _counter(name: str) -> float:
    """Read a cumulative counter value from the shared registry."""
    for line in generate_latest(METRICS).splitlines():
        if line.startswith(f"{name} "):
            return float(line.split()[-1])
    return 0.0


def _phase_count(phase: str) -> float:
    """Read the _count sample of the phase histogram for one phase label."""
    prefix = "erp_phase_latency_seconds_count"
    for line in generate_latest(METRICS).splitlines():
        if line.startswith(prefix) and f'phase="{phase}"' in line:
            return float(line.split()[-1])
    return 0.0


class TestRunLifecycleMetrics:
    def test_create_read_run_increments_created_and_completed(self) -> None:
        from apps.api.main import create_app

        tenant_id = _create_tenant("Metrics Read", "metrics-read")
        user_id = _make_user(tenant_id, [("product", "read"), ("supplier", "read")])
        client = TestClient(create_app())
        created_before = _counter("erp_runs_created_total")
        completed_before = _counter("erp_runs_completed_total")

        response = client.post(
            "/v1/runs",
            json={"tenant_id": tenant_id, "title": "metrics-read"},
            headers={"X-Tenant-ID": tenant_id, "X-User-ID": user_id},
        )

        assert response.status_code == 202
        assert response.json()["status"] == "COMPLETED"
        assert _counter("erp_runs_created_total") == created_before + 1
        assert _counter("erp_runs_completed_total") == completed_before + 1

    def test_completed_run_observes_all_three_phases(self) -> None:
        from apps.api.main import create_app

        tenant_id = _create_tenant("Metrics Phases", "metrics-phases")
        user_id = _make_user(tenant_id, [("product", "read"), ("supplier", "read")])
        client = TestClient(create_app())
        plan_before = _phase_count("plan")
        execute_before = _phase_count("execute")
        verify_before = _phase_count("verify")

        response = client.post(
            "/v1/runs",
            json={"tenant_id": tenant_id, "title": "metrics-phases"},
            headers={"X-Tenant-ID": tenant_id, "X-User-ID": user_id},
        )

        assert response.status_code == 202
        assert response.json()["status"] == "COMPLETED"
        assert _phase_count("plan") >= plan_before + 1
        assert _phase_count("execute") >= execute_before + 1
        assert _phase_count("verify") >= verify_before + 1

    def test_paused_write_run_counts_created_but_not_completed(self) -> None:
        from apps.api.main import create_app

        tenant_id = _create_tenant("Metrics Pause", "metrics-pause")
        user_id = _make_user(
            tenant_id, [("product", "read"), ("supplier", "read"), ("order", "write")]
        )
        client = TestClient(create_app())
        created_before = _counter("erp_runs_created_total")
        completed_before = _counter("erp_runs_completed_total")

        response = client.post(
            "/v1/runs",
            json={
                "tenant_id": tenant_id,
                "title": "metrics-pause",
                "query": "帮我在上海下一单 1 KG 苹果",
            },
            headers={"X-Tenant-ID": tenant_id, "X-User-ID": user_id},
        )

        assert response.status_code == 202
        assert response.json()["status"] == "WAITING_APPROVAL"
        assert _counter("erp_runs_created_total") == created_before + 1
        assert _counter("erp_runs_completed_total") == completed_before

    def test_failed_run_increments_failed_counter(self) -> None:
        from apps.api.main import create_app

        tenant_id = _create_tenant("Metrics Fail", "metrics-fail")
        user_id = _make_user(tenant_id, [("product", "read")])
        client = TestClient(create_app())
        failed_before = _counter("erp_runs_failed_total")

        # "查询库存" carries no product/region entity, so the funnel routes it
        # to tier2; with no LLM node the run honest-fails (ROUTED_TIER23_NO_LLM)
        # into the human queue.
        response = client.post(
            "/v1/runs",
            json={"tenant_id": tenant_id, "title": "metrics-fail", "query": "查询库存"},
            headers={"X-Tenant-ID": tenant_id, "X-User-ID": user_id},
        )

        assert response.status_code == 202
        assert response.json()["status"] == "FAILED"
        assert _counter("erp_runs_failed_total") == failed_before + 1
