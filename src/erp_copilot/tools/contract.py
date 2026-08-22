"""Runtime tool-contract authority — single source for tool metadata.

``datasets/knowledge/tool_contracts.yaml`` is the declared authority for the
25 cloud-ERP tool contracts (selection prose, required_params, risk_level,
required_scope, success_condition). Before this module nothing read one source:
the LLM planner prompt inlined SKILL.md descriptions, validate_plan checked
planner.py's hardcoded WRITE_TOOLS/DANGEROUS_TOOLS, graph_builder hardcoded
WORKER_TOOL_SCHEMAS, and the MCP server hand-wrote its tool descriptions — each
mirror drifting from the others. This module is the one place the runtime reads
the contracts; consumers read ContractCache so a seed change propagates instead
of diverging.

The DB registry is the operator-overridable authority: ``seed_tool_contracts``
bootstraps the global tool rows (tenant_id=NULL) once, and ``ContractCache.get``
merges the DB's ``description`` / ``required_params`` over the seed. The risk /
scope / success_condition fields are deliberately *seed-only*: validate_plan
and the deterministic planner derive their write / dangerous / template
constants from the seed (not the DB), so a tenant or operator DB edit cannot
silently move a defence gate. ``_db_contracts`` never reads those columns — the
constraint is structural, not a convention.

A missing DB or not-yet-run migration degrades gracefully to the seed, mirroring
vocabulary/loader.py's ``_db_terms`` (imports deferred, RuntimeError /
ProgrammingError swallowed).
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING

import yaml
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import ProgrammingError

from erp_copilot.domain.enums import ToolRiskLevel

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_CONTRACTS_PATH = _ROOT / "datasets" / "knowledge" / "tool_contracts.yaml"


class ToolContract(BaseModel):
    """One tool's full contract — the fields downstream consumers derive from.

    extra="forbid" turns a seed typo into a hard ValidationError instead of
    silently corrupting the plan gate or the planner prompt.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str
    required_params: list[str] = Field(default_factory=list)
    risk_level: ToolRiskLevel = ToolRiskLevel.READ
    required_scope: str | None = None
    success_condition: str | None = None


def load_seed_contracts() -> dict[str, ToolContract]:
    """Parse the seed YAML into a name-keyed contract map (pure, no cache)."""
    with open(_CONTRACTS_PATH, encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    return {c.name: c for c in (ToolContract(**entry) for entry in raw)}


def write_tool_names(seed: Mapping[str, ToolContract]) -> frozenset[str]:
    """All non-READ tools — the deterministic planner's WRITE_TOOLS set.

    DANGEROUS tools stay inside (planner.py keeps them in WRITE_TOOLS too), so
    validate_plan's READ-downgrade check keeps firing for deletes.
    """
    return frozenset(
        c.name for c in seed.values() if c.risk_level != ToolRiskLevel.READ
    )


def dangerous_tool_names(seed: Mapping[str, ToolContract]) -> frozenset[str]:
    """Delete-class tools — the deterministic planner's DANGEROUS_TOOLS set."""
    return frozenset(
        c.name for c in seed.values() if c.risk_level == ToolRiskLevel.DANGEROUS
    )


def success_condition_templates(seed: Mapping[str, ToolContract]) -> dict[str, str]:
    """Per-tool success predicates — planner's _SUCCESS_CONDITION_TEMPLATES.

    Set-query tools deliberately carry none: an empty supplier/order list is a
    legitimate answer, so verify stays lazy there.
    """
    return {
        c.name: c.success_condition
        for c in seed.values()
        if c.success_condition is not None
    }


def seed_tool_contracts(session: Session) -> int:
    """Bootstrap the global tool rows from the seed — insert-if-missing only.

    Matches by tool name with tenant_id=NULL and skips existing rows, so a
    restart never overwrites an operator's DB edits (the DB is the authority
    for description/required_params once seeded) and never bumps ``version``
    (a new ToolVersion would be a new contract version — wrong for a seed).
    Returns the number of rows inserted.
    """
    from erp_copilot.domain.entities import Parameter, Tool, ToolVersion

    inserted = 0
    for contract in load_seed_contracts().values():
        existing = (
            session.query(Tool)
            .filter_by(name=contract.name, tenant_id=None)
            .one_or_none()
        )
        if existing is not None:
            continue
        version = ToolVersion(
            version=1,
            risk_level=contract.risk_level.value,
            required_scope=contract.required_scope,
            success_condition=contract.success_condition,
        )
        version.parameters = [
            Parameter(name=p, param_type="string", required=True)
            for p in contract.required_params
        ]
        session.add(
            Tool(
                name=contract.name,
                description=contract.description,
                tenant_id=None,
                versions=[version],
            )
        )
        inserted += 1
    session.commit()
    return inserted


def _db_contracts() -> dict[str, ToolContract]:
    """Active global-seed tool rows as mergeable contracts.

    Reads only ``description`` and ``required_params`` (the operator-overridable
    surface); risk / scope / success_condition stay seed-only by construction.
    A missing engine or not-yet-run migration degrades to an empty overlay.
    """
    try:
        from erp_copilot.domain.entities import Tool
        from erp_copilot.infrastructure.database import get_session

        session = get_session()
        try:
            result: dict[str, ToolContract] = {}
            for tool in session.query(Tool).filter(Tool.tenant_id.is_(None)).all():
                if not tool.versions:
                    continue
                version = max(tool.versions, key=lambda v: v.version)
                result[tool.name] = ToolContract(
                    name=tool.name,
                    description=tool.description,
                    required_params=[p.name for p in version.parameters if p.required],
                )
            return result
        finally:
            session.close()
    except (RuntimeError, ProgrammingError):
        return {}


def _merge(
    seed: dict[str, ToolContract],
    db: dict[str, ToolContract],
) -> dict[str, ToolContract]:
    """Overlay the DB's description/required_params onto the seed base."""
    merged = dict(seed)
    for name, dbc in db.items():
        base = seed.get(name)
        if base is not None:
            merged[name] = base.model_copy(
                update={"description": dbc.description, "required_params": dbc.required_params}
            )
        else:
            merged[name] = dbc
    return merged


class ContractCache:
    """Merged seed + DB contract view, cached per process until invalidate().

    Mirrors vocabulary/loader.py's lazy-cache pattern: the first ``get`` loads
    the DB overlay once (or degrades to the seed); ``ensure_contracts_loaded``
    is the startup seam that seeds the DB and drops the cache so the next read
    picks up operator edits.
    """

    def __init__(self) -> None:
        self._seed = load_seed_contracts()
        self._merged: dict[str, ToolContract] | None = None

    def get(self, name: str) -> ToolContract | None:
        """Return the merged contract for *name*, or None. Never raises."""
        if self._merged is None:
            self._merged = _merge(self._seed, _db_contracts())
        return self._merged.get(name)

    def write_tool_names(self) -> frozenset[str]:
        return write_tool_names(self._seed)

    def dangerous_tool_names(self) -> frozenset[str]:
        return dangerous_tool_names(self._seed)

    def success_condition_templates(self) -> dict[str, str]:
        return success_condition_templates(self._seed)

    def ensure_contracts_loaded(self, session: Session) -> int:
        """Seed the DB (insert-if-missing) and drop the cached overlay.

        Returns the number of rows seeded so callers can log whether the
        registry was bootstrapped fresh or was already present.
        """
        inserted = seed_tool_contracts(session)
        self._merged = None
        return inserted

    def invalidate(self) -> None:
        self._merged = None


_CACHE: ContractCache | None = None


def _cache() -> ContractCache:
    global _CACHE
    if _CACHE is None:
        _CACHE = ContractCache()
    return _CACHE


def get_contract(name: str) -> ToolContract | None:
    """Module-level contract lookup — the app's hot-path read."""
    return _cache().get(name)


def ensure_contracts_loaded(session: Session) -> int:
    """Startup seam: bootstrap the tool registry and refresh the runtime view."""
    return _cache().ensure_contracts_loaded(session)


def invalidate_contract_cache() -> None:
    """Drop the module cache so the next read reloads seed + DB."""
    global _CACHE
    _CACHE = None
