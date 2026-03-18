from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.db.models import CardProductOffer, CreditProductOffer, DepositProductOffer, FaqItem
from app.db.session import get_session

logger = logging.getLogger(__name__)


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


@lru_cache(maxsize=1)
def _load_builtin_faq_alias_items() -> tuple[dict[str, Any], ...]:
    try:
        path = Path(__file__).resolve().parents[1] / "data" / "faq.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        items = raw.get("items") if isinstance(raw, dict) else None
        if not isinstance(items, list):
            return tuple()
        parsed: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            q = str(item.get("q") or "").strip()
            a = str(item.get("a") or "").strip()
            aliases_raw = item.get("aliases") or []
            aliases = [str(x).strip() for x in aliases_raw if str(x).strip()] if isinstance(aliases_raw, list) else []
            if not q or not a:
                continue
            parsed.append({"q": q, "a": a, "aliases": aliases})
        return tuple(parsed)
    except Exception:
        logger.exception("Failed to load builtin FAQ aliases")
        return tuple()


async def _load_credit_product_offers() -> list[dict[str, Any]]:
    try:
        async with get_session() as session:
            result = await session.execute(
                select(
                    CreditProductOffer.section_name,
                    CreditProductOffer.service_name,
                    CreditProductOffer.service_name_en,
                    CreditProductOffer.service_name_uz,
                    CreditProductOffer.amount_text,
                    CreditProductOffer.amount_min,
                    CreditProductOffer.amount_max,
                    CreditProductOffer.term_text,
                    CreditProductOffer.term_min_months,
                    CreditProductOffer.term_max_months,
                    CreditProductOffer.downpayment_text,
                    CreditProductOffer.downpayment_min_pct,
                    CreditProductOffer.downpayment_max_pct,
                    CreditProductOffer.income_type,
                    CreditProductOffer.rate_text,
                    CreditProductOffer.rate_min_pct,
                    CreditProductOffer.rate_max_pct,
                    CreditProductOffer.purpose_text,
                    CreditProductOffer.collateral_text,
                    CreditProductOffer.source_row_order,
                    CreditProductOffer.rate_order,
                ).where(CreditProductOffer.is_active.is_(True))
            )
            rows = result.all()
    except Exception:
        logger.exception("Failed to load credit product offers from DB")
        return []

    items: list[dict[str, Any]] = []
    for row in rows:
        (
            section_name,
            service_name,
            service_name_en,
            service_name_uz,
            amount_text,
            amount_min,
            amount_max,
            term_text,
            term_min_months,
            term_max_months,
            downpayment_text,
            downpayment_min_pct,
            downpayment_max_pct,
            income_type,
            rate_text,
            rate_min_pct,
            rate_max_pct,
            purpose_text,
            collateral_text,
            source_row_order,
            rate_order,
        ) = row
        items.append(
            {
                "section_name": str(section_name or ""),
                "service_name": str(service_name or ""),
                "service_name_en": str(service_name_en) if service_name_en else None,
                "service_name_uz": str(service_name_uz) if service_name_uz else None,
                "amount_text": amount_text,
                "amount_min": int(amount_min) if amount_min is not None else None,
                "amount_max": int(amount_max) if amount_max is not None else None,
                "term_text": str(term_text or "") if term_text is not None else "",
                "term_min_months": int(term_min_months) if term_min_months is not None else None,
                "term_max_months": int(term_max_months) if term_max_months is not None else None,
                "downpayment_text": str(downpayment_text or "") if downpayment_text is not None else "",
                "downpayment_min_pct": float(downpayment_min_pct) if downpayment_min_pct is not None else None,
                "downpayment_max_pct": float(downpayment_max_pct) if downpayment_max_pct is not None else None,
                "income_type": str(income_type or "") if income_type else None,
                "rate_text": str(rate_text or "") if rate_text is not None else "",
                "rate_min_pct": float(rate_min_pct) if rate_min_pct is not None else None,
                "rate_max_pct": float(rate_max_pct) if rate_max_pct is not None else None,
                "purpose_text": str(purpose_text or "") if purpose_text is not None else "",
                "collateral_text": str(collateral_text or "") if collateral_text is not None else "",
                "source_row_order": int(source_row_order or 0),
                "rate_order": int(rate_order or 0),
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
                    DepositProductOffer.min_amount_text,
                    DepositProductOffer.min_amount,
                    DepositProductOffer.term_text,
                    DepositProductOffer.term_months,
                    DepositProductOffer.rate_text,
                    DepositProductOffer.rate_pct,
                    DepositProductOffer.open_channel_text,
                    DepositProductOffer.payout_text,
                    DepositProductOffer.payout_monthly_available,
                    DepositProductOffer.payout_end_available,
                    DepositProductOffer.topup_text,
                    DepositProductOffer.topup_allowed,
                    DepositProductOffer.partial_withdrawal_allowed,
                    DepositProductOffer.notes_text,
                    DepositProductOffer.source_row_order,
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
            min_amount_text,
            min_amount,
            term_text,
            term_months,
            rate_text,
            rate_pct,
            open_channel_text,
            payout_text,
            payout_monthly_available,
            payout_end_available,
            topup_text,
            topup_allowed,
            partial_withdrawal_allowed,
            notes_text,
            source_row_order,
        ) = row
        items.append(
            {
                "service_name": str(service_name or ""),
                "service_name_en": str(service_name_en) if service_name_en else None,
                "service_name_uz": str(service_name_uz) if service_name_uz else None,
                "currency_code": str(currency_code or "UZS"),
                "min_amount_text": str(min_amount_text or "") if min_amount_text is not None else "",
                "min_amount": int(min_amount) if min_amount is not None else None,
                "term_text": str(term_text or "") if term_text is not None else "",
                "term_months": int(term_months) if term_months is not None else None,
                "rate_text": str(rate_text or "") if rate_text is not None else "",
                "rate_pct": float(rate_pct) if rate_pct is not None else None,
                "open_channel_text": str(open_channel_text or "") if open_channel_text is not None else "",
                "payout_text": str(payout_text or "") if payout_text is not None else "",
                "payout_monthly_available": bool(payout_monthly_available) if payout_monthly_available is not None else None,
                "payout_end_available": bool(payout_end_available) if payout_end_available is not None else None,
                "topup_text": str(topup_text or "") if topup_text is not None else "",
                "topup_allowed": bool(topup_allowed) if topup_allowed is not None else None,
                "partial_withdrawal_allowed": bool(partial_withdrawal_allowed) if partial_withdrawal_allowed is not None else None,
                "notes_text": str(notes_text or "") if notes_text is not None else "",
                "source_row_order": int(source_row_order or 0),
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
                    CardProductOffer.cashback_text,
                    CardProductOffer.cashback_pct,
                    CardProductOffer.validity_text,
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
            cashback_text,
            cashback_pct,
            validity_text,
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
                "cashback_text": str(cashback_text or "") if cashback_text is not None else "",
                "cashback_pct": float(cashback_pct) if cashback_pct is not None else None,
                "validity_text": str(validity_text or "") if validity_text is not None else "",
                "validity_months": int(validity_months) if validity_months is not None else None,
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


