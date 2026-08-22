"""P2 feedback half-loop: harvest FAILED runs into a regression eval dataset.

One command turns the last N days of human-intervention failures (the FAILED
runs with a recorded reason that FailureQueue.list_needing_intervention shows)
into eval cases under the failure_20.json schema, so a fixed bug can be
re-verified against the exact query that exposed it — the feedback half of the
loop the P1 metrics opened (docs/08). The Run row stores no query (it lives in
the checkpoint AgentState the worker seeds), so each run's query is recovered
from its latest checkpoint, falling back to the run title.

Reads only — never writes the database — and the output file is a local dev
artifact the engineer should review before committing, since it contains real
production queries.

Usage:
    uv run python -m evals.scripts.export_failure_regression --days 7
    uv run python -m evals.scripts.export_failure_regression \
        --days 30 --tenant-id <id> --output evals/datasets/failure_regression.json
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy.orm import Session

from erp_copilot.domain.entities import Run
from erp_copilot.infrastructure.database import get_session
from erp_copilot.memory.checkpoint import CheckpointSaver

DEFAULT_DAYS = 7
DEFAULT_OUTPUT = Path(__file__).resolve().parent.parent / "datasets" / "failure_regression.json"

# failure_code -> (scenario, expected_behavior). Keys cover the failure codes
# run_persistence can emit (docs/03 §9); anything else falls back so a new code
# still produces a regression case instead of crashing the harvest.
_FAILURE_CODE_TO_CASE: dict[str, tuple[str, str]] = {
    "RECOVERY_RECONCILIATION_REQUIRED": ("reconciliation", "reconcile_no_duplicate"),
    "RECOVERY_REQUIRES_HUMAN": ("reconciliation", "reconcile_no_duplicate"),
    "WRITE_OUTCOME_AMBIGUOUS": ("reconciliation", "reconcile_no_duplicate"),
    "RECOVERY_GIVE_UP": ("reconciliation", "reconcile_no_duplicate"),
    "TIMEOUT": ("tool_timeout", "retry"),
    "PERMANENT_TOOL_ERROR": ("tool_5xx", "retry"),
    "RETRY_BUDGET_EXHAUSTED": ("tool_5xx", "no_failure"),
    "DEADLINE_EXCEEDED": ("deadline", "fail_fast_notify"),
    "EMPTY_PLAN": ("planning", "no_failure"),
    "REPLAN_BUDGET_EXHAUSTED": ("planning", "no_failure"),
    "INSUFFICIENT_STOCK": ("business", "no_failure"),
    "PRODUCT_NOT_FOUND": ("business", "no_failure"),
}
_FALLBACK_SCENARIO = "unknown"
_FALLBACK_EXPECTED = "no_failure"


def collect_failed_runs(
    session: Session,
    *,
    since: datetime,
    tenant_id: str | None = None,
    limit: int | None = None,
) -> list[Run]:
    """Return FAILED runs with a recorded reason at/after *since*, newest first.

    Mirrors FailureQueue.list_needing_intervention's queue definition but spans
    all tenants by default (one tenant when *tenant_id* is given) so the harvest
    can build a cross-tenant regression set.
    """
    query = session.query(Run).filter(
        Run.status == "FAILED",
        Run.failure_reason.is_not(None),
        Run.completed_at >= since,
    )
    if tenant_id is not None:
        query = query.filter(Run.tenant_id == tenant_id)
    query = query.order_by(Run.completed_at.desc(), Run.id.desc())
    if limit is not None:
        query = query.limit(limit)
    return list(query.all())


def classify_failure(failure_code: str) -> tuple[str, str]:
    """Map a failure code to the (scenario, expected_behavior) regression pair."""
    return _FAILURE_CODE_TO_CASE.get(failure_code, (_FALLBACK_SCENARIO, _FALLBACK_EXPECTED))


def recover_query(session: Session, run: Run) -> str | None:
    """Return the run's natural-language query, or None if unrecoverable.

    The Run row stores no query — it lives in the checkpoint AgentState seeded
    by the worker. Load the latest checkpoint and read AgentState.query;
    fall back to the run title for runs whose checkpoint is gone (a crash
    before the graph's first save).
    """
    state = CheckpointSaver(session).load_latest(run.id, run.tenant_id)
    if state is not None and state.query:
        return state.query
    if run.title:
        return run.title
    return None


def build_case(run: Run, query: str, *, index: int) -> dict[str, object]:
    """Turn one failed run into a regression case in the failure_20 schema."""
    scenario, expected = classify_failure(run.failure_code or "")
    return {
        "case_id": f"reg-{index:03d}",
        "query": query,
        "scenario": scenario,
        "expected_behavior": expected,
        # The idempotency invariant holds on every failure path — the regression
        # must never reproduce a duplicate write.
        "expected_duplicate_writes": 0,
        "note": (
            f"生产失败 run_id={run.id}: {run.failure_reason or ''}"
            f"（建议动作：{run.suggested_action or ''}）"
        ),
    }


def render_dataset(cases: Sequence[dict[str, object]], *, days: int) -> dict[str, object]:
    """Wrap cases in the dataset envelope the harness and failure_20.json use."""
    return {
        "description": (
            f"生产 FAILED run 回归集：从最近 {days} 天人工介入队列自动导出，"
            "每条对应一次真实失败（scenario/expected_behavior 由 failure_code 映射，"
            "note 记录观测失败供复核）。"
        ),
        "version": "1.0",
        "cases": list(cases),
    }


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="把最近 N 天 FAILED run 导出为 eval 回归集（P2 反馈半环）"
    )
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS, help="回溯天数（默认 7）")
    parser.add_argument(
        "--tenant-id", default=None, help="只导出指定租户的失败（默认全部租户）"
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="最多回溯的 FAILED run 条数（query 不可恢复者会被跳过）",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="输出 JSON 路径（默认 evals/datasets/failure_regression.json）",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Harvest failed runs and write the regression dataset; 0 on success."""
    args = _parse_args(argv)
    since = datetime.now(UTC) - timedelta(days=args.days)
    session = get_session()
    try:
        runs = collect_failed_runs(
            session, since=since, tenant_id=args.tenant_id, limit=args.limit
        )
        cases: list[dict[str, object]] = []
        skipped = 0
        for run in runs:
            query = recover_query(session, run)
            if query is None:
                skipped += 1
                continue
            cases.append(build_case(run, query, index=len(cases) + 1))
    finally:
        session.close()

    if not cases:
        print(
            f"最近 {args.days} 天无 FAILED run 或 query 全部不可恢复"
            f"（跳过 {skipped} 条），不写文件"
        )
        return 1

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(render_dataset(cases, days=args.days), ensure_ascii=False, indent=2)
        + "\n",
        encoding="utf-8",
    )
    print(f"导出 {len(cases)} 条回归用例（跳过 {skipped} 条 query 不可恢复）→ {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
