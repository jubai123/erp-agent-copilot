"""Shared fixtures for integration tests."""

from __future__ import annotations

from collections.abc import Generator

import pytest
from sqlalchemy import text

from erp_copilot.infrastructure.config import Settings
from erp_copilot.infrastructure.database import Base, get_engine, get_session, init_db


@pytest.fixture(scope="session")
def _init_db(ensure_test_database: str) -> None:
    """Initialize the database once per test session."""
    import erp_copilot.domain.entities  # noqa: F401  ensure models are registered

    settings = Settings(
        database_url=ensure_test_database,
        llm_api_key="sk-test",
    )
    init_db(settings)
    Base.metadata.create_all(get_engine())

    from apps.worker.celery_app import celery_app

    celery_app.conf.task_always_eager = True
    celery_app.conf.task_eager_propagates = True


@pytest.fixture(autouse=True)
def _clean_tables(_init_db: None) -> None:
    """Clean all tables between tests to ensure isolation."""
    session = get_session()
    try:
        for table in reversed(Base.metadata.sorted_tables):
            session.execute(text(f"DELETE FROM {table.name} CASCADE"))
        session.commit()
    finally:
        session.close()


@pytest.fixture(autouse=True)
def _reset_scenario() -> Generator[None, None, None]:
    """Reset the ERP simulator scenario to happy_path after each test.

    Scenario switching mutates the process-global simulator state, so a test
    that leaves a non-happy_path scenario behind would leak it into the next
    test. Mirrors tests/e2e/conftest.py.
    """
    yield
    import apps.erp_simulator.scenarios as _scenarios

    _scenarios._current_scenario = "happy_path"
