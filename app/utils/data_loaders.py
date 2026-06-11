from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select

from app.db.models import (
    CardProductOffer,
    CreditProductOffer,
    CreditRateRule,
    DepositProductOffer,
    FaqItem,
)
from app.db.session import get_session

logger = logging.getLogger(__name__)

# Currency suffix used when deriving amount-strings from numeric columns.
# Russian-style ("сум", "$", "€") — products.py is the layer that re-localizes
# when needed; here we just need a consistent display fallback.
_CURRENCY_SUFFIX = {
    "UZS": "сум",
    "USD": "$",
    "EUR": "€",
}


def _fmt_int(n: int | None) -> str:
    if n is None:
        return ""
    return f"{int(n):,}".replace(",", " ")


def _fmt_amount_range(amount_min: int | None, amount_max: int | None, currency: str = "UZS") -> str:
    """Credit amount range — e.g. 'до 1 500 000 000 сум', '5 000 000–1 500 000 000 сум'."""
    suffix = _CURRENCY_SUFFIX.get(currency, "")
    if amount_min and amount_max and amount_min != amount_max:
        return f"{_fmt_int(amount_min)}–{_fmt_int(amount_max)} {suffix}".strip()
    if amount_max:
        return f"до {_fmt_int(amount_max)} {suffix}".strip()
    if amount_min:
        return f"от {_fmt_int(amount_min)} {suffix}".strip()
    return ""


def _fmt_amount_min(min_amount: int | None, currency: str = "UZS") -> str:
    """Deposit minimum amount — e.g. 'от 500 000 сум'."""
    if not min_amount:
        return ""
    suffix = _CURRENCY_SUFFIX.get(currency, "")
    return f"от {_fmt_int(min_amount)} {suffix}".strip()


def _fmt_term_months_range(t_min: int | None, t_max: int | None) -> str:
    """e.g. '12–60 мес', '12 мес'. Empty when neither bound is set."""
    if t_min and t_max and t_min != t_max:
        return f"{int(t_min)}–{int(t_max)} мес"
    months = t_max or t_min
    if months:
        return f"{int(months)} мес"
    return ""


def _fmt_pct_range(p_min: float | None, p_max: float | None, prefix: str = "от") -> str:
    """e.g. '20–80%', 'от 20%'. Empty when both bounds None."""
    if p_min is not None and p_max is not None and abs(float(p_min) - float(p_max)) > 0.01:
        return f"{float(p_min):.0f}–{float(p_max):.0f}%"
    pct = p_min if p_min is not None else p_max
    if pct is None:
        return ""
    fmt = f"{float(pct):.0f}%"
    return f"{prefix} {fmt}" if prefix else fmt


def _fmt_rate(r_min: float | None, r_max: float | None) -> str:
    """Single-rate or range — e.g. '21%', '21–24%'."""
    if r_min is not None and r_max is not None and abs(float(r_min) - float(r_max)) > 0.01:
        return f"{float(r_min):.0f}–{float(r_max):.0f}%"
    rate = r_min if r_min is not None else r_max
    if rate is None:
        return ""
    return f"{float(rate):.0f}%"


def _fmt_cashback(cashback_pct: float | None) -> str:
    if cashback_pct is None:
        return ""
    v = float(cashback_pct)
    # Trim trailing zeros: 1.0 → "1%", 1.5 → "1.5%".
    formatted = f"{v:.1f}".rstrip("0").rstrip(".")
    return f"до {formatted}%"


def _fmt_validity_months(months: int | None) -> str:
    """e.g. '5 лет', '36 мес'. Prefer whole years when divisible."""
    if not months:
        return ""
    months = int(months)
    if months % 12 == 0:
        years = months // 12
        return f"{years} лет" if years != 1 else "1 год"
    return f"{months} мес"


def _fmt_topup_allowed(topup_allowed: bool | None) -> str:
    if topup_allowed is True:
        return "доступно"
    if topup_allowed is False:
        return "недоступно"
    return ""


def _fmt_pct(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        if 0 < v <= 1:
            v *= 100
        return f"{v:.0f}%"
    text = str(value).strip()
    if not text:
        return None
    import re
    nums = re.findall(r"\d+(?:[.,]\d+)?", text)
    if not nums:
        return text
    v = float(nums[0].replace(",", "."))
    if 0 < v <= 1 and "%" not in text:
        v *= 100
    return f"{v:.0f}%"


def _normalize_language_code(language: str | None) -> str:
    code = (language or "").strip().lower()
    return code if code in {"ru", "uz", "en"} else "ru"


async def _load_faq_items(language: str | None = None) -> list[dict[str, str]]:
    lang = _normalize_language_code(language)
    try:
        async with get_session() as session:
            result = await session.execute(
                select(
                    FaqItem.question_ru,
                    FaqItem.answer_ru,
                    FaqItem.question_en,
                    FaqItem.answer_en,
                    FaqItem.question_uz,
                    FaqItem.answer_uz,
                )
            )
            rows = result.all()
    except Exception:
        logger.exception("Failed to load FAQ items from DB")
        return []
    items: list[dict[str, str]] = []
    for q_ru, a_ru, q_en, a_en, q_uz, a_uz in rows:
        localized = {
            "ru": (q_ru, a_ru),
            "en": (q_en or q_ru, a_en or a_ru),
            "uz": (q_uz or q_ru, a_uz or a_ru),
        }
        q, a = localized.get(lang, localized["ru"])
        if q and a:
            items.append({"q": str(q), "a": str(a)})
    return items


def _rule_to_dict(r: CreditRateRule) -> dict[str, Any]:
    return {
        "income_type": str(r.income_type) if r.income_type else None,
        "age_min": int(r.age_min) if r.age_min is not None else None,
        "age_max": int(r.age_max) if r.age_max is not None else None,
        "amount_min": int(r.amount_min) if r.amount_min is not None else None,
        "amount_max": int(r.amount_max) if r.amount_max is not None else None,
        "term_min_months": int(r.term_min_months) if r.term_min_months is not None else None,
        "term_max_months": int(r.term_max_months) if r.term_max_months is not None else None,
        "downpayment_min_pct": float(r.downpayment_min_pct) if r.downpayment_min_pct is not None else None,
        "downpayment_max_pct": float(r.downpayment_max_pct) if r.downpayment_max_pct is not None else None,
        "currency_code": str(r.currency_code) if r.currency_code else None,
        "rate_min_pct": float(r.rate_min_pct) if r.rate_min_pct is not None else None,
        "rate_max_pct": float(r.rate_max_pct) if r.rate_max_pct is not None else None,
        "condition_text": str(r.condition_text) if r.condition_text else "",
        "priority": int(r.priority) if r.priority is not None else 0,
    }


async def _load_credit_product_offers() -> list[dict[str, Any]]:
    """One dict per product (static fields) with a ``rate_rules`` list.

    Tariffs live in credit_rate_rules now; the per-product rate/term/downpayment
    display ranges are derived downstream (products.py) from these rules.
    """
    try:
        async with get_session() as session:
            products = (
                await session.execute(
                    select(CreditProductOffer).where(CreditProductOffer.is_active.is_(True))
                )
            ).scalars().all()
            rule_rows = (
                await session.execute(
                    select(CreditRateRule)
                    .where(CreditRateRule.is_active.is_(True))
                    .order_by(CreditRateRule.priority.desc(), CreditRateRule.id)
                )
            ).scalars().all()
    except Exception:
        logger.exception("Failed to load credit product offers from DB")
        return []

    rules_by_product: dict[int, list[dict[str, Any]]] = {}
    for r in rule_rows:
        rules_by_product.setdefault(r.credit_product_offer_id, []).append(_rule_to_dict(r))

    items: list[dict[str, Any]] = []
    for p in products:
        a_min = int(p.amount_min) if p.amount_min is not None else None
        a_max = int(p.amount_max) if p.amount_max is not None else None
        items.append(
            {
                "id": int(p.id),
                "section_name": str(p.section_name or ""),
                "service_name": str(p.service_name or ""),
                "service_name_en": str(p.service_name_en) if p.service_name_en else None,
                "service_name_uz": str(p.service_name_uz) if p.service_name_uz else None,
                "min_age": int(p.min_age) if p.min_age is not None else None,
                "amount_text": _fmt_amount_range(a_min, a_max),
                "amount_min": a_min,
                "amount_max": a_max,
                "purpose_text": str(p.purpose_text or "") if p.purpose_text is not None else "",
                "collateral_text": str(p.collateral_text or "") if p.collateral_text is not None else "",
                "rate_condition_kind": str(p.rate_condition_kind) if p.rate_condition_kind else None,
                "for_brand_gm": bool(p.for_brand_gm) if p.for_brand_gm is not None else None,
                "for_brand_other": bool(p.for_brand_other) if p.for_brand_other is not None else None,
                "for_market_primary": bool(p.for_market_primary) if p.for_market_primary is not None else None,
                "for_market_secondary": bool(p.for_market_secondary) if p.for_market_secondary is not None else None,
                "for_renovation": bool(p.for_renovation) if p.for_renovation is not None else None,
                "channel_cbu": bool(p.channel_cbu) if p.channel_cbu is not None else None,
                "channel_online": bool(p.channel_online) if p.channel_online is not None else None,
                "rate_rules": rules_by_product.get(int(p.id), []),
            }
        )
    return items


async def _load_deposit_product_offers() -> list[dict[str, Any]]:
    try:
        async with get_session() as session:
            result = await session.execute(
                select(
                    DepositProductOffer.service_name,
                    DepositProductOffer.service_name_en,
                    DepositProductOffer.service_name_uz,
                    DepositProductOffer.currency_code,
                    DepositProductOffer.min_amount,
                    DepositProductOffer.term_months,
                    DepositProductOffer.rate_pct,
                    DepositProductOffer.open_channel_text,
                    DepositProductOffer.payout_text,
                    DepositProductOffer.payout_monthly_available,
                    DepositProductOffer.payout_end_available,
                    DepositProductOffer.topup_allowed,
                    DepositProductOffer.partial_withdrawal_allowed,
                    DepositProductOffer.notes_text,
                ).where(DepositProductOffer.is_active.is_(True))
            )
            rows = result.all()
    except Exception:
        logger.exception("Failed to load deposit product offers from DB")
        return []

    items: list[dict[str, Any]] = []
    for row in rows:
        (
            service_name,
            service_name_en,
            service_name_uz,
            currency_code,
            min_amount,
            term_months,
            rate_pct,
            open_channel_text,
            payout_text,
            payout_monthly_available,
            payout_end_available,
            topup_allowed,
            partial_withdrawal_allowed,
            notes_text,
        ) = row
        cur = str(currency_code or "UZS")
        min_amt = int(min_amount) if min_amount is not None else None
        t_months = int(term_months) if term_months is not None else None
        r_pct = float(rate_pct) if rate_pct is not None else None
        topup_bool = bool(topup_allowed) if topup_allowed is not None else None
        items.append(
            {
                "service_name": str(service_name or ""),
                "service_name_en": str(service_name_en) if service_name_en else None,
                "service_name_uz": str(service_name_uz) if service_name_uz else None,
                "currency_code": cur,
                "min_amount_text": _fmt_amount_min(min_amt, cur),
                "min_amount": min_amt,
                "term_text": _fmt_term_months_range(t_months, t_months),
                "term_months": t_months,
                "rate_text": _fmt_rate(r_pct, r_pct),
                "rate_pct": r_pct,
                "open_channel_text": str(open_channel_text or "") if open_channel_text is not None else "",
                "payout_text": str(payout_text or "") if payout_text is not None else "",
                "payout_monthly_available": bool(payout_monthly_available) if payout_monthly_available is not None else None,
                "payout_end_available": bool(payout_end_available) if payout_end_available is not None else None,
                "topup_text": _fmt_topup_allowed(topup_bool),
                "topup_allowed": topup_bool,
                "partial_withdrawal_allowed": bool(partial_withdrawal_allowed) if partial_withdrawal_allowed is not None else None,
                "notes_text": str(notes_text or "") if notes_text is not None else "",
            }
        )
    return items


async def _load_card_product_offers() -> list[dict[str, Any]]:
    try:
        async with get_session() as session:
            result = await session.execute(
                select(
                    CardProductOffer.service_name,
                    CardProductOffer.service_name_en,
                    CardProductOffer.service_name_uz,
                    CardProductOffer.card_network,
                    CardProductOffer.currency_code,
                    CardProductOffer.is_fx_card,
                    CardProductOffer.is_debit_card,
                    CardProductOffer.payroll_supported,
                    CardProductOffer.issue_fee_text,
                    CardProductOffer.issue_fee_free,
                    CardProductOffer.reissue_fee_text,
                    CardProductOffer.transfer_fee_text,
                    CardProductOffer.cashback_pct,
                    CardProductOffer.validity_months,
                    CardProductOffer.issuance_time_text,
                    CardProductOffer.pin_setup_cbu_text,
                    CardProductOffer.sms_setup_cbu_text,
                    CardProductOffer.pin_setup_mobile_text,
                    CardProductOffer.sms_setup_mobile_text,
                    CardProductOffer.annual_fee_text,
                    CardProductOffer.annual_fee_free,
                    CardProductOffer.mobile_order_available,
                    CardProductOffer.delivery_available,
                    CardProductOffer.pickup_available,
                    CardProductOffer.source_row_order,
                ).where(CardProductOffer.is_active.is_(True))
            )
            rows = result.all()
    except Exception:
        logger.exception("Failed to load card product offers from DB")
        return []

    items: list[dict[str, Any]] = []
    for row in rows:
        (
            service_name,
            service_name_en,
            service_name_uz,
            card_network,
            currency_code,
            is_fx_card,
            is_debit_card,
            payroll_supported,
            issue_fee_text,
            issue_fee_free,
            reissue_fee_text,
            transfer_fee_text,
            cashback_pct,
            validity_months,
            issuance_time_text,
            pin_setup_cbu_text,
            sms_setup_cbu_text,
            pin_setup_mobile_text,
            sms_setup_mobile_text,
            annual_fee_text,
            annual_fee_free,
            mobile_order_available,
            delivery_available,
            pickup_available,
            source_row_order,
        ) = row
        cb_pct = float(cashback_pct) if cashback_pct is not None else None
        v_months = int(validity_months) if validity_months is not None else None
        items.append(
            {
                "service_name": str(service_name or ""),
                "service_name_en": str(service_name_en) if service_name_en else None,
                "service_name_uz": str(service_name_uz) if service_name_uz else None,
                "card_network": str(card_network or "") if card_network else None,
                "currency_code": str(currency_code or "") if currency_code else None,
                "is_fx_card": bool(is_fx_card),
                "is_debit_card": bool(is_debit_card),
                "payroll_supported": bool(payroll_supported) if payroll_supported is not None else None,
                "issue_fee_text": str(issue_fee_text or "") if issue_fee_text is not None else "",
                "issue_fee_free": bool(issue_fee_free) if issue_fee_free is not None else None,
                "reissue_fee_text": str(reissue_fee_text or "") if reissue_fee_text is not None else "",
                "transfer_fee_text": str(transfer_fee_text or "") if transfer_fee_text is not None else "",
                "cashback_text": _fmt_cashback(cb_pct),
                "cashback_pct": cb_pct,
                "validity_text": _fmt_validity_months(v_months),
                "validity_months": v_months,
                "issuance_time_text": str(issuance_time_text or "") if issuance_time_text is not None else "",
                "pin_setup_cbu_text": str(pin_setup_cbu_text or "") if pin_setup_cbu_text is not None else "",
                "sms_setup_cbu_text": str(sms_setup_cbu_text or "") if sms_setup_cbu_text is not None else "",
                "pin_setup_mobile_text": str(pin_setup_mobile_text or "") if pin_setup_mobile_text is not None else "",
                "sms_setup_mobile_text": str(sms_setup_mobile_text or "") if sms_setup_mobile_text is not None else "",
                "annual_fee_text": str(annual_fee_text or "") if annual_fee_text is not None else "",
                "annual_fee_free": bool(annual_fee_free) if annual_fee_free is not None else None,
                "mobile_order_available": bool(mobile_order_available) if mobile_order_available is not None else None,
                "delivery_available": bool(delivery_available) if delivery_available is not None else None,
                "pickup_available": bool(pickup_available) if pickup_available is not None else None,
                "source_row_order": int(source_row_order or 0),
            }
        )
    return items
