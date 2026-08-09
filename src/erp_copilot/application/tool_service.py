"""ToolService — orchestrate OpenAPI import with validation and persistence."""

from __future__ import annotations

from erp_copilot.domain.entities import Tool
from erp_copilot.tools.openapi_importer import OpenAPIImporter, validate_tools
from erp_copilot.tools.registry import ToolRegistry


class ToolService:
    """Application-layer service for tool import workflows.

    Wires together :class:`OpenAPIImporter` (parse + validate) and
    :class:`ToolRegistry` (persist) into a single call.
    """

    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry
        self._importer = OpenAPIImporter()

    def import_from_openapi(self, spec: dict, tenant_id: str) -> list[Tool]:
        """Parse an OpenAPI 3.x spec, validate tools, and persist them.

        Returns the list of imported :class:`Tool` objects (with eagerly
        loaded versions and parameters).
        """
        parsed = self._importer.parse(spec)
        validate_tools(parsed)

        imported: list[Tool] = []
        for pt in parsed:
            params: list[dict[str, object]] = [
                {
                    "name": p.name,
                    "type": p.param_type,
                    "required": p.required,
                    "default": p.default,
                }
                for p in pt.parameters
            ]

            tool = self._registry.register(
                name=pt.name,
                tenant_id=tenant_id,
                description=pt.summary,
                parameters=params if params else None,
            )
            imported.append(tool)

        return imported
