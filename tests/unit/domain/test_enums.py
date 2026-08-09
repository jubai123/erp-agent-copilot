"""Tests for domain enums."""

import pytest

from erp_copilot.domain.enums import RunStatus, StepStatus, ToolRiskLevel


class TestRunStatus:
    def test_all_values(self) -> None:
        assert RunStatus.PENDING == "pending"
        assert RunStatus.RUNNING == "running"
        assert RunStatus.WAITING_APPROVAL == "waiting_approval"
        assert RunStatus.COMPLETED == "completed"
        assert RunStatus.FAILED == "failed"
        assert RunStatus.CANCELLED == "cancelled"

    def test_from_string(self) -> None:
        assert RunStatus("pending") == RunStatus.PENDING
        assert RunStatus("completed") == RunStatus.COMPLETED
        assert RunStatus("waiting_approval") == RunStatus.WAITING_APPROVAL

    def test_invalid_value_raises(self) -> None:
        with pytest.raises(ValueError):
            RunStatus("bogus_status")

    def test_is_str(self) -> None:
        assert isinstance(RunStatus.PENDING, str)

    def test_compare_across_instances(self) -> None:
        a = RunStatus("pending")
        b = RunStatus.PENDING
        assert a == b
        assert a is not RunStatus.RUNNING


class TestStepStatus:
    def test_all_values(self) -> None:
        assert StepStatus.PENDING == "pending"
        assert StepStatus.IN_PROGRESS == "in_progress"
        assert StepStatus.COMPLETED == "completed"
        assert StepStatus.FAILED == "failed"
        assert StepStatus.SKIPPED == "skipped"

    def test_from_string(self) -> None:
        assert StepStatus("in_progress") == StepStatus.IN_PROGRESS
        assert StepStatus("skipped") == StepStatus.SKIPPED

    def test_is_str(self) -> None:
        assert isinstance(StepStatus.IN_PROGRESS, str)


class TestToolRiskLevel:
    def test_all_values(self) -> None:
        assert ToolRiskLevel.READ == "read"
        assert ToolRiskLevel.WRITE == "write"
        assert ToolRiskLevel.DANGEROUS == "dangerous"

    def test_from_string(self) -> None:
        assert ToolRiskLevel("read") == ToolRiskLevel.READ
        assert ToolRiskLevel("dangerous") == ToolRiskLevel.DANGEROUS

    def test_is_str(self) -> None:
        assert isinstance(ToolRiskLevel.READ, str)

    def test_risk_ordering_by_value(self) -> None:
        levels = list(ToolRiskLevel)
        assert levels == [ToolRiskLevel.READ, ToolRiskLevel.WRITE, ToolRiskLevel.DANGEROUS]
