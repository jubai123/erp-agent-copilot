"""Pydantic schemas for tool import and listing."""

from __future__ import annotations

from pydantic import BaseModel


class ToolParameterResponse(BaseModel):
    name: str
    type: str
    required: bool


class ToolVersionResponse(BaseModel):
    version: int
    risk_level: str
    parameters: list[ToolParameterResponse] = []


class ToolResponse(BaseModel):
    id: str
    name: str
    description: str
    current_version: int
    risk_level: str
    is_active: bool


class ImportOpenAPIResponse(BaseModel):
    imported: list[ToolResponse]
