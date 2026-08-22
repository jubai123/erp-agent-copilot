"""Redis-backed sliding-window rate limiter — 缺口三（限流）.

A sliding-window counter for upstream tool calls (MCP / cloud ERP HTTP),
shared across worker processes via Redis so the aggregate call rate against a
shared upstream stays within budget. When the number of calls in the trailing
``window_s`` seconds reaches ``limit``, the limiter rejects the call: the
executor wraps it in a fail-fast gate that returns RATE_LIMITED (non-retryable,
so the outer retry loop stops immediately) instead of hammering the upstream.

Sliding window via a sorted set — one key, no Lua, the same single-key
primitives discipline as RedisCircuitBreaker. Each admitted call is ZADDed with
its epoch timestamp as the score; a call first prunes entries older than the
window (ZREMRANGEBYSCORE) and counts what remains (ZCARD). Over-limit calls are
rejected BEFORE being added, so a throttled burst does not consume budget and
the limiter recovers once admitted calls age out — no sticky lockout. The
prune/count/admit steps are not atomic, so concurrent processes can slightly
over-admit at a boundary; acceptable for a protective backstop.

Fail-open: every Redis operation is wrapped so a Redis outage never blocks the
executor — ``allow`` returns True (let the call through). The limiter is a
backstop against our own over-frequency and must not itself become a new
single point of failure.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

RATE_LIMITED_CODE = "RATE_LIMITED"


class RedisRateLimiter:
    """A sliding-window rate limiter whose window lives in Redis (shared across
    workers).

    The client is any object exposing the redis-py command surface used here —
    ``zremrangebyscore``, ``zcard``, ``zadd`` — typed as ``Any`` so the module
    stays decoupled from redis-py and unit tests inject a hand-written fake.
    *now* is injected (default ``time.time``) so tests advance a fake clock.
    """

    def __init__(
        self,
        client: Any,
        name: str,
        *,
        limit: int,
        window_s: float = 60.0,
        now: Callable[[], float] | None = None,
    ) -> None:
        self._client = client
        self.name = name
        self._limit = limit
        self._window_s = window_s
        self._now = now if now is not None else time.time
        self._window_key = f"erp:rl:{name}:window"

    def allow(self) -> bool:
        """True when a call is within budget (the executor should let it through).

        Prunes entries older than the window, counts what remains, and admits
        only when under the limit. Rejected calls are not recorded, so a burst
        of rejections never keeps the limiter tripped beyond the admitted
        calls' window. Scores are epoch timestamps (always positive), so ``0``
        is a safe numeric lower bound for the prune.
        """
        now = self._now()
        cutoff = now - self._window_s
        member = f"{now}-{uuid.uuid4().hex}"
        try:
            self._client.zremrangebyscore(self._window_key, 0, cutoff)
            if self._client.zcard(self._window_key) >= self._limit:
                return False
            self._client.zadd(self._window_key, {member: now})
            return True
        except Exception as exc:
            logger.warning(
                "rate limiter %s: Redis unreachable, failing open: %s", self.name, exc
            )
            return True
