"""Unit tests for agent/circuit_breaker.py — 缺口一 (熔断器).

The breaker is the reliability primitive that stops the agent from hammering an
already-down upstream: a sliding window of *retryable* failures trips the
circuit OPEN for a cooldown, and callers fail fast (CIRCUIT_OPEN) instead of
waiting out a full timeout on every subsequent call. State lives in Redis so it
is shared across the prefork worker processes (each run builds its own executor,
so process-local state would reset per run and never accumulate).

Tests drive a hand-written fake (no fakeredis dependency): the breaker only uses
five redis-py commands — exists / incr / expire / set / delete — and the fake
models them with a manually-advanceable clock so TTL expiry is deterministic.
"""

from __future__ import annotations

from erp_copilot.agent.circuit_breaker import CIRCUIT_OPEN_CODE, RedisCircuitBreaker
from erp_copilot.tools.tool_result import ToolResult


class FakeRedis:
    """Minimal redis-py stand-in for the breaker's five commands.

    ``now`` is a mutable epoch the tests advance to simulate TTL expiry; every
    string value stores an optional ``expire_at`` so ``exists`` honours the
    cooldown / window TTLs without a real clock.
    """

    def __init__(self) -> None:
        self._store: dict[str, tuple[str, float | None]] = {}
        self.now: float = 0.0

    def _alive(self, key: str) -> bool:
        entry = self._store.get(key)
        if entry is None:
            return False
        _, expire_at = entry
        if expire_at is not None and self.now >= expire_at:
            del self._store[key]
            return False
        return True

    def exists(self, key: str) -> int:
        return 1 if self._alive(key) else 0

    def incr(self, key: str) -> int:
        if not self._alive(key):
            self._store[key] = ("0", None)
        value, expire_at = self._store[key]
        new_value = int(value) + 1
        self._store[key] = (str(new_value), expire_at)
        return new_value

    def expire(self, key: str, seconds: int) -> bool:
        if not self._alive(key):
            return False
        value, _ = self._store[key]
        self._store[key] = (value, self.now + seconds)
        return True

    def set(self, key: str, value: object, ex: float | None = None) -> bool:
        self._store[key] = (str(value), self.now + ex if ex is not None else None)
        return True

    def delete(self, key: str) -> int:
        if self._alive(key):
            del self._store[key]
            return 1
        return 0


class BrokenRedis:
    """Every command raises ConnectionError — models Redis being unreachable."""

    def __init__(self) -> None:
        self.calls: int = 0

    def _boom(self, *_args: object, **_kwargs: object) -> None:
        self.calls += 1
        raise ConnectionError("redis down")

    def exists(self, key: str) -> int:
        self._boom(key)
        return 0

    def incr(self, key: str) -> int:
        self._boom(key)
        return 0

    def expire(self, key: str, seconds: int) -> bool:
        self._boom(key, seconds)
        return False

    def set(self, key: str, value: object, ex: float | None = None) -> bool:
        self._boom(key, value, ex)
        return False

    def delete(self, key: str) -> int:
        self._boom(key)
        return 0


def _retryable_failure() -> ToolResult:
    return ToolResult.failure(
        tool_version_id="v1", error_code="TIMEOUT", error_message="boom", is_retryable=True
    )


def _business_failure() -> ToolResult:
    return ToolResult.failure(
        tool_version_id="v1",
        error_code="PRODUCT_NOT_FOUND",
        error_message="nope",
        is_retryable=False,
    )


def _success() -> ToolResult:
    return ToolResult.success(tool_version_id="v1", data={})


class TestRedisCircuitBreaker:
    def test_starts_closed(self) -> None:
        breaker = RedisCircuitBreaker(FakeRedis(), "erp", failure_threshold=3)
        assert breaker.is_open() is False

    def test_opens_after_threshold_retryable_failures(self) -> None:
        breaker = RedisCircuitBreaker(FakeRedis(), "erp", failure_threshold=3)
        breaker.record(_retryable_failure())
        breaker.record(_retryable_failure())
        assert breaker.is_open() is False
        breaker.record(_retryable_failure())
        assert breaker.is_open() is True

    def test_below_threshold_stays_closed(self) -> None:
        breaker = RedisCircuitBreaker(FakeRedis(), "erp", failure_threshold=5)
        for _ in range(4):
            breaker.record(_retryable_failure())
        assert breaker.is_open() is False

    def test_success_resets_failure_window(self) -> None:
        breaker = RedisCircuitBreaker(FakeRedis(), "erp", failure_threshold=3)
        breaker.record(_retryable_failure())
        breaker.record(_retryable_failure())
        breaker.record(_success())  # clears the window
        breaker.record(_retryable_failure())
        breaker.record(_retryable_failure())
        assert breaker.is_open() is False  # 2 since reset < 3
        breaker.record(_retryable_failure())
        assert breaker.is_open() is True

    def test_business_failure_does_not_count_and_resets(self) -> None:
        breaker = RedisCircuitBreaker(FakeRedis(), "erp", failure_threshold=2)
        breaker.record(_retryable_failure())
        breaker.record(_business_failure())  # upstream answered -> healthy
        breaker.record(_retryable_failure())
        assert breaker.is_open() is False  # 1 retryable since reset
        breaker.record(_retryable_failure())
        assert breaker.is_open() is True

    def test_open_key_expires_after_cooldown(self) -> None:
        redis = FakeRedis()
        breaker = RedisCircuitBreaker(
            redis, "erp", failure_threshold=1, open_duration_s=30.0
        )
        breaker.record(_retryable_failure())
        assert breaker.is_open() is True

        redis.now = 29.9
        assert breaker.is_open() is True

        redis.now = 30.0
        assert breaker.is_open() is False  # cooldown elapsed -> closed

    def test_fail_open_when_redis_unavailable(self) -> None:
        broken = BrokenRedis()
        breaker = RedisCircuitBreaker(broken, "erp", failure_threshold=1)
        assert breaker.is_open() is False  # fail-open: allow traffic
        breaker.record(_retryable_failure())  # must not raise
        breaker.record(_success())
        assert broken.calls > 0

    def test_circuit_open_code_constant(self) -> None:
        assert CIRCUIT_OPEN_CODE == "CIRCUIT_OPEN"
