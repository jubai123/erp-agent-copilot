"""Shared test infrastructure — dedicated test database bootstrap.

The suite DELETEs every row it knows about between tests (``_clean_tables``),
so it must run against a dedicated database, never the dev/prod one. Before
this file existed, tests hardcoded ``erp_copilot`` (the dev DB) and wiped the
``ablation-study`` tenant in the process.

Override the target via the ``TEST_DATABASE_URL`` environment variable.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from apps.erp_simulator.data.products import PRODUCT_BY_NAME

# The simulator's in-memory stock lives on the same Product objects that back
# SEED_PRODUCTS and PRODUCT_BY_NAME, so a runtime createOrder deducts the
# "seed" itself and there is no clean object to restore from later. Captured at
# conftest load — before any test body runs — this holds the pristine values.
_SEED_STOCK: dict[str, int] = {p.name: p.quantity_in_stock for p in PRODUCT_BY_NAME.values()}

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://copilot:copilot_dev@localhost:5432/erp_copilot_test",
)


def _ensure_test_database(url: str) -> str:
    """Create the test database (and pgvector) if missing; return *url*.

    Two steps, both idempotent:

    1. Connect to the ``postgres`` maintenance database (same credentials)
       and ``CREATE DATABASE`` the target if absent. ``CREATE DATABASE``
       cannot run inside a transaction, so the admin connection uses
       AUTOCOMMIT isolation.
    2. Install the ``vector`` extension on the target database —
       ``Base.metadata`` has a ``Vector(1536)`` column and ``create_all``
       fails without the extension.
    """
    target = make_url(url)

    admin = create_engine(target.set(database="postgres"))
    try:
        with admin.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": target.database},
            ).scalar()
            if not exists:
                conn.execute(text(f'CREATE DATABASE "{target.database}"'))
    finally:
        admin.dispose()

    engine = create_engine(url)
    try:
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    finally:
        engine.dispose()

    return url


@pytest.fixture(scope="session")
def ensure_test_database() -> str:
    """Create the test DB once per session; expose its URL to _init_db fixtures."""
    return _ensure_test_database(TEST_DATABASE_URL)


@pytest.fixture(autouse=True)
def _reset_simulator_stock() -> None:
    """Restore the ERP simulator's in-memory stock before every test.

    createOrder (worker executor and simulator routes) deducts stock in place
    on the shared Product objects; a test that places an order without restoring
    stock pollutes every later absolute-stock assertion (the recovery-decision
    seed mirror, the executor get-order default quantity=20). Resetting per test
    keeps the whole suite order-independent.
    """
    for name, stock in _SEED_STOCK.items():
        PRODUCT_BY_NAME[name].quantity_in_stock = stock
