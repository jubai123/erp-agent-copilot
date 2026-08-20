"""Unit tests for vocabulary/capture.py — OOV observation gating.

should_capture decides whether a terminal run is worth capturing for the
LLM-driven vocabulary pipeline: only tier2/tier3 queries that extracted no
known product/region/unit entity are candidates (they may carry an unknown
term). build_vocabulary_capture returns None when the pipeline flag is off, so
the worker path is byte-for-byte the offline default.
"""

from __future__ import annotations

from erp_copilot.agent.nodes.classify_intent import classify_intent
from erp_copilot.agent.state import AgentState
from erp_copilot.infrastructure.config import Settings
from erp_copilot.vocabulary.capture import build_vocabulary_capture, should_capture


def _state(query: str) -> AgentState:
    return AgentState(
        run_id="r1", tenant_id="t1", query=query, intent=classify_intent(query)
    )


class TestShouldCapture:
    def test_tier1_never_captures(self) -> None:
        assert should_capture(_state("苹果多少钱"), "tier1") is False

    def test_tier2_with_no_known_entity_captures(self) -> None:
        # 榴莲 is OOV -> no product entity extracted -> candidate.
        assert should_capture(_state("榴莲多少钱"), "tier2") is True

    def test_tier3_with_known_entity_does_not_capture(self) -> None:
        # A known product entity is present -> nothing unknown to mine.
        assert should_capture(_state("苹果和香蕉哪个贵"), "tier3") is False

    def test_empty_query_does_not_capture(self) -> None:
        state = AgentState(run_id="r1", tenant_id="t1", query="  ")
        assert should_capture(state, "tier2") is False

    def test_missing_intent_does_not_capture(self) -> None:
        state = AgentState(run_id="r1", tenant_id="t1", query="榴莲多少钱")
        assert should_capture(state, "tier2") is False


class TestBuildVocabularyCapture:
    def test_flag_off_returns_none(self) -> None:
        settings = Settings(vocabulary_llm_updates_enabled=False)
        assert build_vocabulary_capture(None, settings) is None  # type: ignore[arg-type]

    def test_flag_on_returns_callable_hook(self) -> None:
        settings = Settings(vocabulary_llm_updates_enabled=True)
        hook = build_vocabulary_capture(None, settings)  # type: ignore[arg-type]
        assert hook is not None
        assert callable(hook)
