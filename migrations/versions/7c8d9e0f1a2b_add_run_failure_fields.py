"""add_run_failure_fields

Revision ID: 7c8d9e0f1a2b
Revises: 6b5c4d3e2f10
Create Date: 2026-08-09 11:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7c8d9e0f1a2b"
down_revision: str | Sequence[str] | None = "6b5c4d3e2f10"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("runs", sa.Column("failure_code", sa.String(length=50), nullable=True))
    op.add_column("runs", sa.Column("failure_reason", sa.Text(), nullable=True))
    op.add_column("runs", sa.Column("suggested_action", sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("runs", "suggested_action")
    op.drop_column("runs", "failure_reason")
    op.drop_column("runs", "failure_code")
