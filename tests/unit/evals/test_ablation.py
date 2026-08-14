"""Unit tests for the ablation study script (Phase 3.13)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from evals.scripts.run_ablation import (
    _extract_doc_ids,
    _parse_frontmatter,
    analyze_l1_coverage,
    load_eval_queries,
    load_knowledge_base,
)

# ---------------------------------------------------------------------------
# Frontmatter parsing
# ---------------------------------------------------------------------------


class TestParseFrontmatter:
    def test_parses_valid_frontmatter(self) -> None:
        content = '---\ndocument_id: test-doc\nversion: "1.0"\n---\n# Heading\n\nBody.'
        fm = _parse_frontmatter(content)
        assert fm == {"document_id": "test-doc", "version": "1.0"}

    def test_no_frontmatter_returns_empty(self) -> None:
        content = "# Just a heading\n\nNo frontmatter here."
        fm = _parse_frontmatter(content)
        assert fm == {}

    def test_empty_content_returns_empty(self) -> None:
        assert _parse_frontmatter("") == {}

    def test_unclosed_frontmatter_returns_empty(self) -> None:
        content = "---\ndocument_id: oops\n"
        assert _parse_frontmatter(content) == {}


# ---------------------------------------------------------------------------
# Document ID extraction
# ---------------------------------------------------------------------------


class TestExtractDocIds:
    def test_deduplicates_same_source(self) -> None:
        results = [
            {"source": "doc-a", "content": "c1"},
            {"source": "doc-a", "content": "c2"},
            {"source": "doc-b", "content": "c3"},
        ]
        assert _extract_doc_ids(results) == ["doc-a", "doc-b"]

    def test_preserves_rank_order(self) -> None:
        results = [
            {"source": "doc-b", "content": "c1"},
            {"source": "doc-a", "content": "c2"},
            {"source": "doc-b", "content": "c3"},
            {"source": "doc-c", "content": "c4"},
        ]
        assert _extract_doc_ids(results) == ["doc-b", "doc-a", "doc-c"]

    def test_empty_results_returns_empty(self) -> None:
        assert _extract_doc_ids([]) == []


# ---------------------------------------------------------------------------
# L1 coverage analysis
# ---------------------------------------------------------------------------


class TestL1Coverage:
    def test_all_covered_queries(self) -> None:
        queries = [
            {"query": "如何创建订单", "relevant_docs": []},
            {"query": "怎么取消订单", "relevant_docs": []},
        ]
        result = analyze_l1_coverage(queries)
        assert result["coverage_rate"] == 1.0
        assert result["details"][0]["l1_covered"] is True
        assert result["details"][1]["l1_covered"] is True

    def test_partial_coverage(self) -> None:
        queries = [
            {"query": "如何创建订单", "relevant_docs": []},
            {"query": "完全未知的查询XYZ", "relevant_docs": []},
        ]
        result = analyze_l1_coverage(queries)
        assert result["coverage_rate"] == 0.5

    def test_empty_queries(self) -> None:
        result = analyze_l1_coverage([])
        assert result["coverage_rate"] == 0.0

    def test_details_include_intent_and_skills(self) -> None:
        queries = [{"query": "如何创建订单", "relevant_docs": []}]
        result = analyze_l1_coverage(queries)
        d = result["details"][0]
        assert d["intent"] == "order/create"
        assert "approval-policy" in d["l1_skills"]


# ---------------------------------------------------------------------------
# Knowledge base loading
# ---------------------------------------------------------------------------


class TestLoadKnowledgeBase:
    def test_loads_all_md_files(self) -> None:
        docs = load_knowledge_base()
        assert len(docs) >= 10  # at least the known documents

    def test_each_doc_has_required_keys(self) -> None:
        docs = load_knowledge_base()
        for d in docs:
            assert "document_id" in d
            assert "source_path" in d
            assert "content" in d

    def test_document_ids_are_unique(self) -> None:
        docs = load_knowledge_base()
        ids = [d["document_id"] for d in docs]
        assert len(ids) == len(set(ids))


# ---------------------------------------------------------------------------
# Eval queries loading
# ---------------------------------------------------------------------------


class TestLoadEvalQueries:
    def test_loads_expected_structure(self) -> None:
        queries = load_eval_queries()
        assert isinstance(queries, list)
        assert len(queries) >= 40  # expanded to 40 queries

    def test_each_query_has_required_fields(self) -> None:
        queries = load_eval_queries()
        for q in queries:
            assert "query" in q
            assert "relevant_docs" in q
            assert isinstance(q["relevant_docs"], list)


# ---------------------------------------------------------------------------
# Database target selection
# ---------------------------------------------------------------------------


class TestTestDatabaseUrl:
    def test_defaults_to_test_database(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from evals.scripts.run_ablation import _test_database_url

        monkeypatch.delenv("TEST_DATABASE_URL", raising=False)
        assert _test_database_url().endswith("/erp_copilot_test")

    def test_env_override_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from evals.scripts.run_ablation import _test_database_url

        monkeypatch.setenv("TEST_DATABASE_URL", "postgresql://x:y@host:5432/my_test_db")
        assert _test_database_url() == "postgresql://x:y@host:5432/my_test_db"


# ---------------------------------------------------------------------------
# Metrics evaluation (unit-tested via _extract_doc_ids + metrics module)
# ---------------------------------------------------------------------------


class TestMetricsIntegration:
    def test_perfect_retrieval_scores(self) -> None:
        """Verify metrics give perfect scores when retrieval is exact."""
        from erp_copilot.retrieval.metrics import (
            mean_reciprocal_rank,
            ndcg_at_k,
            precision_at_k,
            recall_at_k,
        )

        retrieved = ["doc-a", "doc-b"]
        relevant = {"doc-a", "doc-b"}

        assert recall_at_k(retrieved, relevant, k=2) == 1.0
        assert precision_at_k(retrieved, relevant, k=2) == 1.0
        assert mean_reciprocal_rank(retrieved, relevant) == 1.0
        assert ndcg_at_k(retrieved, relevant, k=2) == pytest.approx(1.0)

    def test_partial_retrieval_scores(self) -> None:
        from erp_copilot.retrieval.metrics import (
            mean_reciprocal_rank,
            recall_at_k,
        )

        retrieved = ["doc-x", "doc-a"]
        relevant = {"doc-a", "doc-b"}

        assert recall_at_k(retrieved, relevant, k=2) == 0.5
        assert mean_reciprocal_rank(retrieved, relevant) == 0.5

    def test_empty_retrieval_scores_zero(self) -> None:
        from erp_copilot.retrieval.metrics import (
            mean_reciprocal_rank,
            ndcg_at_k,
            precision_at_k,
            recall_at_k,
        )

        assert recall_at_k([], {"a"}) == 0.0
        assert mean_reciprocal_rank([], {"a"}) == 0.0
        assert ndcg_at_k([], {"a"}) == 0.0
        assert precision_at_k([], {"a"}) == 0.0


# ---------------------------------------------------------------------------
# Pipeline configuration dispatch (mock-based)
# ---------------------------------------------------------------------------


class TestPipelineDispatch:
    def test_vector_only_calls_search_similar(self) -> None:
        from evals.scripts.run_ablation import search_vector_only

        session = MagicMock()
        with patch(
            "erp_copilot.retrieval.vector_store.search_similar",
            return_value=[{"chunk_id": "c1", "source": "doc-a"}],
        ) as mock_search:
            results = search_vector_only(session, [0.5] * 1536, top_k=10)
            mock_search.assert_called_once()
            assert len(results) == 1

    def test_hybrid_calls_both_searches(self) -> None:
        from evals.scripts.run_ablation import search_hybrid

        session = MagicMock()
        with (
            patch(
                "erp_copilot.retrieval.vector_store.search_similar",
                return_value=[{"chunk_id": "c1", "source": "doc-a"}],
            ) as mock_vec,
            patch(
                "erp_copilot.retrieval.fts.keyword_search",
                return_value=[{"chunk_id": "c2", "source": "doc-b"}],
            ) as mock_kw,
            patch(
                "erp_copilot.retrieval.hybrid.rrf_fuse",
                return_value=[{"chunk_id": "c1", "source": "doc-a", "rrf_score": 0.05}],
            ) as mock_fuse,
        ):
            results = search_hybrid(session, "test query", [0.5] * 1536, top_k=10)
            mock_vec.assert_called_once()
            mock_kw.assert_called_once()
            mock_fuse.assert_called_once()
            assert len(results) == 1

    def test_hybrid_rerank_calls_reranker(self) -> None:
        from evals.scripts.run_ablation import DummyReranker, search_hybrid_rerank

        session = MagicMock()
        reranker = DummyReranker()
        with patch(
            "evals.scripts.run_ablation.search_hybrid",
            return_value=[
                {
                    "chunk_id": "c1",
                    "content": "short",
                    "source": "doc-a",
                    "section_path": [],
                    "char_count": 5,
                },
                {
                    "chunk_id": "c2",
                    "content": "longer text here",
                    "source": "doc-b",
                    "section_path": [],
                    "char_count": 17,
                },
            ],
        ) as mock_hybrid:
            results = search_hybrid_rerank(session, "test", [0.5] * 1536, reranker, top_k=2)
            mock_hybrid.assert_called_once()
            assert len(results) <= 2
            if results:
                assert "rerank_score" in results[0]


# ---------------------------------------------------------------------------
# Dummy providers
# ---------------------------------------------------------------------------


class TestEmbeddingProviderFactory:
    def test_dummy_provider_created(self) -> None:
        from evals.scripts.run_ablation import DummyEmbeddingProvider, create_embedding_provider

        provider = create_embedding_provider("dummy")
        assert isinstance(provider, DummyEmbeddingProvider)

    def test_unknown_provider_raises(self) -> None:
        from evals.scripts.run_ablation import create_embedding_provider

        with pytest.raises(ValueError, match="Unknown embedding provider"):
            create_embedding_provider("nonexistent")

    def test_openai_without_key_raises(self) -> None:
        from evals.scripts.run_ablation import create_embedding_provider

        with (
            patch("os.getenv", return_value=""),
            pytest.raises(RuntimeError, match="LLM_API_KEY"),
        ):
            create_embedding_provider("openai")


class TestDummyProviders:
    def test_dummy_embedding_dimensions(self) -> None:
        from evals.scripts.run_ablation import DummyEmbeddingProvider

        provider = DummyEmbeddingProvider()
        embeddings = provider.embed(["hello", "world"])
        assert len(embeddings) == 2
        assert all(len(e) == 1536 for e in embeddings)
        assert all(isinstance(v, float) for e in embeddings for v in e)

    def test_dummy_embedding_different_for_different_texts(self) -> None:
        from evals.scripts.run_ablation import DummyEmbeddingProvider

        provider = DummyEmbeddingProvider()
        e1 = provider.embed(["aaa"])[0]
        e2 = provider.embed(["bbb"])[0]
        # Both 3-char but different ord sums (291 vs 294) → different base
        assert e1 != e2

    def test_dummy_reranker_prefers_shorter_docs(self) -> None:
        from evals.scripts.run_ablation import DummyReranker

        reranker = DummyReranker()
        results = reranker.rerank("query", ["long document text", "short"])
        # First result should be "short" (index 1 → highest score)
        assert results[0][0] == 1  # index 1 = "short"
        assert results[0][1] > results[1][1]  # higher score for shorter

    def test_dummy_reranker_empty_input(self) -> None:
        from evals.scripts.run_ablation import DummyReranker

        reranker = DummyReranker()
        assert reranker.rerank("query", []) == []
