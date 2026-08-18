"""Agent Skills SKILL.md catalog loader — Tier2 LLM constrained-planning basis.

The 9 skills (datasets/knowledge/agent_skills/<skill-name>/SKILL.md) give the
LLM planner a routing vocabulary the deterministic planner does not have: each
skill's `description` is "what + when + trigger words", and its structured
frontmatter (tool / required_params / risk_level / required_scope /
success_condition) is machine-checkable routing metadata. This module is the
read side of that catalog — the candidate skill set a Tier2 planner routes
over. The risk/scope/success_condition values mirror planner.py so the data
cannot drift from the deterministic planner's authority; the test suite pins
that alignment.

Frontmatter is parsed as YAML and validated strictly by Pydantic
(extra="forbid" rejects unknown fields), and a skill's `name` MUST equal its
directory name per the agentskills.io norm — both caught at load time.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from erp_copilot.domain.enums import ToolRiskLevel

_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_SKILLS_DIR = _ROOT / "datasets" / "knowledge" / "agent_skills"


class AgentSkill(BaseModel):
    """One SKILL.md's frontmatter, parsed and validated.

    extra="forbid" turns frontmatter typos into a hard ValidationError instead
    of silently ignoring them — the LLM routes on these fields, so a misspelled
    key silently degrades routing quality.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str
    tool: str
    required_params: list[str] = Field(default_factory=list)
    risk_level: ToolRiskLevel = ToolRiskLevel.READ
    required_scope: str | None = None
    success_condition: str | None = None


def _parse_skill(path: Path) -> AgentSkill:
    """Parse one SKILL.md: split frontmatter, validate, pin name == dir name."""
    text = path.read_text(encoding="utf-8")
    parts = text.split("---", 2)
    if len(parts) < 3:
        raise ValueError(f"{path}: missing YAML frontmatter (expected `---` delimiters)")
    raw = yaml.safe_load(parts[1])
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: frontmatter must be a YAML mapping")
    skill = AgentSkill(**raw)
    if skill.name != path.parent.name:
        raise ValueError(
            f"{path}: skill name {skill.name!r} must match directory {path.parent.name!r}"
        )
    return skill


def load_agent_skills(root: Path | None = None) -> list[AgentSkill]:
    """Discover and parse every skill SKILL.md under *root*, sorted by name.

    *root* defaults to datasets/knowledge/agent_skills; a non-default root is
    how tests feed fixture catalogs. The sort makes the returned list
    deterministic — the routing basis must not reorder between loads.
    """
    base = root or _SKILLS_DIR
    skills = [_parse_skill(p) for p in sorted(base.glob("*/SKILL.md"))]
    return sorted(skills, key=lambda s: s.name)
