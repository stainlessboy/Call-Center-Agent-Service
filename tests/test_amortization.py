"""Shared annuity math used by the bot PDF and the Mini App calculator."""
from __future__ import annotations

from app.utils.amortization import amortize, annuity_payment, build_schedule, deposit_income
from app.utils.pdf_generator import _annuity_payment


def test_annuity_matches_the_closed_form():
    # 100 000 000 at 24% for 12 months → ~9 455 960 per month.
    payment = annuity_payment(100_000_000, 0.24 / 12, 12)
    assert round(payment) == 9_455_960


def test_zero_rate_splits_principal_evenly():
    assert annuity_payment(1_200_000, 0, 12) == 100_000


def test_pdf_helper_delegates_to_the_shared_function():
    assert _annuity_payment(50_000_000, 0.2 / 12, 24) == annuity_payment(50_000_000, 0.2 / 12, 24)


def test_schedule_has_one_row_per_month_and_ends_at_zero():
    rows = build_schedule(60_000_000, 18.0, 36)
    assert len(rows) == 36
    assert rows[0].month == 1
    assert rows[-1].balance < 1  # rounding tail only
    assert all(row.balance >= 0 for row in rows)


def test_interest_share_decreases_over_time():
    rows = build_schedule(60_000_000, 18.0, 36)
    assert rows[0].interest_part > rows[-1].interest_part


def test_amortize_totals_are_consistent():
    result = amortize(100_000_000, 24.0, 12)
    assert result.term_months == 12
    assert round(result.total_payment) == round(sum(r.payment for r in result.rows))
    assert round(result.overpayment) == round(result.total_payment - 100_000_000)
    assert result.monthly_payment == result.rows[0].payment


def test_zero_term_is_handled():
    result = amortize(1_000_000, 20.0, 0)
    assert result.rows == []
    assert result.monthly_payment == 0.0


def test_deposit_income_uses_simple_interest():
    result = deposit_income(10_000_000, 24.0, 12)
    assert round(result["income"]) == 2_400_000
    assert round(result["total"]) == 12_400_000
    assert round(result["monthly_income"]) == 200_000


def test_deposit_income_of_zero_term_is_empty():
    assert deposit_income(10_000_000, 24.0, 0)["income"] == 0.0
