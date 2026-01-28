"""add branches table"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "0004_branches"
down_revision = "0003_chat_title"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "branches",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("region", sa.String(length=255), nullable=False),
        sa.Column("district", sa.String(length=255), nullable=False),
        sa.Column("address", sa.Text(), nullable=True),
        sa.Column("landmarks", sa.Text(), nullable=True),
        sa.Column("metro", sa.String(length=255), nullable=True),
        sa.Column("phone", sa.String(length=64), nullable=True),
        sa.Column("hours", sa.String(length=255), nullable=True),
        sa.Column("weekend", sa.String(length=255), nullable=True),
        sa.Column("inn", sa.String(length=64), nullable=True),
        sa.Column("mfo", sa.String(length=64), nullable=True),
        sa.Column("postal_index", sa.String(length=32), nullable=True),
        sa.Column("uzcard_accounts", sa.Text(), nullable=True),
        sa.Column("humo_accounts", sa.Text(), nullable=True),
        sa.Column("latitude", sa.Float(), nullable=True),
        sa.Column("longitude", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
    )
    op.create_index("ix_branches_region", "branches", ["region"], unique=False)
    op.create_index("ix_branches_district", "branches", ["district"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_branches_district", table_name="branches")
    op.drop_index("ix_branches_region", table_name="branches")
    op.drop_table("branches")
