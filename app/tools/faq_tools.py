from __future__ import annotations

import difflib
import re
from typing import Any, Optional

from app.tools.data_loaders import _load_faq_items_sync, _load_builtin_faq_alias_items

FAQ_FALLBACK_REPLY = (
    "Не уверен, что правильно понял вопрос. Уточните, пожалуйста, о чем именно речь: "
    "мобильное приложение, карта, перевод, кредит или отделение."
)


def _normalize_text(text: str) -> str:
    lowered = (text or "").lower()
    lowered = re.sub(r"[^\w\s]+", " ", lowered, flags=re.UNICODE)
    return re.sub(r"\s+", " ", lowered).strip()


def _token_stem(token: str) -> str:
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


def _token_set(text: str) -> set[str]:
    return {t for t in (_token_stem(x) for x in _normalize_text(text).split()) if t}


def _faq_similarity(a: str, b: str) -> float:
    na = _normalize_text(a)
    nb = _normalize_text(b)
    if not na or not nb:
        return 0.0
    if na in nb or nb in na:
        return 1.0
    seq = difflib.SequenceMatcher(a=na, b=nb).ratio()
    ta = _token_set(na)
    tb = _token_set(nb)
    overlap = len(ta & tb) / max(1, len(tb)) if ta and tb else 0.0
    return max(seq, overlap)


def _faq_lookup(query: str, language: str | None = None) -> Optional[str]:
    items = _load_faq_items_sync(language)
    best_answer = None
    best_score = 0.0
    for item in items:
        score = _faq_similarity(query, item.get("q") or "")
        if score > best_score:
            best_score = score
            best_answer = item.get("a")
    # Дополнительный слой: aliases из app/data/faq.json (например "забыл пароль").
    for item in _load_builtin_faq_alias_items():
        phrases = [str(item.get("q") or "")] + [str(x) for x in (item.get("aliases") or [])]
        item_best = 0.0
        for phrase in phrases:
            score = _faq_similarity(query, phrase)
            if score > item_best:
                item_best = score
        if item_best > best_score:
            best_score = item_best
            best_answer = str(item.get("a") or "")
    if best_score >= 0.62:
        return best_answer
    return None
