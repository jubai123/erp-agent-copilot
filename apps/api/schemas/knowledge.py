"""Pydantic schemas for knowledge search API."""

from __future__ import annotations

from pydantic import BaseModel, Field


class KnowledgeSearchRequest(BaseModel):
    """Request body for knowledge search."""

    query: str = Field(..., min_length=1, description="Search query text")
    tenant_id: str = Field(..., min_length=1, description="Tenant identifier")
    top_k: int = Field(default=5, ge=1, le=20, description="Maximum results to return")


class SearchResultItem(BaseModel):
    """A single search result with source metadata."""

    chunk_id: str
    content: str
    source: str
    section_path: list[str]
    score: float


class KnowledgeSearchResponse(BaseModel):
    """Response body for knowledge search."""

    results: list[SearchResultItem]
    citations: str = Field(default="", description="Formatted citation block for LLM prompts")
