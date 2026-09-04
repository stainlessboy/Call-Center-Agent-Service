"""Tests for FAQ search: Weaviate hybrid retrieval + LLM rerank.

Replaces tests/test_faq_embeddings.py, which covered the two-leg
(difflib + pgvector) search and its four score thresholds. Both legs and all
four thresholds are gone; confidence now comes from the rerank's refusal, so
that is what these tests pin down.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.utils.vector_store import FaqHit


def _run(coro):
    return asyncio.run(coro)


def _hit(faq_id, question, answer, score=0.9, lang="ru"):
    return FaqHit(faq_id=faq_id, lang=lang, question=question, answer=answer, score=score)


def _openai_returning(content: str):
    """Patch target for ``openai.AsyncOpenAI`` returning a fixed JSON body."""
    client = MagicMock()
    client.chat.completions.create = AsyncMock(
        return_value=SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
        )
    )
    client.close = AsyncMock()
    return MagicMock(return_value=client)


@pytest.fixture
def rerank_on(monkeypatch):
    monkeypatch.setenv("FAQ_RERANK_ENABLED", "true")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    from app.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ── _llm_rerank ────────────────────────────────────────────────────────────

class TestLlmRerank:
    def test_picks_the_named_index(self, rerank_on):
        from app.utils.faq_tools import _llm_rerank

        hits = [_hit(1, "q1", "a1"), _hit(2, "q2", "a2")]
        with patch("openai.AsyncOpenAI", _openai_returning('{"best": 2, "conf": 1}')):
            assert _run(_llm_rerank("вопрос", hits)) == (1, True)

    def test_explicit_refusal_is_trusted(self, rerank_on):
        """best=null means "no entry fits" — a real answer, not a failure."""
        from app.utils.faq_tools import _llm_rerank

        with patch("openai.AsyncOpenAI", _openai_returning('{"best": null, "conf": 0}')):
            assert _run(_llm_rerank("погода", [_hit(1, "q", "a")])) == (None, True)

    def test_out_of_range_index_is_refusal(self, rerank_on):
        from app.utils.faq_tools import _llm_rerank

        with patch("openai.AsyncOpenAI", _openai_returning('{"best": 99}')):
            assert _run(_llm_rerank("q", [_hit(1, "q", "a")])) == (None, True)

    def test_api_failure_reports_not_ok(self, rerank_on):
        """A failed call must be distinguishable from a refusal — the caller
        falls back to surfacing candidates instead of reporting no match."""
        from app.utils.faq_tools import _llm_rerank

        client = MagicMock()
        client.chat.completions.create = AsyncMock(side_effect=RuntimeError("boom"))
        client.close = AsyncMock()
        with patch("openai.AsyncOpenAI", MagicMock(return_value=client)):
            assert _run(_llm_rerank("q", [_hit(1, "q", "a")])) == (None, False)

    def test_malformed_json_reports_not_ok(self, rerank_on):
        from app.utils.faq_tools import _llm_rerank

        with patch("openai.AsyncOpenAI", _openai_returning("not json")):
            assert _run(_llm_rerank("q", [_hit(1, "q", "a")])) == (None, False)

    def test_disabled_returns_not_ok(self, monkeypatch):
        monkeypatch.setenv("FAQ_RERANK_ENABLED", "false")
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")
        from app.config import get_settings

        get_settings.cache_clear()
        try:
            from app.utils.faq_tools import _llm_rerank

            assert _run(_llm_rerank("q", [_hit(1, "q", "a")])) == (None, False)
        finally:
            get_settings.cache_clear()

    def test_without_api_key_returns_not_ok(self, monkeypatch):
        monkeypatch.setenv("FAQ_RERANK_ENABLED", "true")
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        from app.config import get_settings

        get_settings.cache_clear()
        try:
            from app.utils.faq_tools import _llm_rerank

            assert _run(_llm_rerank("q", [_hit(1, "q", "a")])) == (None, False)
        finally:
            get_settings.cache_clear()

    def test_no_hits_short_circuits(self, rerank_on):
        from app.utils.faq_tools import _llm_rerank

        with patch("openai.AsyncOpenAI") as ctor:
            assert _run(_llm_rerank("q", [])) == (None, False)
        ctor.assert_not_called()


# ── faq_search tiers ───────────────────────────────────────────────────────

class TestFaqSearchTiers:
    def test_no_candidates_is_none_tier(self, rerank_on):
        from app.utils.faq_tools import faq_search

        with patch("app.utils.faq_tools.vector_store.search", new=AsyncMock(return_value=[])):
            result = _run(faq_search("что угодно", "ru"))
        assert (result.tier, result.answer, result.candidates) == ("none", None, [])

    def test_rerank_pick_is_strict(self, rerank_on):
        from app.utils.faq_tools import faq_search

        hits = [_hit(1, "Как заблокировать карту?", "Через приложение", 0.8)]
        with patch("app.utils.faq_tools.vector_store.search", new=AsyncMock(return_value=hits)), \
             patch("app.utils.faq_tools._llm_rerank", new=AsyncMock(return_value=(0, True))):
            result = _run(faq_search("заблокируйте карту", "ru"))
        assert result.tier == "strict"
        assert result.answer == "Через приложение"
        assert result.score == pytest.approx(0.8)
        assert len(result.candidates) == 1

    def test_rerank_refusal_is_none_but_keeps_candidates(self, rerank_on):
        """A refusal is authoritative — but the candidates stay attached so a
        caller that wants to show alternatives still can."""
        from app.utils.faq_tools import faq_search

        hits = [_hit(1, "Как закрыть карту?", "Инструкция")]
        with patch("app.utils.faq_tools.vector_store.search", new=AsyncMock(return_value=hits)), \
             patch("app.utils.faq_tools._llm_rerank", new=AsyncMock(return_value=(None, True))):
            result = _run(faq_search("какая погода", "ru"))
        assert result.tier == "none"
        assert result.answer is None
        assert len(result.candidates) == 1

    def test_rerank_unavailable_degrades_to_low(self, rerank_on):
        """OpenAI down must NOT read as "no match" — the candidates go to the
        caller's own LLM, which is exactly what the low tier is for."""
        from app.utils.faq_tools import faq_search

        hits = [_hit(1, "Как закрыть карту?", "Инструкция")]
        with patch("app.utils.faq_tools.vector_store.search", new=AsyncMock(return_value=hits)), \
             patch("app.utils.faq_tools._llm_rerank", new=AsyncMock(return_value=(None, False))):
            result = _run(faq_search("закрыть карту", "ru"))
        assert result.tier == "low"
        assert result.answer is None
        assert result.candidates[0].answer == "Инструкция"


# ── wrappers ───────────────────────────────────────────────────────────────

class TestWrappers:
    @pytest.mark.parametrize("tier,expected", [("strict", "Ответ"), ("low", None), ("none", None)])
    def test_faq_lookup_only_returns_on_strict(self, rerank_on, tier, expected):
        from app.utils.faq_tools import FaqSearch, _faq_lookup

        stub = FaqSearch(answer="Ответ" if tier == "strict" else None, tier=tier)
        with patch("app.utils.faq_tools.faq_search", new=AsyncMock(return_value=stub)):
            assert _run(_faq_lookup("q", "ru")) == expected

    @pytest.mark.parametrize("tier,expected", [("strict", "Ответ"), ("low", None), ("none", None)])
    def test_precheck_only_returns_on_strict(self, rerank_on, tier, expected):
        from app.utils.faq_tools import FaqSearch, faq_precheck_answer

        stub = FaqSearch(answer="Ответ" if tier == "strict" else None, tier=tier)
        with patch("app.utils.faq_tools.faq_search", new=AsyncMock(return_value=stub)):
            assert _run(faq_precheck_answer("q", "ru")) == expected


# ── regressions ────────────────────────────────────────────────────────────

class TestKnownFailureRegressions:
    """Two failures measured against the old two-leg search, both of which
    delivered a wrong answer verbatim while skipping the LLM."""

    def test_antonym_does_not_hijack(self, rerank_on):
        """«мне нужно разблокировать карту» used to return the *blocking*
        instructions: the lexical leg found the right row (score 1.000) but
        the fusion rule "semantic wins ties" handed the turn to the semantic
        leg, which had matched the antonym. There is one ranking now, and the
        rerank reads both candidates as a pair."""
        from app.utils.faq_tools import faq_precheck_answer

        hits = [
            _hit(75, "Как заблокировать карту?", "Как ЗАБЛОКИРОВАТЬ", 0.83),
            _hit(17, "Можете ли разблокировать карту?", "Как РАЗБЛОКИРОВАТЬ", 0.78),
        ]
        with patch("app.utils.faq_tools.vector_store.search", new=AsyncMock(return_value=hits)), \
             patch("openai.AsyncOpenAI", _openai_returning('{"best": 2, "conf": 1}')):
            assert _run(faq_precheck_answer("мне нужно разблокировать карту", "ru")) == "Как РАЗБЛОКИРОВАТЬ"

    def test_product_intent_is_refused(self, rerank_on):
        """«Хочу оформить ипотеку» must not be answered from the FAQ at all —
        it belongs to the product catalog. The BOUNDARY rule in the rerank
        prompt is what enforces this; here we pin the wiring that lets a
        refusal reach the caller as "no answer"."""
        from app.utils.faq_tools import faq_precheck_answer

        hits = [_hit(62, "Какие виды кредитов у вас есть?", "Список кредитов", 0.7)]
        with patch("app.utils.faq_tools.vector_store.search", new=AsyncMock(return_value=hits)), \
             patch("openai.AsyncOpenAI", _openai_returning('{"best": null, "conf": 0}')):
            assert _run(faq_precheck_answer("Хочу оформить ипотеку", "ru")) is None


# ── misc contracts ─────────────────────────────────────────────────────────

class TestMisc:
    def test_invalidate_cache_bumps_generation(self):
        from app.utils import faq_tools

        before = faq_tools._cache_generation
        faq_tools.invalidate_cache()
        faq_tools.invalidate_cache()
        assert faq_tools._cache_generation == before + 2

    def test_removed_threshold_env_vars_warn(self, monkeypatch, caplog):
        """The four per-leg thresholds no longer exist. A deployment that
        still sets them must be told, not silently ignored."""
        import importlib

        monkeypatch.setenv("FAQ_SEM_STRICT_THRESHOLD", "0.6")
        with caplog.at_level("WARNING"):
            from app.utils import faq_tools

            importlib.reload(faq_tools)
        assert any("FAQ_SEM_STRICT_THRESHOLD" in r.getMessage() for r in caplog.records)
        monkeypatch.delenv("FAQ_SEM_STRICT_THRESHOLD")
        importlib.reload(faq_tools)

    def test_faq_fallback_sentinel_still_exported(self):
        from app.utils.faq_tools import FAQ_FALLBACK_REPLY, get_faq_fallback

        assert FAQ_FALLBACK_REPLY == "__FAQ_FALLBACK__"
        assert isinstance(get_faq_fallback("ru"), str)
