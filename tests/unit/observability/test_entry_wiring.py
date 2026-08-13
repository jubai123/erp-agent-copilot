"""Simulator and gateway observability entry-wiring tests — task 6.2/6.3.

A real ``uvicorn`` launch of the ERP simulator or the MCP gateway must
initialize JSON logging and a tracer provider on startup, mirroring the API's
lifespan wiring (session 19). Each process must report its own ``service.name``
so spans are attributable to the right deployment unit. Starlette's TestClient
does not fire lifespan events, so these tests drive each app's lifespan
function directly and assert the setup calls receive the correct service name
and Settings-derived arguments — testing the wiring, not the setup
implementation.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any
from unittest.mock import Mock

import pytest

# Settings() (constructed inside each lifespan) requires DATABASE_URL. The env
# file already provides one; this backstop only keeps the import working when
# the env file is absent (same pattern as test_celery_app.py).
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from apps.erp_simulator import simulator as simulator_mod  # noqa: E402
from apps.mcp_gateway import gateway as gateway_mod  # noqa: E402


def _enter_lifespan(lifespan_fn: Any, app: Any) -> None:
    """Run the lifespan's startup (and shutdown) against *app*."""

    async def _run() -> None:
        async with lifespan_fn(app):
            return None

    asyncio.run(_run())


def _mock_observability(module: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(module, "setup_logging", Mock())
    monkeypatch.setattr(module, "setup_tracing", Mock())
    monkeypatch.setattr(module, "build_otlp_exporter", Mock(return_value="OTLP"))


class TestSimulatorLifespan:
    def test_wires_logging_and_tracing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _mock_observability(simulator_mod, monkeypatch)

        _enter_lifespan(simulator_mod.lifespan, simulator_mod.create_simulator_app())

        simulator_mod.setup_logging.assert_called_once_with(level="INFO", service="erp-simulator")
        simulator_mod.build_otlp_exporter.assert_called_once_with("http://localhost:4317")
        simulator_mod.setup_tracing.assert_called_once_with(
            service_name="erp-simulator", exporter="OTLP"
        )


class TestGatewayLifespan:
    def test_wires_logging_and_tracing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _mock_observability(gateway_mod, monkeypatch)
        # The gateway lifespan also opens the security-events DB channel
        # (init_db); mock it so this wiring test does not mutate the global
        # session factory (it reads DATABASE_URL from the env, which here is
        # the sqlite backstop) and pollute later DB-dependent unit tests.
        monkeypatch.setattr(gateway_mod, "init_db", Mock())

        _enter_lifespan(gateway_mod.lifespan, gateway_mod.create_gateway_app())

        gateway_mod.init_db.assert_called_once()
        gateway_mod.setup_logging.assert_called_once_with(level="INFO", service="mcp-gateway")
        gateway_mod.build_otlp_exporter.assert_called_once_with("http://localhost:4317")
        gateway_mod.setup_tracing.assert_called_once_with(
            service_name="mcp-gateway", exporter="OTLP"
        )
