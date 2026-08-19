"""Deterministic answer-groundedness classification — task 7.9.

The online sentinel for RAG hallucination (docs/10 task 7.9): a terminal
answer's values are checked for coverage against the L2 retrieved citations
(state.retrieved_context), and a run that declined to answer because it had
no basis is marked refused. The check is deliberately lexical and offline —
an NLI judge would not be reproducible, the same reason success_condition is
an AST-whitelisted predicate (defence layer 4). It is an honest proxy, not a
semantic verdict: "yes" means the answer's claims lexically appear in the
citations, not that a model judged them entailed.
"""

from __future__ import annotations

from typing import Any, Literal

from erp_copilot.agent.state import AgentState, AgentStatus
from erp_copilot.domain.enums import StepStatus

GroundedLabel = Literal["yes", "no", "refused"]

# "该拒答时拒答" terminal errors: the run declined to answer because it had no
# basis. A TIMEOUT / budget-exhausted failure is a technical fault, not a
# refusal, so it classifies to None and the metric does not judge it.
_REFUSAL_CODES: frozenset[str] = frozenset(
    {
        "EMPTY_PLAN",            # no tool could be built from the intent
        "ROUTED_TIER23_NO_LLM",  # funnel refuses to over-grab without an LLM
        "NO_INTENT",             # nothing to plan from
    }
)

# Fraction of the answer's values that must appear in some retrieved citation
# for the answer to count as grounded. 0.5 = at least half the claims are
# substantiated by the references.
_COVERAGE_THRESHOLD = 0.5


def _answer_values(data: Any) -> list[str]:
    """Recursively collect leaf string/number values, never dict keys.

    Keys ("name", "stock") are structure, not claims; the values are what a
    citation must substantiate. Bools and None are skipped as non-claims.
    """
    values: list[str] = []
    if isinstance(data, dict):
        for value in data.values():
            values.extend(_answer_values(value))
    elif isinstance(data, (list, tuple)):
        for item in data:
            values.extend(_answer_values(item))
    elif isinstance(data, bool) or data is None:
        return values
    elif isinstance(data, (str, int, float)):
        values.append(str(data))
    return values


def extract_answer_text(state: AgentState) -> str:
    """Join the completed steps' result values into the run's answer text.

    FAILED / SKIPPED steps produced nothing for the user, so only COMPLETED
    results count as the answer; a run where every step was policy-skipped has
    an empty answer.
    """
    if state.plan is None:
        return ""
    chunks: list[str] = []
    for step in state.plan.steps:
        result = state.step_results.get(step.step_id)
        if (
            result is not None
            and result.status == StepStatus.COMPLETED
            and result.data is not None
        ):
            chunks.append(" ".join(_answer_values(result.data)))
    return " ".join(chunks)


def _coverage(answer: str, citations: str) -> float:
    """Fraction of answer values present in the citation corpus (0.0-1.0)."""
    tokens = [token for token in answer.split() if token]
    if not tokens or not citations:
        return 0.0
    hits = sum(1 for token in tokens if token in citations)
    return hits / len(tokens)


def classify_answer_groundedness(state: AgentState) -> GroundedLabel | None:
    """Verdict for one terminal run: "yes" / "no" / "refused", or None to skip.

    - "refused": the run declined to answer (a refusal-class terminal error).
    - "yes":     the run answered and the answer's values appear in the
                 retrieved citations.
    - "no":      the run answered but the citations do not substantiate it
                 (including no citations at all, or every step skipped).
    - None:      a technical failure that delivered no answer and is not a
                 refusal (timeout, deadline, budget...) — left out so the
                 metric only judges answers and refusals.
    """
    if any(error.code in _REFUSAL_CODES for error in state.errors):
        return "refused"
    if state.status != AgentStatus.SUCCEEDED:
        return None
    citations = " ".join(doc.content for doc in state.retrieved_context)
    if _coverage(extract_answer_text(state), citations) >= _COVERAGE_THRESHOLD:
        return "yes"
    return "no"
