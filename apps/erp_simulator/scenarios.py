"""Scenario manager for deterministic simulator behavior.

A global scenario state controls how product, supplier, and order
endpoints behave. Switch scenarios via the /scenario endpoints to
simulate different ERP failure modes without changing test data.
"""

from __future__ import annotations

from fastapi import APIRouter

VALID_SCENARIOS = {"happy_path", "stock_insufficient", "supplier_unavailable", "timeout"}

_current_scenario: str = "happy_path"

router = APIRouter(tags=["scenarios"])


def get_scenario() -> str:
    """Return the currently active scenario."""
    return _current_scenario


@router.get("/scenario")
async def get_current_scenario() -> dict[str, str]:
    return {"scenario": _current_scenario}


@router.put("/scenario/{scenario_id}")
async def set_scenario(scenario_id: str) -> dict[str, str]:
    """Activate a named scenario, changing endpoint behavior globally."""
    if scenario_id not in VALID_SCENARIOS:
        from fastapi import HTTPException

        raise HTTPException(
            status_code=422,
            detail=f"Unknown scenario '{scenario_id}'. Valid: {', '.join(sorted(VALID_SCENARIOS))}",
        )
    global _current_scenario
    _current_scenario = scenario_id
    return {"scenario": _current_scenario}
