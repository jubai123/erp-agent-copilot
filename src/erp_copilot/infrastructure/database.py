"""SQLAlchemy engine, session factory, and declarative base."""

from __future__ import annotations

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from erp_copilot.infrastructure.config import Settings

_session_factory: sessionmaker[Session] | None = None
_engine: Engine | None = None


class Base(DeclarativeBase):
    """Base class for all ORM models. Every model inherits from this."""


def init_db(settings: Settings) -> Engine:
    """Create engine and session factory from application settings.

    Must be called once at application startup.
    """
    global _engine, _session_factory

    url = settings.database_url.get_secret_value()
    _engine = create_engine(url, echo=settings.debug)
    _session_factory = sessionmaker(bind=_engine)

    return _engine


def get_session() -> Session:
    """Return a new database session.

    Raises RuntimeError if :func:`init_db` has not been called yet.
    """
    if _session_factory is None:
        raise RuntimeError("Database not initialized -- call init_db() first")
    return _session_factory()


def get_engine() -> Engine:
    """Return the current SQLAlchemy engine.

    Raises RuntimeError if :func:`init_db` has not been called yet.
    """
    if _engine is None:
        raise RuntimeError("Database not initialized -- call init_db() first")
    return _engine
