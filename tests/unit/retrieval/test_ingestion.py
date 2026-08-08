"""Tests for document ingestion: Markdown parsing and text chunking."""

from __future__ import annotations

from erp_copilot.retrieval.ingestion import (
    Chunk,
    chunk_text,
    ingest_document,
    parse_markdown_sections,
)

# ---------------------------------------------------------------------------
# Sample inputs for testing
# ---------------------------------------------------------------------------

SIMPLE_MARKDOWN = """# 库存管理

库存管理是ERP系统的核心功能。

## 商品查询

用户可以通过商品名称查询商品信息。

## 库存校验

下单前必须校验库存是否充足。
"""

MARKDOWN_WITH_H3 = """# 订单处理

## 创建订单

创建订单需要以下参数：
- product_id
- quantity

### 参数校验

每个参数都有合法性校验规则。

### 幂等机制

使用幂等键避免重复创建。

## 查询订单

通过订单ID查询订单状态。
"""

PLAIN_TEXT = """这是一段纯文本内容，用于测试文本分块功能。
它包含多行内容，应该被正确地切分成重叠的块。
每个块的默认大小是500个字符，重叠50个字符。
这段文本比较短，应该只会产生一个块。"""

LONG_TEXT = "A" * 1200  # 1200 chars → 3 chunks with default settings


# ---------------------------------------------------------------------------
# Markdown parsing
# ---------------------------------------------------------------------------


class TestParseMarkdownSections:
    def test_extracts_all_headings(self) -> None:
        sections = parse_markdown_sections(SIMPLE_MARKDOWN)
        assert len(sections) == 3

    def test_heading_levels_correct(self) -> None:
        sections = parse_markdown_sections(SIMPLE_MARKDOWN)
        assert sections[0]["heading_path"] == ["库存管理"]
        assert sections[1]["heading_path"] == ["库存管理", "商品查询"]
        assert sections[2]["heading_path"] == ["库存管理", "库存校验"]

    def test_h3_nested_under_h2(self) -> None:
        sections = parse_markdown_sections(MARKDOWN_WITH_H3)
        # 4 sections: H2-content, H3-参数校验, H3-幂等机制, H2-查询订单
        assert len(sections) == 4
        assert sections[0]["heading_path"] == ["订单处理", "创建订单"]
        assert sections[1]["heading_path"] == ["订单处理", "创建订单", "参数校验"]
        assert sections[2]["heading_path"] == ["订单处理", "创建订单", "幂等机制"]
        assert sections[3]["heading_path"] == ["订单处理", "查询订单"]

    def test_content_preserved(self) -> None:
        sections = parse_markdown_sections(SIMPLE_MARKDOWN)
        # heading_path holds the heading text; content holds the body
        assert "商品查询" in sections[1]["heading_path"]
        assert "用户可以通过商品名称" in sections[1]["content"]

    def test_no_headings_returns_single_section(self) -> None:
        sections = parse_markdown_sections("plain text without headings")
        assert len(sections) == 1
        assert sections[0]["heading_path"] == []
        assert sections[0]["content"] == "plain text without headings"

    def test_heading_with_inline_formatting(self) -> None:
        """Markdown headings may contain inline code or emphasis — strip them."""
        sections = parse_markdown_sections("# `getProduct` 接口说明\n\n返回产品信息。")
        assert sections[0]["heading_path"] == ["getProduct 接口说明"]
        assert "返回产品信息" in sections[0]["content"]

    def test_skips_headings_in_code_blocks(self) -> None:
        """# inside a fenced code block is NOT a heading."""
        content = """# 真实标题

```
# 这是代码注释，不是标题
print("hello")
```

## 下一节

下一节的内容。"""
        sections = parse_markdown_sections(content)
        headings = [s["heading_path"] for s in sections]
        assert headings == [["真实标题"], ["真实标题", "下一节"]]


# ---------------------------------------------------------------------------
# Text chunking
# ---------------------------------------------------------------------------


class TestChunkText:
    def test_short_text_returns_single_chunk(self) -> None:
        chunks = chunk_text("hello world", chunk_size=500, overlap=50)
        assert len(chunks) == 1
        assert chunks[0] == "hello world"

    def test_long_text_splits_correctly(self) -> None:
        chunks = chunk_text(LONG_TEXT, chunk_size=500, overlap=50)
        assert len(chunks) == 3

    def test_chunk_overlap_content(self) -> None:
        """Overlapping region from chunk N appears at end of chunk N-1 and start of chunk N."""
        text = "0123456789" * 100  # 1000 chars
        chunks = chunk_text(text, chunk_size=300, overlap=50)
        assert len(chunks) >= 3
        # Last 50 chars of chunk 0 should equal first 50 chars of chunk 1
        end_of_first = chunks[0][-50:]
        start_of_second = chunks[1][:50]
        assert end_of_first == start_of_second

    def test_empty_text_returns_empty_list(self) -> None:
        chunks = chunk_text("", chunk_size=500, overlap=50)
        assert chunks == []

    def test_whitespace_only_returns_empty(self) -> None:
        chunks = chunk_text("   \n  \t  ", chunk_size=500, overlap=50)
        assert chunks == []

    def test_overlap_not_larger_than_chunk(self) -> None:
        """When overlap >= chunk_size it should be clamped."""
        chunks = chunk_text("hello world " * 50, chunk_size=100, overlap=150)
        assert len(chunks) > 0
        for c in chunks:
            assert len(c) <= 100


# ---------------------------------------------------------------------------
# Full ingestion pipeline
# ---------------------------------------------------------------------------


class TestIngestDocument:
    def test_returns_chunk_objects(self) -> None:
        chunks = ingest_document(SIMPLE_MARKDOWN, source="test.md")
        assert len(chunks) >= 1
        assert all(isinstance(c, Chunk) for c in chunks)

    def test_chunk_metadata_present(self) -> None:
        chunks = ingest_document(SIMPLE_MARKDOWN, source="test.md")
        for i, c in enumerate(chunks):
            assert c.chunk_id, "chunk_id must not be empty"
            assert c.source_document == "test.md"
            assert c.chunk_index == i
            assert c.char_count > 0
            assert isinstance(c.section_path, list)

    def test_chunk_ids_are_unique(self) -> None:
        chunks = ingest_document(SIMPLE_MARKDOWN, source="test.md")
        ids = [c.chunk_id for c in chunks]
        assert len(ids) == len(set(ids))

    def test_plain_text_ingestion(self) -> None:
        chunks = ingest_document(PLAIN_TEXT, source="notes.txt")
        assert len(chunks) >= 1
        assert chunks[0].source_document == "notes.txt"

    def test_section_path_inherited(self) -> None:
        """Chunks from the same section share the same section_path."""
        # Use a markdown doc with one long section → gets chunked
        long_section = "# 标题\n\n" + ("数据内容。\n" * 300)
        chunks = ingest_document(long_section, source="test.md")
        for c in chunks:
            assert c.section_path == ["标题"]

    def test_custom_chunk_params(self) -> None:
        text = "X" * 800
        chunks = ingest_document(text, source="data.txt", chunk_size=200, overlap=30)
        assert len(chunks) >= 4
        for c in chunks:
            assert c.char_count <= 210  # allow small variance from section boundaries

    def test_empty_content_returns_empty(self) -> None:
        chunks = ingest_document("", source="empty.md")
        assert chunks == []

    def test_whitespace_only_returns_empty(self) -> None:
        chunks = ingest_document("   \n\n  ", source="blank.md")
        assert chunks == []


# ---------------------------------------------------------------------------
# Content hashing and deduplication
# ---------------------------------------------------------------------------


class TestHashContent:
    def test_same_content_same_hash(self) -> None:
        from erp_copilot.retrieval.ingestion import hash_content

        h1 = hash_content("hello world")
        h2 = hash_content("hello world")
        assert h1 == h2

    def test_different_content_different_hash(self) -> None:
        from erp_copilot.retrieval.ingestion import hash_content

        h1 = hash_content("content A")
        h2 = hash_content("content B")
        assert h1 != h2

    def test_hash_is_hex_string_64_chars(self) -> None:
        from erp_copilot.retrieval.ingestion import hash_content

        h = hash_content("test")
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)

    def test_whitespace_matters(self) -> None:
        """Trailing whitespace produces a different hash."""
        from erp_copilot.retrieval.ingestion import hash_content

        h1 = hash_content("text")
        h2 = hash_content("text ")
        assert h1 != h2


class TestFindExistingDocument:
    def test_no_match_returns_none(self) -> None:
        from erp_copilot.infrastructure.database import get_session
        from erp_copilot.retrieval.ingestion import find_existing_document

        session = get_session()
        result = find_existing_document(session, "nonexistent-tenant", "no-such-hash")
        assert result is None

    def test_same_hash_same_tenant_returns_doc(self) -> None:
        from erp_copilot.domain.entities import KnowledgeDocument, Tenant
        from erp_copilot.infrastructure.database import get_session
        from erp_copilot.retrieval.ingestion import find_existing_document

        session = get_session()
        tenant = Tenant(name="Dedup Test", slug="dedup-test")
        session.add(tenant)
        session.flush()

        doc = KnowledgeDocument(
            tenant_id=tenant.id,
            source="dedup.md",
            content_hash="dup-hash-001",
            status="COMPLETED",
            version=1,
        )
        session.add(doc)
        session.commit()

        result = find_existing_document(session, tenant.id, "dup-hash-001")
        assert result is not None
        assert result.id == doc.id

    def test_same_hash_different_tenant_returns_none(self) -> None:
        from erp_copilot.domain.entities import KnowledgeDocument, Tenant
        from erp_copilot.infrastructure.database import get_session
        from erp_copilot.retrieval.ingestion import find_existing_document

        session = get_session()
        t1 = Tenant(name="Dedup T1", slug="dedup-t1")
        t2 = Tenant(name="Dedup T2", slug="dedup-t2")
        session.add_all([t1, t2])
        session.flush()

        doc = KnowledgeDocument(
            tenant_id=t1.id,
            source="cross-tenant.md",
            content_hash="cross-hash",
            status="COMPLETED",
            version=1,
        )
        session.add(doc)
        session.commit()

        # Same hash, different tenant — should NOT match
        result = find_existing_document(session, t2.id, "cross-hash")
        assert result is None
