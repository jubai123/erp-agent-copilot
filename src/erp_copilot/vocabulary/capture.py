"""Vocabulary capture — records OOV queries for the LLM-driven update pipeline.

persist_run calls the injected capture hook before committing a terminal run.
A tier2/tier3 query that extracted no known product/region/unit entity may
contain an out-of-vocabulary term, so it becomes a VocabularyObservation — the
raw material the batch LLM extractor mines (apps/worker/vocabulary_tasks).

The hook never commits: the observation row rides the run's own transaction
and lands atomically at the next session.commit(), so only persisted runs
contribute capture rows and a failed run rolls both back together.
"""

from __future__ import annotations

from collections.abc import Callable

from sqlalchemy.orm import Session

from erp_copilot.agent.state import AgentState
from erp_copilot.domain.entities import Run, VocabularyObservation
from erp_copilot.infrastructure.config import Settings

# Entity keys that, if present, mean the query already carries a known term.
_ENTITY_KEYS = ("product", "region", "unit")


def should_capture(final: AgentState, tier: str) -> bool:
    """True when *final* is a candidate for OOV vocabulary capture.

    Tier1 never captures: the deterministic path only serves in-vocabulary
    queries, so there is nothing unknown to extract. A query that extracted a
    known product/region/unit is not captured either — the pipeline mines for
    unknown terms, and a known-entity query has none to offer.
    """
    if tier == "tier1":
        return False
    if final.intent is None or not final.query.strip():
        return False
    return not any(key in final.intent.entities for key in _ENTITY_KEYS)


def build_vocabulary_capture(
    session: Session, settings: Settings
) -> Callable[[Run, str, AgentState], None] | None:
    """Return the persist_run capture hook, or None when the pipeline is off.

    With the flag on, the returned closure inserts a VocabularyObservation for
    a qualifying run but never commits — it rides the run's transaction and
    lands atomically at the next session.commit(). With the flag off the hook
    is None so the worker path is byte-for-byte the offline default.
    """
    if not settings.vocabulary_llm_updates_enabled:
        return None

    def _capture(run: Run, tier: str, final: AgentState) -> None:
        if not should_capture(final, tier):
            return
        session.add(
            VocabularyObservation(
                run_id=run.id,
                tenant_id=run.tenant_id,
                query=final.query,
                tier=tier,
            )
        )

    return _capture
