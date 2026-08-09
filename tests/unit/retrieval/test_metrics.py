"""Tests for retrieval evaluation metrics (Phase 3.12)."""

from __future__ import annotations

import pytest

from erp_copilot.retrieval.metrics import (
    mean_reciprocal_rank,
    ndcg_at_k,
    precision_at_k,
    recall_at_k,
)


class TestRecallAtK:
    def test_all_relevant_found(self) -> None:
        assert recall_at_k(["a", "b", "c"], {"a", "b"}, k=3) == 1.0

    def test_half_relevant_found(self) -> None:
        assert recall_at_k(["a", "x", "c"], {"a", "b"}, k=3) == 0.5

    def test_none_relevant_found(self) -> None:
        assert recall_at_k(["x", "y"], {"a", "b"}, k=2) == 0.0

    def test_k_truncation(self) -> None:
        assert recall_at_k(["a", "b", "c"], {"a", "b"}, k=1) == 0.5

    def test_empty_relevant_returns_zero(self) -> None:
        assert recall_at_k(["a", "b"], set()) == 0.0

    def test_none_k_uses_all(self) -> None:
        assert recall_at_k(["a", "b"], {"b"}) == 1.0


class TestMRR:
    def test_first_is_relevant(self) -> None:
        assert mean_reciprocal_rank(["a", "b", "c"], {"a"}) == 1.0

    def test_second_is_relevant(self) -> None:
        assert mean_reciprocal_rank(["x", "a", "c"], {"a"}) == 0.5

    def test_none_relevant_returns_zero(self) -> None:
        assert mean_reciprocal_rank(["x", "y"], {"a"}) == 0.0

    def test_empty_relevant_returns_zero(self) -> None:
        assert mean_reciprocal_rank(["a"], set()) == 0.0


class TestNDCG:
    def test_perfect_ordering(self) -> None:
        # All relevant at top → NDCG ~ 1.0
        score = ndcg_at_k(["a", "b"], {"a", "b"})
        assert score == pytest.approx(1.0, rel=0.01)

    def test_non_ideal_ordering(self) -> None:
        # Relevant doc not at top → NDCG < 1.0
        score = ndcg_at_k(["x", "a"], {"a"})
        assert 0.0 < score < 1.0

    def test_no_relevant_found(self) -> None:
        assert ndcg_at_k(["x", "y"], {"a", "b"}) == 0.0

    def test_empty_relevant(self) -> None:
        assert ndcg_at_k(["a", "b"], set()) == 0.0


class TestPrecisionAtK:
    def test_all_relevant(self) -> None:
        assert precision_at_k(["a", "b"], {"a", "b"}, k=2) == 1.0

    def test_half_relevant(self) -> None:
        assert precision_at_k(["a", "x"], {"a", "b"}, k=2) == 0.5

    def test_empty_retrieved(self) -> None:
        assert precision_at_k([], {"a"}) == 0.0
