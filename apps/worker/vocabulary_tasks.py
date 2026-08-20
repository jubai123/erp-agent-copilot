"""Celery batch task for the LLM-driven vocabulary pipeline.

extract_vocabulary_proposals pulls unprocessed VocabularyObservation rows,
runs the batch LLM extractor over their queries, and upserts PENDING
VocabularyProposal rows keyed by proposal_key. The task is gated by the
vocabulary_llm_updates_enabled flag — when off it exits immediately, so the
worker stays offline by default.

LLM output is never authoritative: every proposal stays PENDING until the
approval API (vocabulary/service.py) writes it into vocabulary_terms. The
task is dispatched externally (cron / manual); no beat infrastructure is
introduced here.
"""

from __future__ import annotations

import json

from celery.utils.log import get_task_logger

from apps.worker.celery_app import celery_app
from apps.worker.llm_planner import build_real_llm_complete
from erp_copilot.domain.entities import VocabularyObservation, VocabularyProposal, VocabularyTerm
from erp_copilot.infrastructure.config import Settings
from erp_copilot.vocabulary.extractor import extract_candidates, normalize_unit, proposal_key
from erp_copilot.vocabulary.loader import VocabularyCatalog, get_catalog

logger = get_task_logger(__name__)

_BATCH_LIMIT = 100


def _known_terms(session, catalog: VocabularyCatalog) -> set[tuple[str, str]]:
    """(type, normalized canonical) already known: seed + active terms + live proposals.

    REJECTED proposals are deliberately excluded so the LLM may re-propose a
    term the operator rejected when new evidence lands.
    """
    known: set[tuple[str, str]] = set()
    for vocab_type, terms in (
        ("product", catalog.products),
        ("region", catalog.regions),
        ("unit", catalog.units),
    ):
        known.update((vocab_type, normalize_unit(term, vocab_type)) for term in terms)
    for term in session.query(VocabularyTerm).filter_by(is_active=True):
        known.add((term.vocab_type, normalize_unit(term.canonical, term.vocab_type)))
    for proposal in session.query(VocabularyProposal).filter(
        VocabularyProposal.status.in_(("PENDING", "APPROVED"))
    ):
        known.add(
            (proposal.vocab_type, normalize_unit(proposal.canonical, proposal.vocab_type))
        )
    return known


@celery_app.task(bind=True, name="vocabulary.extract_proposals")
def extract_vocabulary_proposals(self) -> dict:
    """Extract vocabulary candidates from captured observations, one batch."""
    from erp_copilot.infrastructure.database import get_session

    session = get_session()
    settings = Settings()  # type: ignore[call-arg]
    try:
        if not settings.vocabulary_llm_updates_enabled:
            return {"status": "skipped", "reason": "flag_off"}
        rows = (
            session.query(VocabularyObservation)
            .filter_by(processed=False)
            .order_by(VocabularyObservation.created_at.asc())
            .limit(_BATCH_LIMIT)
            .all()
        )
        if not rows:
            return {"status": "idle", "captured": 0, "proposals": 0}
        queries = list(dict.fromkeys(row.query.strip() for row in rows if row.query.strip()))
        catalog = get_catalog()
        candidates = extract_candidates(
            build_real_llm_complete(settings),
            queries,
            catalog,
            _known_terms(session, catalog),
        )
        created = 0
        for candidate in candidates:
            key = proposal_key(candidate.type, candidate.canonical)
            existing = session.query(VocabularyProposal).filter_by(proposal_key=key).first()
            if existing is not None:
                if existing.status == "PENDING":
                    existing.aliases = json.dumps(candidate.aliases, ensure_ascii=False)
                    existing.evidence = json.dumps(candidate.evidence, ensure_ascii=False)
                    existing.confidence = candidate.confidence
                continue
            session.add(
                VocabularyProposal(
                    proposal_key=key,
                    vocab_type=candidate.type,
                    canonical=candidate.canonical,
                    aliases=json.dumps(candidate.aliases, ensure_ascii=False),
                    evidence=json.dumps(candidate.evidence, ensure_ascii=False),
                    confidence=candidate.confidence,
                )
            )
            created += 1
        for row in rows:
            row.processed = True
        session.commit()
        return {"status": "ok", "captured": len(rows), "proposals": created}
    finally:
        session.close()
