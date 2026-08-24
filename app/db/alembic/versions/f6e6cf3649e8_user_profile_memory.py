"""UserProfile — "personal consultant" memory (facts + relationship notes)

Revision ID: f6e6cf3649e8
Revises: b2c3d4e5f6a7
Create Date: 2026-08-06

One row per user. `facts` (JSONB) holds loosely-schemad client facts
(income_monthly, age, employment_type, family_status, goals, preferences, ...)
merged in by app/agent/profile.py::upsert_user_profile. `notes` is a short
free-text relationship recap maintained by the background memory-extractor
(app/agent/memory_extract.py).

NOTE: autogenerate also proposed dropping `checkpoints` / `checkpoint_blobs` /
`checkpoint_writes` / `checkpoint_migrations` — those are the LangGraph
Postgres checkpointer's own tables (created by langgraph's AsyncPostgresSaver
.setup(), not SQLAlchemy models), so they aren't in Base.metadata and
autogenerate always flags them as "removed". That diff was intentionally
excluded from this migration.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f6e6cf3649e8"
down_revision: str | None = "b2c3d4e5f6a7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_profiles",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column(
            "facts",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        sa.Column("notes", sa.Text(), server_default="", nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_user_profiles_user_id"), "user_profiles", ["user_id"], unique=True
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_user_profiles_user_id"), table_name="user_profiles")
    op.drop_table("user_profiles")
