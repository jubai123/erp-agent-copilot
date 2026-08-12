"""Mandatory L2 retrieval node — task 4.4.

Every run executes this node; the LLM never decides whether to retrieve
(docs/03 §4). Retrieving nothing when the model is confident is how
hallucinated ERP procedures sneak into plans.

It fuses vector + keyword hits via RRF (Phase 3.8), threads the run's
tenant_id into both backends so cross-tenant knowledge can never leak, and
writes RetrievedDocument objects — citation source, section path and RRF
score — into state.retrieved_context. build_plan (task 4.6) injects these as
L2 reference knowledge after the L1 hard-constraint skills.

The search backends are injected callables so the node is unit-testable
without a database; the app wires them to search_similar / keyword_search.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from erp_copilot.agent.nodes.classify_intent import domain_keywords
from erp_copilot.agent.state import AgentState, IntentClassification, RetrievedDocument
from erp_copilot.retrieval.hybrid import rrf_fuse
from erp_copilot.security.injection_guard import InjectionGuard, InjectionVerdict

# Search backends receive (query/embedding, top_k, tenant_id) and must return
# result dicts shaped like rrf_fuse expects (chunk_id/content/section_path/
# char_count/source).
VectorSearch = Callable[[list[float], int, str], list[dict]]
KeywordSearch = Callable[[str, int, str], list[dict]]


def _to_retrieved_document(result: dict) -> RetrievedDocument:
    return RetrievedDocument(
        content=result["content"],
        source=result["source"],
        section_path=result.get("section_path", []),
        score=result.get("rrf_score"),
    )


def build_retrieval_query(intent: IntentClassification | None, fallback: str) -> str:
    """Compose the L2 query from intent entities plus domain vocabulary.

    Entity values (product/region/order_id) are the most discriminative
    terms; the domain's Chinese keywords anchor the query to the right
    business area. The raw query is used only as a fallback so question
    fillers never dilute recall. The intent here is the constructed, focused
    query — not the user's sentence verbatim (docs/02 §4, docs/10 task 4.4).
    """
    if intent is None:
        return fallback.strip()
    terms: list[str] = [str(v) for v in intent.entities.values() if isinstance(v, str)]
    terms.extend(domain_keywords(intent))
    return " ".join(terms).strip() or fallback.strip()


def retrieve_l2_knowledge(
    *,
    query: str,
    tenant_id: str,
    embed: Callable[[str], list[float]],
    vector_search: VectorSearch,
    keyword_search: KeywordSearch,
    top_k: int = 10,
    injection_guard: InjectionGuard | None = None,
    record_quarantine: Callable[[RetrievedDocument, InjectionVerdict], None] | None = None,
) -> list[RetrievedDocument]:
    """Hybrid L2 retrieval scoped to *tenant_id*, mapped to citation docs.

    With an *injection_guard* (task 5.4 knowledge layer) a retrieved chunk that
    fails the check is quarantined — dropped from the returned context so
    build_plan never injects poisoned knowledge — and reported through
    *record_quarantine* (the caller wires it to record_injection_event with
    disposition="quarantined"). No guard means no screening, keeping existing
    callers unchanged.
    """
    if not query.strip():
        return []

    fused = rrf_fuse(
        vector_search(embed(query), top_k, tenant_id),
        keyword_search(query, top_k, tenant_id),
        top_k=top_k,
    )
    docs = [_to_retrieved_document(r) for r in fused if r.get("content", "").strip()]
    if injection_guard is None:
        return docs
    kept: list[RetrievedDocument] = []
    for doc in docs:
        verdict = injection_guard.check(doc.content)
        if verdict.flagged:
            if record_quarantine is not None:
                record_quarantine(doc, verdict)
            continue
        kept.append(doc)
    return kept


def build_retrieve_context_node(
    *,
    embed: Callable[[str], list[float]],
    vector_search: VectorSearch,
    keyword_search: KeywordSearch,
    top_k: int = 10,
    injection_guard: InjectionGuard | None = None,
    record_quarantine: Callable[[RetrievedDocument, InjectionVerdict], None] | None = None,
) -> Callable[[AgentState], dict[str, Any]]:
    """Build the retrieve_context LangGraph node with injected backends."""

    def retrieve_context_node(state: AgentState) -> dict[str, Any]:
        return {
            "retrieved_context": retrieve_l2_knowledge(
                query=build_retrieval_query(state.intent, state.query),
                tenant_id=state.tenant_id,
                embed=embed,
                vector_search=vector_search,
                keyword_search=keyword_search,
                top_k=top_k,
                injection_guard=injection_guard,
                record_quarantine=record_quarantine,
            )
        }

    return retrieve_context_node
