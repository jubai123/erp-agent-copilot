"""add_agent_checkpoints

Revision ID: d4e5f6a7b8c9
Revises: a8c3d2e1f4b5
Create Date: 2026-08-08 10:00:00.000000

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "d4e5f6a7b8c9"
down_revision: str | Sequence[str] | None = "a8c3d2e1f4b5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "agent_checkpoints",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("node_name", sa.String(length=50), nullable=False),
        sa.Column("run_status", sa.String(length=50), nullable=False),
        sa.Column("state_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_agent_checkpoints_run_id", "agent_checkpoints", ["run_id"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_agent_checkpoints_run_id", table_name="agent_checkpoints")
    op.drop_table("agent_checkpoints")
