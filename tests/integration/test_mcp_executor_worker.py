"""E2E: the worker's LangGraph resolves tool calls over a real MCP transport.

Serves apps.mcp_gateway.demo_server (an MCP SDK 2.0 MCPServer) under uvicorn on
a free port, then drives the *full worker graph* with
``build_worker_graph(executor=build_mcp_executor(url))`` — the same wiring
apps/worker/tasks.py produces when MCP_SERVER_URL is set. The graph's
execute_ready_steps node calls the MCP executor, which initializes a real MCP
session over Streamable HTTP and executes the tools on the running server: no
mocks on the wire, no simulator executor in the loop. This retires "MCP 是展示件"
from the *worker main path* — the tool call that produces a step's data comes
from a real remote protocol, not an in-process shortcut.

The e2e also proves the SSRF gate holds on the worker's path: an executor whose
guard rejects the (reachable) server URL fails the run's first step with
EGRESS_BLOCKED before any session exists.

These tests need no database beyond the in-memory SQLite session fixture (the
graph's checkpoint saver and policy scopes), and no MCP mocks; the only moving
parts are the uvicorn thread (localhost) and the real SDK client.
"""

from __future__ import annotations

import asyncio
import os
import socket
import threading
import time
from collections.abc import Iterator

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

import httpx  # noqa: E402
import pytest  # noqa: E402
import uvicorn  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from apps.erp_simulator.data.products import PRODUCT_BY_NAME  # noqa: E402
from apps.mcp_gateway.demo_server import build_demo_app  # noqa: E402
from apps.worker.graph_builder import build_worker_graph  # noqa: E402
from apps.worker.mcp_executor import build_mcp_executor  # noqa: E402
from erp_copilot.agent.state import AgentState  # noqa: E402
from erp_copilot.domain.entities import (  # noqa: E402
    AgentCheckpoint,
    IdempotencyRecord,
    Role,
    RoleScope,
    Run,
    RunEvent,
    RunStep,
    Tenant,
    User,
    UserRole,
)
from erp_copilot.memory.checkpoint import CheckpointSaver  # noqa: E402
from erp_copilot.security.ssrf_guard import SSRFConfig, SSRFGuard  # noqa: E402


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_until_ready(base_url: str) -> None:
    """Poll the demo server's /health route until it answers 200.

    trust_env=False: the dev machine's system proxy (Clash on 127.0.0.1:7890)
    otherwise intercepts the loopback request and returns 502 — the same proxy
    the MCP client already bypasses (trust_env=False on its httpx2 client), so
    the probe must share that egress posture to reach the same target.
    """
    last_error: Exception | None = None
    with httpx.Client(trust_env=False) as client:
        for _ in range(100):
            try:
                if client.get(f"{base_url}/health", timeout=1).status_code == 200:
                    return
            except Exception as exc:  # pragma: no cover - transient until boot
                last_error = exc
            time.sleep(0.05)
    raise RuntimeError(f"demo MCP server did not become ready: {last_error!r}")


@pytest.fixture(scope="module")
def demo_mcp_url() -> Iterator[str]:
    """Serve the demo MCP server once for this module; yield its base URL."""
    port = _free_port()
    config = uvicorn.Config(build_demo_app(), host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{port}"
    try:
        _wait_until_ready(base_url)
        yield base_url
    finally:
        server.should_exit = True
        thread.join(timeout=5)


@pytest.fixture()
def session(monkeypatch: pytest.MonkeyPatch) -> Iterator[Session]:
    """In-memory SQLite engine shared by the fixture and the graph's sessions."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Tenant.__table__.create(engine)
    User.__table__.create(engine)
    Role.__table__.create(engine)
    RoleScope.__table__.create(engine)
    UserRole.__table__.create(engine)
    Run.__table__.create(engine)
    RunStep.__table__.create(engine)
    AgentCheckpoint.__table__.create(engine)
    RunEvent.__table__.create(engine)
    IdempotencyRecord.__table__.create(engine)

    monkeypatch.setattr("erp_copilot.infrastructure.database.get_session", lambda: Session(engine))
    with Session(engine) as db:
        yield db
    engine.dispose()


def _make_tenant(session: Session) -> Tenant:
    tenant = Tenant(name="mcp-worker-test", slug="mcp-worker-test")
    session.add(tenant)
    session.commit()
    return tenant


def _make_reader_user(session: Session, tenant_id: str) -> str:
    """Create a user whose role grants the READ scopes read steps require."""
    role = Role(tenant_id=tenant_id, name="tester")
    session.add(role)
    session.flush()
    for resource, action in [("product", "read"), ("supplier", "read")]:
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


def _make_run(session: Session, tenant_id: str) -> Run:
    run = Run(tenant_id=tenant_id, title="查询库存", status="QUEUED")
    session.add(run)
    session.commit()
    return run


def _blocking_guard(port: int) -> SSRFGuard:
    """Guard that rejects the reachable demo server's URL (wrong port).

    127.0.0.1 is loopback, so it must also be in trusted_internal_hosts or the
    address check returns BLOCKED_IP (the fail-closed default for non-public
    addresses) before the port check can name BLOCKED_PORT.
    """
    return SSRFGuard(
        SSRFConfig(
            allowed_schemes=frozenset({"http"}),
            allowed_hosts=frozenset({"127.0.0.1"}),
            allowed_ports=frozenset({port + 1}),
            trusted_internal_hosts=frozenset({"127.0.0.1"}),
        )
    )


class TestWorkerGraphRealMCP:
    """Acceptance: the worker's tool calls resolve over a real MCP transport."""

    def test_read_path_resolves_via_real_mcp(self, session: Session, demo_mcp_url: str) -> None:
        tenant = _make_tenant(session)
        user_id = _make_reader_user(session, tenant.id)
        run = _make_run(session, tenant.id)
        url = f"{demo_mcp_url}/mcp"

        executor = build_mcp_executor(url)
        graph = build_worker_graph(
            session,
            CheckpointSaver(session),
            run_id=run.id,
            executor=executor,
        )

        async def run_graph() -> dict:
            # No executor.close() here: the graph runs steps in asyncio.gather
            # child tasks (execute_steps.py), so the MCP session's anyio task
            # group is entered in a child task and can only be torn down when
            # the loop ends. The production worker relies on the same
            # loop/process teardown, so the e2e mirrors it.
            return await graph.ainvoke(
                AgentState(
                    run_id=run.id,
                    tenant_id=tenant.id,
                    user_id=user_id,
                    query="查苹果库存并推荐供应商",
                ).model_dump(),
                config={"recursion_limit": 50},
            )

        final = asyncio.run(run_graph())

        # Both steps' data come from the live MCP server (the demo server's
        # in-process catalog), not the worker's simulator executor.
        assert final["status"] == "succeeded"
        s1 = final["step_results"]["s1"]
        assert s1.status == "completed"
        assert s1.data["name"] == "苹果"
        assert s1.data["stock"] == PRODUCT_BY_NAME["苹果"].quantity_in_stock
        s2 = final["step_results"]["s2"]
        assert s2.status == "completed"
        # 华东物流 (supplier_id=3) is the first AVAILABLE supplier.
        assert s2.data["supplier_id"] == 3

    def test_ssrf_block_fails_step_before_any_session(
        self, session: Session, demo_mcp_url: str
    ) -> None:
        tenant = _make_tenant(session)
        user_id = _make_reader_user(session, tenant.id)
        run = _make_run(session, tenant.id)
        url = f"{demo_mcp_url}/mcp"
        port = int(demo_mcp_url.rsplit(":", 1)[1])

        # The server is reachable, but the policy admits a different port — the
        # check must fail closed before any egress, so the step reports
        # EGRESS_BLOCKED (permanent) and the run fails.
        executor = build_mcp_executor(url, guard=_blocking_guard(port))
        graph = build_worker_graph(
            session,
            CheckpointSaver(session),
            run_id=run.id,
            executor=executor,
        )

        async def run_graph() -> dict:
            try:
                return await graph.ainvoke(
                    AgentState(
                        run_id=run.id,
                        tenant_id=tenant.id,
                        user_id=user_id,
                        query="查苹果库存并推荐供应商",
                    ).model_dump(),
                    config={"recursion_limit": 50},
                )
            finally:
                await executor.close()

        final = asyncio.run(run_graph())

        assert final["status"] == "failed"
        s1 = final["step_results"]["s1"]
        assert s1.status == "failed"
        assert s1.error_code == "EGRESS_BLOCKED"
        assert s1.is_retryable is False
