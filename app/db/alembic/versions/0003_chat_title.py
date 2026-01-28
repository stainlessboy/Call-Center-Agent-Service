"""add chat title"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "0003_chat_title"
down_revision = "0002_feedback_language"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("chat_sessions", sa.Column("title", sa.String(length=255), nullable=True))


def downgrade() -> None:
    op.drop_column("chat_sessions", "title")
