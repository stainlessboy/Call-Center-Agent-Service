"""FAQ search: Weaviate hybrid retrieval + an LLM rerank on top.

Two stages, and only two:

1. **Retrieval** — :func:`app.utils.vector_store.search` returns up to
   ``FAQ_CANDIDATE_LIMIT`` candidates from one Weaviate hybrid query (BM25 and
   vector fused by ``FAQ_HYBRID_ALPHA``).
2. **Rerank** — one small LLM call picks the entry that actually answers the
   question, or refuses. Its refusal *is* the confidence signal.

This replaced a hand-rolled lexical leg (difflib + token-F1 + stopword lists),
a pgvector cosine leg, and a tri-tier fusion with four tunable thresholds. Two
measurements drove that:

* The hybrid score cannot be thresholded — it is not comparable across
  queries — so a confidence tier could not be derived from retrieval alone.
* A cross-encoder reranker could have supplied one, but Weaviate's local
  ``reranker-transformers`` module only ships English ``ms-marco`` models,
  which collapse on Cyrillic (3/18 on a labelled Russian set). ``gpt-4o-mini``
  scored 18/18 on the same set, 18/18 on Uzbek, and correctly refused 7 of 8
  out-of-domain queries.

The rerank always uses OpenAI, regardless of ``USE_GPT`` — the same policy the
FAQ embeddings had before it. Keep ``OPENAI_API_KEY`` set, or turn the stage
off with ``FAQ_RERANK_ENABLED=false``.
"""
from __future__ import annotations

import contextvars
import json
import logging
import os
from dataclasses import dataclass, field
from typing import NamedTuple, Optional

from app.config import get_settings
from app.utils.data_loaders import _normalize_language_code
from app.utils.text_utils import normalize_text
from app.utils import vector_store

_logger = logging.getLogger(__name__)

for _dead in ("FAQ_STRICT_THRESHOLD", "FAQ_LOW_CONFIDENCE_THRESHOLD",
              "FAQ_SEM_STRICT_THRESHOLD", "FAQ_SEM_LOW_THRESHOLD",
              "FAQ_LEX_STRICT_THRESHOLD", "FAQ_LEX_LOW_THRESHOLD"):
    if os.getenv(_dead):
        _logger.warning(
            "%s is set but ignored — per-leg score thresholds were removed with "
            "the two-leg search; confidence now comes from the LLM rerank "
            "(FAQ_RERANK_ENABLED / FAQ_RERANK_MODEL).", _dead,
        )


class FaqCandidate(NamedTuple):
    question: str
    answer: str
    score: float


@dataclass
class FaqSearch:
    """Result of a FAQ search.

    ``tier`` keeps its three values so callers read unchanged:

    * ``strict`` — the rerank picked this entry; answer it directly.
    * ``low``    — the rerank was unavailable (disabled, or the call failed),
      so retrieval candidates are surfaced and the caller's own LLM decides.
    * ``none``   — nothing matched, or the rerank refused.
    """

    answer: Optional[str]
    tier: str  # "strict" | "low" | "none"
    score: float = 0.0
    candidates: list[FaqCandidate] = field(default_factory=list)


# Sentinel kept for backward compat (tests, imports). Use get_faq_fallback(lang) for display.
FAQ_FALLBACK_REPLY = "__FAQ_FALLBACK__"


def get_faq_fallback(lang: str | None = None) -> str:
    from app.agent.i18n import at

    return at("faq_fallback", lang)


# Process-local invalidation counter — bumped by the SQLAlchemy listeners after
# any FaqItem write. The index itself lives in Weaviate and every search is a
# fresh query, so there is nothing to evict in-process; downstream caches (if
# any) can observe this counter.
_cache_generation = 0


def invalidate_cache() -> None:
    """Called by the SQLAlchemy event listeners after FAQ writes."""
    global _cache_generation
    _cache_generation += 1


# ---------------------------------------------------------------------------
# Rerank
# ---------------------------------------------------------------------------

# Measured prompt — do not reword casually, both halves earned their place.
# The BOUNDARY paragraph is what stops the FAQ from swallowing product intent:
# without it "Хочу оформить ипотеку" matched a credit FAQ entry and never
# reached get_products (the hijack this project already had an incident over).
# With it, refusals on out-of-domain and product-intent queries went 5/8 → 7/8
# with no loss on the 18 positive probes.
_RERANK_SYSTEM = (
    "ГРАНИЦА: если клиент хочет ПОДОБРАТЬ, ОФОРМИТЬ или РАССЧИТАТЬ продукт "
    "(ипотека, автокредит, микрозайм, вклад, карта) — это НЕ вопрос к FAQ, "
    'верни {"best": null, "conf": 0}: такой запрос обслуживает каталог '
    "продуктов и калькулятор. FAQ отвечает только на вопросы КАК УСТРОЕНО и "
    "ЧТО ДЕЛАТЬ ЕСЛИ.\n"
    "Ты подбираешь запись FAQ банка под вопрос клиента. Тебе дан список "
    "пронумерованных вопросов из базы. Верни СТРОГО JSON "
    '{"best": <номер>, "conf": <0..1>} — номер записи, которая отвечает на '
    'вопрос клиента. Если ни одна не подходит, верни {"best": null, "conf": 0}.'
)


def _openai_kwargs() -> dict | None:
    """AsyncOpenAI kwargs, or None when no API key is configured."""
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return None
    kwargs: dict = {
        "api_key": api_key,
        "timeout": float(os.getenv("OPENAI_REQUEST_TIMEOUT") or 15.0),
        "max_retries": int(os.getenv("OPENAI_MAX_RETRIES") or 1),
    }
    base_url = os.getenv("OPENAI_BASE_URL")
    if base_url:
        kwargs["base_url"] = base_url
    return kwargs


async def _llm_rerank(query: str, hits: list) -> tuple[Optional[int], bool]:
    """Pick the hit that answers *query*.

    Returns ``(index, ok)``: ``index`` into *hits* or ``None`` for a refusal,
    and ``ok`` telling whether the rerank actually ran. A failed call returns
    ``(None, False)`` so the caller can fall back to surfacing candidates
    rather than silently reporting "no match".
    """
    settings = get_settings()
    if not settings.faq_rerank_enabled or not hits:
        return None, False

    kwargs = _openai_kwargs()
    if kwargs is None:
        _logger.warning("faq rerank skipped — OPENAI_API_KEY is not set")
        return None, False

    try:
        from openai import AsyncOpenAI
    except ImportError:
        _logger.error("openai package not installed — cannot rerank FAQ")
        return None, False

    listing = "\n".join(f"{i}. {h.question}" for i, h in enumerate(hits, 1))
    client = None
    try:
        client = AsyncOpenAI(**kwargs)
        response = await client.chat.completions.create(
            model=settings.faq_rerank_model,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": _RERANK_SYSTEM},
                {
                    "role": "user",
                    "content": f"Вопрос клиента: {query}\n\nЗаписи FAQ:\n{listing}",
                },
            ],
        )
        payload = json.loads(response.choices[0].message.content or "{}")
    except Exception:
        _logger.warning("faq rerank call failed", exc_info=True)
        return None, False
    finally:
        if client is not None:
            try:
                await client.close()
            except Exception:
                pass

    best = payload.get("best")
    if best is None:
        return None, True  # an explicit, trustworthy refusal
    try:
        idx = int(best)
    except (TypeError, ValueError):
        return None, True
    if not 1 <= idx <= len(hits):
        return None, True
    return idx - 1, True


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

# Per-turn memoization. node_faq's pre-check and the faq_lookup tool invoked
# later in the same turn's ToolNode loop both call faq_search with the same
# (query, language) — without this they would pay for two rerank calls. Set
# once per agent turn (Agent._ainvoke → reset_faq_turn_cache) and, being an
# asyncio contextvar, inherited by every coroutine awaited in that task. Left
# at its default None, faq_search behaves exactly as before: no caching.
_faq_turn_cache: contextvars.ContextVar[Optional[dict]] = contextvars.ContextVar(
    "_faq_turn_cache", default=None
)


def reset_faq_turn_cache() -> None:
    """Start a fresh per-turn faq_search memoization scope."""
    _faq_turn_cache.set({})


async def faq_search(query: str, language: str | None = None) -> FaqSearch:
    """Retrieve candidates from Weaviate, then let the LLM pick one.

    Memoized within the current agent turn (see ``_faq_turn_cache``).
    """
    turn_cache = _faq_turn_cache.get()
    cache_key = None
    if turn_cache is not None:
        cache_key = (normalize_text(query), _normalize_language_code(language))
        cached = turn_cache.get(cache_key)
        if cached is not None:
            return cached

    hits = await vector_store.search(query, language)
    candidates = [FaqCandidate(h.question, h.answer, h.score) for h in hits]

    if not hits:
        result = FaqSearch(answer=None, tier="none", candidates=[])
    else:
        idx, ok = await _llm_rerank(query, hits)
        if idx is not None:
            pick = hits[idx]
            result = FaqSearch(
                answer=pick.answer, tier="strict", score=pick.score, candidates=candidates
            )
        elif ok:
            # The rerank ran and refused — trust it.
            result = FaqSearch(
                answer=None, tier="none", score=hits[0].score, candidates=candidates
            )
        else:
            # The rerank could not run (disabled, no key, OpenAI down). Do NOT
            # report "no match" — hand the candidates to the caller's own LLM,
            # which is exactly what the "low" tier is for.
            result = FaqSearch(
                answer=None, tier="low", score=hits[0].score, candidates=candidates
            )

    # Local import: app.agent.tools imports this module, so a module-level
    # import of app.agent would be circular.
    from app.agent.pii_masker import mask_pii

    safe_query = mask_pii(query)[:120]
    if result.tier == "strict":
        _logger.debug(
            "faq_hit score=%.3f query=%r", result.score, safe_query
        )
    else:
        # INFO on purpose: unanswered queries are the signal for which FAQ
        # entries are missing — grep production logs for "faq_miss".
        _logger.info(
            "faq_miss tier=%s cands=%d query=%r top=%r",
            result.tier, len(candidates), safe_query,
            candidates[0].question[:120] if candidates else None,
        )

    if turn_cache is not None:
        turn_cache[cache_key] = result
    return result


async def _faq_lookup(query: str, language: str | None = None) -> Optional[str]:
    """Binary wrapper — returns the answer iff the tier is strict, else None.

    Preserves the contract node_faq's APIError fallback and calc_flow's
    side-question handler were written against.
    """
    result = await faq_search(query, language)
    return result.answer if result.tier == "strict" else None


async def faq_precheck_answer(query: str, language: str | None = None) -> Optional[str]:
    """Deterministic pre-check for node_faq's pre-LLM shortcut.

    Returns an answer only on ``strict`` — i.e. only when the rerank positively
    identified an entry. Taking this path skips node_faq's LLM turn (and with
    it every product/office tool), so the guard against swallowing a
    product-catalog request lives in the rerank prompt's BOUNDARY rule rather
    than in a score threshold here.

    It is no longer stricter than :func:`faq_search`'s own tier: the two-leg
    version had to be, because the combined tier took the *max* of two
    independently-thresholded legs and a single confident leg could carry a bad
    match through. There is one signal now, and it already encodes the refusal.
    """
    result = await faq_search(query, language)
    return result.answer if result.tier == "strict" else None
