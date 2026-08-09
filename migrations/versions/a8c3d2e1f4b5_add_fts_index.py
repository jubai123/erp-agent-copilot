"""add_fts_index

Revision ID: a8c3d2e1f4b5
Revises: f3a2b1c4d5e6
Create Date: 2026-08-06 14:30:00.000000

"""
from collections.abc import Sequence

from alembic import op

revision: str = "a8c3d2e1f4b5"
down_revision: str | Sequence[str] | None = "f3a2b1c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_TRIGGER_NAME = "trg_document_chunks_search_vector"


def upgrade() -> None:
    op.execute("""
        ALTER TABLE document_chunks
        ADD COLUMN search_vector tsvector
    """)
    op.execute("""
        CREATE INDEX idx_document_chunks_fts
        ON document_chunks USING GIN(search_vector)
    """)
    op.execute(f"""
        CREATE FUNCTION {_TRIGGER_NAME}() RETURNS trigger AS $$
        BEGIN
            NEW.search_vector := to_tsvector('simple', COALESCE(NEW.content, ''));
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute(f"""
        CREATE TRIGGER {_TRIGGER_NAME}
        BEFORE INSERT OR UPDATE ON document_chunks
        FOR EACH ROW EXECUTE FUNCTION {_TRIGGER_NAME}()
    """)


def downgrade() -> None:
    op.execute(f"DROP TRIGGER IF EXISTS {_TRIGGER_NAME} ON document_chunks")
    op.execute(f"DROP FUNCTION IF EXISTS {_TRIGGER_NAME}()")
    op.execute("DROP INDEX IF EXISTS idx_document_chunks_fts")
    op.execute("ALTER TABLE document_chunks DROP COLUMN IF EXISTS search_vector")
