"""Unit tests for the credit rate-rule matching engine (app/agent/rate_rules.py)."""
from app.agent.rate_rules import (
    has_usable_rate,
    needs_age,
    rate_bounds,
    select_rate,
    select_rate_value,
)


def _rule(**kw):
    base = {"priority": 0, "rate_min_pct": None, "rate_max_pct": None}
    base.update(kw)
    return base


class TestHelpers:
    def test_has_usable_rate(self):
        assert has_usable_rate([_rule(rate_min_pct=18.0)]) is True
        assert has_usable_rate([_rule(rate_min_pct=None)]) is False
        assert has_usable_rate([]) is False
        assert has_usable_rate(None) is False

    def test_needs_age(self):
        assert needs_age([_rule(age_min=18, age_max=30, rate_min_pct=19.0)]) is True
        assert needs_age([_rule(age_max=65, rate_min_pct=19.0)]) is True
        assert needs_age([_rule(rate_min_pct=19.0)]) is False
        assert needs_age(None) is False

    def test_rate_bounds(self):
        rules = [
            _rule(rate_min_pct=18.0, rate_max_pct=20.0),
            _rule(rate_min_pct=14.0, rate_max_pct=14.0),
            _rule(rate_min_pct=None),  # ignored
        ]
        assert rate_bounds(rules) == (14.0, 20.0)
        assert rate_bounds([_rule(rate_min_pct=22.0)]) == (22.0, 22.0)
        assert rate_bounds([]) == (None, None)


class TestSelectRate:
    def test_no_usable_rate_returns_none(self):
        assert select_rate([_rule(rate_min_pct=None)]) is None
        assert select_rate([]) is None
        assert select_rate(None) is None

    def test_unconstrained_rule_always_matches(self):
        rules = [_rule(rate_min_pct=20.0)]
        assert select_rate_value(rules) == 20.0
        assert select_rate_value(rules, age=99, amount=10**9) == 20.0

    def test_range_axis_requires_value_in_range(self):
        rules = [_rule(rate_min_pct=14.0, term_min_months=12, term_max_months=60)]
        assert select_rate_value(rules, term_months=36) == 14.0
        assert select_rate(rules, term_months=120) is None  # out of range
        assert select_rate(rules) is None  # constrained axis, value missing

    def test_specificity_prefers_more_constrained(self):
        rules = [
            _rule(rate_min_pct=25.0),  # specificity 0
            _rule(rate_min_pct=19.0, age_min=18, age_max=30),  # specificity 1
        ]
        # Both match for age 25; the more specific (age) rule wins even though its
        # rate happens to be lower here — but specificity is the deciding factor.
        assert select_rate_value(rules, age=25) == 19.0
        # Without age only the unconstrained rule matches.
        assert select_rate_value(rules, age=None) == 25.0

    def test_priority_beats_specificity(self):
        rules = [
            _rule(rate_min_pct=10.0, priority=5),  # high priority, specificity 0
            _rule(rate_min_pct=18.0, age_min=18, age_max=30, priority=0),
        ]
        assert select_rate_value(rules, age=25) == 10.0

    def test_lowest_rate_breaks_ties(self):
        rules = [
            _rule(rate_min_pct=22.0, term_min_months=6, term_max_months=60),
            _rule(rate_min_pct=18.0, downpayment_min_pct=10, downpayment_max_pct=90),
        ]
        # Both specificity 1, same priority → lower rate wins.
        assert select_rate_value(rules, term_months=24, downpayment_pct=30) == 18.0

    def test_income_type_exact_match(self):
        rules = [
            _rule(rate_min_pct=22.0, income_type="payroll"),
            _rule(rate_min_pct=18.0, income_type="official"),
        ]
        assert select_rate_value(rules, income_type="payroll") == 22.0
        assert select_rate_value(rules, income_type="official") == 18.0
        # No income supplied → neither income-typed rule matches.
        assert select_rate(rules) is None

    def test_currency_exact_match(self):
        rules = [
            _rule(rate_min_pct=8.0, currency_code="USD"),
            _rule(rate_min_pct=24.0, currency_code="UZS"),
        ]
        assert select_rate_value(rules, currency_code="USD") == 8.0
        assert select_rate_value(rules, currency_code="UZS") == 24.0
