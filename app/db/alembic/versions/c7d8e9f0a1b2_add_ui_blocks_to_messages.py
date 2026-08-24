"""Add ui_blocks JSONB column to messages (Mini App structured cards, Phase 3)

Revision ID: c7d8e9f0a1b2
Revises: f6e6cf3649e8
Create Date: 2026-08-06

Persists the structured `ui_blocks` produced alongside a turn's text answer
(see BotState.ui_blocks / AgentTurnResult.ui_blocks / docs/MINIAPP.md "UI
blocks") on the corresponding `role="agent"` Message row, so the Mini App
history screen (GET /api/miniapp/chat/history) can re-render product cards /
tables on reload instead of falling back to text only. NULL for every
existing row and for user/system/operator messages, which never carry one.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "c7d8e9f0a1b2"
down_revision: str | None = "f6e6cf3649e8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("messages", sa.Column("ui_blocks", JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("messages", "ui_blocks")
