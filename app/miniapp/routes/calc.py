"""Payment calculator, amortization schedule and the PDF export.

Rate selection and input clamping are delegated to the same helpers the bot's
``node_calc_flow`` uses, so a Mini App calculation and a chat calculation can
never disagree.
"""
from __future__ import annotations

import os
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.agent.i18n import _localized_name
from app.agent.nodes.calc_flow import (
    _clamp_downpayment,
    _clamp_term,
    _lookup_credit_rate,
    _lookup_deposit_rate,
)
from app.db.models import User
from app.miniapp.deps import get_db_user, user_lang
from app.miniapp.routes.catalog import _find_product, _require_category
from app.miniapp.serializers import CREDIT_CATEGORIES, product_bounds
from app.utils.amortization import amortize, deposit_income
from app.utils.pdf_generator import generate_amortization_pdf

router = APIRouter()

SCHEDULE_PREVIEW_ROWS = 6


class CalcPayload(BaseModel):
    product_id: str
    amount: int = Field(gt=0)
    term_months: int = Field(gt=0, le=600)
    downpayment_pct: float | None = Field(default=None, ge=0, le=100)
    age: int | None = Field(default=None, ge=14, le=100)
    currency: str = "UZS"
    # Qualification answers narrow the applicable rate rules (income type).
    income_type: str | None = None


class Adjustment(BaseModel):
    field: Literal["amount", "term_months", "downpayment_pct"]
    requested: float
    applied: float
    reason: Literal["min", "max", "not_available"]


def _clamp_amount(amount: int, bounds: dict) -> tuple[int, Adjustment | None]:
    low = bounds.get("amount_min")
    high = bounds.get("amount_max")
    if low and amount < low:
        return int(low), Adjustment(field="amount", requested=amount, applied=low, reason="min")
    if high and amount > high:
        return int(high), Adjustment(field="amount", requested=amount, applied=high, reason="max")
    return amount, None


async def _resolve(product_id_value: str) -> tuple[dict, str]:
    category, _, _ = product_id_value.rpartition("_")
    _require_category(category)
    found = await _find_product(category, product_id_value)
    if found is None:
        raise HTTPException(status_code=404, detail="Product not found")
    return found, category


def _compute(product: dict, category: str, payload: CalcPayload) -> dict[str, Any]:
    bounds = product_bounds(product, category)
    adjustments: list[Adjustment] = []

    amount, amount_adj = _clamp_amount(payload.amount, bounds)
    if amount_adj:
        adjustments.append(amount_adj)

    term, term_adjusted = _clamp_term(payload.term_months, product, category)
    if term_adjusted:
        adjustments.append(
            Adjustment(
                field="term_months",
                requested=payload.term_months,
                applied=term,
                reason="not_available" if category == "deposit" else (
                    "min" if term > payload.term_months else "max"
                ),
            )
        )

    downpayment = payload.downpayment_pct
    if category in CREDIT_CATEGORIES and downpayment is not None:
        clamped_dp, dp_adjusted = _clamp_downpayment(float(downpayment), product)
        if dp_adjusted:
            adjustments.append(
                Adjustment(
                    field="downpayment_pct",
                    requested=downpayment,
                    applied=clamped_dp,
                    reason="min" if clamped_dp > downpayment else "max",
                )
            )
        downpayment = clamped_dp

    calc_slots = {
        "amount": amount,
        "term_months": term,
        "downpayment": downpayment,
        "age": payload.age,
    }

    if category == "deposit":
        rate = _lookup_deposit_rate(product, calc_slots)
        income = deposit_income(amount, rate, term)
        return {
            "kind": "deposit",
            "amount": amount,
            "term_months": term,
            "rate_pct": rate,
            "income": round(income["income"]),
            "monthly_income": round(income["monthly_income"]),
            "total": round(income["total"]),
            "adjustments": [a.model_dump() for a in adjustments],
        }

    dialog = {"qualify_answers": {"income_types": [payload.income_type]} if payload.income_type else {}}
    rate = _lookup_credit_rate(product, calc_slots, dialog)

    # Downpayment reduces the financed principal; the entered amount is the
    # purchase price, matching the bot's calculator.
    principal = amount
    downpayment_amount = 0
    if downpayment:
        downpayment_amount = round(amount * downpayment / 100)
        principal = amount - downpayment_amount

    result = amortize(principal, rate, term)
    return {
        "kind": "credit",
        "amount": amount,
        "principal": principal,
        "downpayment_pct": downpayment,
        "downpayment_amount": downpayment_amount,
        "term_months": term,
        "rate_pct": rate,
        "monthly_payment": round(result.monthly_payment),
        "total_payment": round(result.total_payment),
        "overpayment": round(result.overpayment),
        "adjustments": [a.model_dump() for a in adjustments],
    }


@router.post("/calc")
async def calc(payload: CalcPayload, user: User = Depends(get_db_user)) -> dict:
    product, category = await _resolve(payload.product_id)
    lang = user_lang(user)
    result = _compute(product, category, payload)
    result["product"] = {
        "id": payload.product_id,
        "category": category,
        "name": _localized_name(product, lang),
    }
    return result


@router.post("/calc/schedule")
async def schedule(
    payload: CalcPayload,
    preview: bool = True,
    user: User = Depends(get_db_user),
) -> dict:
    product, category = await _resolve(payload.product_id)
    if category not in CREDIT_CATEGORIES:
        raise HTTPException(status_code=422, detail="Schedule is available for credits only")

    computed = _compute(product, category, payload)
    result = amortize(computed["principal"], computed["rate_pct"], computed["term_months"])
    rows = [
        {
            "month": r.month,
            "payment": round(r.payment),
            "principal_part": round(r.principal_part),
            "interest_part": round(r.interest_part),
            "balance": round(r.balance),
        }
        for r in result.rows
    ]
    return {
        "total_rows": len(rows),
        "rows": rows[:SCHEDULE_PREVIEW_ROWS] if preview else rows,
        "monthly_payment": computed["monthly_payment"],
        "total_payment": computed["total_payment"],
        "overpayment": computed["overpayment"],
        "rate_pct": computed["rate_pct"],
    }


@router.get("/calc/schedule.pdf")
async def schedule_pdf(
    product_id: str,
    amount: int,
    term_months: int,
    downpayment_pct: float | None = None,
    age: int | None = None,
    income_type: str | None = None,
    user: User = Depends(get_db_user),
) -> FileResponse:
    payload = CalcPayload(
        product_id=product_id,
        amount=amount,
        term_months=term_months,
        downpayment_pct=downpayment_pct,
        age=age,
        income_type=income_type,
    )
    product, category = await _resolve(product_id)
    if category not in CREDIT_CATEGORIES:
        raise HTTPException(status_code=422, detail="Schedule is available for credits only")

    lang = user_lang(user)
    computed = _compute(product, category, payload)
    path = generate_amortization_pdf(
        product_name=_localized_name(product, lang),
        principal=int(computed["principal"]),
        annual_rate_pct=float(computed["rate_pct"]),
        term_months=int(computed["term_months"]),
        borrower_name=(user.first_name or "").strip(),
        lang=lang,
    )
    if not os.path.exists(path):
        raise HTTPException(status_code=500, detail="Failed to generate schedule")
    return FileResponse(
        path,
        media_type="application/pdf",
        filename=os.path.basename(path),
    )
