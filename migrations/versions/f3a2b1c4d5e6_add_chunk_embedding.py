"""add_chunk_embedding

Revision ID: f3a2b1c4d5e6
Revises: 1c62f8126a12
Create Date: 2026-08-06 14:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

revision: str = "f3a2b1c4d5e6"
down_revision: str | Sequence[str] | None = "1c62f8126a12"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.add_column(
        "document_chunks",
        sa.Column("embedding", Vector(1536), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("document_chunks", "embedding")
