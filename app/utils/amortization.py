"""Annuity math shared by the PDF generator, the bot calculator and the Mini App.

Kept free of formatting and i18n so every caller renders the numbers its own
way — the bot as text, the PDF as a table, the Mini App as JSON.
"""
from __future__ import annotations

from dataclasses import dataclass


def annuity_payment(principal: float, monthly_rate: float, n: int) -> float:
    """Monthly annuity payment for *principal* over *n* months."""
    if n <= 0:
        return 0.0
    if monthly_rate == 0:
        return principal / n
    return principal * monthly_rate * (1 + monthly_rate) ** n / ((1 + monthly_rate) ** n - 1)


@dataclass(frozen=True)
class ScheduleRow:
    month: int
    payment: float
    principal_part: float
    interest_part: float
    balance: float


@dataclass(frozen=True)
class Amortization:
    monthly_payment: float
    total_payment: float
    overpayment: float
    principal: float
    annual_rate_pct: float
    term_months: int
    rows: list[ScheduleRow]


def build_schedule(
    principal: float,
    annual_rate_pct: float,
    term_months: int,
) -> list[ScheduleRow]:
    """Full amortization table, one row per month.

    The final balance is clamped at zero so rounding never leaves a negative
    tail on the last row.
    """
    if term_months <= 0:
        return []
    monthly_rate = annual_rate_pct / 100 / 12
    payment = annuity_payment(float(principal), monthly_rate, term_months)

    rows: list[ScheduleRow] = []
    balance = float(principal)
    for month in range(1, term_months + 1):
        interest = balance * monthly_rate
        principal_part = payment - interest
        balance -= principal_part
        if balance < 0:
            balance = 0.0
        rows.append(
            ScheduleRow(
                month=month,
                payment=payment,
                principal_part=principal_part,
                interest_part=interest,
                balance=balance,
            )
        )
    return rows


def amortize(principal: float, annual_rate_pct: float, term_months: int) -> Amortization:
    """Payment, totals and the full schedule in one pass."""
    rows = build_schedule(principal, annual_rate_pct, term_months)
    monthly = rows[0].payment if rows else 0.0
    total = sum(r.payment for r in rows)
    return Amortization(
        monthly_payment=monthly,
        total_payment=total,
        overpayment=max(total - float(principal), 0.0),
        principal=float(principal),
        annual_rate_pct=float(annual_rate_pct),
        term_months=int(term_months),
        rows=rows,
    )


def deposit_income(principal: float, annual_rate_pct: float, term_months: int) -> dict:
    """Simple-interest deposit income (the bot's existing deposit convention)."""
    if term_months <= 0 or principal <= 0:
        return {"income": 0.0, "monthly_income": 0.0, "total": float(principal)}
    income = float(principal) * (annual_rate_pct / 100) * (term_months / 12)
    return {
        "income": income,
        "monthly_income": income / term_months,
        "total": float(principal) + income,
    }
