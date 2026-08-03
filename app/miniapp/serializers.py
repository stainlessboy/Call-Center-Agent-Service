"""Domain dicts/ORM objects → JSON payloads for the Mini App.

Deposits and cards are aggregated by name in ``app.agent.products`` and carry no
database id, so every product gets a deterministic surrogate id derived from
``category`` + name. It survives restarts and re-seeds of unrelated rows, which
is all the client needs to re-fetch a product it linked to.
"""
from __future__ import annotations

import hashlib
from typing import Any, Iterable

from app.agent.branches import get_office_type_label
from app.agent.i18n import _localized_name, category_label
from app.agent.rate_rules import needs_age, rate_bounds

CREDIT_CATEGORIES = ("mortgage", "autoloan", "microloan", "education_credit")

# The agent's `category_label` is lowercase and phrased for mid-sentence use
# ("программы автокредита"); screens need a standalone nominative title. These
# mirror the bot's menu button labels minus the emoji.
CATEGORY_TITLES = {
    "mortgage": {"ru": "Ипотека", "en": "Mortgage", "uz": "Ipoteka"},
    "autoloan": {"ru": "Автокредит", "en": "Auto loan", "uz": "Avtokredit"},
    "microloan": {"ru": "Микрозайм", "en": "Microloan", "uz": "Mikroqarz"},
    "education_credit": {
        "ru": "Образовательный кредит",
        "en": "Education loan",
        "uz": "Ta'lim krediti",
    },
    "deposit": {"ru": "Вклад", "en": "Deposit", "uz": "Omonat"},
    "debit_card": {"ru": "Дебетовая карта", "en": "Debit card", "uz": "Debet karta"},
    "fx_card": {"ru": "Валютная карта", "en": "FX card", "uz": "Valyuta kartasi"},
}


def category_title(category: str, lang: str) -> str:
    """Standalone screen title for a category (falls back to the agent label)."""
    entry = CATEGORY_TITLES.get(category) or {}
    return entry.get(lang) or entry.get("ru") or category_label(category, lang)


def product_id(category: str, name: str) -> str:
    digest = hashlib.sha1(f"{category}|{name}".encode()).hexdigest()[:10]
    return f"{category}_{digest}"


def _num(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int | None:
    n = _num(value)
    return int(n) if n is not None else None


def product_summary(product: dict, category: str, lang: str) -> dict:
    """Compact shape for list screens (catalog, qualification results, chat cards)."""
    name = str(product.get("name") or "")
    out: dict[str, Any] = {
        "id": product_id(category, name),
        "category": category,
        "name": _localized_name(product, lang),
        "name_ru": name,
        "rate_text": product.get("rate") or "",
        "rate_min_pct": _num(product.get("rate_min_pct")),
        "rate_max_pct": _num(product.get("rate_max_pct")),
    }

    if category in CREDIT_CATEGORIES:
        out.update(
            {
                "amount_text": product.get("amount") or "",
                "term_text": product.get("term") or "",
                "downpayment_text": product.get("downpayment") or "",
                "needs_age": bool(product.get("needs_age")),
            }
        )
    elif category == "deposit":
        out.update(
            {
                "rate_pct": _num(product.get("rate_pct")),
                "currency": product.get("currency") or "",
                "term_min": _int(product.get("term_min")),
                "term_max": _int(product.get("term_max")),
                "min_amount_text": product.get("min_amount") or "",
            }
        )
    else:  # debit_card / fx_card
        out.update(
            {
                "network": product.get("network") or "",
                "currency": product.get("currency") or "",
                "cashback": product.get("cashback") or "",
                "issue_fee": product.get("issue_fee") or "",
                "annual_fee": product.get("annual_fee") or "",
            }
        )
    return out


def product_detail(product: dict, category: str, lang: str) -> dict:
    """Full shape for the product screen (14/15 in the design handoff)."""
    out = product_summary(product, category, lang)
    out["category_label"] = category_title(category, lang)

    if category in CREDIT_CATEGORIES:
        rules = product.get("rate_rules") or []
        low, high = rate_bounds(rules)
        out.update(
            {
                "purpose": product.get("purpose") or "",
                "collateral": product.get("collateral") or "",
                "rate_low_pct": _num(low),
                "rate_high_pct": _num(high),
                "needs_age": bool(product.get("needs_age")) or needs_age(rules),
                "rate_condition_kind": product.get("rate_condition_kind") or "",
                # What drives the user's rate — rendered as the "От чего зависит
                # ваша ставка" explainer block.
                "rate_matrix": [
                    {
                        "income_type": e.get("income_type"),
                        "rate_min_pct": _num(e.get("rate_min_pct")),
                        "rate_max_pct": _num(e.get("rate_max_pct")),
                        "condition_text": e.get("rate_condition_text") or "",
                        "term_min_months": _int(e.get("term_min_months")),
                        "term_max_months": _int(e.get("term_max_months")),
                        "downpayment_min_pct": _num(e.get("downpayment_min_pct")),
                        "downpayment_max_pct": _num(e.get("downpayment_max_pct")),
                    }
                    for e in (product.get("rate_matrix") or [])
                ],
                "bounds": product_bounds(product, category),
            }
        )
    elif category == "deposit":
        out.update(
            {
                "topup": product.get("topup") or "",
                "payout": product.get("payout") or "",
                "rate_schedule": [
                    {
                        "currency": e.get("currency") or "UZS",
                        "term_months": _int(e.get("term_months")),
                        "term_text": e.get("term_text") or "",
                        "rate_pct": _num(e.get("rate_pct")),
                        "min_amount": _int(e.get("min_amount")),
                        "min_amount_text": e.get("min_amount_text") or "",
                    }
                    for e in (product.get("rate_schedule") or [])
                ],
                "currencies": sorted(
                    {
                        (e.get("currency") or "UZS")
                        for e in (product.get("rate_schedule") or [])
                    }
                ),
                "bounds": product_bounds(product, category),
            }
        )
    else:
        out.update(
            {
                "validity": product.get("validity") or "",
                "reissue_fee": product.get("reissue_fee") or "",
                "transfer_fee": product.get("transfer_fee") or "",
                "issuance_time": product.get("issuance_time") or "",
                "delivery": product.get("delivery"),
                "pickup": product.get("pickup"),
                "mobile_order": product.get("mobile_order"),
                "payroll": product.get("payroll"),
            }
        )
    return out


def product_bounds(product: dict, category: str) -> dict:
    """Input limits for the calculator — drives the soft-correction UX (screen 16)."""
    if category == "deposit":
        schedule = product.get("rate_schedule") or []
        terms = sorted({_int(e.get("term_months")) for e in schedule if e.get("term_months")})
        amounts = [_int(e.get("min_amount")) for e in schedule if e.get("min_amount")]
        return {
            "amount_min": min(amounts) if amounts else None,
            "amount_max": None,
            "term_min_months": terms[0] if terms else None,
            "term_max_months": terms[-1] if terms else None,
            "term_options": terms,
            "downpayment_min_pct": None,
            "downpayment_max_pct": None,
        }

    rules = product.get("rate_rules") or []
    term_mins = [_int(r.get("term_min_months")) for r in rules if r.get("term_min_months")]
    term_maxs = [_int(r.get("term_max_months")) for r in rules if r.get("term_max_months")]
    amt_mins = [_int(r.get("amount_min")) for r in rules if r.get("amount_min")]
    amt_maxs = [_int(r.get("amount_max")) for r in rules if r.get("amount_max")]
    dp_mins = [_num(r.get("downpayment_min_pct")) for r in rules if r.get("downpayment_min_pct") is not None]
    dp_maxs = [_num(r.get("downpayment_max_pct")) for r in rules if r.get("downpayment_max_pct") is not None]

    # Product-level bounds win; rule-level bounds are the fallback for products
    # whose limits are only expressed per tariff.
    amount_min = _int(product.get("amount_min")) or (min(amt_mins) if amt_mins else None)
    amount_max = _int(product.get("amount_max")) or (max(amt_maxs) if amt_maxs else None)

    return {
        "amount_min": amount_min,
        "amount_max": amount_max,
        "term_min_months": min(term_mins) if term_mins else None,
        "term_max_months": max(term_maxs) if term_maxs else None,
        "term_options": [],
        "downpayment_min_pct": min(dp_mins) if dp_mins else None,
        "downpayment_max_pct": max(dp_maxs) if dp_maxs else None,
    }


# ---------------------------------------------------------------------------
# Offices
# ---------------------------------------------------------------------------

def office(obj: Any, lang: str, distance_km: float | None = None) -> dict:
    """Serialize a Filial / SalesOffice / SalesPoint row."""
    code = getattr(obj, "OFFICE_TYPE_CODE", "")

    def localized(field: str) -> str:
        if lang == "uz":
            value = getattr(obj, f"{field}_uz", None)
            if value:
                return str(value)
        return str(getattr(obj, f"{field}_ru", None) or "")

    return {
        "id": f"{code}:{obj.id}",
        "office_type": code,
        "office_type_label": get_office_type_label(code, lang),
        "name": localized("name"),
        "address": localized("address"),
        "landmark": localized("landmark") if hasattr(obj, "landmark_ru") else "",
        "location_url": getattr(obj, "location_url", None) or "",
        "latitude": _num(getattr(obj, "latitude", None)),
        "longitude": _num(getattr(obj, "longitude", None)),
        "phone": getattr(obj, "phone", None) or "",
        "hours": getattr(obj, "hours", None) or "",
        "distance_km": round(distance_km, 1) if distance_km is not None else None,
    }


def offices(objs: Iterable[Any], lang: str) -> list[dict]:
    return [office(o, lang) for o in objs]
