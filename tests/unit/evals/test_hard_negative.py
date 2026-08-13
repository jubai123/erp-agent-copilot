"""Unit tests for the RAG hard-negative consumption logic — task 3.12 increment.

The real _knowledge_rag runner needs a pgvector database, so the per-case
hard-negative decision is factored into a pure function (_hard_negative_failed)
that is tested here offline. It mirrors the runner's rule: a retrieved
hard-negative document means the retrieval was confused, regardless of whether
a relevant document was also retrieved.
"""

from __future__ import annotations

from evals.run_all import _hard_negative_failed


class TestHardNegativeFailed:
    def test_hit_hard_negative_is_confusion(self) -> None:
        assert _hard_negative_failed(
            retrieved=["api-product-stock", "api-product-query"],
            relevant=["api-product-stock"],
            hard_negative=["api-product-query"],
        )

    def test_hit_both_relevant_and_hard_negative_still_fails(self) -> None:
        assert _hard_negative_failed(
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
