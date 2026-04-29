from __future__ import annotations

import difflib
import logging
import os
from typing import Optional

from app.utils.data_loaders import _load_faq_items
from app.utils.text_utils import normalize_text, token_set

_logger = logging.getLogger(__name__)

# Thresholds for the tri-tier FAQ confidence system (overridable via env).
_STRICT_THRESHOLD: float = float(os.getenv("FAQ_STRICT_THRESHOLD", "0.62"))
_LOW_CONFIDENCE_THRESHOLD: float = float(os.getenv("FAQ_LOW_CONFIDENCE_THRESHOLD", "0.45"))

# Sentinel kept for backward compat (tests, imports). Use get_faq_fallback(lang) for display.
FAQ_FALLBACK_REPLY = "__FAQ_FALLBACK__"


def get_faq_fallback(lang: str | None = None) -> str:
    from app.agent.i18n import at
    return at("faq_fallback", lang)


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


async def _faq_lookup_with_score(
    query: str, language: str | None = None
) -> tuple[Optional[str], float]:
    """Return (best_answer, best_score) regardless of threshold.

    Callers that need the raw score (e.g. faq_lookup tool for tri-tier dispatch)
    use this; callers that only want a binary hit/miss use _faq_lookup.
    """
    items = await _load_faq_items(language)
    best_answer: Optional[str] = None
    best_score: float = 0.0
    for item in items:
        score = _faq_similarity(query, item.get("q") or "")
        if score > best_score:
            best_score = score
            best_answer = item.get("a")
    _logger.debug("faq_lookup score=%.2f, query=%r", best_score, query[:80])
    return best_answer, best_score


async def _faq_lookup(query: str, language: str | None = None) -> Optional[str]:
    """Thin binary wrapper — returns answer iff score >= STRICT_THRESHOLD, else None.

    The strict-only semantics are preserved here so that node_faq's APIError
    fallback and calc_flow's side-question handler continue to work unchanged.
    """
    answer, score = await _faq_lookup_with_score(query, language)
    if score >= _STRICT_THRESHOLD:
        return answer
    return None
