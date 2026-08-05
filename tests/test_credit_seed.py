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


class TestParseRuleRows:
    """The inline tariff editor shows only the four business criteria. Columns it
    no longer renders must keep their stored value instead of being nulled."""

    @staticmethod
    def _view():
        from app.admin.views import CreditProductOfferAdmin

        # ModelView.__init__ needs an app context; only pure parsing is exercised.
        return CreditProductOfferAdmin.__new__(CreditProductOfferAdmin)

    @staticmethod
    def _form(**overrides):
        form = {
            "rule-0-id": "7",
            "rule-0-delete": "",
            "rule-0-is_active": "on",
            "rule-0-term_min_months": "12",
            "rule-0-term_max_months": "24",
            "rule-0-downpayment_min_pct": "30",
            "rule-0-downpayment_max_pct": "",
            "rule-0-income_type": "payroll",
            "rule-0-rate_min_pct": "22.5",
            "rule-0-age_min": "",
            "rule-0-age_max": "",
            "rule-0-amount_min": "",
            "rule-0-amount_max": "",
        }
        form.update(overrides)
        return form

    def test_fields_absent_from_the_form_are_not_in_the_row(self):
        row = self._view()._parse_rule_rows(self._form())[0]
        for dropped in ("currency_code", "priority", "condition_text", "rate_max_pct"):
            assert dropped not in row, f"{dropped} would overwrite the stored value"

    def test_submitted_fields_are_parsed(self):
        row = self._view()._parse_rule_rows(self._form())[0]
        assert row["term_min_months"] == 12
        assert row["term_max_months"] == 24
        assert row["downpayment_min_pct"] == 30.0
        # Rendered but left blank → an explicit "unconstrained".
        assert row["downpayment_max_pct"] is None
        assert row["income_type"] == "payroll"
        assert row["rate_min_pct"] == 22.5

    def test_unchecked_is_active_reads_as_false(self):
        form = self._form()
        del form["rule-0-is_active"]
        assert self._view()._parse_rule_rows(form)[0]["is_active"] is False

    def test_still_editable_via_the_standalone_view(self):
        """A form that does submit the dropped columns still updates them."""
        row = self._view()._parse_rule_rows(
            self._form(**{"rule-0-priority": "5", "rule-0-currency_code": "USD"})
        )[0]
        assert row["priority"] == 5
        assert row["currency_code"] == "USD"
