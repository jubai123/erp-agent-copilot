"""Runtime DB merge tests for the vocabulary loader — Commit 8.

get_catalog() merges the manifest seed with approved vocabulary_terms rows
only when VOCABULARY_LLM_UPDATES_ENABLED is on; with the flag off the DB is
never consulted (manifest is the sole source, routing stays pure/offline).
With the flag on the module cache also rebuilds on a TTL, and _db_terms()
degrades to [] when the engine is missing or the migration hasn't run.
"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import ProgrammingError

from erp_copilot.vocabulary import loader
from erp_copilot.vocabulary.loader import VocabularyTermLike, get_catalog


@pytest.fixture(autouse=True)
def _reset_vocab_cache():
    """The module cache is process-global; every test leaves it clean."""
    yield
    loader.invalidate()


def test_flag_off_uses_manifest_seed_only(monkeypatch):
    # Even if the DB held approved terms, the flag-off catalog must ignore them.
    monkeypatch.setattr(
        loader,
        "_db_terms",
        lambda: [VocabularyTermLike("region", "苏州")],
    )
    assert "苏州" not in get_catalog().regions


def test_flag_off_never_queries_db(monkeypatch):
    def _explode():
        raise AssertionError("_db_terms must not run when the flag is off")

    monkeypatch.setattr(loader, "_db_terms", _explode)
    get_catalog()  # must not raise


def test_flag_on_merges_db_terms(monkeypatch):
    monkeypatch.setenv("VOCABULARY_LLM_UPDATES_ENABLED", "true")
    monkeypatch.setattr(
        loader,
        "_db_terms",
        lambda: [VocabularyTermLike("product", "榴莲")],
    )
    assert "榴莲" in get_catalog().products


def test_ttl_expiry_rebuilds_with_new_db_terms(monkeypatch):
    monkeypatch.setenv("VOCABULARY_LLM_UPDATES_ENABLED", "true")
    monkeypatch.setattr(
        loader,
        "_db_terms",
        lambda: [VocabularyTermLike("product", "榴莲")],
    )
    assert "榴莲" in get_catalog().products

    # Cache is warm: a changed DB is not visible until the TTL elapses.
    monkeypatch.setattr(
        loader,
        "_db_terms",
        lambda: [VocabularyTermLike("product", "蓝莓")],
    )
    assert "蓝莓" not in get_catalog().products
    assert "榴莲" in get_catalog().products

    # Force the TTL window to expire -> the rebuild picks up the new terms.
    loader._CACHE_AT -= 1000
    assert "蓝莓" in get_catalog().products
    assert "榴莲" not in get_catalog().products


def test_db_terms_degrades_to_empty_on_missing_engine(monkeypatch):
    import erp_copilot.infrastructure.database as database

    def _no_engine():
        raise RuntimeError("Database not initialized")

    monkeypatch.setattr(database, "get_session", _no_engine)
    assert loader._db_terms() == []


def test_db_terms_degrades_to_empty_on_unrun_migration(monkeypatch):
    import erp_copilot.infrastructure.database as database

    def _unrun_migration():
        raise ProgrammingError("SELECT ...", {}, RuntimeError("relation does not exist"))

    monkeypatch.setattr(database, "get_session", _unrun_migration)
    assert loader._db_terms() == []
