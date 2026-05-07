from __future__ import annotations

import asyncio
import difflib
import logging
import os
from typing import Optional

from sqlalchemy import select, text

from app.config import get_settings
from app.db.models import FaqItem
from app.db.session import get_session
from app.utils.data_loaders import _load_faq_items, _normalize_language_code
from app.utils.text_utils import normalize_text, token_set

_logger = logging.getLogger(__name__)

# Lexical thresholds — kept for the lexical leg of the hybrid score and as
# pure fallback when the semantic leg is unavailable.
_STRICT_THRESHOLD: float = float(os.getenv("FAQ_STRICT_THRESHOLD", "0.62"))
_LOW_CONFIDENCE_THRESHOLD: float = float(os.getenv("FAQ_LOW_CONFIDENCE_THRESHOLD", "0.45"))

# Sentinel kept for backward compat (tests, imports). Use get_faq_fallback(lang) for display.
FAQ_FALLBACK_REPLY = "__FAQ_FALLBACK__"


def get_faq_fallback(lang: str | None = None) -> str:
    from app.agent.i18n import at
    return at("faq_fallback", lang)


# ---------------------------------------------------------------------------
# Lexical scoring (unchanged from the previous implementation).
# ---------------------------------------------------------------------------

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


async def _lexical_lookup(
    query: str, language: str | None = None
) -> tuple[Optional[str], float]:
    """Best-match lexical lookup. Returns (answer, score)."""
    items = await _load_faq_items(language)
    best_answer: Optional[str] = None
    best_score: float = 0.0
    for item in items:
        score = _faq_similarity(query, item.get("q") or "")
        if score > best_score:
            best_score = score
            best_answer = item.get("a")
    return best_answer, best_score


# ---------------------------------------------------------------------------
# Semantic scoring (new) — pgvector cosine via SQL.
# ---------------------------------------------------------------------------

# Process-local invalidation flag — bumped by SQLAlchemy event listeners after
# any FaqItem write. The semantic lookup itself does not cache results (each
# query goes to Postgres anyway), but external callers can read this counter
# if they ever want to invalidate their own derived state.
_cache_generation = 0


def invalidate_cache() -> None:
    """Called by SQLAlchemy event listeners after FAQ writes.

    With pgvector the FAQ vectors live in Postgres and every search is a fresh
    SQL query, so there is nothing to evict in-process. We only bump a counter
    that downstream caches (if any) can observe.
    """
    global _cache_generation
    _cache_generation += 1


async def _semantic_lookup(
    query: str, language: str | None = None
) -> tuple[Optional[str], float]:
    """pgvector cosine search. Returns (answer, score) where score ∈ [0, 1].

    score = 1 - cosine_distance, so 1.0 is identical, ~0.7+ is "strong match",
    ~0.5 is "vaguely related", < 0.5 is noise. Returns (None, 0.0) when the
    feature is disabled, the embedding fails, or no vectors exist for the
    requested language.
    """
    settings = get_settings()
    if not settings.faq_embedding_enabled:
        return None, 0.0

    from app.utils.embeddings import embed_texts

    vectors = await embed_texts([query])
    q_vec = vectors[0] if vectors else None
    if q_vec is None:
        return None, 0.0

    lang = _normalize_language_code(language)
    # Validate against a static mapping — never interpolate user input into the
    # column name.
    column_map = {
        "ru": ("embedding_ru", "answer_ru", "question_ru"),
        "en": ("embedding_en", "answer_en", "question_en"),
        "uz": ("embedding_uz", "answer_uz", "question_uz"),
    }
    emb_col, ans_col, q_col = column_map[lang]
    # Render vector literal manually — pgvector accepts the textual form
    # ``'[0.1, 0.2, ...]'`` cast to ``vector``.
    vec_literal = "[" + ",".join(f"{x:.6f}" for x in q_vec) + "]"

    sql = text(
        f"""
        SELECT
            COALESCE({ans_col}, answer_ru) AS answer,
            1 - ({emb_col} <=> :vec ::vector) AS similarity
        FROM faq
        WHERE {emb_col} IS NOT NULL
        ORDER BY {emb_col} <=> :vec ::vector
        LIMIT 1
        """
    )
    try:
        async with get_session() as session:
            result = await session.execute(sql, {"vec": vec_literal})
            row = result.first()
    except Exception:
        _logger.exception("semantic FAQ lookup failed")
        return None, 0.0

    if row is None:
        return None, 0.0
    answer, similarity = row
    score = float(similarity) if similarity is not None else 0.0
    # Clamp — small floating noise can push cosine above 1 or below -1.
    if score < 0:
        score = 0.0
    if score > 1:
        score = 1.0
    return (str(answer) if answer else None), score


# ---------------------------------------------------------------------------
# Hybrid lookup — exposed APIs.
# ---------------------------------------------------------------------------

async def _faq_lookup_with_score(
    query: str, language: str | None = None
) -> tuple[Optional[str], float]:
    """Return (best_answer, best_score) — hybrid of lexical + semantic.

    Strategy: run both legs in parallel, pick whichever scored higher. The two
    score scales are roughly comparable in [0, 1] for our purposes; we
    calibrate per-leg via separate threshold env vars upstream.
    """
    settings = get_settings()
    if settings.faq_embedding_enabled:
        lex_task = asyncio.create_task(_lexical_lookup(query, language))
        sem_task = asyncio.create_task(_semantic_lookup(query, language))
        lex_answer, lex_score = await lex_task
        sem_answer, sem_score = await sem_task
    else:
        lex_answer, lex_score = await _lexical_lookup(query, language)
        sem_answer, sem_score = None, 0.0

    if sem_score >= lex_score and sem_answer:
        best_answer, best_score = sem_answer, sem_score
        leg = "sem"
    else:
        best_answer, best_score = lex_answer, lex_score
        leg = "lex"

    _logger.debug(
        "faq_lookup leg=%s lex=%.2f sem=%.2f → %.2f, query=%r",
        leg, lex_score, sem_score, best_score, query[:80],
    )
    return best_answer, best_score


async def _faq_lookup(query: str, language: str | None = None) -> Optional[str]:
    """Binary wrapper — returns answer iff hybrid score crosses STRICT, else None.

    Preserves the old contract for node_faq's APIError fallback and calc_flow's
    side-question handler.
    """
    answer, score = await _faq_lookup_with_score(query, language)
    if score >= _STRICT_THRESHOLD:
        return answer
    return None
