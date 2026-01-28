"""add language and feedback fields"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "0002_feedback_language"
down_revision = "0001_init"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("language", sa.String(length=8), nullable=True))
    op.add_column("chat_sessions", sa.Column("last_activity_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=True))
    op.add_column("chat_sessions", sa.Column("feedback_rating", sa.Integer(), nullable=True))
    op.add_column("chat_sessions", sa.Column("feedback_comment", sa.Text(), nullable=True))
    op.add_column("chat_sessions", sa.Column("closed_reason", sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column("chat_sessions", "closed_reason")
    op.drop_column("chat_sessions", "feedback_comment")
    op.drop_column("chat_sessions", "feedback_rating")
    op.drop_column("chat_sessions", "last_activity_at")
    op.drop_column("users", "language")
