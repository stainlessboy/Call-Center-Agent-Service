"""add faq table"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "0006_faq"
down_revision = "0005_human_mode"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "faq",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("answer", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
    )
    op.create_index("ix_faq_question", "faq", ["question"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_faq_question", table_name="faq")
    op.drop_table("faq")
