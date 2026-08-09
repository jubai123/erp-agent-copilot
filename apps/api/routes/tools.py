"""Tool import and listing API routes."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from apps.api.schemas.tools import ImportOpenAPIResponse, ToolResponse
from erp_copilot.application.tool_service import ToolService
from erp_copilot.domain.errors import ValidationError
from erp_copilot.tools.registry import ToolRegistry

router = APIRouter(prefix="/v1/tools", tags=["tools"])


def _tool_to_response(tool) -> ToolResponse:
    latest = tool.versions[-1] if tool.versions else None
    return ToolResponse(
        id=tool.id,
        name=tool.name,
        description=tool.description or "",
        current_version=latest.version if latest else 0,
        risk_level=latest.risk_level if latest else "READ",
        is_active=tool.is_active,
    )


@router.post("/import/openapi", response_model=ImportOpenAPIResponse)
async def import_openapi(body: dict) -> ImportOpenAPIResponse:
    """Import tools from an OpenAPI 3.x specification."""
    spec = body.get("spec")
    tenant_id = body.get("tenant_id")

    if not isinstance(spec, dict):
        raise HTTPException(status_code=422, detail="'spec' must be an OpenAPI JSON object")
    if not isinstance(tenant_id, str) or not tenant_id.strip():
        raise HTTPException(status_code=422, detail="'tenant_id' is required")

    service = ToolService(ToolRegistry())

    try:
        tools = service.import_from_openapi(spec, tenant_id)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return ImportOpenAPIResponse(
        imported=[_tool_to_response(t) for t in tools],
    )


@router.get("", response_model=list[ToolResponse])
async def list_tools(tenant_id: str = Query(..., description="Tenant ID")) -> list[ToolResponse]:
    """List all tools for a tenant."""
    registry = ToolRegistry()
    tools = registry.list_by_tenant(tenant_id)
    return [_tool_to_response(t) for t in tools]
