"""Tests for unified ToolResult and ToolError data models."""

from __future__ import annotations

import json
from datetime import datetime


class TestToolResultSuccess:
    """Acceptance: Success result includes data, status, and timing."""

    def test_success_result_basic(self) -> None:
        from erp_copilot.tools.tool_result import ToolResult

        result = ToolResult.success(
            tool_version_id="tv-1",
            data={"product": "apple", "price": 10},
            started_at=datetime(2026, 8, 5, 10, 0, 0),
            finished_at=datetime(2026, 8, 5, 10, 0, 1),
        )

        assert result.status == "SUCCEEDED"
        assert result.tool_version_id == "tv-1"
        assert result.data == {"product": "apple", "price": 10}
        assert result.elapsed_ms == 1000
        assert result.error is None
        assert result.attempt == 1

    def test_success_result_default_timestamps(self) -> None:
        from erp_copilot.tools.tool_result import ToolResult

        result = ToolResult.success(
            tool_version_id="tv-1",
            data={"ok": True},
        )

        assert result.started_at is not None
        assert result.finished_at is not None
        assert result.elapsed_ms >= 0

    def test_success_result_with_metadata(self) -> None:
        from erp_copilot.tools.tool_result import ToolResult

        result = ToolResult.success(
            tool_version_id="tv-1",
            data={"x": 1},
            metadata={"http_status": 200, "remaining_quota": 98},
        )

        assert result.metadata == {"http_status": 200, "remaining_quota": 98}

    def test_success_result_serializable(self) -> None:
        from erp_copilot.tools.tool_result import ToolResult

        result = ToolResult.success(
            tool_version_id="tv-1",
            data={"items": [1, 2, 3]},
            started_at=datetime(2026, 8, 5, 10, 0, 0),
            finished_at=datetime(2026, 8, 5, 10, 0, 2),
        )

        d = result.to_dict()
        assert d["status"] == "SUCCEEDED"
        assert d["data"] == {"items": [1, 2, 3]}
        assert d["elapsed_ms"] == 2000
        assert d["error"] is None
        assert d["tool_version_id"] == "tv-1"


class TestToolError:
    """Acceptance: Error result includes error_code, error_message, retryable flag."""

    def test_error_temporary(self) -> None:
        from erp_copilot.tools.tool_result import ToolResult

        result = ToolResult.failure(
            tool_version_id="tv-1",
            error_code="TIMEOUT",
            error_message="Request timed out after 30s",
            is_retryable=True,
        )

        assert result.status == "FAILED"
        assert result.error is not None
        assert result.error.error_code == "TIMEOUT"
        assert result.error.error_message == "Request timed out after 30s"
        assert result.error.is_retryable is True

    def test_error_permanent(self) -> None:
        from erp_copilot.tools.tool_result import ToolResult

        result = ToolResult.failure(
            tool_version_id="tv-1",
            error_code="VALIDATION_ERROR",
            error_message="Missing required field: product_id",
            is_retryable=False,
        )

        assert result.status == "FAILED"
        assert result.error is not None
        assert result.error.is_retryable is False

    def test_error_serializable(self) -> None:
        from erp_copilot.tools.tool_result import ToolResult

        result = ToolResult.failure(
            tool_version_id="tv-1",
            error_code="RATE_LIMITED",
            error_message="Too many requests",
            is_retryable=True,
            attempt=3,
        )

        d = result.to_dict()
        assert d["status"] == "FAILED"
        assert d["error"]["error_code"] == "RATE_LIMITED"
        assert d["error"]["is_retryable"] is True
        assert d["attempt"] == 3


class TestArtifactHandling:
    """Acceptance: Large results (>100KB) are written to artifact storage."""

    def test_large_result_writes_artifact(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        from erp_copilot.tools.tool_result import ToolResult

        large_data = {"content": "x" * 150_000}  # ~150KB serialised

        result = ToolResult.success(
            tool_version_id="tv-1",
            data=large_data,
            artifact_dir=str(tmp_path),
        )

        assert result.data is None
        assert result.artifact_uri is not None
        assert result.artifact_uri.startswith("artifact://")
        # Artifact file exists and contains the data
        artifact_path = tmp_path / result.artifact_uri.removeprefix("artifact://")
        stored = json.loads(artifact_path.read_text())
        assert stored == large_data

    def test_small_result_stays_inline(self) -> None:
        from erp_copilot.tools.tool_result import ToolResult

        small_data = {"answer": "ok"}

        result = ToolResult.success(
            tool_version_id="tv-1",
            data=small_data,
        )

        assert result.data == small_data
        assert result.artifact_uri is None
