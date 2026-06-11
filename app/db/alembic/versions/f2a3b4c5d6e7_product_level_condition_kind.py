"""move condition kind to the product level (one axis per product)

Adds credit_product_offers.rate_condition_kind and drops the per-rule
credit_rate_rules.condition_kind. The condition axis is now a property of the
product: all its tariffs vary by a single axis (term/age/amount/downpayment) or
none ('flat'); income_type/currency_code stay on each rule as overlay filters.

Data migration: infer each product's axis from its rules (the axis most rules
constrain, ties broken age→downpayment→term→amount, else 'flat'), then NULL out
the off-axis bounds on its rules so the data matches the new single-axis model.

revision = f2a3b4c5d6e7
down_revision = e1f2a3b4c5d6
"""

revision: str = "f2a3b4c5d6e7"
down_revision: str | None = "e1f2a3b4c5d6"
branch_labels: tuple[str, ...] | None = None
depends_on: str | None = None

from alembic import op
import sqlalchemy as sa

# axis -> (min_col, max_col) on credit_rate_rules
_AXIS_COLS = {
    "age": ("age_min", "age_max"),
    "downpayment": ("downpayment_min_pct", "downpayment_max_pct"),
    "term": ("term_min_months", "term_max_months"),
    "amount": ("amount_min", "amount_max"),
}
# tie-break / preference order when several axes are constrained
_AXIS_ORDER = ("age", "downpayment", "term", "amount")


def upgrade() -> None:
    op.add_column(
        "credit_product_offers",
        sa.Column("rate_condition_kind", sa.String(length=16), nullable=True),
    )

    conn = op.get_bind()
    product_ids = [r[0] for r in conn.execute(sa.text("SELECT id FROM credit_product_offers"))]
    for pid in product_ids:
        rules = conn.execute(
            sa.text(
                "SELECT age_min, age_max, amount_min, amount_max, "
                "term_min_months, term_max_months, "
                "downpayment_min_pct, downpayment_max_pct "
                "FROM credit_rate_rules WHERE credit_product_offer_id = :pid"
            ),
            {"pid": pid},
        ).mappings().all()

        counts = {axis: 0 for axis in _AXIS_ORDER}
        for row in rules:
            for axis, (lo, hi) in _AXIS_COLS.items():
                if row[lo] is not None or row[hi] is not None:
                    counts[axis] += 1

        kind = "flat"
        best = 0
        for axis in _AXIS_ORDER:
            if counts[axis] > best:
                best = counts[axis]
                kind = axis

        conn.execute(
            sa.text("UPDATE credit_product_offers SET rate_condition_kind = :k WHERE id = :pid"),
            {"k": kind, "pid": pid},
        )

        # NULL out every bound that is not on the chosen axis.
        clear_cols: list[str] = []
        for axis, cols in _AXIS_COLS.items():
            if axis != kind:
                clear_cols.extend(cols)
        if clear_cols:
            set_clause = ", ".join(f"{c} = NULL" for c in clear_cols)
            conn.execute(
                sa.text(f"UPDATE credit_rate_rules SET {set_clause} WHERE credit_product_offer_id = :pid"),
                {"pid": pid},
            )

    op.drop_column("credit_rate_rules", "condition_kind")


def downgrade() -> None:
    op.add_column(
        "credit_rate_rules",
        sa.Column("condition_kind", sa.String(length=16), nullable=True),
    )
    conn = op.get_bind()
    conn.execute(
        sa.text(
            "UPDATE credit_rate_rules AS cr "
            "SET condition_kind = p.rate_condition_kind "
            "FROM credit_product_offers AS p "
            "WHERE cr.credit_product_offer_id = p.id"
        )
    )
    op.drop_column("credit_product_offers", "rate_condition_kind")
