"""Tests for FAQ embedding helpers and hybrid lookup behaviour."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _run(coro):
    return asyncio.run(coro)


# ── embed_texts ───────────────────────────────────────────────────────────

class TestEmbedTexts:
    def test_returns_none_list_when_disabled(self, monkeypatch):
        monkeypatch.setenv("FAQ_EMBEDDING_ENABLED", "false")
        from app.config import get_settings
        get_settings.cache_clear()
        from app.utils.embeddings import embed_texts
        result = _run(embed_texts(["hello", "world"]))
        assert result == [None, None]
        get_settings.cache_clear()

    def test_returns_none_list_when_no_api_key(self, monkeypatch):
        monkeypatch.setenv("FAQ_EMBEDDING_ENABLED", "true")
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        from app.config import get_settings
        get_settings.cache_clear()
        from app.utils.embeddings import embed_texts
        result = _run(embed_texts(["hello"]))
        assert result == [None]
        get_settings.cache_clear()

    def test_empty_input_returns_empty_list(self, monkeypatch):
        from app.config import get_settings
        get_settings.cache_clear()
        from app.utils.embeddings import embed_texts
        result = _run(embed_texts([]))
        assert result == []

    def test_only_blank_inputs_short_circuit(self, monkeypatch):
        monkeypatch.setenv("FAQ_EMBEDDING_ENABLED", "true")
        monkeypatch.setenv("OPENAI_API_KEY", "fake-key")
        from app.config import get_settings
        get_settings.cache_clear()
        from app.utils.embeddings import embed_texts
        result = _run(embed_texts(["", "  ", None]))  # type: ignore[list-item]
        assert result == [None, None, None]
        get_settings.cache_clear()


# ── embed_one_sync ────────────────────────────────────────────────────────

class TestEmbedOneSync:
    def test_returns_none_when_disabled(self, monkeypatch):
        monkeypatch.setenv("FAQ_EMBEDDING_ENABLED", "false")
        from app.config import get_settings
        get_settings.cache_clear()
        from app.utils.embeddings import embed_one_sync
        assert embed_one_sync("hello") is None
        get_settings.cache_clear()

    def test_returns_none_when_blank(self):
        from app.utils.embeddings import embed_one_sync
        assert embed_one_sync("") is None
        assert embed_one_sync("   ") is None

    def test_returns_none_when_no_api_key(self, monkeypatch):
        monkeypatch.setenv("FAQ_EMBEDDING_ENABLED", "true")
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        from app.config import get_settings
        get_settings.cache_clear()
        from app.utils.embeddings import embed_one_sync
        assert embed_one_sync("hello") is None
        get_settings.cache_clear()


# ── _faq_similarity: containment is scaled, not absolute ──────────────────

class TestFaqSimilarityContainment:
    def test_identical_text_is_perfect(self):
        from app.utils.faq_tools import _faq_similarity
        assert _faq_similarity("как заблокировать карту", "Как заблокировать карту?") == 1.0

    def test_short_substring_is_not_perfect(self):
        """A one-word query inside a long FAQ question must NOT score 1.0 —
        the old flat rule made it a STRICT hit on the first matching row."""
        from app.utils.faq_tools import _faq_similarity
        score = _faq_similarity("кредит", "как досрочно погасить кредит в приложении банка")
        assert score < 0.75  # below lex strict


# ── faq_search (hybrid) ───────────────────────────────────────────────────

class TestHybridLookup:
    def test_falls_back_to_lex_when_sem_unavailable(self, monkeypatch):
        """Without an API key the semantic leg returns []; only lex contributes."""
        monkeypatch.setenv("FAQ_EMBEDDING_ENABLED", "true")
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        from app.config import get_settings
        get_settings.cache_clear()

        from app.utils.faq_tools import faq_search
        with patch(
            "app.utils.faq_tools._load_faq_items",
            new=AsyncMock(return_value=[
                {"q": "как заблокировать карту", "a": "Зайдите в приложение"}
            ]),
        ):
            result = _run(faq_search("как заблокировать карту", "ru"))

        assert result.answer == "Зайдите в приложение"
        assert result.tier == "strict"  # exact match → lex 1.0 ≥ lex strict
        get_settings.cache_clear()

    def test_sem_wins_tier_tie(self, monkeypatch):
        """On equal tiers the semantic answer is preferred."""
        monkeypatch.setenv("FAQ_EMBEDDING_ENABLED", "true")
        monkeypatch.setenv("OPENAI_API_KEY", "fake-key")
        from app.config import get_settings
        get_settings.cache_clear()

        from app.utils.faq_tools import FaqCandidate, faq_search
        with patch(
            "app.utils.faq_tools._lexical_lookup",
            new=AsyncMock(return_value=("lex answer", 0.80)),  # lex strict (≥0.75)
        ), patch(
            "app.utils.faq_tools._semantic_lookup",
            new=AsyncMock(return_value=[FaqCandidate("q", "sem answer", 0.85)]),  # sem strict (≥0.60)
        ):
            result = _run(faq_search("hello", "ru"))

        assert result.answer == "sem answer"
        assert result.tier == "strict"
        get_settings.cache_clear()

    def test_per_leg_thresholds(self, monkeypatch):
        """sem 0.55 is only LOW (<0.60), while lex 0.80 is STRICT (≥0.75) — lex
        wins despite the lower raw number: the legs are on different scales."""
        monkeypatch.setenv("FAQ_EMBEDDING_ENABLED", "true")
        monkeypatch.setenv("OPENAI_API_KEY", "fake-key")
        from app.config import get_settings
        get_settings.cache_clear()

        from app.utils.faq_tools import FaqCandidate, faq_search
        with patch(
            "app.utils.faq_tools._lexical_lookup",
            new=AsyncMock(return_value=("lex answer", 0.80)),
        ), patch(
            "app.utils.faq_tools._semantic_lookup",
            new=AsyncMock(return_value=[FaqCandidate("q", "sem answer", 0.55)]),
        ):
            result = _run(faq_search("hello", "ru"))

        assert result.answer == "lex answer"
        assert result.tier == "strict"
        get_settings.cache_clear()

    def test_mid_sem_score_is_low_tier_with_candidates(self, monkeypatch):
        monkeypatch.setenv("FAQ_EMBEDDING_ENABLED", "true")
        monkeypatch.setenv("OPENAI_API_KEY", "fake-key")
        from app.config import get_settings
        get_settings.cache_clear()

        from app.utils.faq_tools import FaqCandidate, faq_search
        candidates = [
            FaqCandidate("q1", "a1", 0.50),
            FaqCandidate("q2", "a2", 0.47),
        ]
        with patch(
            "app.utils.faq_tools._lexical_lookup",
            new=AsyncMock(return_value=(None, 0.0)),
        ), patch(
            "app.utils.faq_tools._semantic_lookup",
            new=AsyncMock(return_value=candidates),
        ):
            result = _run(faq_search("hello", "ru"))

        assert result.tier == "low"
        assert result.answer == "a1"
        assert result.candidates == candidates
        get_settings.cache_clear()

    def test_no_items_returns_none(self, monkeypatch):
        monkeypatch.setenv("FAQ_EMBEDDING_ENABLED", "false")
        from app.config import get_settings
        get_settings.cache_clear()
        from app.utils.faq_tools import faq_search
        with patch("app.utils.faq_tools._load_faq_items", new=AsyncMock(return_value=[])):
            result = _run(faq_search("anything", "ru"))
        assert result.answer is None
        assert result.tier == "none"
        get_settings.cache_clear()


# ── faq_precheck_answer: node_faq's pre-LLM shortcut (stricter than tier) ──

class TestFaqPrecheckAnswer:
    """Regression coverage for the mortgage→microloan hijack (commit after
    6b62221): faq_search's combined tier is the BEST of the two legs, so a
    single mediocre leg was enough to bypass the LLM (and every product
    tool) and return a wrong FAQ answer verbatim. faq_precheck_answer fixes
    this by requiring BOTH legs to independently clear their own strict
    threshold. Numbers below are the measured production scores from the
    bug report."""

    def test_both_legs_strict_returns_answer(self, monkeypatch):
        """Genuine FAQ hit: 'Как заблокировать карту?' — lex=1.000, sem=1.000."""
        monkeypatch.setenv("FAQ_EMBEDDING_ENABLED", "true")
        monkeypatch.setenv("OPENAI_API_KEY", "fake-key")
        from app.config import get_settings
        get_settings.cache_clear()

        from app.utils.faq_tools import FaqCandidate, faq_precheck_answer
        with patch(
            "app.utils.faq_tools._lexical_lookup",
            new=AsyncMock(return_value=("Зайдите в приложение", 1.000)),
        ), patch(
            "app.utils.faq_tools._semantic_lookup",
            new=AsyncMock(return_value=[
                FaqCandidate("Как заблокировать карту?", "Зайдите в приложение", 1.000)
            ]),
        ):
            result = _run(faq_precheck_answer("Как заблокировать карту?", "ru"))

        assert result == "Зайдите в приложение"
        get_settings.cache_clear()

    def test_one_leg_strict_returns_none(self, monkeypatch):
        """Measured hijack: 'Хочу оформить ипотеку' — lex=0.727, sem=0.667.

        Both below their own strict thresholds (lex 0.75, sem 0.60) even
        though faq_search's combined (max-of-legs) tier would call this
        "strict". Must NOT return an answer — this is exactly the mortgage→
        микрозайм hijack from production.
        """
        monkeypatch.setenv("FAQ_EMBEDDING_ENABLED", "true")
        monkeypatch.setenv("OPENAI_API_KEY", "fake-key")
        from app.config import get_settings
        get_settings.cache_clear()

        from app.utils.faq_tools import FaqCandidate, faq_precheck_answer
        with patch(
            "app.utils.faq_tools._lexical_lookup",
            new=AsyncMock(return_value=("FAQ про микрозайм", 0.727)),
        ), patch(
            "app.utils.faq_tools._semantic_lookup",
            new=AsyncMock(return_value=[
                FaqCandidate("q", "FAQ про микрозайм", 0.667)
            ]),
        ):
            result = _run(faq_precheck_answer("Хочу оформить ипотеку", "ru"))

        assert result is None
        get_settings.cache_clear()

    def test_embeddings_disabled_falls_back_to_lexical_only(self, monkeypatch):
        """With semantic search off, the semantic leg can never score — only
        the lexical leg is required, otherwise the pre-check could never
        fire in that configuration."""
        monkeypatch.setenv("FAQ_EMBEDDING_ENABLED", "false")
        from app.config import get_settings
        get_settings.cache_clear()

        from app.utils.faq_tools import faq_precheck_answer
        with patch(
            "app.utils.faq_tools._lexical_lookup",
            new=AsyncMock(return_value=("Зайдите в приложение", 1.000)),
        ):
            result = _run(faq_precheck_answer("Как заблокировать карту?", "ru"))

        assert result == "Зайдите в приложение"
        get_settings.cache_clear()

    def test_embeddings_disabled_and_lex_not_strict_returns_none(self, monkeypatch):
        monkeypatch.setenv("FAQ_EMBEDDING_ENABLED", "false")
        from app.config import get_settings
        get_settings.cache_clear()

        from app.utils.faq_tools import faq_precheck_answer
        with patch(
            "app.utils.faq_tools._lexical_lookup",
            new=AsyncMock(return_value=("FAQ про микрозайм", 0.727)),
        ):
            result = _run(faq_precheck_answer("Хочу оформить ипотеку", "ru"))

        assert result is None
        get_settings.cache_clear()


# ── _semantic_lookup short-circuits ───────────────────────────────────────

class TestSemanticLookupGuards:
    def test_returns_empty_when_disabled(self, monkeypatch):
        monkeypatch.setenv("FAQ_EMBEDDING_ENABLED", "false")
        from app.config import get_settings
        get_settings.cache_clear()
        from app.utils.faq_tools import _semantic_lookup
        assert _run(_semantic_lookup("hello", "ru")) == []
        get_settings.cache_clear()

    def test_returns_empty_when_embed_fails(self, monkeypatch):
        monkeypatch.setenv("FAQ_EMBEDDING_ENABLED", "true")
        monkeypatch.setenv("OPENAI_API_KEY", "fake-key")
        from app.config import get_settings
        get_settings.cache_clear()
        from app.utils.faq_tools import _semantic_lookup
        with patch(
            "app.utils.embeddings.embed_texts",
            new=AsyncMock(return_value=[None]),
        ):
            assert _run(_semantic_lookup("hello", "ru")) == []
        get_settings.cache_clear()


# ── invalidate_cache (no-op contract for pgvector) ────────────────────────

class TestInvalidateCache:
    def test_bumps_generation_counter(self):
        from app.utils import faq_tools
        before = faq_tools._cache_generation
        faq_tools.invalidate_cache()
        faq_tools.invalidate_cache()
        assert faq_tools._cache_generation == before + 2


# ── event listener registration is idempotent ─────────────────────────────

class TestEventRegistrationIdempotent:
    def test_register_twice_is_safe(self):
        from app.db.events import register_faq_embedding_events
        register_faq_embedding_events()
        register_faq_embedding_events()
        # Should not raise; second call is a no-op via the _registered flag.
