"""Unit tests for the Redis-backed sliding-window rate limiter (缺口三 — 限流).

RedisRateLimiter gates upstream MCP / cloud ERP calls so a burst cannot
overwhelm the shared upstream: a call is admitted only when the number of
calls in the trailing window is below the limit, otherwise it is rejected
without consuming budget. Tests drive the sliding window with a hand-written
FakeRedis (zremrangebyscore / zcard / zadd) and an injected fake clock.
"""

from __future__ import annotations

from erp_copilot.agent.rate_limiter import RedisRateLimiter


class _FakeClock:
    def __init__(self, t: float = 1_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


class _FakeRedis:
    """Minimal redis-py sorted-set stand-in for the limiter tests.

    Only the commands RedisRateLimiter touches are modelled; the window lives
    in ``zsets`` as {member: score}, mirroring real ZADD / ZCARD /
    ZREMRANGEBYSCORE semantics (inclusive range, non-destructive count).
    """

    def __init__(self) -> None:
        self.zsets: dict[str, dict[str, float]] = {}

    def zremrangebyscore(self, key: str, min_: float, max_: float) -> int:
        z = self.zsets.setdefault(key, {})
        stale = [member for member, score in z.items() if min_ <= score <= max_]
        for member in stale:
            del z[member]
        return len(stale)

    def zcard(self, key: str) -> int:
        return len(self.zsets.get(key, {}))

    def zadd(self, key: str, mapping: dict[str, float]) -> int:
        z = self.zsets.setdefault(key, {})
        added = sum(1 for member in mapping if member not in z)
        z.update(mapping)
        return added


def _make(
    *, limit: int = 3, window_s: float = 60.0
) -> tuple[_FakeRedis, _FakeClock, RedisRateLimiter]:
    redis = _FakeRedis()
    clock = _FakeClock()
    limiter = RedisRateLimiter(
        redis, "test-upstream", limit=limit, window_s=window_s, now=clock
    )
    return redis, clock, limiter


def _window_key() -> str:
    return "erp:rl:test-upstream:window"


class TestAllowAdmission:
    def test_admits_calls_up_to_the_limit(self) -> None:
        redis, _, limiter = _make(limit=3)
        assert limiter.allow() is True
        assert limiter.allow() is True
        assert limiter.allow() is True
        assert redis.zcard(_window_key()) == 3

    def test_same_instant_calls_are_distinct_members(self) -> None:
        redis, _, limiter = _make(limit=5)
        assert limiter.allow() is True
        assert limiter.allow() is True  # identical now -> unique members -> both counted
        assert redis.zcard(_window_key()) == 2


class TestRejection:
    def test_rejects_when_window_reaches_the_limit(self) -> None:
        _, _, limiter = _make(limit=2)
        assert limiter.allow() is True
        assert limiter.allow() is True
        assert limiter.allow() is False

    def test_rejection_does_not_consume_budget(self) -> None:
        redis, _, limiter = _make(limit=2)
        limiter.allow()
        limiter.allow()
        assert limiter.allow() is False
        assert redis.zcard(_window_key()) == 2  # rejected call not recorded

    def test_rejects_when_seeded_window_is_full(self) -> None:
        redis, clock, limiter = _make(limit=2)
        redis.zadd(_window_key(), {"a": clock.t - 10, "b": clock.t - 5})
        assert limiter.allow() is False


class TestSlidingWindow:
    def test_prunes_stale_entries_before_counting(self) -> None:
        redis, clock, limiter = _make(limit=3, window_s=60.0)
        limiter.allow()  # t = 1_000_000
        clock.advance(59)
        limiter.allow()  # t = 1_000_059
        clock.advance(2)  # t = 1_000_061; the first entry is now 61s old
        assert limiter.allow() is True  # stale pruned -> count 1 -> admit
        assert redis.zcard(_window_key()) == 2

    def test_full_window_opens_again_after_it_slides(self) -> None:
        redis, clock, limiter = _make(limit=3, window_s=60.0)
        for _ in range(3):
            limiter.allow()  # window full at t = 1_000_000
        clock.advance(61)
        assert limiter.allow() is True  # all entries aged out -> fresh budget
        assert redis.zcard(_window_key()) == 1


class TestFailOpen:
    def test_redis_outage_allows_call(self) -> None:
        class _BrokenRedis:
            def zremrangebyscore(self, key: str, min_: float, max_: float) -> int:
                raise ConnectionError("redis down")

            def zcard(self, key: str) -> int:
                raise ConnectionError("redis down")

            def zadd(self, key: str, mapping: dict[str, float]) -> int:
                raise ConnectionError("redis down")

        limiter = RedisRateLimiter(_BrokenRedis(), "broken", limit=3, now=lambda: 1.0)
        assert limiter.allow() is True
