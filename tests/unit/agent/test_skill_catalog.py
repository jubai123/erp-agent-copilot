"""Unit tests for the Agent Skills SKILL.md catalog — Tier2 routing basis.

The 9-skill catalog (datasets/knowledge/agent_skills/*/SKILL.md) is the LLM
constrained-planner's routing vocabulary: each skill's `description` is the
"what + when + trigger words" the planner reasons over, and its structured
frontmatter (tool / required_params / risk_level / required_scope /
success_condition) is the machine-checkable routing metadata. These tests pin
the catalog against the authoritative sources so the data files cannot drift:

- tool set == V6_TOOL_NAMES (candidate_filter) — 9 tools, no more no less
- risk/scope/success_condition == planner.py's WRITE_TOOLS & templates
- required_params == apps/worker/graph_builder WORKER_TOOL_SCHEMAS
- name == directory name, kebab-case (agentskills.io norm)
- frontmatter is strictly validated (Pydantic extra="forbid")
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

# WORKER_TOOL_SCHEMAS pulls Settings() through apps.worker.executor; the
# placeholders are only a backstop — nothing connects to these URLs.
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

from apps.worker.graph_builder import WORKER_TOOL_SCHEMAS  # noqa: E402
from erp_copilot.agent.nodes.verify_results import evaluate_success_condition  # noqa: E402
from erp_copilot.agent.planner import (  # noqa: E402
    _SUCCESS_CONDITION_TEMPLATES,
    DANGEROUS_TOOLS,
    WRITE_TOOLS,
)
from erp_copilot.agent.skill_catalog import AgentSkill, load_agent_skills  # noqa: E402
from erp_copilot.domain.enums import ToolRiskLevel  # noqa: E402
from erp_copilot.tools.candidate_filter import V6_TOOL_NAMES  # noqa: E402

_KEBAB = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
_SKILLS_ROOT = Path(__file__).resolve().parents[3] / "datasets" / "knowledge" / "agent_skills"
_READ_SCOPES = {"product:read", "supplier:read", "order:read"}

# Example arguments per predicate-bearing tool, plus the response data that
# satisfies the formatted condition — proves each predicate is evaluable by
# verify_results (defence layer 4) with realistic shapes.
_SUCCESS_EXAMPLES: dict[str, tuple[dict[str, object], dict[str, object]]] = {
    "getProductByName": ({"name": "苹果"}, {"name": "苹果"}),
    "getProductById": ({"product_id": 4}, {"product_id": 4}),
    "getOrderByOrderId": ({"order_id": "3f2a9c1d"}, {"order_id": "3f2a9c1d"}),
    "createOrder": ({}, {"amount": 100.0}),
}


def _skills() -> list[AgentSkill]:
    return load_agent_skills()


def _skill_for(tool: str) -> AgentSkill:
    return next(s for s in _skills() if s.tool == tool)


class TestCatalog:
    def test_all_nine_tools_covered(self) -> None:
        assert {s.tool for s in _skills()} == set(V6_TOOL_NAMES)

    def test_skill_name_matches_directory_and_is_kebab(self) -> None:
        for skill in _skills():
            assert _KEBAB.fullmatch(skill.name), skill.name
            assert (_SKILLS_ROOT / skill.name / "SKILL.md").is_file()

    def test_success_condition_presence_matches_planner(self) -> None:
        with_condition = {s.tool for s in _skills() if s.success_condition is not None}
        assert with_condition == set(_SUCCESS_CONDITION_TEMPLATES)

    def test_success_conditions_are_valid_predicates(self) -> None:
        for tool, (args, data) in _SUCCESS_EXAMPLES.items():
            skill = _skill_for(tool)
            predicate = skill.success_condition.format(**args)
            ok, error = evaluate_success_condition(predicate, data)
            assert ok is True
            assert error is None

    def test_risk_level_and_scope_align_with_planner(self) -> None:
        for skill in _skills():
            if skill.tool in DANGEROUS_TOOLS:
                assert skill.risk_level == ToolRiskLevel.DANGEROUS
                assert skill.required_scope == "order:write"
            elif skill.tool in WRITE_TOOLS:
                assert skill.risk_level == ToolRiskLevel.WRITE
                assert skill.required_scope == "order:write"
            else:
                assert skill.risk_level == ToolRiskLevel.READ
                assert skill.required_scope in _READ_SCOPES

    def test_required_params_align_with_worker_schemas(self) -> None:
        for skill in _skills():
            assert skill.required_params == WORKER_TOOL_SCHEMAS[skill.tool].required_params


class TestStrictFrontmatter:
    def test_unknown_frontmatter_field_is_rejected(self, tmp_path: Path) -> None:
        skill_dir = tmp_path / "order-create"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\n"
            "name: order-create\n"
            "description: 创建订单\n"
            "tool: createOrder\n"
            "risk_level: write\n"
            "required_scope: order:write\n"
            "unknown_field: x\n"
            "---\n",
            encoding="utf-8",
        )
        with pytest.raises(ValidationError):
            load_agent_skills(tmp_path)
