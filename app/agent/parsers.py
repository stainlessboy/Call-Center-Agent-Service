from __future__ import annotations

import re
from typing import Optional


def _parse_amount(text: str) -> Optional[int]:
    """Parse amount from text, returns integer in UZS."""
    cleaned = text.replace(" ", "").replace(",", "").lower()
    m = re.search(r"(\d+(?:\.\d+)?)\s*(млрд|млн|тыс|тысяч|billion|bln|million|mln|k)?", cleaned)
    if not m:
        return None
    value = float(m.group(1))
    suffix = m.group(2) or ""
    if suffix in ("млрд", "billion", "bln"):
        value *= 1_000_000_000
    elif suffix in ("млн", "million", "mln"):
        value *= 1_000_000
    elif suffix in ("тыс", "тысяч", "k"):
        value *= 1_000
    return int(value)


def _parse_term_months(text: str) -> Optional[int]:
    """Parse term from text, returns months."""
    lower = text.lower().strip()
    m = re.search(r"(\d+)\s*(лет|год|years?|г\.?|yil|months?|мес\.?|м\.?|oy)?", lower)
    if not m:
        return None
    value = int(m.group(1))
    unit = (m.group(2) or "").lower()
    if any(u in unit for u in ("лет", "год", "year", "г.", "yil")):
        return value * 12
    return value  # assume months


def _parse_downpayment(text: str) -> Optional[float]:
    """Parse downpayment percentage from text."""
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*%?", text)
    if m:
        return float(m.group(1).replace(",", "."))
    return None
