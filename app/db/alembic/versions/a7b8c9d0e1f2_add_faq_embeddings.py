"""add pgvector extension + embedding columns to faq

Revision ID: a7b8c9d0e1f2
Revises: f6a7b8c9d0e1
Create Date: 2026-05-06 12:00:00.000000

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

revision: str = "a7b8c9d0e1f2"
down_revision: str | None = "f6a7b8c9d0e1"
branch_labels: str | None = None
depends_on: str | None = None

EMBEDDING_DIM = 1536


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.add_column("faq", sa.Column("embedding_ru", Vector(EMBEDDING_DIM), nullable=True))
    op.add_column("faq", sa.Column("embedding_en", Vector(EMBEDDING_DIM), nullable=True))
    op.add_column("faq", sa.Column("embedding_uz", Vector(EMBEDDING_DIM), nullable=True))


def downgrade() -> None:
    op.drop_column("faq", "embedding_uz")
    op.drop_column("faq", "embedding_en")
    op.drop_column("faq", "embedding_ru")
