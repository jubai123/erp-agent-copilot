"""Unit tests for agent/retry_policy.py — task 5.7.

The retry policy decides *whether* a failure may be retried and *how long to
wait* (docs/06 §7): transient errors — rate-limit 429, temporary 5xx,
connection reset, provably-not-executed timeouts — are retried with
exponential backoff; permanent business errors (other 4xx) are not. The
executor wraps a tool call returning a ToolResult and sleeps between attempts,
bounded by the step's max_retries budget.
"""

from __future__ import annotations

import asyncio

from erp_copilot.agent.retry_policy import AsyncRetryExecutor, RetryExecutor, RetryPolicy
from erp_copilot.tools.tool_result import ToolResult


class TestIsRetryableHttp:
    def test_429_rate_limited_is_retryable(self) -> None:
        assert RetryPolicy().is_retryable_http(429) is True

    def test_transient_5xx_retryable(self) -> None:
        policy = RetryPolicy()
        assert all(policy.is_retryable_http(s) for s in (500, 502, 503, 504))

    def test_4xx_business_errors_not_retryable(self) -> None:
        policy = RetryPolicy()
        assert not any(policy.is_retryable_http(s) for s in (400, 401, 403, 404, 422))

    def test_non_error_codes_not_retryable(self) -> None:
        policy = RetryPolicy()
        assert not policy.is_retryable_http(200)
        assert not policy.is_retryable_http(301)


class TestIsRetryableException:
    def test_connection_and_timeout_families_retryable(self) -> None:
        policy = RetryPolicy()
        assert policy.is_retryable_exception(ConnectionError("reset"))
        assert policy.is_retryable_exception(ConnectionResetError("reset"))
        assert policy.is_retryable_exception(TimeoutError("timed out"))

    def test_plain_oserror_retryable(self) -> None:
        # Connection resets often surface as a bare OSError from the socket.
        assert RetryPolicy().is_retryable_exception(OSError("connection reset"))

    def test_missing_file_and_permission_not_retryable(self) -> None:
        policy = RetryPolicy()
        assert not policy.is_retryable_exception(FileNotFoundError("nope"))
        assert not policy.is_retryable_exception(PermissionError("nope"))

    def test_programmer_errors_not_retryable(self) -> None:
        policy = RetryPolicy()
        assert not policy.is_retryable_exception(ValueError("bad"))
        assert not policy.is_retryable_exception(TypeError("bad"))


class TestBackoff:
    def test_exponential_sequence_1_2_4(self) -> None:
        policy = RetryPolicy(base_delay_s=1.0)
        assert policy.delay_before_attempt(0) == 1.0
        assert policy.delay_before_attempt(1) == 2.0
        assert policy.delay_before_attempt(2) == 4.0

    def test_delay_capped_at_max_delay(self) -> None:
        policy = RetryPolicy(base_delay_s=1.0, max_delay_s=100.0)
        assert policy.delay_before_attempt(6) == 64.0  # uncapped: 1 * 2**6
        assert policy.delay_before_attempt(7) == 100.0  # capped: 2**7=128 -> 100
        assert policy.delay_before_attempt(20) == 100.0


class _FakeSleep:
    """Records every delay passed to it instead of sleeping."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def _failed(retryable: bool, code: str = "TOOL_ERROR") -> ToolResult:
    return ToolResult.failure(
        tool_version_id="v1",
        error_code=code,
        error_message="boom",
        is_retryable=retryable,
    )


def _succeeded() -> ToolResult:
    return ToolResult.success(tool_version_id="v1", data={})


class TestRetryExecutor:
    def test_retries_transient_until_success_with_backoff(self) -> None:
        sleep = _FakeSleep()
        attempts: list[int] = []

        def flaky() -> ToolResult:
            attempts.append(len(attempts) + 1)
            return _failed(retryable=True) if len(attempts) < 3 else _succeeded()

        result = RetryExecutor(sleep=sleep).execute(flaky, max_retries=3)

        assert result.status == "SUCCEEDED"
        assert len(attempts) == 3
        assert sleep.calls == [1.0, 2.0]  # backoff before retry 2 and 3

    def test_permanent_failure_is_not_retried(self) -> None:
        sleep = _FakeSleep()
        calls: list[int] = []

        def permanent() -> ToolResult:
            calls.append(1)
            return _failed(retryable=False)

        result = RetryExecutor(sleep=sleep).execute(permanent, max_retries=5)

        assert result.status == "FAILED"
        assert len(calls) == 1
        assert sleep.calls == []  # no backoff, no retry

    def test_retries_bounded_by_max_retries(self) -> None:
        sleep = _FakeSleep()
        calls: list[int] = []

        def always_fails() -> ToolResult:
            calls.append(1)
            return _failed(retryable=True)

        result = RetryExecutor(sleep=sleep).execute(always_fails, max_retries=2)

        assert result.status == "FAILED"
        assert len(calls) == 3  # initial attempt + 2 retries
        assert sleep.calls == [1.0, 2.0]

    def test_max_retries_zero_single_attempt(self) -> None:
        sleep = _FakeSleep()
        calls: list[int] = []

        def fails() -> ToolResult:
            calls.append(1)
            return _failed(retryable=True)

        RetryExecutor(sleep=sleep).execute(fails, max_retries=0)
        assert len(calls) == 1
        assert sleep.calls == []

    def test_immediate_success_no_sleep(self) -> None:
        sleep = _FakeSleep()
        result = RetryExecutor(sleep=sleep).execute(_succeeded, max_retries=5)
        assert result.status == "SUCCEEDED"
        assert sleep.calls == []

    def test_default_policy_used_when_none_given(self) -> None:
        # RetryExecutor() with no policy still retries a retryable failure.
        sleep = _FakeSleep()
        attempts: list[int] = []

        def transient() -> ToolResult:
            attempts.append(len(attempts) + 1)
            return _failed(retryable=True) if len(attempts) < 2 else _succeeded()

        result = RetryExecutor(sleep=sleep).execute(transient, max_retries=2)
        assert result.status == "SUCCEEDED"
        assert sleep.calls == [1.0]


class _AsyncFakeSleep:
    """Records every delay passed to it; a coroutine so the async executor
    awaits it exactly where it would otherwise asyncio.sleep."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


class TestAsyncRetryExecutor:
    """Async sibling of RetryExecutor for the async agent executor path.

    Mirrors the sync behaviour (same backoff sequence, same retryability
    gates) but awaits the tool call and sleeps via an injectable coroutine so
    the node's asyncio.gather never blocks on time.sleep.
    """

    def test_retries_transient_until_success_with_backoff(self) -> None:
        sleep = _AsyncFakeSleep()
        attempts: list[int] = []

        async def flaky() -> ToolResult:
            attempts.append(len(attempts) + 1)
            return _failed(retryable=True) if len(attempts) < 3 else _succeeded()

        result = asyncio.run(AsyncRetryExecutor(sleep=sleep).execute(flaky, max_retries=3))

        assert result.status == "SUCCEEDED"
        assert len(attempts) == 3
        assert sleep.calls == [1.0, 2.0]

    def test_permanent_failure_is_not_retried(self) -> None:
        sleep = _AsyncFakeSleep()
        calls: list[int] = []

        async def permanent() -> ToolResult:
            calls.append(1)
            return _failed(retryable=False)

        result = asyncio.run(AsyncRetryExecutor(sleep=sleep).execute(permanent, max_retries=5))

        assert result.status == "FAILED"
        assert len(calls) == 1
        assert sleep.calls == []

    def test_retries_bounded_by_max_retries(self) -> None:
        sleep = _AsyncFakeSleep()
        calls: list[int] = []

        async def always_fails() -> ToolResult:
            calls.append(1)
            return _failed(retryable=True)

        result = asyncio.run(AsyncRetryExecutor(sleep=sleep).execute(always_fails, max_retries=2))

        assert result.status == "FAILED"
        assert len(calls) == 3  # initial attempt + 2 retries
        assert sleep.calls == [1.0, 2.0]

    def test_max_retries_zero_single_attempt(self) -> None:
        sleep = _AsyncFakeSleep()
        calls: list[int] = []

        async def fails() -> ToolResult:
            calls.append(1)
            return _failed(retryable=True)

        asyncio.run(AsyncRetryExecutor(sleep=sleep).execute(fails, max_retries=0))
        assert len(calls) == 1
        assert sleep.calls == []

    def test_immediate_success_no_sleep(self) -> None:
        sleep = _AsyncFakeSleep()

        async def ok() -> ToolResult:
            return _succeeded()

        result = asyncio.run(AsyncRetryExecutor(sleep=sleep).execute(ok, max_retries=5))
        assert result.status == "SUCCEEDED"
        assert sleep.calls == []
