"""add_vocabulary_tables

Revision ID: f2c3d4e5f6a7
Revises: 7c8d9e0f1a2b
Create Date: 2026-08-21 11:00:00.000000

Adds the LLM-driven vocabulary pipeline tables: approved terms (runtime
extensions to the manifest.yaml seed), captured OOV observations, and
LLM-proposed candidates awaiting human approval.

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f2c3d4e5f6a7"
down_revision: str | Sequence[str] | None = "7c8d9e0f1a2b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "vocabulary_terms",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("vocab_type", sa.String(length=20), nullable=False),
        sa.Column("canonical", sa.String(length=255), nullable=False),
        sa.Column("aliases", sa.Text(), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=True),
        sa.Column("source", sa.String(length=50), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "vocab_type", "canonical", name="uq_vocabulary_terms_type_canonical"
        ),
    )
    op.create_table(
        "vocabulary_observations",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=True),
        sa.Column("query", sa.Text(), nullable=False),
        sa.Column("tier", sa.String(length=10), nullable=False),
        sa.Column("processed", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("run_id", "tenant_id", name="uq_vocabulary_obs_run_tenant"),
    )
    op.create_index(
        op.f("ix_vocabulary_observations_run_id"), "vocabulary_observations", ["run_id"]
    )
    op.create_index(
        op.f("ix_vocabulary_observations_processed"),
        "vocabulary_observations",
        ["processed"],
    )
    op.create_table(
        "vocabulary_proposals",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("proposal_key", sa.String(length=64), nullable=False),
        sa.Column("vocab_type", sa.String(length=20), nullable=False),
        sa.Column("canonical", sa.String(length=255), nullable=False),
        sa.Column("aliases", sa.Text(), nullable=False),
        sa.Column("evidence", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("decided_by", sa.String(length=100), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("proposal_key", name="uq_vocabulary_proposals_key"),
    )
    op.create_index(
        op.f("ix_vocabulary_proposals_proposal_key"),
        "vocabulary_proposals",
        ["proposal_key"],
    )
    op.create_index(
        op.f("ix_vocabulary_proposals_status"), "vocabulary_proposals", ["status"]
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        op.f("ix_vocabulary_proposals_status"), table_name="vocabulary_proposals"
    )
    op.drop_index(
        op.f("ix_vocabulary_proposals_proposal_key"),
        table_name="vocabulary_proposals",
    )
    op.drop_table("vocabulary_proposals")
    op.drop_index(
        op.f("ix_vocabulary_observations_processed"),
        table_name="vocabulary_observations",
    )
    op.drop_index(
        op.f("ix_vocabulary_observations_run_id"), table_name="vocabulary_observations"
    )
    op.drop_table("vocabulary_observations")
    op.drop_table("vocabulary_terms")
