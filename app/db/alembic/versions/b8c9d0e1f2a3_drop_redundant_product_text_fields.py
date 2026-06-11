"""drop redundant text condition fields from product offers

revision = b8c9d0e1f2a3
down_revision = a7b8c9d0e1f2
"""

revision: str = "b8c9d0e1f2a3"
down_revision: str | None = "a7b8c9d0e1f2"
branch_labels: tuple[str, ...] | None = None
depends_on: str | None = None

from alembic import op
import sqlalchemy as sa


def upgrade() -> None:
    # ── credit_product_offers ────────────────────────────────────────────────
    op.drop_constraint(
        "uq_credit_product_offers_row_rate",
        "credit_product_offers",
        type_="unique",
    )
    op.drop_column("credit_product_offers", "amount_text")
    op.drop_column("credit_product_offers", "min_age_text")
    op.drop_column("credit_product_offers", "term_text")
    op.drop_column("credit_product_offers", "downpayment_text")
    op.drop_column("credit_product_offers", "rate_text")
    op.drop_column("credit_product_offers", "rate_order")
    op.drop_column("credit_product_offers", "source_row_order")

    # ── deposit_product_offers ───────────────────────────────────────────────
    op.drop_constraint(
        "uq_deposit_product_offers_row_currency",
        "deposit_product_offers",
        type_="unique",
    )
    op.drop_column("deposit_product_offers", "min_amount_text")
    op.drop_column("deposit_product_offers", "term_text")
    op.drop_column("deposit_product_offers", "rate_text")
    op.drop_column("deposit_product_offers", "topup_text")
    op.drop_column("deposit_product_offers", "source_row_order")
    op.create_unique_constraint(
        "uq_deposit_product_offers_service_currency_term",
        "deposit_product_offers",
        ["service_name", "currency_code", "term_months"],
    )

    # ── card_product_offers ──────────────────────────────────────────────────
    op.drop_column("card_product_offers", "cashback_text")
    op.drop_column("card_product_offers", "validity_text")


def downgrade() -> None:
    # ── card_product_offers ──────────────────────────────────────────────────
    op.add_column(
        "card_product_offers",
        sa.Column("validity_text", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "card_product_offers",
        sa.Column("cashback_text", sa.String(length=128), nullable=True),
    )

    # ── deposit_product_offers ───────────────────────────────────────────────
    op.drop_constraint(
        "uq_deposit_product_offers_service_currency_term",
        "deposit_product_offers",
        type_="unique",
    )
    op.add_column(
        "deposit_product_offers",
        sa.Column("source_row_order", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "deposit_product_offers",
        sa.Column("topup_text", sa.Text(), nullable=True),
    )
    op.add_column(
        "deposit_product_offers",
        sa.Column("rate_text", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "deposit_product_offers",
        sa.Column("term_text", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "deposit_product_offers",
        sa.Column("min_amount_text", sa.Text(), nullable=True),
    )
    op.create_unique_constraint(
        "uq_deposit_product_offers_row_currency",
        "deposit_product_offers",
        ["service_name", "currency_code", "source_row_order"],
    )

    # ── credit_product_offers ────────────────────────────────────────────────
    op.add_column(
        "credit_product_offers",
        sa.Column("source_row_order", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "credit_product_offers",
        sa.Column(
            "rate_order",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
    )
    op.add_column(
        "credit_product_offers",
        sa.Column("rate_text", sa.Text(), nullable=True),
    )
    op.add_column(
        "credit_product_offers",
        sa.Column("downpayment_text", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "credit_product_offers",
        sa.Column("term_text", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "credit_product_offers",
        sa.Column("min_age_text", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "credit_product_offers",
        sa.Column("amount_text", sa.Text(), nullable=True),
    )
    op.create_unique_constraint(
        "uq_credit_product_offers_row_rate",
        "credit_product_offers",
        ["section_name", "source_row_order", "rate_order", "income_type"],
    )
