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


# ── _faq_lookup_with_score (hybrid) ───────────────────────────────────────

class TestHybridLookup:
    def test_falls_back_to_lex_when_sem_unavailable(self, monkeypatch):
        """Without an API key the semantic leg returns (None, 0.0); only lex contributes."""
        monkeypatch.setenv("FAQ_EMBEDDING_ENABLED", "true")
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        from app.config import get_settings
        get_settings.cache_clear()

        from app.utils.faq_tools import _faq_lookup_with_score
        with patch(
            "app.utils.faq_tools._load_faq_items",
            new=AsyncMock(return_value=[
                {"q": "как заблокировать карту", "a": "Зайдите в приложение"}
            ]),
        ):
            answer, score = _run(_faq_lookup_with_score("как заблокировать карту", "ru"))

        assert answer == "Зайдите в приложение"
        assert score >= 0.99  # exact substring match → lex returns 1.0
        get_settings.cache_clear()

    def test_picks_higher_of_two_legs(self, monkeypatch):
        """When both legs return answers, the one with higher score wins."""
        monkeypatch.setenv("FAQ_EMBEDDING_ENABLED", "true")
        monkeypatch.setenv("OPENAI_API_KEY", "fake-key")
        from app.config import get_settings
        get_settings.cache_clear()

        from app.utils.faq_tools import _faq_lookup_with_score
        with patch(
            "app.utils.faq_tools._lexical_lookup",
            new=AsyncMock(return_value=("lex answer", 0.40)),
        ), patch(
            "app.utils.faq_tools._semantic_lookup",
            new=AsyncMock(return_value=("sem answer", 0.85)),
        ):
            answer, score = _run(_faq_lookup_with_score("hello", "ru"))

        assert answer == "sem answer"
        assert score == pytest.approx(0.85)
        get_settings.cache_clear()

    def test_lex_wins_when_higher(self, monkeypatch):
        monkeypatch.setenv("FAQ_EMBEDDING_ENABLED", "true")
        monkeypatch.setenv("OPENAI_API_KEY", "fake-key")
        from app.config import get_settings
        get_settings.cache_clear()

        from app.utils.faq_tools import _faq_lookup_with_score
        with patch(
            "app.utils.faq_tools._lexical_lookup",
            new=AsyncMock(return_value=("lex answer", 0.95)),
        ), patch(
            "app.utils.faq_tools._semantic_lookup",
            new=AsyncMock(return_value=("sem answer", 0.60)),
        ):
            answer, score = _run(_faq_lookup_with_score("hello", "ru"))

        assert answer == "lex answer"
        assert score == pytest.approx(0.95)
        get_settings.cache_clear()

    def test_no_items_returns_none(self, monkeypatch):
        monkeypatch.setenv("FAQ_EMBEDDING_ENABLED", "false")
        from app.config import get_settings
        get_settings.cache_clear()
        from app.utils.faq_tools import _faq_lookup_with_score
        with patch("app.utils.faq_tools._load_faq_items", new=AsyncMock(return_value=[])):
            answer, score = _run(_faq_lookup_with_score("anything", "ru"))
        assert answer is None
        assert score == 0.0
        get_settings.cache_clear()


# ── _semantic_lookup short-circuits ───────────────────────────────────────

class TestSemanticLookupGuards:
    def test_returns_zero_when_disabled(self, monkeypatch):
        monkeypatch.setenv("FAQ_EMBEDDING_ENABLED", "false")
        from app.config import get_settings
        get_settings.cache_clear()
        from app.utils.faq_tools import _semantic_lookup
        answer, score = _run(_semantic_lookup("hello", "ru"))
        assert answer is None
        assert score == 0.0
        get_settings.cache_clear()

    def test_returns_zero_when_embed_fails(self, monkeypatch):
        monkeypatch.setenv("FAQ_EMBEDDING_ENABLED", "true")
        monkeypatch.setenv("OPENAI_API_KEY", "fake-key")
        from app.config import get_settings
        get_settings.cache_clear()
        from app.utils.faq_tools import _semantic_lookup
        with patch(
            "app.utils.embeddings.embed_texts",
            new=AsyncMock(return_value=[None]),
        ):
            answer, score = _run(_semantic_lookup("hello", "ru"))
        assert answer is None
        assert score == 0.0
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
