"""User.theme — Mini App appearance preference

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-07-27

NULL / 'auto' = follow the Telegram client theme; 'light' / 'dark' pin it.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "b2c3d4e5f6a7"
down_revision: str | None = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("theme", sa.String(length=8), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "theme")
