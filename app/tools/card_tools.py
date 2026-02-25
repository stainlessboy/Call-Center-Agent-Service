from __future__ import annotations

from typing import Any, Optional

from app.tools.data_loaders import _load_card_product_offers_sync


def _select_debit_card_options(slots: dict[str, Any], limit: int = 3) -> list[dict[str, Any]]:
    cards = [dict(x) for x in _load_card_product_offers_sync() if not x.get("is_fx_card")]
    usage = slots.get("usage_type")
    purpose = slots.get("purpose")
    scored: list[tuple[float, dict[str, Any]]] = []
    for item in cards:
        name = str(item.get("service_name") or "").strip()
        lower = name.lower()
        if not name:
            continue
        fee = str(item.get("annual_fee_text") or "").lower()
        issue = str(item.get("issue_fee_text") or "").lower()
        mobile = str(item.get("issuance_time_text") or "").lower()
        score = 0.0
        if item.get("card_network") in {"uzcard", "humo"} or any(t in lower for t in ("uzcard", "humo")):
            score += 1.0
        if purpose == "shopping_transfers":
            score += 0.5
        if usage == "payroll" and (item.get("payroll_supported") is True or "зарплат" in lower):
            score += 2.0
        if usage == "personal" and not ("зарплат" in lower and item.get("payroll_supported") is True):
            score += 0.8
        free = bool(item.get("annual_fee_free")) or bool(item.get("issue_fee_free")) or ("бесплат" in fee or "бесплат" in issue or fee.strip() == "-")
        if free:
            score += 0.7
        if item.get("mobile_order_available") or "мобил" in mobile:
            score += 0.4
        if "индивиду" in lower:
            score -= 0.2
        scored.append(
            (
                score,
                {
                    "name": name,
                    "free": free,
                    "pickup": bool(item.get("pickup_available")) if item.get("pickup_available") is not None else True,
                },
            )
        )
    scored.sort(key=lambda x: x[0], reverse=True)
    if not scored:
        return [_pick_debit_card()]
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for _, card in scored:
        key = card["name"].lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(card)
        if len(result) >= limit:
            break
    return result


def _select_fx_card_options(slots: dict[str, Any], limit: int = 3) -> list[dict[str, Any]]:
    cards = [dict(x) for x in _load_card_product_offers_sync() if x.get("is_fx_card")]
    system = str(slots.get("system") or "visa").lower()
    currency = str(slots.get("currency") or "usd").lower()
    scored: list[tuple[float, dict[str, Any]]] = []
    for item in cards:
        name = str(item.get("service_name") or "").strip()
        lower = name.lower()
        if not name:
            continue
        score = 0.0
        if str(item.get("card_network") or "").lower() == system or system in lower:
            score += 2.0
        card_currency = str(item.get("currency_code") or "").lower()
        if currency == "usd" and (card_currency == "usd" or "usd" in lower or "доллар" in lower or card_currency in {"multi", "unknown", ""}):
            score += 1.0
        if currency == "eur" and (card_currency == "eur" or "eur" in lower or "евро" in lower or card_currency in {"multi", "unknown", ""}):
            score += 1.0
        scored.append((score, {"name": name}))
    scored.sort(key=lambda x: x[0], reverse=True)
    if not scored:
        return [_pick_fx_card(system=system, currency=currency)]
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for _, card in scored:
        key = card["name"].lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(card)
        if len(result) >= limit:
            break
    return result


def _pick_debit_card() -> dict[str, Any]:
    options = _select_debit_card_options({}, limit=1)
    if options:
        return options[0]
    return {"name": "дебетовая карта «X»", "free": True, "pickup": True}


def _pick_fx_card(system: Optional[str] = None, currency: Optional[str] = None) -> dict[str, Any]:
    system = (system or "visa").lower()
    currency = (currency or "usd").lower()
    options = _select_fx_card_options({"system": system, "currency": currency}, limit=1)
    if options:
        return options[0]
    return {"name": f"{system.upper()} {currency.upper()}"}
