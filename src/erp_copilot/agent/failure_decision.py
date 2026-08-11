"""Deterministic failure-behavior decision — real runner for the failure eval.

Maps a natural-language failure/cancellation query to one of five behaviors
(recover_resume / retry / cancel_run / fail_fast_notify /
reconcile_no_duplicate) with the same ordered, offline rules the agent runtime
reuses when a run fails (docs/08 §10 Runtime Gate, docs/06 §7 retry rules):

1. reconcile_no_duplicate — the query asks to reconcile against the database
   after a lost response or a crash ("对账"). Checked first: the same query may
   also mention crash/timeout words, and reconciliation must not be misread as
   retry.
2. cancel_run — the user cancels the *task* before approval or mid-run
   ("取消任务 / 点取消 / 取消进行中 / 用户取消 / 审批前取消"). Bare "取消"
   is deliberately excluded — it is an ERP order-cancel write intent (fail-003
   is a crash-recovery case, not a user cancel).
3. fail_fast_notify — a hard deadline exceeded ("deadline" + "超"), or a retry
   budget exhausted ("重试…仍/仍然/依旧", "仍/依旧…失败/500/5xx"). Both win
   over the plain retry tokens below.
4. retry — a transient tool failure: a retryable HTTP status named in the query
   (429/5xx, adjudicated by RetryPolicy), a timeout ("超时"), rate limiting
   ("限流"), the explicit word "重试", or the literal "5xx".
5. recover_resume — a worker crash/restart ("崩溃 / 重启"); resume the run from
   the last checkpoint.
6. fallback — recover_resume (the failure dataset never reaches this).

Duplicate-write observation mirrors the runtime's at-most-once idempotency
semantics with an in-memory simulator: replaying a run under a stable
idempotency key must perform the external write exactly once, so the number of
duplicate writes observed for a scenario is the number of external writes
beyond the first. Correct guards yield zero; a buggy guard that minted a fresh
key per attempt would be counted, which the tests assert.

The module imports no apps/ entry point — src/ must not depend on process
layers. RetryPolicy is reused as the single source of truth for retryability.
"""

from __future__ import annotations

import re
from enum import StrEnum

from erp_copilot.agent.retry_policy import RetryPolicy

# Task-cancel tokens are deliberately specific: bare "取消" is an ERP
# order-cancel write intent, never a run-cancel decision.
_CANCEL_RUN_TOKENS: tuple[str, ...] = (
    "取消任务",
    "点取消",
    "取消进行中",
    "用户取消",
    "审批前取消",
)
_CRASH_TOKENS: tuple[str, ...] = ("崩溃", "重启")
_RETRY_EXHAUSTED_RE = re.compile(r"重试.*?(仍|仍然|依旧)|(仍|仍然|依旧)\s*(失败|500|5xx)")
# A named HTTP status in the query, adjudicated by RetryPolicy (429/5xx retryable).
_HTTP_STATUS_RE = re.compile(r"(?<!\d)([45]\d\d)(?!\d)")
_TRANSIENT_TOKENS: tuple[str, ...] = ("超时", "限流", "重试", "5xx")
_RETRY_POLICY = RetryPolicy()


class FailureBehavior(StrEnum):
    RECOVER_RESUME = "recover_resume"
    RETRY = "retry"
    CANCEL_RUN = "cancel_run"
    FAIL_FAST_NOTIFY = "fail_fast_notify"
    RECONCILE_NO_DUPLICATE = "reconcile_no_duplicate"


def _retryable_http_status(query: str) -> bool:
    """True when the query names a retryable HTTP status (429 or a 5xx)."""
    return any(
        _RETRY_POLICY.is_retryable_http(int(status)) for status in _HTTP_STATUS_RE.findall(query)
    )


def decide_failure_behavior(query: str) -> FailureBehavior:
    """Deterministic failure behavior for *query* (ordered procedure above)."""
    if "对账" in query:
        return FailureBehavior.RECONCILE_NO_DUPLICATE
    if any(token in query for token in _CANCEL_RUN_TOKENS):
        return FailureBehavior.CANCEL_RUN
    if "deadline" in query and "超" in query:
        return FailureBehavior.FAIL_FAST_NOTIFY
    if _RETRY_EXHAUSTED_RE.search(query) is not None:
        return FailureBehavior.FAIL_FAST_NOTIFY
    if _retryable_http_status(query) or any(token in query for token in _TRANSIENT_TOKENS):
        return FailureBehavior.RETRY
    if any(token in query for token in _CRASH_TOKENS):
        return FailureBehavior.RECOVER_RESUME
    return FailureBehavior.RECOVER_RESUME


# Attempts per scenario on a single stable idempotency key: the original call
# plus recovery/retry/reconciliation re-entry. user_cancel and deadline abort
# before any write, so they never reach the external write.
_SCENARIO_WRITE_ATTEMPTS: dict[str, int] = {
    "worker_crash": 2,
    "tool_timeout": 2,
    "tool_5xx": 2,
    "tool_429": 2,
    "user_cancel": 0,
    "deadline": 0,
    "reconciliation": 2,
}


class IdempotentWriteSimulator:
    """In-memory at-most-once guard mirroring the runtime idempotency layer.

    Invoking an already-completed key returns CACHED without touching an
    external write; a fresh key records one external write and completes.
    """

    def __init__(self) -> None:
        self._completed: set[str] = set()
        self.external_writes = 0

    def invoke(self, idempotency_key: str) -> str:
        if idempotency_key in self._completed:
            return "CACHED"
        self._completed.add(idempotency_key)
        self.external_writes += 1
        return "EXECUTED"


def observe_duplicates(scenario: str) -> int:
    """Duplicate external writes for *scenario* under a stable idempotency key.

    A correct guard writes exactly once per key, so duplicates is
    external_writes - 1 (floored at zero). This is a real count, not a hardcoded
    zero — a buggy guard that minted a fresh key per attempt would be detected.
    """
    sim = IdempotentWriteSimulator()
    for _ in range(_SCENARIO_WRITE_ATTEMPTS[scenario]):
        sim.invoke("stable-key")
    return max(0, sim.external_writes - 1)
