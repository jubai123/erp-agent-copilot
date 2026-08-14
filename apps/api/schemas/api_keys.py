"""Pydantic schemas for API-key management (create / list / revoke)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class CreateApiKeyRequest(BaseModel):
    # Lengths mirror the DB columns (users.id is a 36-char UUID, ApiKey.label
    # is String(255)); exceeding them would otherwise surface as a 500.
    user_id: str = Field(min_length=1, max_length=36, description="User the key authenticates as")
    label: str = Field(
        default="",
        max_length=255,
        description="Free-form label for the key's purpose",
    )


class ApiKeyCreatedResponse(BaseModel):
    id: int
    tenant_id: str
    user_id: str
    label: str
    created_at: datetime
    # The plaintext key, returned exactly once on creation; never persisted.
    api_key: str


class ApiKeySummaryResponse(BaseModel):
    id: int
    tenant_id: str
    user_id: str
    label: str
    created_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None
