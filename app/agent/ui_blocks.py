"""Shared helpers for building Mini App `ui_blocks` payloads (Phase 3 —
"Mini App UX" of the personal-consultant redesign).

See docs/MINIAPP.md "UI blocks" for the full JSON schema of every block
type. This module only holds the `calc_result` schedule-serialization logic,
which is shared verbatim by two call sites that must produce byte-identical
shapes for the same numbers:

  * `app.agent.tools.custom_loan_calculator` — free-form amount/term/rate,
    not tied to a product.
  * `app.agent.nodes.calc_flow` — the deterministic per-product calculator
    finalization (credit + deposit).

Both consume `app.utils.amortization` (the same annuity math already shared
with the PDF generator), so this module stays free of any math of its own —
it only shapes `Amortization` into JSON-safe dicts.
"""
from __future__ import annotations

from typing import Optional

from app.utils.amortization import Amortization, ScheduleRow

# Cap on schedule rows shipped in a calc_result block. A realistic bank
# product tops out around 240 months (20-year mortgage); custom_loan_calculator
# allows free-form input up to 600 months (50 years —
# tools._MAX_CUSTOM_LOAN_TERM_MONTHS). 360 rows (30 years) covers every real
# product with headroom, without risking an oversized JSON payload for a
# pathological custom-calc input. The Mini App gets `schedule_truncated: true`
# when this cap bites, so it can fall back to "full schedule as PDF" instead
# of silently showing a cut-off table. Decision recorded here per the Phase 3
# spec ("подумай про размер — 240 строк это ок для JSON, но зафиксируй решение").
MAX_SCHEDULE_ROWS = 360


def schedule_rows_public(rows: list[ScheduleRow], cap: int = MAX_SCHEDULE_ROWS) -> tuple[list[dict], bool]:
    """Return (rows_as_dicts[:cap], truncated)."""
    truncated = len(rows) > cap
    out = [
        {
            "month": r.month,
            "payment": round(r.payment, 2),
            "principal_part": round(r.principal_part, 2),
            "interest_part": round(r.interest_part, 2),
            "balance": round(r.balance, 2),
        }
        for r in rows[:cap]
    ]
    return out, truncated


def credit_calc_result_block(
    amort: Amortization,
    *,
    product_name: Optional[str],
    amount: float,
    downpayment: float,
) -> dict:
    """Build a `calc_result` (kind="credit") ui_block from an Amortization."""
    rows, truncated = schedule_rows_public(amort.rows)
    downpayment_pct = (downpayment / amount * 100) if amount else 0.0
    return {
        "type": "calc_result",
        "data": {
            "kind": "credit",
            "product_name": product_name,
            "amount": amount,
            "downpayment": downpayment,
            "downpayment_pct": round(downpayment_pct, 2),
            "principal": amort.principal,
            "rate_pct": amort.annual_rate_pct,
            "term_months": amort.term_months,
            "monthly_payment": round(amort.monthly_payment, 2),
            "total_payment": round(amort.total_payment, 2),
            "overpayment": round(amort.overpayment, 2),
            "schedule": rows,
            "schedule_truncated": truncated,
        },
    }


def deposit_calc_result_block(
    *,
    product_name: Optional[str],
    amount: float,
    term_months: int,
    rate_pct: float,
    interest_total: float,
    total: float,
) -> dict:
    """Build a `calc_result` (kind="deposit") ui_block.

    Deposits use simple interest (see amortization.deposit_income), not an
    amortization schedule, so there is no `schedule` field here.
    """
    monthly_income = interest_total / term_months if term_months else 0.0
    return {
        "type": "calc_result",
        "data": {
            "kind": "deposit",
            "product_name": product_name,
            "amount": amount,
            "term_months": term_months,
            "rate_pct": rate_pct,
            "interest_total": round(interest_total, 2),
            "monthly_income": round(monthly_income, 2),
            "total": round(total, 2),
        },
    }
