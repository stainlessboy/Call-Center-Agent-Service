"""Lead: client_request_id (idempotency) + source

Revision ID: a1b2c3d4e5f6
Revises: f2a3b4c5d6e7
Create Date: 2026-07-27

Adds the columns the Mini App lead form needs: a client-generated idempotency
key so a retried submit cannot create a duplicate lead, and a channel marker
separating Mini App leads from bot leads.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "a1b2c3d4e5f6"
down_revision: str | None = "f2a3b4c5d6e7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("leads", sa.Column("client_request_id", sa.String(length=64), nullable=True))
    op.add_column(
        "leads",
        sa.Column("source", sa.String(length=16), server_default="bot", nullable=False),
    )
    op.create_index(
        "ix_leads_client_request_id", "leads", ["client_request_id"], unique=True
    )


def downgrade() -> None:
    op.drop_index("ix_leads_client_request_id", table_name="leads")
    op.drop_column("leads", "source")
    op.drop_column("leads", "client_request_id")
