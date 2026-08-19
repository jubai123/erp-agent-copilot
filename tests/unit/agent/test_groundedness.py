"""Unit tests for answer-groundedness classification — task 7.9.

Pins the three-way split the erp_answer_grounded_total metric records: an
answer covered by the retrieved citations is "yes", a run that declined to
answer with no basis is "refused", and an answer the citations do not
substantiate is "no" (docs/10 task 7.9). A technical failure that delivered
no answer and is not a refusal classifies to None — the metric only judges
answers and refusals.
"""

from __future__ import annotations

from erp_copilot.agent.groundedness import classify_answer_groundedness, extract_answer_text
from erp_copilot.agent.state import (
    AgentState,
    AgentStatus,
    Plan,
    PlanStep,
    RetrievedDocument,
    StateError,
    StepResult,
)
from erp_copilot.domain.enums import StepStatus, ToolRiskLevel


def _state(
    *,
    status: AgentStatus,
    query: str = "查询苹果库存",
    errors: tuple[StateError, ...] = (),
    results: tuple[StepResult, ...] = (),
    docs: tuple[RetrievedDocument, ...] = (),
) -> AgentState:
    step = PlanStep(
        step_id="s1",
        tool_name="getProductByName",
        arguments={"name": "苹果"},
        risk_level=ToolRiskLevel.READ,
    )
    return AgentState(
        run_id="run-1",
        tenant_id="tenant-1",
        query=query,
        status=status,
        plan=Plan(steps=[step]),
        errors=list(errors),
        step_results={result.step_id: result for result in results},
        retrieved_context=list(docs),
    )


def _completed(data: dict) -> StepResult:
    return StepResult(step_id="s1", status=StepStatus.COMPLETED, data=data)


def _doc(content: str) -> RetrievedDocument:
    return RetrievedDocument(content=content, source="doc.md")


class TestClassifyAnswerGroundedness:
    def test_refused_when_empty_plan(self) -> None:
        state = _state(
            status=AgentStatus.QUEUED,
            errors=[StateError(code="EMPTY_PLAN", message="无")],
        )
        assert classify_answer_groundedness(state) == "refused"

    def test_refused_when_tier23_without_llm(self) -> None:
        state = _state(
            status=AgentStatus.QUEUED,
            errors=[StateError(code="ROUTED_TIER23_NO_LLM", message="无")],
        )
        assert classify_answer_groundedness(state) == "refused"

    def test_grounded_yes_when_answer_covered_by_citations(self) -> None:
        state = _state(
            status=AgentStatus.SUCCEEDED,
            results=[_completed({"name": "苹果", "stock": 10094})],
            docs=[_doc("苹果 10094 库存 查询流程")],
        )
        assert classify_answer_groundedness(state) == "yes"

    def test_grounded_no_when_citations_do_not_match(self) -> None:
        state = _state(
            status=AgentStatus.SUCCEEDED,
            results=[_completed({"name": "苹果", "stock": 10094})],
            docs=[_doc("香蕉 库存 查询流程")],
        )
        assert classify_answer_groundedness(state) == "no"

    def test_no_citations_is_ungrounded(self) -> None:
        state = _state(
            status=AgentStatus.SUCCEEDED,
            results=[_completed({"name": "苹果", "stock": 10094})],
        )
        assert classify_answer_groundedness(state) == "no"

    def test_all_steps_skipped_is_ungrounded(self) -> None:
        state = _state(
            status=AgentStatus.SUCCEEDED,
            results=[StepResult(step_id="s1", status=StepStatus.SKIPPED)],
            docs=[_doc("苹果 10094")],
        )
        assert classify_answer_groundedness(state) == "no"

    def test_technical_failure_is_not_counted(self) -> None:
        state = _state(
            status=AgentStatus.FAILED,
            errors=[StateError(code="TIMEOUT", message="超时")],
        )
        assert classify_answer_groundedness(state) is None

    def test_deadline_expiry_is_not_counted(self) -> None:
        state = _state(
            status=AgentStatus.EXPIRED,
            errors=[StateError(code="DEADLINE_EXCEEDED", message="超时")],
        )
        assert classify_answer_groundedness(state) is None

    def test_partial_coverage_below_threshold_is_no(self) -> None:
        state = _state(
            status=AgentStatus.SUCCEEDED,
            results=[_completed({"name": "苹果", "stock": 10094, "region": "上海", "score": 9})],
            docs=[_doc("苹果 库存")],
        )
        # Only "苹果" matches out of four answer values -> 0.25 < 0.5.
        assert classify_answer_groundedness(state) == "no"

    def test_threshold_boundary_is_grounded(self) -> None:
        state = _state(
            status=AgentStatus.SUCCEEDED,
            results=[_completed({"name": "苹果", "stock": 10094})],
            docs=[_doc("苹果")],
        )
        # Half the answer values ("苹果") appear in the citation -> 0.5 >= 0.5.
        assert classify_answer_groundedness(state) == "yes"


class TestExtractAnswerText:
    def test_joins_completed_step_values_excluding_keys(self) -> None:
        state = _state(
            status=AgentStatus.SUCCEEDED,
            results=[_completed({"name": "苹果", "stock": 10094})],
        )
        assert extract_answer_text(state) == "苹果 10094"

    def test_ignores_failed_and_skipped_steps(self) -> None:
        state = _state(
            status=AgentStatus.SUCCEEDED,
            results=[StepResult(step_id="s1", status=StepStatus.FAILED, error_code="E")],
        )
        assert extract_answer_text(state) == ""
