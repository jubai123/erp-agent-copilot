"""Tests for hybrid search with RRF fusion (Phase 3.8)."""

from __future__ import annotations

from erp_copilot.retrieval.hybrid import rrf_fuse


def _r(chunk_id: str, content: str = "x", **kw):
    """Short helper for building result dicts in tests."""
    d = {
        "chunk_id": chunk_id,
        "content": content,
        "section_path": [],
        "char_count": len(content),
        "source": "test.md",
    }
    d.update(kw)
    return d


class TestRRFFuse:
    def test_fuses_two_result_sets(self) -> None:
        semantic = [_r("a"), _r("b")]
        keyword = [_r("b"), _r("c")]

        result = rrf_fuse(semantic, keyword, k=60)
        assert result[0]["chunk_id"] == "b"
        assert len(result) == 3

    def test_higher_rank_higher_score(self) -> None:
        semantic = [_r("a"), _r("b")]
        result = rrf_fuse(semantic, [], k=60)
        assert result[0]["chunk_id"] == "a"
        assert result[0]["rrf_score"] > result[1]["rrf_score"]

    def test_empty_second_list(self) -> None:
        result = rrf_fuse([_r("x")], [], k=60)
        assert len(result) == 1
        assert "rrf_score" in result[0]

    def test_empty_first_list(self) -> None:
        result = rrf_fuse([], [_r("y")], k=60)
        assert len(result) == 1
        assert result[0]["chunk_id"] == "y"

    def test_empty_both_returns_empty(self) -> None:
        assert rrf_fuse([], [], k=60) == []

    def test_preserves_source_fields_in_output(self) -> None:
        result = rrf_fuse([_r("a", content="text", section_path=["H1"])], [], k=60)
        r = result[0]
        assert r["content"] == "text"
        assert r["section_path"] == ["H1"]
        assert "distance" not in r
        assert "rank" not in r

    def test_k_value_affects_score(self) -> None:
        semantic = [_r("a"), _r("b")]
        result_k10 = rrf_fuse(semantic, [], k=10)
        result_k100 = rrf_fuse(semantic, [], k=100)
        diff_k10 = result_k10[0]["rrf_score"] - result_k10[1]["rrf_score"]
        diff_k100 = result_k100[0]["rrf_score"] - result_k100[1]["rrf_score"]
        assert diff_k10 > diff_k100

    def test_deduplication_picks_first_appearance(self) -> None:
        result = rrf_fuse(
            [_r("dup", content="semantic version")],
            [_r("dup", content="keyword version")],
            k=60,
        )
        assert len(result) == 1
        assert result[0]["content"] == "semantic version"

    def test_limits_to_top_k(self) -> None:
        semantic = [_r(str(i)) for i in range(20)]
        keyword = [_r(str(i + 10)) for i in range(20)]
        result = rrf_fuse(semantic, keyword, k=60, top_k=10)
        assert len(result) == 10

    def test_default_top_k_is_20(self) -> None:
        semantic = [_r(str(i)) for i in range(50)]
        result = rrf_fuse(semantic, [], k=60)
        assert len(result) == 20
