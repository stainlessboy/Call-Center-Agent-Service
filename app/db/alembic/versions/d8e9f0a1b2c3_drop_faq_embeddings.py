"""drop pgvector embedding columns from faq

FAQ search moved to Weaviate: the hybrid (BM25 + vector) index lives there and
Weaviate vectorizes the text itself via text2vec-openai, so the per-language
embedding columns on ``faq`` have no reader left.

Deploy order matters: bring Weaviate up and press "Переиндексировать FAQ" in
/admin/seed BEFORE running this migration. Between those two steps search is
already served from Weaviate, so dropping the columns changes nothing at
runtime — but doing it the other way round leaves a window with no index and
no columns.

The ``vector`` extension is deliberately NOT dropped: other databases in the
same cluster may use it, and downgrade() needs it back.

Revision ID: d8e9f0a1b2c3
Revises: c7d8e9f0a1b2
Create Date: 2026-09-04 12:00:00.000000

"""
from __future__ import annotations

from alembic import op

revision: str = "d8e9f0a1b2c3"
down_revision: str | None = "c7d8e9f0a1b2"
branch_labels: str | None = None
depends_on: str | None = None

EMBEDDING_DIM = 1536


def upgrade() -> None:
    op.drop_column("faq", "embedding_uz")
    op.drop_column("faq", "embedding_en")
    op.drop_column("faq", "embedding_ru")


def downgrade() -> None:
    # Recreates the columns empty. The vectors themselves are not restored —
    # rolling back to the pgvector search also means re-running its backfill.
    # Raw DDL on purpose: the pgvector Python package is no longer a
    # dependency, and Alembic imports every version file in this directory.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    for lang in ("ru", "en", "uz"):
        op.execute(f"ALTER TABLE faq ADD COLUMN embedding_{lang} vector({EMBEDDING_DIM})")
