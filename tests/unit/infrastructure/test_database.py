"""Tests for database engine and session management."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from erp_copilot.infrastructure.config import Settings
from erp_copilot.infrastructure.database import (
    Base,
    get_engine,
    get_session,
    init_db,
)


class TestInitDb:
    def test_creates_engine_and_session_factory(self) -> None:
        settings = Settings(
            database_url="postgresql://u:p@localhost:5432/db",
            llm_api_key="sk-test",
        )

        engine = init_db(settings)

        assert engine is not None
        session = get_session()
        assert session is not None

    def test_engine_echo_respects_debug(self) -> None:
        prod = Settings(
            database_url="postgresql://u:p@localhost:5432/db",
            llm_api_key="sk-test",
            debug=False,
        )
        engine = init_db(prod)
        assert engine.echo is False


class TestGetSession:
    def test_raises_when_not_initialized(self) -> None:
        # Reset globals
        import erp_copilot.infrastructure.database as db

        db._session_factory = None
        db._engine = None

        with pytest.raises(RuntimeError, match="not initialized"):
            get_session()

    def test_raises_when_engine_not_initialized(self) -> None:
        import erp_copilot.infrastructure.database as db

        db._engine = None

        with pytest.raises(RuntimeError, match="not initialized"):
            get_engine()


class TestBase:
    def test_base_exists(self) -> None:
        assert Base is not None
        assert hasattr(Base, "metadata")


class TestDatabaseIntegration:
    """Integration tests against the running docker PostgreSQL."""

    def test_can_connect_and_query(self, ensure_test_database: str) -> None:
        settings = Settings(
            database_url=ensure_test_database,
            llm_api_key="sk-test",
        )
        init_db(settings)
        session = get_session()

        result = session.execute(text("SELECT 1"))
        assert result.scalar() == 1

    def test_can_create_temp_table(self, ensure_test_database: str) -> None:
        settings = Settings(
            database_url=ensure_test_database,
            llm_api_key="sk-test",
        )
        init_db(settings)
        session = get_session()

        session.execute(text("CREATE TEMPORARY TABLE _test_ping (id int)"))
        session.execute(text("INSERT INTO _test_ping VALUES (1)"))
        result = session.execute(text("SELECT id FROM _test_ping")).scalar()
        assert result == 1
        session.execute(text("DROP TABLE _test_ping"))
        session.commit()
