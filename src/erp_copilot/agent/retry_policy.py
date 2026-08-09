"""Retry policy and backoff executor — task 5.7.

Implements docs/06 §7 retry rules deterministically: transient failures —
rate-limit 429, temporary 5xx, connection reset, provably-not-executed
timeouts — may be retried; permanent business errors (other 4xx) may not. The
executor drives a tool call returning a :class:`ToolResult`, sleeping an
exponential backoff between attempts (1s, 2s, 4s, ...), bounded by the step's
``max_retries`` budget and a cap on the per-attempt delay.

The executor stays synchronous and pure: ``sleep`` is injected (default
``time.sleep``) so tests drive the exact backoff sequence, and it returns the
final ``ToolResult`` instead of raising — one transient failure must not kill
the run, and the verifier already branches on ``result.error.is_retryable``.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from erp_copilot.tools.tool_result import ToolResult


@dataclass(frozen=True)
class RetryPolicy:
    """Deterministic retryability rules and exponential backoff schedule."""

    base_delay_s: float = 1.0
    max_delay_s: float = 60.0

    def is_retryable_http(self, status: int) -> bool:
        """True for rate-limit 429 and temporary 5xx; other 4xx are permanent."""
        return status == 429 or 500 <= status < 600

    def is_retryable_exception(self, exc: Exception) -> bool:
        """True for the transient OSError families (docs/06 §7).

        ConnectionError and TimeoutError are OSError subclasses, so a single
        check covers connection resets, timeouts, and bare socket errors; the
        concrete not-found/permission variants are permanent.
        """
        return isinstance(exc, OSError) and not isinstance(
            exc, (FileNotFoundError, PermissionError, NotADirectoryError)
        )

    def delay_before_attempt(self, attempt: int) -> float:
        """Backoff before retry number *attempt*: 1s, 2s, 4s, ... capped."""
        delay: float = self.base_delay_s * (2**attempt)
        return min(delay, self.max_delay_s)


class RetryExecutor:
    """Runs a tool call, backing off between transient failures."""

    def __init__(
        self,
        policy: RetryPolicy | None = None,
        *,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self._policy = policy if policy is not None else RetryPolicy()
        self._sleep = sleep if sleep is not None else time.sleep

    def execute(self, fn: Callable[[], ToolResult], *, max_retries: int) -> ToolResult:
        """Return the first successful result, or the final failed result.

        *max_retries* is the step's budget (``PlanStep.max_retries``): the tool
        runs once, then up to *max_retries* more times with backoff. A
        non-retryable failure returns immediately, never re-running.
        """
        attempt = 0
        while True:
            result = fn()
            if result.status == "SUCCEEDED":
                return result
            if attempt >= max_retries or not self._retryable(result):
                return result
            self._sleep(self._policy.delay_before_attempt(attempt))
            attempt += 1

    def _retryable(self, result: ToolResult) -> bool:
        return result.error is not None and result.error.is_retryable
