"""Tests for citation assembly (Phase 3.10)."""

from __future__ import annotations

from erp_copilot.retrieval.citation import assemble_citations


def _r(chunk_id: str, content: str, **kw):
    return {
        "chunk_id": chunk_id,
        "content": content,
        "section_path": kw.pop("section_path", ["H1"]),
        "char_count": len(content),
        "source": kw.pop("source", "doc.md"),
    }


class TestAssembleCitations:
    def test_formats_single_result(self) -> None:
        results = [_r("a", "订单必须校验库存后才能创建。")]
        text = assemble_citations(results)
        assert "[1]" in text
        assert "doc.md" in text
        assert "订单必须校验库存后才能创建" in text

    def test_formats_multiple_results(self) -> None:
        results = [
            _r("a", "第一条规则", source="rules.md", section_path=["规则"]),
            _r("b", "第二条流程", source="process.md", section_path=["流程"]),
        ]
        text = assemble_citations(results)
        assert "[1]" in text
        assert "[2]" in text
        assert "rules.md > 规则" in text
        assert "process.md > 流程" in text

    def test_section_path_formatting(self) -> None:
        results = [
            _r("a", "内容", source="api.md", section_path=["API 指南", "下单接口"]),
        ]
        text = assemble_citations(results)
        assert "api.md > API 指南 > 下单接口" in text

    def test_empty_section_path(self) -> None:
        results = [_r("a", "内容", section_path=[])]
        text = assemble_citations(results)
        assert ">" not in text

    def test_empty_results_returns_empty_string(self) -> None:
        assert assemble_citations([]) == ""

    def test_includes_header_block(self) -> None:
        results = [_r("a", "test")]
        text = assemble_citations(results)
        assert text.startswith("##")

    def test_numbering_is_sequential(self) -> None:
        results = [_r(str(i), f"content {i}") for i in range(5)]
        text = assemble_citations(results)
        for i in range(1, 6):
            assert f"[{i}]" in text
