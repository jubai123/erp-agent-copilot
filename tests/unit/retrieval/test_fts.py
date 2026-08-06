"""Tests for full-text keyword search (Phase 3.7)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from erp_copilot.retrieval.fts import jieba_segment, keyword_search


class TestJiebaSegment:
    def test_segments_chinese_text(self) -> None:
        result = jieba_segment("如何创建订单")
        tokens = result.split()
        assert len(tokens) >= 3  # at minimum: 如何 创建 订单

    def test_chinese_tokens_are_space_separated(self) -> None:
        result = jieba_segment("订单状态机")
        assert " " in result
        assert "订单" in result

    def test_mixed_chinese_and_ascii(self) -> None:
        result = jieba_segment("订单 CREATED → CONFIRMED")
        # Chinese tokens should be segmented, ASCII tokens preserved
        assert "订单" in result
        assert "CREATED" in result
        assert "CONFIRMED" in result

    def test_empty_string_returns_empty(self) -> None:
        assert jieba_segment("") == ""

    def test_whitespace_only_returns_empty(self) -> None:
        assert jieba_segment("   ") == ""

    def test_pure_ascii_preserved(self) -> None:
        result = jieba_segment("hello world test")
        assert "hello" in result
        assert "world" in result

    def test_segments_erp_terms(self) -> None:
        result = jieba_segment("订单创建后供应商不可用")
        assert "订单" in result
        assert "供应商" in result


def _fake_row(**kwargs):
    m = MagicMock()
    m.configure_mock(**kwargs)
    m.__getitem__.side_effect = lambda k: getattr(m, k)
    return m


class TestKeywordSearch:
    def test_question_words_are_filtered_from_tsquery(self) -> None:
        """Question words (如何/怎么/哪些) never match docs — drop them.

        The tsquery must use OR (|) semantics: plainto_tsquery's AND
        behaviour requires every token to match, which never happened
        because question words don't appear in technical documents.
        """
        session = MagicMock()
        session.execute.return_value.mappings.return_value.all.return_value = []

        keyword_search(session, "如何创建订单", "t1")
        query_param = session.execute.call_args[0][1]["query"]

        assert "如何" not in query_param
        assert "订单" in query_param
        assert " | " in query_param  # OR semantics, not AND

    def test_single_cjk_char_fragments_dropped(self) -> None:
        """Single CJK chars (字/段/幂) are jieba fragments, never KB terms."""
        session = MagicMock()
        session.execute.return_value.mappings.return_value.all.return_value = []

        keyword_search(session, "商品库存信息包括哪些字段", "t1")
        query_param = session.execute.call_args[0][1]["query"]

        assert "字" not in query_param
        assert "段" not in query_param
        assert "商品" in query_param

    def test_all_stopwords_returns_empty(self) -> None:
        """A query that is entirely stopwords has no keyword signal."""
        session = MagicMock()

        results = keyword_search(session, "有哪些", "t1")
        assert results == []

    def test_returns_matching_results(self) -> None:
        session = MagicMock()
        session.execute.return_value.mappings.return_value.all.return_value = [
            _fake_row(
                chunk_id="c1",
                content="订单状态机合法转换：CREATED → CONFIRMED",
                section_path='["Test"]',
                char_count=30,
                source="order-lifecycle.md",
                rank=0.5,
            ),
        ]

        results = keyword_search(session, "状态机", "tenant-1", top_k=5)

        assert len(results) == 1
        assert results[0]["chunk_id"] == "c1"
        assert "状态机" in results[0]["content"]

    def test_returns_empty_for_no_match(self) -> None:
        session = MagicMock()
        session.execute.return_value.mappings.return_value.all.return_value = []

        results = keyword_search(session, "nonexistent", "tenant-1")
        assert results == []

    def test_empty_query_raises(self) -> None:
        session = MagicMock()
        with pytest.raises(ValueError, match="query must not be empty"):
            keyword_search(session, "", "tenant-1")

    def test_whitespace_only_query_raises(self) -> None:
        session = MagicMock()
        with pytest.raises(ValueError, match="query must not be empty"):
            keyword_search(session, "   ", "tenant-1")

    def test_enforces_tenant_isolation(self) -> None:
        session = MagicMock()
        session.execute.return_value.mappings.return_value.all.return_value = []

        keyword_search(session, "订单", "tenant-x", top_k=3)
        params = session.execute.call_args[0][1]
        assert params["tenant_id"] == "tenant-x"

    def test_result_structure(self) -> None:
        session = MagicMock()
        session.execute.return_value.mappings.return_value.all.return_value = [
            _fake_row(
                chunk_id="c1",
                content="test",
                section_path='["H1"]',
                char_count=4,
                source="doc.md",
                rank=0.9,
            ),
        ]

        results = keyword_search(session, "test", "t1")
        r = results[0]
        assert isinstance(r["chunk_id"], str)
        assert isinstance(r["content"], str)
        assert isinstance(r["section_path"], list)
        assert isinstance(r["char_count"], int)
        assert isinstance(r["source"], str)
        assert isinstance(r["rank"], float)

    def test_results_ordered_by_rank_desc(self) -> None:
        session = MagicMock()
        session.execute.return_value.mappings.return_value.all.return_value = [
            _fake_row(
                chunk_id="high", content="a", section_path="[]",
                char_count=1, source="x", rank=0.9,
            ),
            _fake_row(
                chunk_id="low", content="b", section_path="[]",
                char_count=1, source="x", rank=0.1,
            ),
        ]

        results = keyword_search(session, "test", "t1")
        assert results[0]["chunk_id"] == "high"
        assert results[1]["chunk_id"] == "low"
