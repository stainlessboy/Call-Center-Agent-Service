"""Shared text normalization.

Only ``normalize_text`` survives here. The suffix stemmer, token sets and the
question-frame stopword list this module used to carry existed solely for the
hand-rolled lexical leg of the FAQ search (``_faq_similarity``); keyword
matching is now BM25 inside Weaviate, which brings its own tokenization.
"""
from __future__ import annotations

import re


def normalize_text(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace.

    Used for the per-turn FAQ cache key, where two spellings of the same
    question must hash alike.
    """
    lowered = (text or "").lower()
    lowered = re.sub(r"[^\w\s]+", " ", lowered, flags=re.UNICODE)
    return re.sub(r"\s+", " ", lowered).strip()
