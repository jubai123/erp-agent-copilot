"""Unit tests for structured JSON logging — task 6.1.

Every log line is a single JSON object carrying timestamp, level, run_id,
message and extra; the run_id is pulled automatically from a ContextVar so any
component in the call tree logs with run identity attached (docs/08 §6). The
JSON formatter is pure stdlib — no third-party logging dependency.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest

from erp_copilot.observability.logging import (
    JsonFormatter,
    get_trace_context,
    setup_logging,
    trace_context,
)


@pytest.fixture(autouse=True)
def _restore_root_logger() -> Iterator[None]:
    """Keep setup_logging from leaking handlers/level into other tests."""
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    yield
    root.handlers = saved_handlers
    root.setLevel(saved_level)


def _record(
    msg: str, level: int = logging.INFO, *, extra: dict[str, object] | None = None
) -> logging.LogRecord:
    record = logging.LogRecord(
        name="erp_copilot.test",
        level=level,
        pathname=__file__,
        lineno=10,
        msg=msg,
        args=(),
        exc_info=None,
    )
    if extra:
        for key, value in extra.items():
            setattr(record, key, value)
    return record


def _fmt() -> JsonFormatter:
    return JsonFormatter(service="erp-agent-copilot")


class TestJsonFormatter:
    def test_output_is_single_valid_json_object(self) -> None:
        payload = json.loads(_fmt().format(_record("hello")))
        assert payload["message"] == "hello"

    def test_required_fields_present(self) -> None:
        payload = json.loads(_fmt().format(_record("m")))
        assert {"timestamp", "level", "run_id", "message", "extra"} <= set(payload)
        assert payload["level"] == "INFO"
        assert payload["service"] == "erp-agent-copilot"
        assert payload["logger"] == "erp_copilot.test"

    def test_timestamp_is_iso8601_with_timezone(self) -> None:
        parsed = datetime.fromisoformat(json.loads(_fmt().format(_record("m")))["timestamp"])
        assert parsed.tzinfo is not None
        assert parsed.tzinfo == UTC

    def test_run_id_pulled_from_context(self) -> None:
        with trace_context(run_id="run-123"):
            payload = json.loads(_fmt().format(_record("m")))
        assert payload["run_id"] == "run-123"

    def test_run_id_null_when_unbound(self) -> None:
        payload = json.loads(_fmt().format(_record("m")))
        assert payload["run_id"] is None

    def test_step_id_from_context(self) -> None:
        with trace_context(run_id="r1", step_id="s1"):
            payload = json.loads(_fmt().format(_record("m")))
        assert payload["step_id"] == "s1"

    def test_extra_carries_non_standard_attributes(self) -> None:
        payload = json.loads(_fmt().format(_record("m", extra={"error_code": "TIMEOUT"})))
        assert payload["extra"]["error_code"] == "TIMEOUT"

    def test_standard_attributes_not_duplicated_in_extra(self) -> None:
        payload = json.loads(_fmt().format(_record("m")))
        assert "message" not in payload["extra"]
        assert "name" not in payload["extra"]
        assert "args" not in payload["extra"]

    def test_message_substitution(self) -> None:
        record = logging.LogRecord(
            name="t",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="run %s started",
            args=("r-1",),
            exc_info=None,
        )
        payload = json.loads(_fmt().format(record))
        assert payload["message"] == "run r-1 started"

    def test_extra_non_serializable_value_is_stringified(self) -> None:
        payload = json.loads(_fmt().format(_record("m", extra={"obj": object()})))
        assert isinstance(payload["extra"]["obj"], str)


class TestTraceContext:
    def test_get_returns_default_when_unbound(self) -> None:
        assert get_trace_context().run_id is None

    def test_context_manager_sets_and_restores(self) -> None:
        with trace_context(run_id="run-1"):
            assert get_trace_context().run_id == "run-1"
        assert get_trace_context().run_id is None

    def test_inner_context_merges_with_outer(self) -> None:
        with trace_context(run_id="run-1"):
            with trace_context(step_id="s1"):
                ctx = get_trace_context()
                assert ctx.run_id == "run-1"
                assert ctx.step_id == "s1"
            assert get_trace_context().run_id == "run-1"

    def test_nested_context_restores_outer(self) -> None:
        with trace_context(run_id="outer"):
            with trace_context(run_id="inner"):
                assert get_trace_context().run_id == "inner"
            assert get_trace_context().run_id == "outer"


class TestSetupLogging:
    def test_emits_json_line_to_stderr(self, capsys: pytest.CaptureFixture[str]) -> None:
        setup_logging(level="INFO", service="svc")
        logging.getLogger("erp_copilot.svc").info("hello %s", "world")
        err = capsys.readouterr().err
        payload = json.loads(err.strip().splitlines()[0])
        assert payload["message"] == "hello world"
        assert payload["service"] == "svc"
        assert payload["level"] == "INFO"

    def test_respects_level_filter(self, capsys: pytest.CaptureFixture[str]) -> None:
        setup_logging(level="WARNING", service="svc")
        logging.getLogger("erp_copilot.svc").info("suppressed")
        assert capsys.readouterr().err == ""
