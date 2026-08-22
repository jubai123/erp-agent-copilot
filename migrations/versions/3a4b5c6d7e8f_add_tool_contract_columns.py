"""add_tool_contract_columns

Revision ID: 3a4b5c6d7e8f
Revises: f2c3d4e5f6a7
Create Date: 2026-08-22 09:00:00.000000

Adds the tool-contract columns to tool_versions (required_scope,
success_condition) and relaxes tools.tenant_id to nullable so the global
seed rows (tenant_id=NULL, the VocabularyTerm global convention) can carry
the operator-owned tool contracts. On Postgres the NOT NULL drop is
independent of the existing FK — no constraint rebuild is needed.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "3a4b5c6d7e8f"
down_revision: str | Sequence[str] | None = "f2c3d4e5f6a7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "tool_versions",
        sa.Column("required_scope", sa.String(length=20), nullable=True),
    )
    op.add_column(
        "tool_versions",
        sa.Column("success_condition", sa.Text(), nullable=True),
    )
    op.alter_column("tools", "tenant_id", existing_type=sa.String(length=36), nullable=True)


def downgrade() -> None:
    """Downgrade schema."""
    op.alter_column("tools", "tenant_id", existing_type=sa.String(length=36), nullable=False)
    op.drop_column("tool_versions", "success_condition")
    op.drop_column("tool_versions", "required_scope")
