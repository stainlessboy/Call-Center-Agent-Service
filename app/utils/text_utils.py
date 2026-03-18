"""Shared text normalization utilities for FAQ matching and intent classification."""
from __future__ import annotations

import re


def normalize_text(text: str) -> str:
    lowered = (text or "").lower()
    lowered = re.sub(r"[^\w\s]+", " ", lowered, flags=re.UNICODE)
    return re.sub(r"\s+", " ", lowered).strip()


def token_stem(token: str) -> str:
    token = token.strip()
    if len(token) <= 3:
        return token
    for suffix in (
        "ами", "ями", "ого", "ому", "ему", "ыми", "ими", "иях", "ах", "ях",
        "ов", "ев", "ей", "ой", "ый", "ий", "ая", "ое", "ые", "ую", "ам",
        "ям", "ом", "ем", "а", "я", "у", "ю", "е", "ы", "и",
    ):
        if len(token) > 4 and token.endswith(suffix):
            return token[: -len(suffix)]
    return token


def token_set(text: str) -> set[str]:
    return {t for t in (token_stem(x) for x in normalize_text(text).split()) if t}
