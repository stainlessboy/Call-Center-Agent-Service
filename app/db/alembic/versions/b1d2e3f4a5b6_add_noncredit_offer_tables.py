"""add normalized noncredit offer tables"""

revision = "b1d2e3f4a5b6"
down_revision = "a65d3b2f1b3f"
branch_labels = None
depends_on = None

from alembic import op
import sqlalchemy as sa


def upgrade() -> None:
    op.create_table(
        "deposit_product_offers",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("service_name", sa.String(length=512), nullable=False),
        sa.Column("currency_code", sa.String(length=8), nullable=False),
        sa.Column("min_amount_text", sa.Text(), nullable=True),
        sa.Column("min_amount", sa.BigInteger(), nullable=True),
        sa.Column("term_text", sa.String(length=255), nullable=True),
        sa.Column("term_months", sa.Integer(), nullable=True),
        sa.Column("rate_text", sa.String(length=128), nullable=True),
        sa.Column("rate_pct", sa.Float(), nullable=True),
        sa.Column("open_channel_text", sa.Text(), nullable=True),
        sa.Column("payout_text", sa.Text(), nullable=True),
        sa.Column("payout_monthly_available", sa.Boolean(), nullable=True),
        sa.Column("payout_end_available", sa.Boolean(), nullable=True),
        sa.Column("topup_text", sa.Text(), nullable=True),
        sa.Column("topup_allowed", sa.Boolean(), nullable=True),
        sa.Column("partial_withdrawal_allowed", sa.Boolean(), nullable=True),
        sa.Column("notes_text", sa.Text(), nullable=True),
        sa.Column("source_path", sa.String(length=255), nullable=True),
        sa.Column("source_row_order", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("service_name", "currency_code", "source_row_order", name="uq_deposit_product_offers_row_currency"),
    )
    op.create_index(op.f("ix_deposit_product_offers_service_name"), "deposit_product_offers", ["service_name"], unique=False)
    op.create_index(op.f("ix_deposit_product_offers_currency_code"), "deposit_product_offers", ["currency_code"], unique=False)
    op.create_index(op.f("ix_deposit_product_offers_term_months"), "deposit_product_offers", ["term_months"], unique=False)
    op.create_index(op.f("ix_deposit_product_offers_rate_pct"), "deposit_product_offers", ["rate_pct"], unique=False)
    op.create_index(op.f("ix_deposit_product_offers_topup_allowed"), "deposit_product_offers", ["topup_allowed"], unique=False)

    op.create_table(
        "card_product_offers",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("service_name", sa.String(length=512), nullable=False),
        sa.Column("card_network", sa.String(length=32), nullable=True),
        sa.Column("currency_code", sa.String(length=16), nullable=True),
        sa.Column("is_fx_card", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("is_debit_card", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("payroll_supported", sa.Boolean(), nullable=True),
        sa.Column("issue_fee_text", sa.Text(), nullable=True),
        sa.Column("issue_fee_free", sa.Boolean(), nullable=True),
        sa.Column("reissue_fee_text", sa.Text(), nullable=True),
        sa.Column("transfer_fee_text", sa.Text(), nullable=True),
        sa.Column("cashback_text", sa.String(length=128), nullable=True),
        sa.Column("cashback_pct", sa.Float(), nullable=True),
        sa.Column("validity_text", sa.String(length=255), nullable=True),
        sa.Column("validity_months", sa.Integer(), nullable=True),
        sa.Column("issuance_time_text", sa.Text(), nullable=True),
        sa.Column("pin_setup_cbu_text", sa.Text(), nullable=True),
        sa.Column("sms_setup_cbu_text", sa.Text(), nullable=True),
        sa.Column("pin_setup_mobile_text", sa.Text(), nullable=True),
        sa.Column("sms_setup_mobile_text", sa.Text(), nullable=True),
        sa.Column("annual_fee_text", sa.Text(), nullable=True),
        sa.Column("annual_fee_free", sa.Boolean(), nullable=True),
        sa.Column("mobile_order_available", sa.Boolean(), nullable=True),
        sa.Column("delivery_available", sa.Boolean(), nullable=True),
        sa.Column("pickup_available", sa.Boolean(), nullable=True),
        sa.Column("source_path", sa.String(length=255), nullable=True),
        sa.Column("source_row_order", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("service_name", "source_row_order", name="uq_card_product_offers_row"),
    )
    op.create_index(op.f("ix_card_product_offers_service_name"), "card_product_offers", ["service_name"], unique=False)
    op.create_index(op.f("ix_card_product_offers_card_network"), "card_product_offers", ["card_network"], unique=False)
    op.create_index(op.f("ix_card_product_offers_currency_code"), "card_product_offers", ["currency_code"], unique=False)
    op.create_index(op.f("ix_card_product_offers_is_fx_card"), "card_product_offers", ["is_fx_card"], unique=False)
    op.create_index(op.f("ix_card_product_offers_is_debit_card"), "card_product_offers", ["is_debit_card"], unique=False)
    op.create_index(op.f("ix_card_product_offers_payroll_supported"), "card_product_offers", ["payroll_supported"], unique=False)
    op.create_index(op.f("ix_card_product_offers_issue_fee_free"), "card_product_offers", ["issue_fee_free"], unique=False)
    op.create_index(op.f("ix_card_product_offers_mobile_order_available"), "card_product_offers", ["mobile_order_available"], unique=False)
    op.create_index(op.f("ix_card_product_offers_delivery_available"), "card_product_offers", ["delivery_available"], unique=False)
    op.create_index(op.f("ix_card_product_offers_pickup_available"), "card_product_offers", ["pickup_available"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_card_product_offers_pickup_available"), table_name="card_product_offers")
    op.drop_index(op.f("ix_card_product_offers_delivery_available"), table_name="card_product_offers")
    op.drop_index(op.f("ix_card_product_offers_mobile_order_available"), table_name="card_product_offers")
    op.drop_index(op.f("ix_card_product_offers_issue_fee_free"), table_name="card_product_offers")
    op.drop_index(op.f("ix_card_product_offers_payroll_supported"), table_name="card_product_offers")
    op.drop_index(op.f("ix_card_product_offers_is_debit_card"), table_name="card_product_offers")
    op.drop_index(op.f("ix_card_product_offers_is_fx_card"), table_name="card_product_offers")
    op.drop_index(op.f("ix_card_product_offers_currency_code"), table_name="card_product_offers")
    op.drop_index(op.f("ix_card_product_offers_card_network"), table_name="card_product_offers")
    op.drop_index(op.f("ix_card_product_offers_service_name"), table_name="card_product_offers")
    op.drop_table("card_product_offers")

    op.drop_index(op.f("ix_deposit_product_offers_topup_allowed"), table_name="deposit_product_offers")
    op.drop_index(op.f("ix_deposit_product_offers_rate_pct"), table_name="deposit_product_offers")
    op.drop_index(op.f("ix_deposit_product_offers_term_months"), table_name="deposit_product_offers")
    op.drop_index(op.f("ix_deposit_product_offers_currency_code"), table_name="deposit_product_offers")
    op.drop_index(op.f("ix_deposit_product_offers_service_name"), table_name="deposit_product_offers")
    op.drop_table("deposit_product_offers")
