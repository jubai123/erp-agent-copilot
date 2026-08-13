"""App lifespan DB-init wiring test — v1.1 wiring gap.

A real ``uvicorn`` launch must initialize the SQLAlchemy engine at startup
(``init_db``), otherwise every DB-backed endpoint (``POST /v1/runs``,
knowledge search, ...) raises ``RuntimeError: Database not initialized``.
Starlette's TestClient does not fire lifespan events (verified against the
installed version), so this test drives the app's lifespan function
directly and asserts the engine is bound to the configured database.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import Mock

import pytest
from sqlalchemy import text

from apps.api.main import create_app, lifespan
from erp_copilot.infrastructure.database import get_engine, get_session
from tests.conftest import TEST_DATABASE_URL


@pytest.fixture(autouse=True)
def _mock_observability(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the observability setup calls so lifespan wiring is testable.

    The lifespan now invokes setup_logging (mutates the root logger; conftest
    has no global reset) and setup_tracing (overwrites the module-scoped
    provider that test_tracing / test_langfuse inject). Mocking keeps these
    side effects out of the rest of the session and lets the wiring test
    assert the functions were called with Settings-derived arguments.
    """
    monkeypatch.setattr("apps.api.main.setup_logging", Mock())
    monkeypatch.setattr("apps.api.main.setup_tracing", Mock())
    monkeypatch.setattr("apps.api.main.build_otlp_exporter", Mock(return_value="OTLP"))


def _enter_lifespan(app: Any) -> None:
    """Run the lifespan's startup (and shutdown) against *app*."""

    async def _run() -> None:
        async with lifespan(app):
            return None

    asyncio.run(_run())


class TestAppLifespan:
    def test_lifespan_initializes_database(
        self, monkeypatch: pytest.MonkeyPatch, ensure_test_database: str
    ) -> None:
        monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)

        app = create_app()
        _enter_lifespan(app)

        engine = get_engine()
        assert engine.url.database == "erp_copilot_test"
        session = get_session()
        try:
            assert session.execute(text("SELECT 1")).scalar() == 1
        finally:
            session.close()

    def test_lifespan_wires_logging_and_tracing(
        self, monkeypatch: pytest.MonkeyPatch, ensure_test_database: str
    ) -> None:
        from apps.api import main

        monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)

        app = create_app()
        _enter_lifespan(app)

        main.setup_logging.assert_called_once_with(level="INFO", service="erp-agent-copilot")
        main.build_otlp_exporter.assert_called_once_with("http://localhost:4317")
        main.setup_tracing.assert_called_once_with(
            service_name="erp-agent-copilot", exporter="OTLP"
        )
