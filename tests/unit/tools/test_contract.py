"""Unit tests for the tool-contract layer (tools/contract.py) — the DB authority.

``datasets/knowledge/tool_contracts.yaml`` is the static bootstrap authority:
every field a downstream consumer relies on — selection prose (LLM planner
prompt), required_params (validate_plan's MISSING_REQUIRED_ARG gate), risk /
success_condition (planner's write / dangerous / template constants) — must
agree with the authorities it replaces (planner.py constants,
candidate_filter.V6_TOOL_NAMES, graph_builder.WORKER_TOOL_SCHEMAS). These tests
pin the migration faithful, and pin the DB upsert semantics: insert-if-missing
(no overwrite, no version bump) and global tenant=NULL rows.

The DB tests use a fresh SQLite in-memory engine with just the four tables the
contract needs — the established unit-test pattern (test_rbac / test_idempotency)
— so the upsert logic is exercised without the Postgres test DB.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

# WORKER_TOOL_SCHEMAS pulls Settings() through apps.worker.executor; the
# placeholders are only a backstop — nothing connects to these URLs.
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

from apps.worker.graph_builder import WORKER_TOOL_SCHEMAS  # noqa: E402
from erp_copilot.agent.planner import (  # noqa: E402
    _SUCCESS_CONDITION_TEMPLATES,
    DANGEROUS_TOOLS,
    WRITE_TOOLS,
)
from erp_copilot.domain.entities import Parameter, Tenant, Tool, ToolVersion  # noqa: E402
from erp_copilot.domain.enums import ToolRiskLevel  # noqa: E402
from erp_copilot.tools.candidate_filter import V6_TOOL_NAMES  # noqa: E402
from erp_copilot.tools.contract import (  # noqa: E402
    ContractCache,
    dangerous_tool_names,
    load_seed_contracts,
    seed_tool_contracts,
    success_condition_templates,
    write_tool_names,
)

if TYPE_CHECKING:
    pass

_READ_SCOPES = {"product:read", "supplier:read", "order:read"}


@pytest.fixture()
def engine() -> Iterator[Engine]:
    """Fresh in-memory SQLite with only the contract tables."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Tenant.__table__.create(engine)
    Tool.__table__.create(engine)
    ToolVersion.__table__.create(engine)
    Parameter.__table__.create(engine)
    yield engine
    engine.dispose()


@pytest.fixture()
def session(engine: Engine) -> Iterator[Session]:
    with Session(engine) as db:
        yield db


class TestSeedAlignment:
    """The seed must agree with every authority it replaces (faithful migration)."""

    def test_seed_covers_entire_registry(self) -> None:
        assert set(load_seed_contracts()) == set(V6_TOOL_NAMES)

    def test_risk_derivation_matches_planner(self) -> None:
        seed = load_seed_contracts()
        assert write_tool_names(seed) == WRITE_TOOLS
        assert dangerous_tool_names(seed) == DANGEROUS_TOOLS
        assert dangerous_tool_names(seed) <= write_tool_names(seed)

    def test_success_condition_templates_match_planner(self) -> None:
        assert success_condition_templates(load_seed_contracts()) == _SUCCESS_CONDITION_TEMPLATES

    def test_required_params_match_worker_schemas(self) -> None:
        seed = load_seed_contracts()
        for name in V6_TOOL_NAMES:
            assert seed[name].required_params == WORKER_TOOL_SCHEMAS[name].required_params

    def test_scope_aligns_with_risk(self) -> None:
        for contract in load_seed_contracts().values():
            if contract.risk_level == ToolRiskLevel.READ:
                assert contract.required_scope in _READ_SCOPES
            else:
                assert contract.required_scope == "order:write"

    def test_descriptions_are_nonempty(self) -> None:
        for contract in load_seed_contracts().values():
            assert contract.description, contract.name


class TestSeedParse:
    def test_load_seed_parses_25_contracts(self) -> None:
        assert len(load_seed_contracts()) == 25

    def test_every_contract_has_risk_and_scope(self) -> None:
        for contract in load_seed_contracts().values():
            assert contract.risk_level in ToolRiskLevel
            assert contract.required_scope


class TestSeedUpsert:
    def test_seed_inserts_missing_global_rows(self, session: Session) -> None:
        inserted = seed_tool_contracts(session)
        assert inserted == 25
        tools = session.query(Tool).filter(Tool.tenant_id.is_(None)).all()
        assert len(tools) == 25
        create_order = next(t for t in tools if t.name == "createOrder")
        assert create_order.tenant_id is None
        version = create_order.versions[0]
        assert version.version == 1
        assert version.risk_level == "write"
        assert version.required_scope == "order:write"
        assert version.success_condition == "response.amount > 0"
        assert {p.name for p in version.parameters} == {
            "product_id",
            "supplier_id",
            "quantity",
            "region",
        }

    def test_seed_is_idempotent_and_never_overwrites(self, session: Session) -> None:
        assert seed_tool_contracts(session) == 25
        # An operator edit must survive a restart-seed: DB is the authority for
        # description/required_params once seeded.
        tool = session.query(Tool).filter_by(name="createOrder").one()
        tool.description = "OPERATOR EDITED"
        session.commit()

        assert seed_tool_contracts(session) == 0  # nothing missing -> nothing inserted
        session.expire_all()
        tool = session.query(Tool).filter_by(name="createOrder").one()
        assert tool.description == "OPERATOR EDITED"
        # No version bump: a second seed must not create a new ToolVersion row.
        assert len(tool.versions) == 1
        assert tool.versions[0].version == 1


class TestCache:
    def test_degrades_to_seed_without_db(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from erp_copilot.infrastructure import database as db_module

        def _no_engine() -> None:
            raise RuntimeError("no engine configured")

        monkeypatch.setattr(db_module, "get_session", _no_engine)
        cache = ContractCache()
        contract = cache.get("createOrder")
        assert contract is not None
        assert contract.description == load_seed_contracts()["createOrder"].description
        assert contract.risk_level == ToolRiskLevel.WRITE

    def test_merges_db_description_and_params_over_seed(
        self, engine: Engine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with Session(engine) as db:
            seed_tool_contracts(db)
            tool = db.query(Tool).filter_by(name="createOrder").one()
            tool.description = "DB OVERRIDE"
            tool.versions[0].parameters = [
                Parameter(name="only_param", param_type="string", required=True)
            ]
            db.commit()

        from erp_copilot.infrastructure import database as db_module

        monkeypatch.setattr(db_module, "get_session", lambda: Session(engine))
        cache = ContractCache()
        contract = cache.get("createOrder")
        assert contract is not None
        assert contract.description == "DB OVERRIDE"
        assert contract.required_params == ["only_param"]

    def test_risk_stays_seed_only_even_after_db_edit(
        self, engine: Engine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Red line #1: a DB edit must never move the risk gate (validate_plan
        and the deterministic planner derive from the seed, not the DB)."""
        with Session(engine) as db:
            seed_tool_contracts(db)
            tool = db.query(Tool).filter_by(name="removeProductById").one()
            tool.versions[0].risk_level = "read"  # a hostile / accidental edit
            db.commit()

        from erp_copilot.infrastructure import database as db_module

        monkeypatch.setattr(db_module, "get_session", lambda: Session(engine))
        cache = ContractCache()
        contract = cache.get("removeProductById")
        assert contract is not None
        assert contract.risk_level == ToolRiskLevel.DANGEROUS

    def test_get_unknown_tool_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from erp_copilot.infrastructure import database as db_module

        def _no_engine() -> None:
            raise RuntimeError("no engine configured")

        monkeypatch.setattr(db_module, "get_session", _no_engine)
        assert ContractCache().get("not-a-tool") is None
