"""normalize credit offers: one product row + credit_rate_rules child table

revision = d0e1f2a3b4c5
down_revision = c9d0e1f2a3b4

Moves the per-tariff fields (income_type, rate_*, term_*, downpayment_*,
rate_condition_text) out of credit_product_offers into a new credit_rate_rules
child table (FK by PK). Existing duplicate product rows are collapsed to one
row per (section_name, service_name); their tariff data is preserved as
'seed'-sourced rules.
"""

revision: str = "d0e1f2a3b4c5"
down_revision: str | None = "c9d0e1f2a3b4"
branch_labels: tuple[str, ...] | None = None
depends_on: str | None = None

from alembic import op
import sqlalchemy as sa


_DROPPED_COLUMNS = (
    "income_type",
    "term_min_months",
    "term_max_months",
    "downpayment_min_pct",
    "downpayment_max_pct",
    "rate_min_pct",
    "rate_max_pct",
    "rate_condition_text",
)


def upgrade() -> None:
    # 1. New child table for rate tiers.
    op.create_table(
        "credit_rate_rules",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("credit_product_offer_id", sa.Integer(), nullable=False),
        sa.Column("income_type", sa.String(length=32), nullable=True),
        sa.Column("age_min", sa.Integer(), nullable=True),
        sa.Column("age_max", sa.Integer(), nullable=True),
        sa.Column("amount_min", sa.BigInteger(), nullable=True),
        sa.Column("amount_max", sa.BigInteger(), nullable=True),
        sa.Column("term_min_months", sa.Integer(), nullable=True),
        sa.Column("term_max_months", sa.Integer(), nullable=True),
        sa.Column("downpayment_min_pct", sa.Float(), nullable=True),
        sa.Column("downpayment_max_pct", sa.Float(), nullable=True),
        sa.Column("currency_code", sa.String(length=8), nullable=True),
        sa.Column("rate_min_pct", sa.Float(), nullable=True),
        sa.Column("rate_max_pct", sa.Float(), nullable=True),
        sa.Column("condition_text", sa.Text(), nullable=True),
        sa.Column("priority", sa.Integer(), server_default="0", nullable=False),
        sa.Column("source", sa.String(length=16), server_default="manual", nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["credit_product_offer_id"],
            ["credit_product_offers.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_credit_rate_rules_credit_product_offer_id",
        "credit_rate_rules",
        ["credit_product_offer_id"],
    )
    op.create_index(
        "ix_credit_rate_rules_income_type",
        "credit_rate_rules",
        ["income_type"],
    )

    # 2. Backfill: every existing offer row becomes a 'seed' rule attached to the
    #    canonical (MIN id) product row for its (section_name, service_name).
    op.execute(
        """
        INSERT INTO credit_rate_rules (
            credit_product_offer_id, income_type,
            term_min_months, term_max_months,
            downpayment_min_pct, downpayment_max_pct,
            rate_min_pct, rate_max_pct, condition_text,
            source, priority, is_active
        )
        SELECT
            canon.canon_id, o.income_type,
            o.term_min_months, o.term_max_months,
            o.downpayment_min_pct, o.downpayment_max_pct,
            o.rate_min_pct, o.rate_max_pct, o.rate_condition_text,
            'seed', 0, true
        FROM credit_product_offers o
        JOIN (
            SELECT section_name, service_name, MIN(id) AS canon_id
            FROM credit_product_offers
            GROUP BY section_name, service_name
        ) canon
          ON o.section_name = canon.section_name
         AND o.service_name = canon.service_name
        """
    )

    # 3. Collapse duplicate product rows, keeping the canonical one.
    op.execute(
        """
        DELETE FROM credit_product_offers o
        USING (
            SELECT section_name, service_name, MIN(id) AS canon_id
            FROM credit_product_offers
            GROUP BY section_name, service_name
        ) canon
        WHERE o.section_name = canon.section_name
          AND o.service_name = canon.service_name
          AND o.id <> canon.canon_id
        """
    )

    # 4. Drop the per-tariff columns (now living in credit_rate_rules).
    op.execute("DROP INDEX IF EXISTS ix_credit_product_offers_income_type")
    for col in _DROPPED_COLUMNS:
        op.drop_column("credit_product_offers", col)

    # 5. Enforce one row per product.
    op.create_unique_constraint(
        "uq_credit_product_offers_section_service",
        "credit_product_offers",
        ["section_name", "service_name"],
    )


def downgrade() -> None:
    # Drop the one-row-per-product guard first so we can re-expand into rows.
    op.drop_constraint(
        "uq_credit_product_offers_section_service",
        "credit_product_offers",
        type_="unique",
    )

    # Re-add the per-tariff columns.
    op.add_column("credit_product_offers", sa.Column("income_type", sa.String(length=32), nullable=True))
    op.add_column("credit_product_offers", sa.Column("term_min_months", sa.Integer(), nullable=True))
    op.add_column("credit_product_offers", sa.Column("term_max_months", sa.Integer(), nullable=True))
    op.add_column("credit_product_offers", sa.Column("downpayment_min_pct", sa.Float(), nullable=True))
    op.add_column("credit_product_offers", sa.Column("downpayment_max_pct", sa.Float(), nullable=True))
    op.add_column("credit_product_offers", sa.Column("rate_min_pct", sa.Float(), nullable=True))
    op.add_column("credit_product_offers", sa.Column("rate_max_pct", sa.Float(), nullable=True))
    op.add_column("credit_product_offers", sa.Column("rate_condition_text", sa.Text(), nullable=True))
    op.create_index("ix_credit_product_offers_income_type", "credit_product_offers", ["income_type"])

    # First rule per product updates the canonical row in place.
    op.execute(
        """
        WITH ranked AS (
            SELECT *, ROW_NUMBER() OVER (
                PARTITION BY credit_product_offer_id ORDER BY id
            ) AS rn
            FROM credit_rate_rules
        )
        UPDATE credit_product_offers p
        SET income_type = ranked.income_type,
            term_min_months = ranked.term_min_months,
            term_max_months = ranked.term_max_months,
            downpayment_min_pct = ranked.downpayment_min_pct,
            downpayment_max_pct = ranked.downpayment_max_pct,
            rate_min_pct = ranked.rate_min_pct,
            rate_max_pct = ranked.rate_max_pct,
            rate_condition_text = ranked.condition_text
        FROM ranked
        WHERE ranked.credit_product_offer_id = p.id AND ranked.rn = 1
        """
    )

    # Remaining rules become extra duplicate product rows.
    op.execute(
        """
        WITH ranked AS (
            SELECT *, ROW_NUMBER() OVER (
                PARTITION BY credit_product_offer_id ORDER BY id
            ) AS rn
            FROM credit_rate_rules
        )
        INSERT INTO credit_product_offers (
            section_name, service_name, service_name_en, service_name_uz,
            min_age, purpose_text, amount_min, amount_max,
            for_brand_gm, for_brand_other, for_market_primary,
            for_market_secondary, for_renovation, channel_cbu, channel_online,
            collateral_text, source_path, is_active,
            income_type, term_min_months, term_max_months,
            downpayment_min_pct, downpayment_max_pct,
            rate_min_pct, rate_max_pct, rate_condition_text
        )
        SELECT
            p.section_name, p.service_name, p.service_name_en, p.service_name_uz,
            p.min_age, p.purpose_text, p.amount_min, p.amount_max,
            p.for_brand_gm, p.for_brand_other, p.for_market_primary,
            p.for_market_secondary, p.for_renovation, p.channel_cbu, p.channel_online,
            p.collateral_text, p.source_path, p.is_active,
            ranked.income_type, ranked.term_min_months, ranked.term_max_months,
            ranked.downpayment_min_pct, ranked.downpayment_max_pct,
            ranked.rate_min_pct, ranked.rate_max_pct, ranked.condition_text
        FROM ranked
        JOIN credit_product_offers p ON p.id = ranked.credit_product_offer_id
        WHERE ranked.rn > 1
        """
    )

    op.drop_index("ix_credit_rate_rules_income_type", table_name="credit_rate_rules")
    op.drop_index("ix_credit_rate_rules_credit_product_offer_id", table_name="credit_rate_rules")
    op.drop_table("credit_rate_rules")
