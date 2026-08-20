"""Core domain entities: Tenant, User, Role, RoleScope.

Multi-tenant data model with RBAC (Role-Based Access Control).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column, relationship

from erp_copilot.infrastructure.database import Base


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _new_uuid() -> str:
    return str(uuid.uuid4())


class Tenant(Base):
    """An organisation whose data is isolated from other tenants."""

    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    users: Mapped[list[User]] = relationship(
        "User", back_populates="tenant", cascade="all, delete-orphan"
    )
    roles: Mapped[list[Role]] = relationship(
        "Role", back_populates="tenant", cascade="all, delete-orphan"
    )
    tools: Mapped[list[Tool]] = relationship(
        "Tool", back_populates="tenant", cascade="all, delete-orphan"
    )
    runs: Mapped[list[Run]] = relationship(
        "Run", back_populates="tenant", cascade="all, delete-orphan"
    )
    knowledge_documents: Mapped[list[KnowledgeDocument]] = relationship(
        "KnowledgeDocument", back_populates="tenant", cascade="all, delete-orphan"
    )
    api_keys: Mapped[list[ApiKey]] = relationship("ApiKey", back_populates="tenant")


class User(Base):
    """A user that belongs to exactly one tenant."""

    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("tenant_id", "email", name="uq_users_tenant_email"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), default="")
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    tenant: Mapped[Tenant] = relationship("Tenant", back_populates="users")
    roles: Mapped[list[Role]] = relationship(
        "Role",
        secondary="user_roles",
        back_populates="users",
    )
    runs: Mapped[list[Run]] = relationship("Run", back_populates="user")


class Role(Base):
    """A named role within a tenant, granting a set of scopes."""

    __tablename__ = "roles"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    tenant: Mapped[Tenant] = relationship("Tenant", back_populates="roles")
    scopes: Mapped[list[RoleScope]] = relationship(
        "RoleScope", back_populates="role", cascade="all, delete-orphan"
    )
    users: Mapped[list[User]] = relationship(
        "User",
        secondary="user_roles",
        back_populates="roles",
    )


class RoleScope(Base):
    """A permission scope attached to a role (resource + action pair)."""

    __tablename__ = "role_scopes"
    __table_args__ = (
        UniqueConstraint("role_id", "resource", "action", name="uq_role_scopes_pair"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    role_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("roles.id", ondelete="CASCADE"), nullable=False
    )
    resource: Mapped[str] = mapped_column(String(100), nullable=False)
    action: Mapped[str] = mapped_column(String(50), nullable=False)

    role: Mapped[Role] = relationship("Role", back_populates="scopes")


class UserRole(Base):
    """Association table for many-to-many User <-> Role."""

    __tablename__ = "user_roles"
    __table_args__ = (UniqueConstraint("user_id", "role_id", name="uq_user_roles_pair"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    role_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("roles.id", ondelete="CASCADE"), nullable=False
    )


class Tool(Base):
    """A registered tool that can be executed by the agent."""

    __tablename__ = "tools"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(String(1000), default="")
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    tenant: Mapped[Tenant] = relationship("Tenant", back_populates="tools")
    versions: Mapped[list[ToolVersion]] = relationship(
        "ToolVersion",
        back_populates="tool",
        cascade="all, delete-orphan",
        order_by="ToolVersion.version",
    )


class ToolVersion(Base):
    """An immutable version of a tool definition."""

    __tablename__ = "tool_versions"
    __table_args__ = (UniqueConstraint("tool_id", "version", name="uq_tool_versions_tool_version"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tool_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tools.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(nullable=False)
    risk_level: Mapped[str] = mapped_column(String(20), default="READ")
    source_url: Mapped[str] = mapped_column(String(2048), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    tool: Mapped[Tool] = relationship("Tool", back_populates="versions")
    parameters: Mapped[list[Parameter]] = relationship(
        "Parameter", back_populates="tool_version", cascade="all, delete-orphan"
    )


class Parameter(Base):
    """A parameter definition for a tool version."""

    __tablename__ = "parameters"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tool_version_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("tool_versions.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    param_type: Mapped[str] = mapped_column(String(50), nullable=False)
    required: Mapped[bool] = mapped_column(default=False)
    default_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    description: Mapped[str] = mapped_column(String(500), default="")

    tool_version: Mapped[ToolVersion] = relationship("ToolVersion", back_populates="parameters")


class AgentCheckpoint(Base):
    """Point-in-time snapshot of an agent run's state after one node.

    Written by the CheckpointSaver after every graph node (task 4.12); the
    state_json column carries the full serialized AgentState so a crashed
    worker can resume from the latest row. run_status is the persistence view
    (RunStatus) mapped from the runtime AgentStatus. sequence is the
    insertion-ordered key load_latest sorts on — created_at alone can tie when
    nodes save within the same clock tick, and the v4-UUID id is random.
    """

    __tablename__ = "agent_checkpoints"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    run_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(36), nullable=False)
    node_name: Mapped[str] = mapped_column(String(50), nullable=False)
    run_status: Mapped[str] = mapped_column(String(50), nullable=False)
    state_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class Run(Base):
    """A single agent execution run with lifecycle state."""

    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    title: Mapped[str] = mapped_column(String(500), default="")
    status: Mapped[str] = mapped_column(String(50), default="QUEUED")
    version: Mapped[int] = mapped_column(default=1)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Failure detail (docs/03 §9 error codes) — populated when the run fails
    # beyond auto-recovery and enters the human intervention queue (task 5.9).
    failure_code: Mapped[str | None] = mapped_column(String(50), nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    suggested_action: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    tenant: Mapped[Tenant] = relationship("Tenant", back_populates="runs")
    user: Mapped[User | None] = relationship("User", back_populates="runs")
    steps: Mapped[list[RunStep]] = relationship(
        "RunStep", back_populates="run", cascade="all, delete-orphan"
    )
    events: Mapped[list[RunEvent]] = relationship(
        "RunEvent", back_populates="run", cascade="all, delete-orphan"
    )


class RunStep(Base):
    """A single step within a run, recording input, output, and timing."""

    __tablename__ = "run_steps"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
    )
    step_index: Mapped[int] = mapped_column(nullable=False)
    step_type: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = mapped_column(String(50), default="PENDING")
    tool_version_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("tool_versions.id", ondelete="SET NULL"), nullable=True
    )
    input: Mapped[str] = mapped_column(Text, default="")
    output: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    run: Mapped[Run] = relationship("Run", back_populates="steps")
    tool_version: Mapped[ToolVersion | None] = relationship("ToolVersion")


class RunEvent(Base):
    """Append-only event log entry for a run."""

    __tablename__ = "run_events"
    __table_args__ = (UniqueConstraint("run_id", "sequence", name="uq_run_events_run_sequence"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(nullable=False)
    event_type: Mapped[str] = mapped_column(String(50), nullable=False)
    payload: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    run: Mapped[Run] = relationship("Run", back_populates="events")


class SecurityEvent(Base):
    """A recorded security incident (docs/07 security_events).

    Written by guards (SSRF, injection, redaction) when they intercept an
    attack; layer names the detector and disposition the outcome. run_id
    links the incident to the run it happened in, when one is present.
    """

    __tablename__ = "security_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    attack_type: Mapped[str] = mapped_column(String(50), nullable=False)
    layer: Mapped[str] = mapped_column(String(50), nullable=False)
    severity: Mapped[str] = mapped_column(String(20), default="HIGH")
    input_summary: Mapped[str] = mapped_column(Text, default="")
    disposition: Mapped[str] = mapped_column(String(20), default="blocked")
    run_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("runs.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    run: Mapped[Run | None] = relationship("Run")


class AuditLog(Base):
    """Append-only record of a privileged action (docs/07 audit_logs).

    Written by approval/security flows when an operator acts (approve, deny,
    ...). actor is the identity, resource what it acted on, action the verb,
    result the outcome; ip/trace_id carry the transport context. created_at is
    default-only (no onupdate) so the timestamp is immutable — auditing keeps
    one unalterable time even if the row is later touched.
    """

    __tablename__ = "audit_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    actor: Mapped[str] = mapped_column(String(100), nullable=False)
    resource: Mapped[str] = mapped_column(String(255), nullable=False)
    action: Mapped[str] = mapped_column(String(50), nullable=False)
    result: Mapped[str] = mapped_column(String(50), nullable=False)
    ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    trace_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class IdempotencyRecord(Base):
    """Write-intent ledger enforcing at-most-once tool execution (docs/06 §7).

    One row per write operation, keyed by (tenant_id, idempotency_key); the
    unique constraint is what makes replay safe under concurrency — a second
    attempt at the same key collides at the database, not just in application
    logic. status moves PENDING -> COMPLETED/FAILED: an intent is recorded
    before the tool runs, the result lands on success, and recovery can replay
    a COMPLETED record or resume a FAILED one instead of re-running blind.
    """

    __tablename__ = "idempotency_records"
    __table_args__ = (
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_idempotency_tenant_key"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[str] = mapped_column(String(36), nullable=False)
    run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    step_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="PENDING", nullable=False)
    request_payload: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    result_payload: Mapped[str | None] = mapped_column(Text, nullable=True)
    external_operation_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class KnowledgeDocument(Base):
    """A knowledge document ingested into the retrieval system."""

    __tablename__ = "knowledge_documents"
    __table_args__ = (UniqueConstraint("tenant_id", "content_hash", name="uq_kd_tenant_hash"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    source: Mapped[str] = mapped_column(String(2048), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(50), default="PENDING")
    version: Mapped[int] = mapped_column(default=1)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    tenant: Mapped[Tenant] = relationship("Tenant", back_populates="knowledge_documents")
    chunks: Mapped[list[DocumentChunk]] = relationship(
        "DocumentChunk", back_populates="document", cascade="all, delete-orphan"
    )


class DocumentChunk(Base):
    """A text chunk belonging to a knowledge document."""

    __tablename__ = "document_chunks"
    __table_args__ = (UniqueConstraint("document_id", "chunk_index", name="uq_chunks_doc_index"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    document_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("knowledge_documents.id", ondelete="CASCADE"),
        nullable=False,
    )
    chunk_index: Mapped[int] = mapped_column(nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    section_path: Mapped[str] = mapped_column(Text, default="[]")
    char_count: Mapped[int] = mapped_column(default=0)
    embedding = mapped_column(Vector(1536), nullable=True)
    # FTS index column — populated by vector_store.store_embeddings with
    # jieba-segmented content (migration a8c3d2e1f4b5 creates the GIN index
    # and trigger that fill it automatically in the dev/prod schema).
    search_vector = mapped_column(TSVECTOR, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    document: Mapped[KnowledgeDocument] = relationship("KnowledgeDocument", back_populates="chunks")


class ApiKey(Base):
    """A long-lived credential binding an actor to a (tenant, user).

    Only the HMAC-SHA256 digest is stored (``key_hash``), so a database leak
    does not expose usable keys. On authentication the key's (tenant, user)
    *is* the identity — client headers never override it.
    """

    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    label: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    tenant: Mapped[Tenant] = relationship("Tenant", back_populates="api_keys")


class VocabularyTerm(Base):
    """An approved vocabulary entry extending the manifest.yaml seed.

    The runtime vocabulary loader merges these terms into the entity catalogs
    (products / regions / units) after the approved-proposal gate. canonical is
    the runtime term; aliases is a JSON list of recorded variants. tenant_id
    None means global (v1); a future tenant-scoped term filters on
    ``tenant_id IN (NULL, tenant)`` at merge time with no migration.
    """

    __tablename__ = "vocabulary_terms"
    __table_args__ = (
        UniqueConstraint(
            "vocab_type", "canonical", name="uq_vocabulary_terms_type_canonical"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    vocab_type: Mapped[str] = mapped_column(String(20), nullable=False)  # product | region | unit
    canonical: Mapped[str] = mapped_column(String(255), nullable=False)
    aliases: Mapped[str] = mapped_column(Text, default="[]")
    tenant_id: Mapped[str | None] = mapped_column(String(36), nullable=True)  # None = global (v1)
    source: Mapped[str] = mapped_column(String(50), default="approved_proposal")
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class VocabularyObservation(Base):
    """A terminal run whose query may contain unknown vocabulary.

    Captured on persist_run when the query reaches tier2/3 with no known
    product/region/unit entity extracted — the raw material the batch LLM
    extractor consumes. One row per (run_id, tenant_id) so a replayed run does
    not double-count.
    """

    __tablename__ = "vocabulary_observations"
    __table_args__ = (
        UniqueConstraint("run_id", "tenant_id", name="uq_vocabulary_obs_run_tenant"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    run_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    tenant_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    query: Mapped[str] = mapped_column(Text, nullable=False)
    tier: Mapped[str] = mapped_column(String(10), nullable=False)
    processed: Mapped[bool] = mapped_column(default=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class VocabularyProposal(Base):
    """An LLM-proposed term awaiting human approval (PENDING → APPROVED/REJECTED).

    LLM output is never authoritative: a proposal only enters vocabulary_terms
    after an operator approves it. proposal_key (sha1 of type + normalized
    canonical) makes the batch task idempotent — re-extracting the same
    candidate updates the PENDING row instead of duplicating it.
    """

    __tablename__ = "vocabulary_proposals"
    __table_args__ = (
        UniqueConstraint("proposal_key", name="uq_vocabulary_proposals_key"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    proposal_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    vocab_type: Mapped[str] = mapped_column(String(20), nullable=False)
    canonical: Mapped[str] = mapped_column(String(255), nullable=False)
    aliases: Mapped[str] = mapped_column(Text, default="[]")
    evidence: Mapped[str] = mapped_column(Text, default="[]")
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(20), default="PENDING", index=True)
    decided_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decision_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )
