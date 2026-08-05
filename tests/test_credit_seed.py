"""Tests for the Excel credit seeder (app/admin/services/credit_seed.py)."""
from types import SimpleNamespace

from app.admin.services.credit_seed import (
    _KIND_AXIS_COLS,
    _SECTION_CONDITION_KIND,
    _product_condition_kind,
)


class TestProductConditionKind:
    """Which axis's parsed bounds survive seeding. A product's own choice wins so
    an axis set by hand in SQLAdmin is not reset on the next re-seed."""

    def test_new_product_falls_back_to_section(self):
        assert _product_condition_kind(None, "Ипотека") == "downpayment"
        assert _product_condition_kind(None, "Автокредит") == "flat"

    def test_unknown_section_is_flat(self):
        assert _product_condition_kind(None, "Овердрафт") == "flat"

    def test_product_axis_wins_over_section_default(self):
        product = SimpleNamespace(rate_condition_kind="term")
        # The section default is 'downpayment' — the manual choice must survive.
        assert _SECTION_CONDITION_KIND["Ипотека"] == "downpayment"
        assert _product_condition_kind(product, "Ипотека") == "term"

    def test_blank_product_axis_falls_back(self):
        for blank in (None, "", "   "):
            product = SimpleNamespace(rate_condition_kind=blank)
            assert _product_condition_kind(product, "Ипотека") == "downpayment"

    def test_composite_axis_keeps_both_bound_pairs(self):
        product = SimpleNamespace(rate_condition_kind="term_downpayment")
        kind = _product_condition_kind(product, "Автокредит")
        assert kind == "term_downpayment"
        assert set(_KIND_AXIS_COLS[kind]) == {
            "term_min_months",
            "term_max_months",
            "downpayment_min_pct",
            "downpayment_max_pct",
        }
