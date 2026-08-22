"""Integration tests for the P2 failure-regression export (DB half).

collect_failed_runs queries the real test database for FAILED runs with a
recorded reason inside the harvest window — the same queue definition
FailureQueue.list_needing_intervention uses, but spanning all tenants (or one
tenant when requested). recover_query reads the natural-language query back
from the run's latest checkpoint (AgentState.query), because the Run row
itself stores no query; title is the fallback. Runs and checkpoints are seeded
directly, mirroring what the worker leaves behind when a run fails.

Tests run against the dedicated test database (tests/conftest.py).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from erp_copilot.agent.state import AgentState
from erp_copilot.domain.entities import Run, Tenant
from erp_copilot.infrastructure.database import get_session
from erp_copilot.memory.checkpoint import CheckpointSaver
from evals.scripts.export_failure_regression import (
    collect_failed_runs,
    main,
    recover_query,
)

_NOW = datetime(2026, 8, 22, 12, 0, 0, tzinfo=UTC)


def _add_tenant(name: str, slug: str) -> str:
    session = get_session()
    try:
        tenant = Tenant(name=name, slug=slug)
        session.add(tenant)
        session.commit()
        session.refresh(tenant)
        return tenant.id
    finally:
        session.close()


def _add_run(
    *,
    run_id: str,
    tenant_id: str,
    completed_at: datetime,
    title: str = "",
    status: str = "FAILED",
    failure_code: str | None = "TIMEOUT",
    failure_reason: str | None = "request timed out",
) -> Run:
    session = get_session()
    try:
        run = Run(
            id=run_id,
            tenant_id=tenant_id,
            title=title,
            status=status,
            failure_code=failure_code,
            failure_reason=failure_reason,
            suggested_action="检查后重试",
            completed_at=completed_at,
        )
        session.add(run)
        session.commit()
        return run
    finally:
        session.close()


def _seed_checkpoint(*, run_id: str, tenant_id: str, query: str) -> None:
    session = get_session()
    try:
        CheckpointSaver(session).save(
            "planning", AgentState(run_id=run_id, tenant_id=tenant_id, query=query)
        )
    finally:
        session.close()


class TestCollectFailedRuns:
    def test_filters_to_failed_runs_in_window(self) -> None:
        tenant_id = _add_tenant("F1", "f1")
        _add_run(run_id="r-in", tenant_id=tenant_id, completed_at=_NOW)
        _add_run(run_id="r-old", tenant_id=tenant_id, completed_at=_NOW - timedelta(days=30))
        _add_run(
            run_id="r-completed",
            tenant_id=tenant_id,
            completed_at=_NOW,
            status="COMPLETED",
        )
        _add_run(
            run_id="r-no-reason",
            tenant_id=tenant_id,
            completed_at=_NOW,
            failure_code=None,
            failure_reason=None,
        )

        runs = collect_failed_runs(get_session(), since=_NOW - timedelta(days=7))
        assert [r.id for r in runs] == ["r-in"]

    def test_orders_most_recent_first(self) -> None:
        tenant_id = _add_tenant("F2", "f2")
        _add_run(run_id="r-older", tenant_id=tenant_id, completed_at=_NOW - timedelta(days=1))
        _add_run(run_id="r-newer", tenant_id=tenant_id, completed_at=_NOW)

        runs = collect_failed_runs(get_session(), since=_NOW - timedelta(days=7))
        assert [r.id for r in runs] == ["r-newer", "r-older"]

    def test_limit_caps_result_count(self) -> None:
        tenant_id = _add_tenant("F3", "f3")
        _add_run(run_id="r-a", tenant_id=tenant_id, completed_at=_NOW)
        _add_run(run_id="r-b", tenant_id=tenant_id, completed_at=_NOW - timedelta(hours=1))

        runs = collect_failed_runs(get_session(), since=_NOW - timedelta(days=7), limit=1)
        assert [r.id for r in runs] == ["r-a"]

    def test_tenant_filter_scopes_to_one_tenant(self) -> None:
        tenant_a = _add_tenant("F4", "f4")
        tenant_b = _add_tenant("F5", "f5")
        _add_run(run_id="r-a", tenant_id=tenant_a, completed_at=_NOW)
        _add_run(run_id="r-b", tenant_id=tenant_b, completed_at=_NOW)

        runs = collect_failed_runs(
            get_session(), since=_NOW - timedelta(days=7), tenant_id=tenant_b
        )
        assert [r.id for r in runs] == ["r-b"]


class TestRecoverQuery:
    def test_reads_query_from_latest_checkpoint(self) -> None:
        tenant_id = _add_tenant("F6", "f6")
        _add_run(run_id="r-cp", tenant_id=tenant_id, completed_at=_NOW)
        _seed_checkpoint(
            run_id="r-cp", tenant_id=tenant_id, query="下单 10 KG 苹果到上海"
        )

        run = _fetch_run("r-cp")
        assert recover_query(get_session(), run) == "下单 10 KG 苹果到上海"

    def test_falls_back_to_title_without_checkpoint(self) -> None:
        tenant_id = _add_tenant("F7", "f7")
        _add_run(run_id="r-title", tenant_id=tenant_id, completed_at=_NOW, title="查苹果库存")

        run = _fetch_run("r-title")
        assert recover_query(get_session(), run) == "查苹果库存"

    def test_returns_none_when_no_checkpoint_and_empty_title(self) -> None:
        tenant_id = _add_tenant("F8", "f8")
        _add_run(run_id="r-blank", tenant_id=tenant_id, completed_at=_NOW, title="")

        run = _fetch_run("r-blank")
        assert recover_query(get_session(), run) is None


def _fetch_run(run_id: str) -> Run:
    session = get_session()
    try:
        return session.query(Run).filter_by(id=run_id).one()
    finally:
        session.close()


class TestMainPipeline:
    def test_writes_regression_dataset_for_recoverable_runs(self, tmp_path) -> None:
        tenant_id = _add_tenant("F9", "f9")
        # completed_at relative to now so the script's own --days window (built
        # from datetime.now) reliably includes it regardless of wall-clock time.
        _add_run(
            run_id="r-e2e",
            tenant_id=tenant_id,
            completed_at=datetime.now(UTC) - timedelta(days=1),
            failure_code="TIMEOUT",
            failure_reason="request timed out",
        )
        _seed_checkpoint(run_id="r-e2e", tenant_id=tenant_id, query="下单 10 KG 苹果到上海")

        out = tmp_path / "regression.json"
        assert main(["--days", "7", "--output", str(out)]) == 0

        dataset = json.loads(out.read_text(encoding="utf-8"))
        assert len(dataset["cases"]) == 1
        case = dataset["cases"][0]
        assert case["query"] == "下单 10 KG 苹果到上海"
        assert case["scenario"] == "tool_timeout"
        assert case["expected_behavior"] == "retry"

    def test_returns_1_and_writes_nothing_when_no_recoverable_query(
        self, tmp_path
    ) -> None:
        tenant_id = _add_tenant("F10", "f10")
        _add_run(
            run_id="r-blank-e2e",
            tenant_id=tenant_id,
            completed_at=datetime.now(UTC) - timedelta(days=1),
            title="",
        )

        out = tmp_path / "regression.json"
        assert main(["--days", "7", "--output", str(out)]) == 1
        assert not out.exists()
