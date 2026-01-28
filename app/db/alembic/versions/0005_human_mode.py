"""add human mode fields"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "0005_human_mode"
down_revision = "0004_branches"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "chat_sessions",
        sa.Column("human_mode", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.add_column(
        "chat_sessions",
        sa.Column("human_mode_since", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "chat_sessions",
        sa.Column("assigned_operator_id", sa.BigInteger(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("chat_sessions", "assigned_operator_id")
    op.drop_column("chat_sessions", "human_mode_since")
    op.drop_column("chat_sessions", "human_mode")
