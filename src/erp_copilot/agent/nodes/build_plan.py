"""LLM Plan DAG builder node — task 4.6.

Turns the constrained inputs into a :class:`Plan` by prompting the LLM once.
The input is deliberately small: intent-filtered tool candidates (3-8, never
the full registry), L1 active skills (hard constraints) and L2 retrieved
knowledge (reference), assembled in the injection order
System → L1 → L2 → candidates → user query (docs/03 section 4).

The planner only emits a Plan DAG; it never calls a tool. Two deterministic
guards wrap the LLM output: parse_plan_response validates the JSON against the
strict Plan schema (extra="forbid" — a wayward plan cannot silently corrupt the
run), and reject_l1_violations drops steps that break a hard L1 rule. The only
injected dependency is the LLM callable; candidate filtering and skill matching
are deterministic pure functions the node calls directly.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

from pydantic import ValidationError

from erp_copilot.agent.state import AgentState, Plan, PlanStep, RetrievedDocument, StateError
from erp_copilot.retrieval.skill_matcher import match_skills
from erp_copilot.tools.candidate_filter import filter_candidates

# System section of the planner prompt. It must stay above the L1/L2/tool
# sections — build_planner_prompt prepends it and the whole block is the
# planner's system context.
SYSTEM_PROMPT = """你是 ERP 系统的计划器（Planner）。
你的唯一任务是根据用户查询输出一份符合 JSON Schema 的 Plan DAG，绝不能调用任何工具。

Plan 必须遵守：
- 只使用下方 Available Tools 中列出的候选工具，不得发明工具名。
- 每个 Step 包含 step_id、tool_name、arguments、depends_on（依赖的 step_id 列表）。
- argument_sources 记录每个参数的来源：user_query / retrieved / pending。
- L1 Active Skills 是硬约束，任何违反它们的 Step 都必须拒绝生成（例如非法订单状态转换）。
- L2 Retrieved Knowledge 是参考，帮助你补全参数语义。
- risk_level 只取 READ / WRITE / ADMIN 之一。

输出为 JSON 对象：{{"title": str, "steps": [PlanStep, ...]}}。不要输出其他文本。"""

# A state token is uppercase English (CREATED, CONFIRMED, SHIPPED, ...).
_STATE_TOKEN = re.compile(r"[A-Z][A-Z]+")
# "任何状态" in a transition rule means the source/target is unconstrained.
_ANY_STATE = "*"


def build_planner_prompt(
    *,
    query: str,
    active_skills: list[dict[str, Any]],
    retrieved_context: list[RetrievedDocument],
    candidate_tools: list[str],
    system: str = SYSTEM_PROMPT,
) -> str:
    """Assemble the constrained planner prompt in the documented order.

    The order matters: L1 hard constraints come before L2 reference knowledge,
    both before the candidate tools, and the user query lands last so the LLM
    sees the instruction context before the question.
    """
    skills_block = "\n".join(
        f"- {skill.get('skill_id', 'skill')}: {skill.get('content', '')}" for skill in active_skills
    )
    knowledge_block = "\n".join(f"- [{doc.source}] {doc.content}" for doc in retrieved_context)
    tools_block = "\n".join(f"- {tool}" for tool in candidate_tools)
    sections = [
        system,
        "## L1 Active Skills（硬约束）\n" + skills_block,
        "## L2 Retrieved Knowledge（参考）\n" + knowledge_block,
        "## Available Tools（候选）\n" + tools_block,
        "## User Query\n\n" + query,
    ]
    return "\n\n".join(sections)


def parse_plan_response(raw: str) -> Plan:
    """Parse the LLM's JSON (optionally fenced) into a strict Plan.

    Accepts either a bare step list or a {"steps": [...]} object. The strict
    schema rejects unknown fields, so hallucinated tool names or extra keys
    surface as ValidationError instead of silently corrupting the run.
    """
    data = json.loads(_strip_code_fence(raw))
    if isinstance(data, list):
        data = {"steps": data}
    return Plan.model_validate(data)


def _strip_code_fence(raw: str) -> str:
    text = raw.strip()
    lines = text.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _parse_transition_side(side: str) -> str | None:
    if side == "任何状态":
        return _ANY_STATE
    if _STATE_TOKEN.match(side):
        return side
    return None


def _extract_forbidden_transitions(skills: list[dict[str, Any]]) -> set[tuple[str, str]]:
    """Parse "非法转换（必须拒绝）" bullets from L1 skill content.

    Reads the order-state-machine skill's forbidden block into (source, target)
    pairs, where "*" stands for "任何状态". Data-driven — build_plan does not
    hardcode the state machine.
    """
    transitions: set[tuple[str, str]] = set()
    for skill in skills:
        content = skill.get("content", "")
        if "非法转换" not in content:
            continue
        for line in content.splitlines():
            line = line.strip()
            if not line.startswith("-") or "→" not in line:
                continue
            body = line.lstrip("-").strip()
            left, right = (part.strip() for part in body.split("→", 1))
            source = _parse_transition_side(left)
            target = _parse_transition_side(right.split("（")[0].strip())
            if source is not None and target is not None:
                transitions.add((source, target))
    return transitions


def reject_l1_violations(
    plan: Plan,
    active_skills: list[dict[str, Any]],
) -> tuple[Plan, list[StateError]]:
    """Drop steps that violate a hard L1 rule; report them as StateErrors.

    Only "任何状态 → X" rules are decidable at plan time — the current order
    state is unknown until execution, so "X → 任何状态" transitions are left to
    the verify_results layer (defence layer 4).
    """
    forbidden = _extract_forbidden_transitions(active_skills)
    if not forbidden:
        return plan, []

    kept: list[PlanStep] = []
    errors: list[StateError] = []
    for step in plan.steps:
        arguments = step.arguments if isinstance(step.arguments, dict) else {}
        target = arguments.get("status")
        if isinstance(target, str) and any(
            source == _ANY_STATE and target.upper() == dst for source, dst in forbidden
        ):
            errors.append(
                StateError(
                    code="L1_VIOLATION",
                    message=f"step {step.step_id} 违反 L1 硬约束：任何状态 → {target} 为非法转换",
                    step_id=step.step_id,
                )
            )
        else:
            kept.append(step)

    if not errors:
        return plan, []
    return Plan(steps=kept, title=plan.title), errors


def build_plan_node(
    *,
    llm_complete: Callable[[str], str],
    available_tools: frozenset[str] | set[str] | None = None,
    system: str = SYSTEM_PROMPT,
) -> Callable[[AgentState], dict[str, Any]]:
    """Build the build_plan LangGraph node with an injected LLM callable.

    The app wires *llm_complete* to a real provider (OpenAI-compatible);
    tests substitute a stub. Candidate filtering and L1 skill matching are
    deterministic pure functions called directly here.
    """

    def plan_node(state: AgentState) -> dict[str, Any]:
        domain = state.intent.domain if state.intent else ""
        action = state.intent.action if state.intent else ""
        skills = match_skills(domain, action)
        candidates = filter_candidates(domain, action, available_tools)
        prompt = build_planner_prompt(
            query=state.query,
            active_skills=skills,
            retrieved_context=state.retrieved_context,
            candidate_tools=candidates,
            system=system,
        )
        try:
            plan = parse_plan_response(llm_complete(prompt))
        except (json.JSONDecodeError, ValidationError) as exc:
            return {
                "plan": None,
                "candidate_tools": candidates,
                "active_skills": skills,
                "errors": [
                    StateError(
                        code="PLAN_PARSE_ERROR", message=f"LLM 输出无法解析为合法 Plan: {exc}"
                    )
                ],
            }

        filtered_plan, l1_errors = reject_l1_violations(plan, skills)
        updates: dict[str, Any] = {
            "plan": filtered_plan,
            "candidate_tools": candidates,
            "active_skills": skills,
        }
        if l1_errors:
            updates["errors"] = l1_errors
        return updates

    return plan_node
