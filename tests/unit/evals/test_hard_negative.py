"""Unit tests for the RAG hard-negative consumption logic — task 3.12 increment.

The real _knowledge_rag runner needs a pgvector database, so the per-case
hard-negative decision is factored into a pure function (_hard_negative_failed)
that is tested here offline. It aligns with tool_retrieval's confusion
semantics: selecting the trap *instead of* the relevant tool is a failure,
while the trap being co-surfaced alongside the relevant document is a
legitimate co-candidate and only tracked as a monitoring metric.
"""

from __future__ import annotations

from evals.run_all import _hard_negative_failed


class TestHardNegativeFailed:
    def test_trap_without_relevant_is_confusion(self) -> None:
        """Trap surfaced and no relevant doc — the retrieval answered wrong."""
        assert _hard_negative_failed(
            retrieved=["api-product-query"],
            relevant=["api-product-stock"],
            hard_negative=["api-product-query"],
        )

    def test_trap_co_surfaced_with_relevant_passes(self) -> None:
        """Relevant doc retrieved alongside the trap — routing succeeded."""
        assert not _hard_negative_failed(
            retrieved=["api-product-stock", "api-product-query"],
            relevant=["api-product-stock"],
            hard_negative=["api-product-query"],
        )

    def test_only_relevant_retrieved_passes(self) -> None:
        assert not _hard_negative_failed(
            retrieved=["api-product-stock"],
            relevant=["api-product-stock"],
            hard_negative=["api-product-query"],
        )

    def test_nothing_retrieved_passes_hard_negative_check(self) -> None:
        assert not _hard_negative_failed(
            retrieved=[],
            relevant=["api-product-stock"],
            hard_negative=["api-product-query"],
        )

    def test_irrelevant_non_hard_docs_do_not_fail(self) -> None:
        assert not _hard_negative_failed(
            retrieved=["api-order-create"],
            relevant=["api-product-stock"],
            hard_negative=["api-product-query"],
        )
