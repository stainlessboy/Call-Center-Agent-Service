"""add pgvector extension + embedding columns to faq

Revision ID: a7b8c9d0e1f2
Revises: f6a7b8c9d0e1
Create Date: 2026-05-06 12:00:00.000000

"""
from __future__ import annotations

from alembic import op

revision: str = "a7b8c9d0e1f2"
down_revision: str | None = "f6a7b8c9d0e1"
branch_labels: str | None = None
depends_on: str | None = None

EMBEDDING_DIM = 1536


def upgrade() -> None:
    # Raw DDL rather than pgvector.sqlalchemy.Vector: the package was dropped
    # from requirements when FAQ search moved to Weaviate (revision
    # d8e9f0a1b2c3), and Alembic imports every version file in this directory,
    # so a module-level import here would break the whole chain. The emitted
    # DDL is identical to what Vector(1536) produced.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    for lang in ("ru", "en", "uz"):
        op.execute(f"ALTER TABLE faq ADD COLUMN embedding_{lang} vector({EMBEDDING_DIM})")


def downgrade() -> None:
    op.drop_column("faq", "embedding_uz")
    op.drop_column("faq", "embedding_en")
    op.drop_column("faq", "embedding_ru")
