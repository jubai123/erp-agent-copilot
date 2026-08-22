"""Redis-backed circuit breaker (缺口一 — 熔断器).

A sliding-window failure counter shared across worker processes via Redis, plus
an "open" key with a cooldown TTL. When the number of *retryable* failures in
the window reaches ``failure_threshold``, the breaker trips OPEN for
``open_duration_s``: callers short-circuit with a CIRCUIT_OPEN failure instead
of waiting out a full timeout on an upstream that is already down, and the
upstream gets a cooldown window to recover.

State machine (CLOSED → OPEN → CLOSED):
- CLOSED: retryable failures increment the window; a success — or a permanent
  business failure, which proves the upstream answered correctly — clears it.
- OPEN: the ``open`` key exists, so callers fail fast and the window counter is
  cleared for a clean-slate recovery.
- cooldown expiry: the ``open`` key's TTL lapses and the first request after it
  probes the upstream again (no separate HALF_OPEN state — the cooldown expiry
  *is* the half-open probe, keeping the cross-process protocol to single-key
  exists/incr/expire/set/delete primitives that need no Lua).

Fail-open: every Redis operation is wrapped so a Redis outage never takes the
executor down — ``is_open`` returns False (allow) and ``record`` is a no-op. The
breaker is a backstop against an unhealthy *upstream* and must not itself become
a new single point of failure.
"""

from __future__ import annotations

import logging
from typing import Any

from erp_copilot.tools.tool_result import ToolResult

logger = logging.getLogger(__name__)

CIRCUIT_OPEN_CODE = "CIRCUIT_OPEN"


class RedisCircuitBreaker:
    """A circuit breaker whose state lives in Redis (shared across workers).

    The client is any object exposing the redis-py command surface used here —
    ``exists``, ``incr``, ``expire``, ``set``, ``delete`` — typed as ``Any`` so
    the module stays decoupled from redis-py and unit tests inject a
    hand-written fake.
    """

    def __init__(
        self,
        client: Any,
        name: str,
        *,
        failure_threshold: int = 5,
        open_duration_s: float = 30.0,
        window_s: float = 60.0,
    ) -> None:
        self._client = client
        self.name = name
        self._failure_threshold = failure_threshold
        self._open_duration_s = open_duration_s
        self._window_s = window_s
        self._open_key = f"erp:cb:{name}:open"
        self._failure_key = f"erp:cb:{name}:failures"

    def is_open(self) -> bool:
        """True when the breaker is OPEN (traffic should fail fast)."""
        try:
            return bool(self._client.exists(self._open_key))
        except Exception as exc:
            logger.warning(
                "circuit breaker %s: Redis unreachable, failing open: %s", self.name, exc
            )
            return False

    def record_failure(self) -> None:
        """Count one retryable failure; trip OPEN at the threshold."""
        try:
            failures = self._client.incr(self._failure_key)
            # Refresh the TTL on every failure so the window slides: a failure
            # counts only if it lands within window_s of the latest one.
            self._client.expire(self._failure_key, int(self._window_s))
            if failures >= self._failure_threshold:
                self._client.set(self._open_key, "1", ex=self._open_duration_s)
                self._client.delete(self._failure_key)
        except Exception as exc:
            logger.warning(
                "circuit breaker %s: failed to record failure (fail-open): %s", self.name, exc
            )

    def record_success(self) -> None:
        """Clear the failure window (upstream answered — healthy)."""
        try:
            self._client.delete(self._failure_key)
        except Exception as exc:
            logger.warning(
                "circuit breaker %s: failed to record success (fail-open): %s", self.name, exc
            )

    def record(self, result: ToolResult) -> None:
        """Fold one ToolResult into the breaker state.

        A permanent business failure (e.g. PRODUCT_NOT_FOUND) means the upstream
        *did* respond correctly, so it clears the window like a success — only
        retryable failures (timeouts, 5xx, transport errors) count toward the
        threshold.
        """
        if result.status == "SUCCEEDED":
            self.record_success()
        elif result.error is not None and result.error.is_retryable:
            self.record_failure()
        else:
            self.record_success()
