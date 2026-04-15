from __future__ import annotations

import html as _html
from collections import defaultdict
from typing import Optional

from app.agent.constants import CREDIT_SECTION_MAP
from app.agent.i18n import (
    _localized_name,
    at,
    category_label,
    income_type_label,
)
from app.utils.data_loaders import (
    _load_credit_product_offers,
    _load_deposit_product_offers,
    _load_card_product_offers,
)


def _fmt_rate(offer: dict, lang: str = "ru") -> str:
    low = offer.get("rate_min_pct")
    high = offer.get("rate_max_pct")
    if low is not None and high is not None and abs(float(low) - float(high)) > 0.01:
        return f"{float(low):.1f}–{float(high):.1f}%"
    if low is not None:
        return f"{float(low):.1f}%"
    return str(offer.get("rate_text") or at("rate_tbd", lang))


async def _get_products_by_category(category: str) -> list[dict]:
    """Return aggregated list of products for given category from DB."""

    if category in CREDIT_SECTION_MAP:
        section = CREDIT_SECTION_MAP[category]
        groups: dict[str, list[dict]] = defaultdict(list)
        for offer in await _load_credit_product_offers():
            if offer.get("section_name") != section:
                continue
            name = str(offer.get("service_name") or "").strip()
            if name:
                groups[name].append(offer)

        result: list[dict] = []
        for name, rows in groups.items():
            first = rows[0]
            all_min = [r["rate_min_pct"] for r in rows if r.get("rate_min_pct") is not None]
            all_max = [r["rate_max_pct"] for r in rows if r.get("rate_max_pct") is not None]
            g_min = min(all_min) if all_min else None
            g_max = max(all_max) if all_max else None
            rate_matrix = [
                {
                    "income_type": r.get("income_type"),
                    "rate_min_pct": r.get("rate_min_pct"),
                    "rate_max_pct": r.get("rate_max_pct"),
                    "rate_condition_text": r.get("rate_condition_text") or r.get("rate_text") or "",
                    "term_min_months": r.get("term_min_months"),
                    "term_max_months": r.get("term_max_months"),
                    "downpayment_min_pct": r.get("downpayment_min_pct"),
                    "downpayment_max_pct": r.get("downpayment_max_pct"),
                }
                for r in rows
            ]
            result.append({
                "name": name,
                "name_en": first.get("service_name_en"),
                "name_uz": first.get("service_name_uz"),
                "rate": _fmt_rate({"rate_min_pct": g_min, "rate_max_pct": g_max, "rate_text": first.get("rate_text")}),
                "rate_min_pct": g_min,
                "rate_max_pct": g_max,
                "term": first.get("term_text") or "",
                "amount": first.get("amount_text") or "",
                "downpayment": first.get("downpayment_text") or "",
                "collateral": first.get("collateral_text") or "",
                "purpose": first.get("purpose_text") or "",
                "rate_matrix": rate_matrix,
            })
        return result

    if category == "deposit":
        groups_d: dict[str, list[dict]] = defaultdict(list)
        for offer in await _load_deposit_product_offers():
            name = str(offer.get("service_name") or "").strip()
            if name:
                groups_d[name].append(offer)

        result = []
        for name, rows in groups_d.items():
            first = rows[0]
            rate_schedule = [
                {
                    "currency": r.get("currency_code") or "UZS",
                    "term_months": r.get("term_months"),
                    "term_text": r.get("term_text") or "",
                    "rate_pct": r.get("rate_pct"),
                    "rate_text": r.get("rate_text") or "",
                    "min_amount": r.get("min_amount"),
                    "min_amount_text": r.get("min_amount_text") or "",
                }
                for r in rows
            ]
            all_rates = [e["rate_pct"] for e in rate_schedule if e.get("rate_pct") is not None]
            currencies = sorted({r.get("currency_code") or "UZS" for r in rows})
            rate_range = ""
            if all_rates:
                lo, hi = min(all_rates), max(all_rates)
                rate_range = f"{lo:.1f}%" if abs(lo - hi) < 0.01 else f"{lo:.1f}–{hi:.1f}%"

            # Collect min_amount per currency (take the smallest per currency across terms)
            min_amounts_by_currency: dict[str, tuple[int | None, str]] = {}
            for r in rows:
                cur = r.get("currency_code") or "UZS"
                amt = r.get("min_amount")
                amt_text = r.get("min_amount_text") or ""
                if cur not in min_amounts_by_currency:
                    min_amounts_by_currency[cur] = (amt, amt_text)
                elif amt is not None:
                    existing_amt = min_amounts_by_currency[cur][0]
                    if existing_amt is None or amt < existing_amt:
                        min_amounts_by_currency[cur] = (amt, amt_text)

            # Collect all available terms for term range display
            all_terms = sorted({e["term_months"] for e in rate_schedule if e.get("term_months") is not None})

            result.append({
                "name": name,
                "name_en": first.get("service_name_en"),
                "name_uz": first.get("service_name_uz"),
                "rate": rate_range,
                "rate_pct": first.get("rate_pct"),
                "term": first.get("term_text") or "",
                "term_months": first.get("term_months"),
                "term_min": all_terms[0] if all_terms else None,
                "term_max": all_terms[-1] if all_terms else None,
                "min_amount": first.get("min_amount_text") or "",
                "min_amounts_by_currency": min_amounts_by_currency,
                "currency": ", ".join(currencies),
                "topup": first.get("topup_text") or "",
                "payout": first.get("payout_text") or "",
                "rate_schedule": rate_schedule,
            })
        return result

    if category in ("debit_card", "fx_card"):
        is_fx = category == "fx_card"
        seen: set[str] = set()
        result = []
        for offer in await _load_card_product_offers():
            if bool(offer.get("is_fx_card")) != is_fx:
                continue
            name = str(offer.get("service_name") or "").strip()
            if not name or name in seen:
                continue
            seen.add(name)
            result.append({
                "name": name,
                "name_en": offer.get("service_name_en"),
                "name_uz": offer.get("service_name_uz"),
                "network": offer.get("card_network") or "",
                "currency": offer.get("currency_code") or "",
                "cashback": offer.get("cashback_text") or "",
                "issue_fee": offer.get("issue_fee_text") or "",
                "annual_fee": offer.get("annual_fee_text") or "",
                "delivery": offer.get("delivery_available"),
                "validity": offer.get("validity_text") or "",
                "reissue_fee": offer.get("reissue_fee_text") or "",
                "transfer_fee": offer.get("transfer_fee_text") or "",
                "issuance_time": offer.get("issuance_time_text") or "",
                "mobile_order": offer.get("mobile_order_available"),
                "pickup": offer.get("pickup_available"),
                "payroll": offer.get("payroll_supported"),
            })
        return result

    return []


def _format_product_list_text(products: list[dict], category: str, lang: str = "ru") -> str:
    label = category_label(category, lang)
    lines = [at("product_list_header", lang, label=label)]
    for i, p in enumerate(products, 1):
        pname = _html.escape(_localized_name(p, lang))
        line = f"{i}. {pname}"
        if category == "deposit":
            # For deposits, rates vary by currency — show currencies instead of rate range
            currency = p.get("currency") or ""
            if currency:
                line += f" — {currency}"
        else:
            rate = p.get("rate") or ""
            if rate:
                line += f" — {rate}"
        lines.append(line)
    lines.append(at("product_list_footer", lang))
    return "\n".join(lines)


def _format_product_card(product: dict, category: str, lang: str = "ru") -> str:
    name = _html.escape(_localized_name(product, lang))
    lines = [f"<b>{name}</b>\n"]

    if category in ("mortgage", "autoloan", "microloan", "education_credit"):
        if product.get("rate"):
            lines.append(f"{at('label_rate', lang)}: {product['rate']}")
        if product.get("amount"):
            lines.append(f"{at('label_amount', lang)}: {product['amount']}")
        if product.get("term"):
            lines.append(f"{at('label_term', lang)}: {product['term']}")
        if product.get("downpayment"):
            lines.append(f"{at('label_downpayment', lang)}: {product['downpayment']}")
        if product.get("purpose"):
            lines.append(f"{at('label_purpose', lang)}: {product['purpose']}")
        if product.get("collateral"):
            lines.append(f"{at('label_collateral', lang)}: {product['collateral']}")

        rate_matrix = product.get("rate_matrix") or []
        if len(rate_matrix) > 1:
            lines.append("")
            lines.append(f"<b>{at('label_rates_by_condition', lang)}</b>")
            displayed = rate_matrix[:8]
            for entry in displayed:
                parts = []
                if entry.get("income_type"):
                    parts.append(income_type_label(entry["income_type"], lang))
                rate_lo = entry.get("rate_min_pct")
                rate_hi = entry.get("rate_max_pct")
                if rate_lo is not None:
                    if rate_hi is not None and abs(rate_lo - rate_hi) > 0.01:
                        parts.append(f"{rate_lo:.1f}–{rate_hi:.1f}%")
                    else:
                        parts.append(f"{rate_lo:.1f}%")
                cond = entry.get("rate_condition_text") or ""
                if cond and not parts:
                    parts.append(cond)
                if parts:
                    lines.append(f"  • {' — '.join(parts)}")
            if len(rate_matrix) > 8:
                lines.append(f"  {at('label_more_variants', lang, count=len(rate_matrix) - 8)}")

    elif category == "deposit":
        if product.get("rate"):
            lines.append(f"{at('label_rate', lang)}: {product['rate']}")
        # Show term range (e.g. "от 1 до 30 мес.")
        t_min = product.get("term_min")
        t_max = product.get("term_max")
        if t_min is not None and t_max is not None:
            if t_min == t_max:
                mo = at("label_months_short", lang)
                lines.append(f"{at('label_term', lang)}: {t_min} {mo}")
            else:
                lines.append(f"{at('label_term', lang)}: {at('label_term_range', lang, t_min=t_min, t_max=t_max)}")
        # Show min amount per currency instead of a single value
        min_by_cur = product.get("min_amounts_by_currency") or {}
        if min_by_cur:
            if len(min_by_cur) == 1:
                cur, (amt, amt_text) = next(iter(min_by_cur.items()))
                display = amt_text or (f"{amt:,}".replace(",", " ") if amt else "—")
                lines.append(f"{at('label_min_amount', lang)}: {display} {cur}")
            else:
                lines.append(f"{at('label_min_amount', lang)}:")
                for cur in sorted(min_by_cur):
                    amt, amt_text = min_by_cur[cur]
                    display = amt_text or (f"{amt:,}".replace(",", " ") if amt else "—")
                    lines.append(f"  • {cur}: {display}")
        elif product.get("min_amount"):
            lines.append(f"{at('label_min_amount', lang)}: {product['min_amount']}")
        if product.get("currency"):
            lines.append(f"{at('label_currency', lang)}: {product['currency']}")
        if product.get("topup"):
            lines.append(f"{at('label_topup', lang)}: {product['topup']}")
        if product.get("payout"):
            lines.append(f"{at('label_payout', lang)}: {product['payout']}")

        rate_schedule = product.get("rate_schedule") or []
        if rate_schedule:
            lines.append("")
            lines.append(f"<b>{at('label_rates_by_term', lang)}</b>")
            by_currency: dict[str, list] = defaultdict(list)
            for entry in rate_schedule:
                by_currency[entry.get("currency") or "UZS"].append(entry)
            mo = at("label_months_short", lang)
            for cur in sorted(by_currency):
                entries = sorted(by_currency[cur], key=lambda x: x.get("term_months") or 0)
                if len(by_currency) > 1:
                    lines.append(f"  <b>{cur}:</b>")
                for e in entries[:12]:
                    term = e.get("term_text") or f"{e.get('term_months') or '?'} {mo}"
                    rate = e.get("rate_text") or (f"{e['rate_pct']:.1f}%" if e.get("rate_pct") is not None else "—")
                    lines.append(f"  • {term}: {rate}")
                if len(entries) > 12:
                    lines.append(f"  {at('label_more_entries', lang, count=len(entries) - 12)}")

    elif category in ("debit_card", "fx_card"):
        if product.get("network"):
            lines.append(f"{at('label_network', lang)}: {product['network']}")
        if product.get("currency"):
            lines.append(f"{at('label_currency', lang)}: {product['currency']}")
        if product.get("issue_fee"):
            lines.append(f"{at('label_issue_fee', lang)}: {product['issue_fee']}")
        if product.get("reissue_fee"):
            lines.append(f"{at('label_reissue_fee', lang)}: {product['reissue_fee']}")
        if product.get("annual_fee"):
            lines.append(f"{at('label_annual_fee', lang)}: {product['annual_fee']}")
        if product.get("cashback"):
            lines.append(f"{at('label_cashback', lang)}: {product['cashback']}")
        if product.get("transfer_fee"):
            lines.append(f"{at('label_transfer_fee', lang)}: {product['transfer_fee']}")
        if product.get("validity"):
            lines.append(f"{at('label_validity', lang)}: {product['validity']}")
        if product.get("issuance_time"):
            lines.append(f"{at('label_issuance_time', lang)}: {product['issuance_time']}")
        if product.get("delivery"):
            lines.append(at("label_delivery", lang))
        if product.get("mobile_order"):
            lines.append(at("label_mobile_order", lang))
        if product.get("pickup"):
            lines.append(at("label_pickup", lang))

    return "\n".join(lines)


def _stem(word: str, prefix_len: int = 5) -> str:
    """Return a stem prefix for fuzzy word matching (truncate words longer than prefix_len)."""
    return word[:prefix_len] if len(word) > prefix_len else word


def _all_names(product: dict) -> list[str]:
    """Return all non-empty, lowercased name variants for a product."""
    names: list[str] = []
    for key in ("name", "name_en", "name_uz"):
        val = (product.get(key) or "").lower().strip()
        if val:
            names.append(val)
    return names


def _find_product_by_name(user_text: str, products: list[dict]) -> Optional[dict]:
    """Find product by numeric index, exact, contains, or word-overlap match.

    Uses all localized name variants (name, name_en, name_uz) and stem-based
    fuzzy matching for robustness across languages.
    """
    lower = user_text.lower().strip()

    # 1. Numeric index (e.g. "2" → second product, 1-based)
    if lower.isdigit() and products:
        idx = int(lower) - 1
        if 0 <= idx < len(products):
            return products[idx]

    # 2. Exact match against all name variants
    for p in products:
        if any(n == lower for n in _all_names(p)):
            return p

    # 3. Substring containment match against all name variants
    for p in products:
        for n in _all_names(p):
            if n in lower or lower in n:
                return p

    # 4. Stem-based word-overlap match — tolerates inflection and partial input
    user_words = {_stem(w) for w in lower.split() if len(w) > 3}
    if user_words:
        for p in products:
            for n in _all_names(p):
                pwords = {_stem(w) for w in n.split() if len(w) > 3}
                if user_words & pwords:
                    return p

    return None
