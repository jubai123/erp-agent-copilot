"""ToolRegistry - register, query, and version tools."""

from __future__ import annotations

from sqlalchemy.orm import joinedload

from erp_copilot.domain.entities import Parameter, Tool, ToolVersion
from erp_copilot.infrastructure.database import get_session


class ToolRegistry:
    """Register and query tools with immutable version management.

    Every call to :meth:`register` with the same (name, tenant_id)
    creates a new :class:`ToolVersion` — old versions are preserved
    so historical runs remain reproducible.
    """

    def register(
        self,
        name: str,
        tenant_id: str,
        description: str = "",
        risk_level: str = "READ",
        source_url: str = "",
        parameters: list[dict[str, object]] | None = None,
    ) -> Tool:
        """Register a tool, creating a new version if it already exists."""
        session = get_session()
        try:
            tool = (
                session.query(Tool)
                .filter_by(name=name, tenant_id=tenant_id)
                .first()
            )

            if tool is None:
                tool = Tool(
                    tenant_id=tenant_id,
                    name=name,
                    description=description,
                )
                session.add(tool)
                session.flush()

            next_version = (
                session.query(ToolVersion)
                .filter_by(tool_id=tool.id)
                .count()
                + 1
            )

            version = ToolVersion(
                tool_id=tool.id,
                version=next_version,
                risk_level=risk_level,
                source_url=source_url,
            )
            session.add(version)
            session.flush()

            if parameters:
                for param in parameters:
                    session.add(
                        Parameter(
                            tool_version_id=version.id,
                            name=str(param["name"]),
                            param_type=str(param["type"]),
                            required=bool(param.get("required", False)),
                            default_value=str(param["default"])
                            if "default" in param
                            else None,
                        )
                    )
                session.flush()

            session.commit()

            # Eager-load versions + parameters for the caller
            return (
                session.query(Tool)
                .options(
                    joinedload(Tool.versions)
                    .joinedload(ToolVersion.parameters)
                )
                .filter_by(id=tool.id)
                .one()
            )
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def get_by_name(self, name: str, tenant_id: str) -> Tool | None:
        """Return the tool with its versions, or None."""
        session = get_session()
        try:
            return (
                session.query(Tool)
                .options(
                    joinedload(Tool.versions).joinedload(ToolVersion.parameters)
                )
                .filter_by(name=name, tenant_id=tenant_id)
                .first()
            )
        finally:
            session.close()

    def list_by_tenant(self, tenant_id: str) -> list[Tool]:
        """Return all tools for a tenant with versions eagerly loaded."""
        session = get_session()
        try:
            return (
                session.query(Tool)
                .options(
                    joinedload(Tool.versions).joinedload(ToolVersion.parameters)
                )
                .filter_by(tenant_id=tenant_id, is_active=True)
                .order_by(Tool.name)
                .all()
            )
        finally:
            session.close()

    def get_version(self, tool_id: str, version: int) -> ToolVersion | None:
        """Return a specific version of a tool, or None."""
        session = get_session()
        try:
            return (
                session.query(ToolVersion)
                .options(joinedload(ToolVersion.parameters))
                .filter_by(tool_id=tool_id, version=version)
                .first()
            )
        finally:
            session.close()
