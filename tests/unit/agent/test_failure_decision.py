"""Unit tests for agent/failure_decision.py — deterministic failure-behavior decision.

``decide_failure_behavior`` maps a natural-language failure/cancellation query to
one of five behaviors (recover_resume / retry / cancel_run / fail_fast_notify /
reconcile_no_duplicate) using the same ordered, offline rules the agent runtime
will reuse (docs/08 §10, docs/06 §7). ``observe_duplicates`` counts duplicate
external writes under each failure scenario through the same at-most-once
semantics the idempotency layer guarantees. The datasets/failure_20.json labels
are the ground-truth contract: every query must trigger its labeled behavior
and every scenario must observe zero duplicate writes.

No LLM, no DB, no network.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from erp_copilot.agent.failure_decision import (
    FailureBehavior,
    IdempotentWriteSimulator,
    decide_failure_behavior,
    observe_duplicates,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
FAILURE_DATASET = PROJECT_ROOT / "evals" / "datasets" / "failure_20.json"

_SCENARIOS = (
    "worker_crash",
    "tool_timeout",
    "tool_5xx",
    "tool_429",
    "user_cancel",
    "deadline",
    "reconciliation",
)


def _failure_cases() -> list[dict]:
    return json.loads(FAILURE_DATASET.read_text(encoding="utf-8"))["cases"]


@pytest.mark.parametrize("case", _failure_cases(), ids=lambda c: c["case_id"])
def test_dataset_labels_are_produced_by_the_decision_module(case: dict) -> None:
    expected = FailureBehavior(case["expected_behavior"])
    assert decide_failure_behavior(case["query"]) == expected


def test_deadline_timeout_fast_fails_where_tool_timeout_retries() -> None:
    assert (
        decide_failure_behavior("下单任务超过 30 秒 deadline 未完成")
        == FailureBehavior.FAIL_FAST_NOTIFY
    )
    assert decide_failure_behavior("createOrder 超时后重试") == FailureBehavior.RETRY


def test_retry_exhausted_fast_fails_instead_of_retrying_again() -> None:
    assert (
        decide_failure_behavior("查询订单返回 500，重试一次仍 500")
        == FailureBehavior.FAIL_FAST_NOTIFY
    )


def test_cancel_run_does_not_collide_with_order_cancel() -> None:
    assert decide_failure_behavior("用户在下单审批确认前取消任务") == FailureBehavior.CANCEL_RUN
    assert (
        decide_failure_behavior("取消订单 3f2a9c1d 途中 worker 崩溃，恢复后继续取消")
        == FailureBehavior.RECOVER_RESUME
    )


@pytest.mark.parametrize("scenario", _SCENARIOS)
def test_scenario_observes_zero_duplicate_writes(scenario: str) -> None:
    assert observe_duplicates(scenario) == 0


def test_buggy_new_key_per_attempt_duplicates_are_counted() -> None:
    sim = IdempotentWriteSimulator()
    attempts = 3
    for i in range(attempts):
        sim.invoke(f"key-{i}")  # buggy: a fresh idempotency key per attempt
    assert sim.external_writes == attempts
    assert max(0, sim.external_writes - 1) == attempts - 1
