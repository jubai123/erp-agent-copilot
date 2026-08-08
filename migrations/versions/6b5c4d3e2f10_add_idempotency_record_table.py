"""add_idempotency_record_table

Revision ID: 6b5c4d3e2f10
Revises: f7e8d9c0b1a2
Create Date: 2026-08-09 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "6b5c4d3e2f10"
down_revision: str | Sequence[str] | None = "f7e8d9c0b1a2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "idempotency_records",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=True),
        sa.Column("step_id", sa.String(length=36), nullable=True),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("request_payload", sa.Text(), nullable=False),
        sa.Column("result_payload", sa.Text(), nullable=True),
        sa.Column("external_operation_id", sa.String(length=255), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "idempotency_key", name="uq_idempotency_tenant_key"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("idempotency_records")
