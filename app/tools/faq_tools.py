from __future__ import annotations

import difflib
from typing import Optional

from app.tools.data_loaders import _load_faq_items, _load_builtin_faq_alias_items
from app.tools.text_utils import normalize_text, token_set

FAQ_FALLBACK_REPLY = (
    "Не уверен, что правильно понял вопрос. Уточните, пожалуйста, о чем именно речь: "
    "мобильное приложение, карта, перевод, кредит или отделение."
)


def _faq_similarity(a: str, b: str) -> float:
    na = normalize_text(a)
    nb = normalize_text(b)
    if not na or not nb:
        return 0.0
    if na in nb or nb in na:
        return 1.0
    seq = difflib.SequenceMatcher(a=na, b=nb).ratio()
    ta = token_set(na)
    tb = token_set(nb)
    overlap = len(ta & tb) / max(1, len(tb)) if ta and tb else 0.0
    return max(seq, overlap)


async def _faq_lookup(query: str, language: str | None = None) -> Optional[str]:
    items = await _load_faq_items(language)
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
