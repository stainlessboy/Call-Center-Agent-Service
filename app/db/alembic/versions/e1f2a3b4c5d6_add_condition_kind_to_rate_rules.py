"""add condition_kind UI hint column to credit_rate_rules

revision = e1f2a3b4c5d6
down_revision = d0e1f2a3b4c5
"""

revision: str = "e1f2a3b4c5d6"
down_revision: str | None = "d0e1f2a3b4c5"
branch_labels: tuple[str, ...] | None = None
depends_on: str | None = None

from alembic import op
import sqlalchemy as sa


def upgrade() -> None:
    op.add_column("credit_rate_rules", sa.Column("condition_kind", sa.String(length=16), nullable=True))


def downgrade() -> None:
    op.drop_column("credit_rate_rules", "condition_kind")
