"""Typed AgentState — the strong-typed runtime state flowing through LangGraph.

Task 4.1. The state is the single object every node reads and writes. It is
JSON-serializable so it can be checkpointed (task 4.12), and it validates
strictly so a wayward LLM plan (task 4.6) cannot silently corrupt the run.

The runtime status machine (AgentStatus) is separate from the persistence
RunStatus in domain.enums — the agent runtime runs through its own lifecycle
and maps onto the stored Run when checkpointed (task 4.12).
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from erp_copilot.domain.enums import StepStatus, ToolRiskLevel

# LLM/OpenAPI risk labels (READ/WRITE/ADMIN) normalised onto the domain
# enum. ADMIN is the top tier — the domain's DANGEROUS.
_RISK_ALIASES: dict[str, ToolRiskLevel] = {
    "read": ToolRiskLevel.READ,
    "write": ToolRiskLevel.WRITE,
    "dangerous": ToolRiskLevel.DANGEROUS,
    "admin": ToolRiskLevel.DANGEROUS,
}


class _StrictModel(BaseModel):
    """Reject unknown fields so typos cannot silently change run behaviour."""

    model_config = ConfigDict(extra="forbid")


class AgentStatus(StrEnum):
    """Runtime lifecycle of an agent run (docs/03 section 3).

    The happy path is QUEUED → PLANNING → WAITING_APPROVAL → EXECUTING →
    VERIFYING → SUCCEEDED. The abnormal branches are RETRYING, REPLANNING,
    FAILED, CANCELLED and EXPIRED.
    """

    QUEUED = "queued"
    PLANNING = "planning"
    WAITING_APPROVAL = "waiting_approval"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    RETRYING = "retrying"
    REPLANNING = "replanning"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class IntentClassification(_StrictModel):
    """Output of the classify_intent node: task type, business domain, risk."""

    domain: str
    action: str
    risk_level: ToolRiskLevel = ToolRiskLevel.READ
    entities: dict[str, Any] = Field(default_factory=dict)


class RetrievedDocument(_StrictModel):
    """A single L2 retrieval hit with citation metadata."""

    content: str
    source: str
    section_path: list[str] = Field(default_factory=list)
    score: float | None = None


class PlanStep(_StrictModel):
    """One step in the plan DAG (docs/03 section 5 PlanStep contract).

    The Planner only ever emits steps like this; it never calls a tool
    directly. risk_level is normalised from the READ/WRITE/ADMIN labels the
    model may produce onto the domain ToolRiskLevel enum.
    """

    step_id: str
    tool_name: str
    depends_on: list[str] = Field(default_factory=list)
    description: str = ""
    arguments: dict[str, Any] = Field(default_factory=dict)
    argument_sources: dict[str, str] = Field(default_factory=dict)
    risk_level: ToolRiskLevel = ToolRiskLevel.READ
    required_scope: str | None = None
    requires_approval: bool = False
    timeout_s: int | None = None
    max_retries: int = 0
    idempotency_key: str | None = None
    expected_output_schema: str | None = None
    success_condition: str | None = None
    fallback: str | None = None

    @field_validator("risk_level", mode="before")
    @classmethod
    def _normalize_risk_level(cls, value: Any) -> Any:
        if isinstance(value, str):
            return _RISK_ALIASES.get(value.lower(), value)
        return value


class Plan(_StrictModel):
    """An ordered list of steps forming a DAG.

    validate_plan (task 4.7) is the authority on whether the DAG is legal;
    this model only guarantees the shape.
    """

    steps: list[PlanStep]
    title: str = ""


class StepResult(_StrictModel):
    """Outcome of executing one plan step.

    Keeps the error fields flat (copied from ToolResult) so the verifier and
    recovery nodes can branch without digging into nested dicts.
    """

    step_id: str
    status: StepStatus
    data: dict[str, Any] | None = None
    error_code: str | None = None
    error_message: str | None = None
    is_retryable: bool = False
    started_at: datetime | None = None
    finished_at: datetime | None = None


class StateError(_StrictModel):
    """A structured error accumulated during a run (docs/03 section 9 codes)."""

    code: str
    message: str
    step_id: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class PolicyDecision(StrEnum):
    """Outcome of the policy_check gate for one plan step (task 4.8).

    ALLOW proceeds to execution; REQUIRE_APPROVAL blocks until a human
    approves (Phase 5 / task 5.2); DENY is final — the user's scopes do not
    cover the step, and approval cannot grant scope.
    """

    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


class ApprovalStatus(StrEnum):
    """Lifecycle of one approval request (task 5.2).

    The request_approval node creates PENDING requests and pauses the run.
    The approval decision API (task 5.2) flips a request to APPROVED or DENIED
    before the run is resumed; DENIED-step skipping lands with that same task.
    """

    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"


class ApprovalRequest(_StrictModel):
    """A step awaiting (or granted) human approval, recorded for audit.

    Carried in AgentState.approvals. It mirrors the step's tool/risk context
    so a human can decide without digging into the plan; the fields that name
    the decider and the timestamp arrive with the decision API (task 5.2).
    """

    step_id: str
    tool_name: str
    description: str = ""
    risk_level: ToolRiskLevel = ToolRiskLevel.READ
    required_scope: str | None = None
    status: ApprovalStatus = ApprovalStatus.PENDING


class PlanValidation(_StrictModel):
    """Outcome of validate_plan: legality plus scheduling hints (task 4.7).

    is_valid is the deterministic gate the graph routes on; topological_order
    and parallel_groups tell execute_ready_steps (tasks 4.9-4.10) which steps
    are ready and which READ steps may run concurrently.
    """

    is_valid: bool
    errors: list[StateError] = Field(default_factory=list)
    topological_order: list[str] = Field(default_factory=list)
    parallel_groups: list[list[str]] = Field(default_factory=list)


class AgentState(_StrictModel):
    """Strong-typed runtime state shared across LangGraph nodes.

    Only small results live in the state; large logs and tool responses are
    offloaded to artifacts and referenced by URI (docs/03 section 2). Each
    field maps to a node or subsystem built in Phase 4.
    """

    run_id: str
    tenant_id: str
    user_id: str | None = None
    query: str
    status: AgentStatus = AgentStatus.QUEUED

    intent: IntentClassification | None = None
    active_skills: list[dict[str, Any]] = Field(default_factory=list)
    retrieved_context: list[RetrievedDocument] = Field(default_factory=list)
    candidate_tools: list[str] = Field(default_factory=list)

    plan: Plan | None = None
    plan_validation: PlanValidation | None = None
    policy_decisions: dict[str, PolicyDecision] = Field(default_factory=dict)
    approvals: list[ApprovalRequest] = Field(default_factory=list)
    current_step_id: str | None = None
    step_results: dict[str, StepResult] = Field(default_factory=dict)
    artifacts: dict[str, str] = Field(default_factory=dict)
    working_memory: dict[str, Any] = Field(default_factory=dict)

    retry_count: int = 0
    replan_count: int = 0
    errors: list[StateError] = Field(default_factory=list)

    started_at: datetime | None = None
    deadline_at: datetime | None = None
