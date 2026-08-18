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
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from erp_copilot.agent.planner import WRITE_TOOLS, stamp_idempotency_keys
from erp_copilot.agent.skill_catalog import AgentSkill, load_agent_skills
from erp_copilot.agent.state import AgentState, Plan, PlanStep, RetrievedDocument, StateError
from erp_copilot.retrieval.skill_matcher import match_skills
from erp_copilot.tools.candidate_filter import filter_candidates

if TYPE_CHECKING:
    from erp_copilot.agent.nodes.validate_plan import ToolSpec

# System section of the planner prompt. It must stay above the L1/L2/tool
# sections — build_planner_prompt prepends it and the whole block is the
# planner's system context.
SYSTEM_PROMPT = """你是 ERP 系统的计划器（Planner）。
你的唯一任务是根据用户查询输出一份符合 JSON Schema 的 Plan DAG，绝不能调用任何工具。

Plan 必须遵守：
- 只使用下方 Available Tools 中列出的候选工具，不得发明工具名。
- 每个 Step 包含 step_id、tool_name、arguments、depends_on（依赖的 step_id 列表）。
- arguments 直接给出每个参数的具体值（从用户查询中提取）。
- 跨步骤参数：argument_sources 写 {"参数名": "step:{step_id}"}，并加入 depends_on。
- L1 Active Skills 是硬约束，任何违反它们的 Step 都必须拒绝生成（例如非法订单状态转换）。
- L2 Retrieved Knowledge 是参考，帮助你补全参数语义。
- risk_level 只取 READ / WRITE / ADMIN 之一。
- WRITE / DANGEROUS 步骤必须提供非空 fallback（补偿/回退说明，例如"取消新建订单以补偿"）。
- 参数名必须与 Available Tools 中标注的必填参数名完全一致。
- 不要输出 idempotency_key：运行时按 run_id 自动生成。

输出为 JSON 对象：{{"title": str, "steps": [PlanStep, ...]}}。不要输出其他文本。"""

# Fixed policy rules appended to the prompt (A/B-validated against planning_50,
# 2026-08-16: 64% -> 100% set-exact). The risk table derives from
# planner.WRITE_TOOLS — the same set validate_plan's RISK_DOWNGRADE gate checks
# — so the LLM is told the policy the deterministic guard enforces. Prompt is
# advisory (cut mislabeling from 89% to ~5%); the guard stays authoritative.
SYSTEM_PROMPT = (
    SYSTEM_PROMPT
    + "\n\n固定规则（违反即计划不合法）:\n"
    + "- 以下工具的 risk_level 固定为 WRITE，严禁标成 READ："
    + "、".join(sorted(WRITE_TOOLS))
    + "。\n"
    + "- 供应商查询二选一：查询给出配送区域时只用 querySuppliersByDeliveryRegion；"
    "仅询问可用性时用 getSupplierByStatus；不得同时选。\n"
    + "- 只选完成任务所必需的工具，不要添加多余的查询步骤。\n"
    + "- 跨步骤数据引用：argument_sources 里引用的每个 step 必须同时列入该 step "
    "的 depends_on，漏掉任一引用即计划不合法。"
)

# A state token is uppercase English (CREATED, CONFIRMED, SHIPPED, ...).
_STATE_TOKEN = re.compile(r"[A-Z][A-Z]+")
# A literal "step:{id}" argument value — the LLM's way of writing a dependency
# that the executor only honours when it sits in argument_sources.
_STEP_REF_RE = re.compile(r"^step:[A-Za-z0-9]+$")
# "任何状态" in a transition rule means the source/target is unconstrained.
_ANY_STATE = "*"


def _render_candidate_tool(
    name: str,
    spec: ToolSpec | None,
    skill: AgentSkill | None = None,
) -> str:
    """Render one candidate tool with its routing description and required params.

    A bare tool name leaves the LLM guessing both the argument names (it invented
    ``product_name`` for getProductByName — a MISSING_REQUIRED_ARG gate failure)
    and *when* the tool applies. The skill catalog (Tier2 routing basis) fixes
    the second: its description — what the tool does, when to use it, trigger
    words — is inlined next to the name so the LLM reasons over "which tool for
    this query" at the point of choice. Without a description the line keeps the
    bare name + required params format (backward compatible).
    """
    params = (
        f"(必填参数: {', '.join(spec.required_params)})"
        if spec is not None and spec.required_params
        else ""
    )
    if skill is not None and skill.description:
        return f"- {name}: {skill.description} {params}".rstrip()
    return f"- {name}{params}"


def build_planner_prompt(
    *,
    query: str,
    active_skills: list[dict[str, Any]],
    retrieved_context: list[RetrievedDocument],
    candidate_tools: list[str],
    system: str = SYSTEM_PROMPT,
    tool_schemas: dict[str, ToolSpec] | None = None,
    skill_catalog: dict[str, AgentSkill] | None = None,
) -> str:
    """Assemble the constrained planner prompt in the documented order.

    The order matters: L1 hard constraints come before L2 reference knowledge,
    both before the candidate tools, and the user query lands last so the LLM
    sees the instruction context before the question.

    When *tool_schemas* is given, each candidate tool renders with its required
    parameter names so the LLM emits arguments that pass validate_plan's
    MISSING_REQUIRED_ARG gate; without it tools render as bare names (backward
    compatible for callers that only pass names). When *skill_catalog* is given,
    each candidate tool also renders its skill description (the Tier2 routing
    basis) inline; a candidate the catalog does not cover falls back to the
    bare format.
    """
    skills_block = "\n".join(
        f"- {skill.get('skill_id', 'skill')}: {skill.get('content', '')}" for skill in active_skills
    )
    knowledge_block = "\n".join(f"- [{doc.source}] {doc.content}" for doc in retrieved_context)
    if tool_schemas or skill_catalog:

        def _render(tool: str) -> str:
            spec = tool_schemas.get(tool) if tool_schemas else None
            skill = skill_catalog.get(tool) if skill_catalog else None
            return _render_candidate_tool(tool, spec, skill)

        tools_block = "\n".join(_render(tool) for tool in candidate_tools)
    else:
        tools_block = "\n".join(f"- {tool}" for tool in candidate_tools)
    sections = [
        system,
        "## L1 Active Skills（硬约束）\n" + skills_block,
        "## L2 Retrieved Knowledge（参考）\n" + knowledge_block,
        "## Available Tools（候选）\n" + tools_block,
        "## User Query\n\n" + query,
    ]
    return "\n\n".join(sections)


def _promote_step_ref_values(plan: Plan) -> Plan:
    """Promote literal ``"step:{id}"`` argument values into argument_sources.

    LLMs often emit a dependency reference as a literal argument value
    (``{"product_id": "step:1"}``) with empty argument_sources, but
    resolve_arguments only resolves refs listed in argument_sources — the
    literal would otherwise be sent to the tool verbatim (``productId="step:1"``
    → cloud 302). Any ``"step:{id}"``-shaped value whose param is not already
    sourced is promoted to argument_sources; the placeholder stays in arguments
    because resolution overwrites it at execution time.
    """
    changed = False
    steps: list[PlanStep] = []
    for step in plan.steps:
        promoted = {
            name: value
            for name, value in step.arguments.items()
            if isinstance(value, str)
            and _STEP_REF_RE.match(value)
            and name not in step.argument_sources
        }
        if promoted:
            changed = True
            step = step.model_copy(
                update={"argument_sources": {**step.argument_sources, **promoted}}
            )
        steps.append(step)
    if not changed:
        return plan
    return plan.model_copy(update={"steps": steps})


def parse_plan_response(raw: str) -> Plan:
    """Parse the LLM's JSON (optionally fenced) into a strict Plan.

    Accepts either a bare step list or a {"steps": [...]} object. The strict
    schema rejects unknown fields, so hallucinated tool names or extra keys
    surface as ValidationError instead of silently corrupting the run. Numeric
    step_id/depends_on values (a common LLM type slip) are normalized to
    strings so a numerically consistent DAG still validates.
    """
    data = json.loads(_strip_code_fence(raw))
    if isinstance(data, list):
        data = {"steps": data}
    for step in data.get("steps", []):
        if isinstance(step.get("step_id"), int):
            step["step_id"] = str(step["step_id"])
        if isinstance(step.get("depends_on"), list):
            step["depends_on"] = [str(dep) for dep in step["depends_on"]]
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


_SKILL_CATALOG_CACHE: dict[str, AgentSkill] | None = None


def _get_skill_catalog() -> dict[str, AgentSkill]:
    """Load the on-disk skill catalog once, keyed by tool name.

    Mirrors skill_matcher's module-level cache so the per-request build_plan
    node never re-reads the SKILL.md files. Keyed by tool (not skill name) so
    the prompt renders a description for every candidate tool in O(1).
    """
    global _SKILL_CATALOG_CACHE
    if _SKILL_CATALOG_CACHE is None:
        _SKILL_CATALOG_CACHE = {skill.tool: skill for skill in load_agent_skills()}
    return _SKILL_CATALOG_CACHE


def build_plan_node(
    *,
    llm_complete: Callable[[str], str],
    available_tools: frozenset[str] | set[str] | None = None,
    system: str = SYSTEM_PROMPT,
    tool_schemas: dict[str, ToolSpec] | None = None,
    skill_catalog: dict[str, AgentSkill] | None = None,
) -> Callable[[AgentState], dict[str, Any]]:
    """Build the build_plan LangGraph node with an injected LLM callable.

    The app wires *llm_complete* to a real provider (OpenAI-compatible);
    tests substitute a stub. Candidate filtering and L1 skill matching are
    deterministic pure functions called directly here.

    *tool_schemas* mirrors the validator's ToolSpec map so the prompt can state
    each candidate tool's required params and the node can stamp deterministic
    idempotency keys on WRITE/DANGEROUS steps — matching the deterministic
    planner's at-most-once contract (execute_steps only routes keyed steps
    through the IdempotencyStore).

    *skill_catalog* (tool → AgentSkill) feeds the prompt's inline tool
    descriptions — the Tier2 routing basis. It defaults to the on-disk catalog
    (datasets/knowledge/agent_skills) so the interactive path and the LLM
    planner eval both render real descriptions; an explicit dict lets tests
    inject a controlled catalog.
    """
    if skill_catalog is None:
        skill_catalog = _get_skill_catalog()

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
            tool_schemas=tool_schemas,
            skill_catalog=skill_catalog,
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
        filtered_plan = _promote_step_ref_values(filtered_plan)
        filtered_plan = stamp_idempotency_keys(filtered_plan, state.run_id)
        updates: dict[str, Any] = {
            "plan": filtered_plan,
            "candidate_tools": candidates,
            "active_skills": skills,
        }
        if l1_errors:
            updates["errors"] = l1_errors
        return updates

    return plan_node
