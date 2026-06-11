"""add qualification-flow tag columns to credit_product_offers

revision = c9d0e1f2a3b4
down_revision = b8c9d0e1f2a3
"""

revision: str = "c9d0e1f2a3b4"
down_revision: str | None = "b8c9d0e1f2a3"
branch_labels: tuple[str, ...] | None = None
depends_on: str | None = None

from alembic import op
import sqlalchemy as sa


_TAG_COLUMNS = (
    "for_brand_gm",
    "for_brand_other",
    "for_market_primary",
    "for_market_secondary",
    "for_renovation",
    "channel_cbu",
    "channel_online",
)


def upgrade() -> None:
    for col in _TAG_COLUMNS:
        op.add_column(
            "credit_product_offers",
            sa.Column(col, sa.Boolean(), nullable=True),
        )
        op.create_index(
            f"ix_credit_product_offers_{col}",
            "credit_product_offers",
            [col],
        )


def downgrade() -> None:
    for col in reversed(_TAG_COLUMNS):
        op.drop_index(f"ix_credit_product_offers_{col}", table_name="credit_product_offers")
        op.drop_column("credit_product_offers", col)
