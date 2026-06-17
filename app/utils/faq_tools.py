from __future__ import annotations

import asyncio
import difflib
import logging
import os
from dataclasses import dataclass, field
from typing import NamedTuple, Optional

from sqlalchemy import select, text

from app.config import get_settings
from app.db.models import FaqItem
from app.db.session import get_session
from app.utils.data_loaders import _load_faq_items, _normalize_language_code
from app.utils.text_utils import normalize_text, token_set

_logger = logging.getLogger(__name__)

# Per-leg confidence thresholds live in app/config.py (FAQ_SEM_*/FAQ_LEX_*
# env vars) — single source of truth. The two legs are on different scales:
# embedding cosine ~0.5 is near-noise while lexical 0.5 is a moderate overlap,
# so a shared threshold can't be calibrated for both. Each leg maps its score
# to a tier ("strict" / "low" / "none") against its own pair, and the best
# tier wins.

if os.getenv("FAQ_STRICT_THRESHOLD") or os.getenv("FAQ_LOW_CONFIDENCE_THRESHOLD"):
    _logger.warning(
        "FAQ_STRICT_THRESHOLD / FAQ_LOW_CONFIDENCE_THRESHOLD are deprecated and "
        "ignored — use FAQ_SEM_{STRICT,LOW}_THRESHOLD and FAQ_LEX_{STRICT,LOW}_THRESHOLD."
    )

# How many semantic candidates to fetch — surfaced to the LLM on low
# confidence so it can pick the right FAQ entry or ask the user.
_SEM_TOP_K: int = int(os.getenv("FAQ_SEM_TOP_K", "3"))


class FaqCandidate(NamedTuple):
    question: str
    answer: str
    score: float


@dataclass
class FaqSearch:
    """Result of a hybrid FAQ search."""

    answer: Optional[str]
    tier: str  # "strict" | "low" | "none"
    lex_score: float = 0.0
    sem_score: float = 0.0
    candidates: list[FaqCandidate] = field(default_factory=list)


_TIER_RANK = {"none": 0, "low": 1, "strict": 2}


def _score_tier(score: float, strict: float, low: float) -> str:
    if score >= strict:
        return "strict"
    if score >= low:
        return "low"
    return "none"

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
    if na == nb:
        return 1.0
    if na in nb or nb in na:
        # Containment scaled by length ratio — a short query inside a long FAQ
        # question is weak evidence, not a perfect match. The old flat 1.0 made
        # "кредит" a STRICT hit on the first question containing the word.
        shorter, longer = (na, nb) if len(na) <= len(nb) else (nb, na)
        containment = 0.5 + 0.5 * (len(shorter) / len(longer))
    else:
        containment = 0.0
    seq = difflib.SequenceMatcher(a=na, b=nb).ratio()
    ta = token_set(na)
    tb = token_set(nb)
    if ta and tb and (inter := len(ta & tb)):
        # F1 of token-level precision (query coverage) and recall (FAQ coverage).
        # Old |A∩B|/|B| was insensitive to *extra* query tokens — e.g. query
        # "карту нерезидентам" matched FAQ "Как открыть виртуальную карту?" at
        # 0.75 because "нерезидентам" (the discriminative token) was ignored.
        token_score = (2 * inter) / (len(ta) + len(tb))
    else:
        token_score = 0.0
    return max(seq, token_score, containment)


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


# Static column mapping — never interpolate user input into column names.
_LANG_COLUMNS = {
    "ru": ("embedding_ru", "answer_ru", "question_ru"),
    "en": ("embedding_en", "answer_en", "question_en"),
    "uz": ("embedding_uz", "answer_uz", "question_uz"),
}


def _semantic_leg_sql(lang: str) -> str:
    """One per-language SELECT over its embedding column, ready for UNION ALL."""
    emb_col, ans_col, q_col = _LANG_COLUMNS[lang]
    return f"""
        (SELECT
            COALESCE({q_col}, question_ru) AS question,
            COALESCE({ans_col}, answer_ru) AS answer,
            1 - ({emb_col} <=> :vec ::vector) AS similarity
        FROM faq
        WHERE {emb_col} IS NOT NULL
        ORDER BY {emb_col} <=> :vec ::vector
        LIMIT :k)
    """


async def _semantic_lookup(
    query: str, language: str | None = None
) -> list[FaqCandidate]:
    """pgvector cosine search — top-K candidates, best first. score ∈ [0, 1].

    score = 1 - cosine_distance: 1.0 is identical, ~0.7+ is "strong match",
    ~0.5 is "vaguely related", < 0.5 is noise. The embedding model is
    multilingual, so for non-ru queries the ru column is searched as well —
    rows that lack a translation (and its embedding) stay findable. Returns []
    when the feature is disabled, the embedding fails, or no vectors exist.
    """
    settings = get_settings()
    if not settings.faq_embedding_enabled:
        return []

    from app.utils.embeddings import embed_texts

    vectors = await embed_texts([query])
    q_vec = vectors[0] if vectors else None
    if q_vec is None:
        return []

    lang = _normalize_language_code(language)
    legs = [_semantic_leg_sql(lang)]
    if lang != "ru":
        legs.append(_semantic_leg_sql("ru"))

    # Render vector literal manually — pgvector accepts the textual form
    # ``'[0.1, 0.2, ...]'`` cast to ``vector``.
    vec_literal = "[" + ",".join(f"{x:.6f}" for x in q_vec) + "]"
    sql = text(
        "SELECT question, answer, similarity FROM ("
        + " UNION ALL ".join(legs)
        + ") AS c ORDER BY similarity DESC LIMIT :k"
    )
    try:
        async with get_session() as session:
            result = await session.execute(sql, {"vec": vec_literal, "k": _SEM_TOP_K})
            rows = result.all()
    except Exception:
        _logger.exception("semantic FAQ lookup failed")
        return []

    candidates: list[FaqCandidate] = []
    seen_answers: set[str] = set()
    for question, answer, similarity in rows:
        if not answer:
            continue
        answer = str(answer)
        # The same row can surface via both the lang and the ru leg.
        if answer in seen_answers:
            continue
        seen_answers.add(answer)
        score = float(similarity) if similarity is not None else 0.0
        # Clamp — small floating noise can push cosine above 1 or below -1.
        score = min(1.0, max(0.0, score))
        candidates.append(FaqCandidate(str(question or ""), answer, score))
    return candidates


# ---------------------------------------------------------------------------
# Hybrid lookup — exposed APIs.
# ---------------------------------------------------------------------------

import re as _re_faq

def _normalize_answer(text: str) -> str:
    """Minimal normalization for same-row comparison between legs.

    Strips leading/trailing whitespace, casefoldes, and collapses internal
    whitespace. Used only to decide whether two answer strings point at the
    same FAQ row — not for display or scoring.
    """
    return _re_faq.sub(r"\s+", " ", (text or "").strip().casefold())


async def faq_search(query: str, language: str | None = None) -> FaqSearch:
    """Hybrid FAQ search: lexical + semantic legs in parallel, per-leg tiers.

    Each leg maps its score to "strict" / "low" / "none" against its own
    threshold pair; the leg with the better tier supplies the answer (semantic
    wins ties — embeddings are the more reliable signal). Semantic candidates
    are always attached so callers can surface alternatives on low confidence.
    """
    settings = get_settings()
    if settings.faq_embedding_enabled:
        lex_task = asyncio.create_task(_lexical_lookup(query, language))
        sem_task = asyncio.create_task(_semantic_lookup(query, language))
        lex_answer, lex_score = await lex_task
        candidates = await sem_task
    else:
        lex_answer, lex_score = await _lexical_lookup(query, language)
        candidates = []

    sem_top = candidates[0] if candidates else None
    sem_score = sem_top.score if sem_top else 0.0
    sem_tier = (
        _score_tier(sem_score, settings.faq_sem_strict_threshold, settings.faq_sem_low_threshold)
        if sem_top else "none"
    )
    lex_tier = (
        _score_tier(lex_score, settings.faq_lex_strict_threshold, settings.faq_lex_low_threshold)
        if lex_answer else "none"
    )

    if sem_top and _TIER_RANK[sem_tier] >= _TIER_RANK[lex_tier]:
        answer, tier, leg = sem_top.answer, sem_tier, "sem"
    else:
        answer, tier, leg = lex_answer, lex_tier, "lex"

    # Cross-leg agreement promotion: if BOTH legs independently agree on the
    # SAME FAQ entry at tier=low, treat it as strict. Two independent weak
    # signals pointing at the same row are collectively a strong signal.
    # Conservative: only promotes low+low agreement, never upgrades "none".
    if (
        sem_top is not None
        and lex_answer is not None
        and sem_tier == "low"
        and lex_tier == "low"
        and _normalize_answer(sem_top.answer) == _normalize_answer(lex_answer)
    ):
        tier = "strict"

    # Local import: app.agent.tools imports this module, so a module-level
    # import of app.agent would be circular.
    from app.agent.pii_masker import mask_pii

    safe_query = mask_pii(query)[:120]
    if tier == "strict":
        _logger.debug(
            "faq_hit leg=%s lex=%.2f sem=%.2f query=%r",
            leg, lex_score, sem_score, safe_query,
        )
    else:
        # INFO on purpose: unanswered queries are the signal for which FAQ
        # entries are missing — grep production logs for "faq_miss".
        _logger.info(
            "faq_miss tier=%s lex=%.2f sem=%.2f query=%r top=%r",
            tier, lex_score, sem_score, safe_query,
            sem_top.question[:120] if sem_top else None,
        )

    return FaqSearch(
        answer=answer if tier != "none" else None,
        tier=tier,
        lex_score=lex_score,
        sem_score=sem_score,
        candidates=candidates,
    )


async def _faq_lookup(query: str, language: str | None = None) -> Optional[str]:
    """Binary wrapper — returns answer iff the hybrid tier is strict, else None.

    Preserves the old contract for node_faq's APIError fallback and calc_flow's
    side-question handler.
    """
    result = await faq_search(query, language)
    if result.tier == "strict":
        return result.answer
    return None
