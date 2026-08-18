"""Unit tests for the worker queue-depth reporter wiring.

The ``erp_worker_queue_length`` gauge was defined (task 6.4) but never set:
nothing sampled the broker. These tests cover the sampling helper, the
gauge-refresh wrapper, and the daemon reporter thread that keeps the value
current in the worker parent. No real Redis is touched — ``_FakeRedis`` stands
in for the broker, so the tests are hermetic.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

from erp_copilot.observability.metrics import (
    METRICS,
    generate_latest,
    refresh_queue_length,
    sample_queue_length,
    start_queue_length_reporter,
)


class _FakeRedis:
    """Minimal redis.Redis stand-in exposing only ``llen`` (queue depth)."""

    def __init__(self, lengths: dict[str, int | None], fail: bool = False) -> None:
        self._lengths = lengths
        self._fail = fail

    def llen(self, name: str) -> int | None:
        if self._fail:
            raise ConnectionError("broker unreachable")
        return self._lengths.get(name, 0)


def _wait_for(predicate: Callable[[], bool], timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition not met before timeout")


class TestSampleQueueLength:
    def test_sums_pending_across_queues(self) -> None:
        redis = _FakeRedis({"celery": 2, "priority": 3})
        assert sample_queue_length(redis, ["celery", "priority"]) == 5

    def test_unknown_queue_counts_zero(self) -> None:
        redis = _FakeRedis({})
        assert sample_queue_length(redis, ["celery", "nope"]) == 0

    def test_none_length_counts_zero(self) -> None:
        redis = _FakeRedis({"celery": None})
        assert sample_queue_length(redis, ["celery"]) == 0


class TestRefreshQueueLength:
    def test_records_sampled_length_on_gauge(self) -> None:
        redis = _FakeRedis({"celery": 4})
        try:
            assert refresh_queue_length(redis, ["celery"]) == 4
            assert "erp_worker_queue_length 4.0" in generate_latest(METRICS)
        finally:
            METRICS.worker_queue.set(0)

    def test_broker_failure_degrades_to_zero_without_raising(self) -> None:
        redis = _FakeRedis({"celery": 4}, fail=True)
        try:
            assert refresh_queue_length(redis, ["celery"]) == 0
            assert "erp_worker_queue_length 0.0" in generate_latest(METRICS)
        finally:
            METRICS.worker_queue.set(0)


class TestStartQueueLengthReporter:
    def test_loop_samples_and_stops_cleanly(self) -> None:
        redis = _FakeRedis({"celery": 7})
        stop = threading.Event()
        thread = start_queue_length_reporter(redis, ["celery"], interval_s=0.01, stop_event=stop)
        try:
            _wait_for(
                lambda: "erp_worker_queue_length 7.0" in generate_latest(METRICS),
                timeout=2.0,
            )
        finally:
            stop.set()
            thread.join(timeout=2.0)
        assert not thread.is_alive()

    def test_survives_transient_broker_failure(self) -> None:
        redis = _FakeRedis({"celery": 3}, fail=True)
        stop = threading.Event()
        thread = start_queue_length_reporter(redis, ["celery"], interval_s=0.01, stop_event=stop)
        try:
            # A failing broker must not kill the reporter thread: it samples 0,
            # logs a warning, and keeps going. Let it run a few cycles.
            time.sleep(0.06)
            assert thread.is_alive()
        finally:
            stop.set()
            thread.join(timeout=2.0)
        assert not thread.is_alive()
        METRICS.worker_queue.set(0)
