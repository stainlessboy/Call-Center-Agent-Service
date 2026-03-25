"""Fetch exchange rates from the Central Bank of Uzbekistan (cbu.uz)."""
from __future__ import annotations

import logging
import time
from typing import Optional

import httpx

_logger = logging.getLogger(__name__)

CBU_API_URL = "https://cbu.uz/oz/arkhiv-kursov-valyut/json/"
_MAIN_CURRENCIES = ("USD", "EUR", "RUB", "GBP", "KZT", "CNY", "JPY", "CHF", "TRY", "KRW")

_ICONS: dict[str, str] = {
    "USD": "🇺🇸",
    "EUR": "🇪🇺",
    "RUB": "🇷🇺",
    "GBP": "🇬🇧",
    "KZT": "🇰🇿",
    "CNY": "🇨🇳",
    "JPY": "🇯🇵",
    "CHF": "🇨🇭",
    "TRY": "🇹🇷",
    "KRW": "🇰🇷",
}

# Simple in-memory cache (TTL 10 min)
_cache: dict[str, tuple[float, list[dict]]] = {}
_CACHE_TTL = 600


async def fetch_cbu_rates(
    currencies: Optional[tuple[str, ...]] = None,
) -> list[dict]:
    """Return list of dicts: {code, name_ru, name_en, name_uz, nominal, rate, diff, date, icon}."""
    currencies = currencies or _MAIN_CURRENCIES
    cache_key = ",".join(currencies)

    cached = _cache.get(cache_key)
    if cached and time.time() - cached[0] < _CACHE_TTL:
        return cached[1]

    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(CBU_API_URL)
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        _logger.warning("CBU API error: %s", exc)
        if cached:
            return cached[1]
        return []

    by_code = {item["Ccy"]: item for item in data}
    result = []
    for code in currencies:
        item = by_code.get(code)
        if not item:
            continue
        result.append({
            "code": code,
            "name_ru": item.get("CcyNm_RU", code),
            "name_en": item.get("CcyNm_EN", code),
            "name_uz": item.get("CcyNm_UZ", code),
            "nominal": item.get("Nominal", "1"),
            "rate": item.get("Rate", "—"),
            "diff": item.get("Diff", "0"),
            "date": item.get("Date", ""),
            "icon": _ICONS.get(code, "💱"),
        })

    _cache[cache_key] = (time.time(), result)
    return result
