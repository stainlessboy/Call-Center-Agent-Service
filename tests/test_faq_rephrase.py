"""Tests for app/agent/faq_rephrase.py — the natural-language rephrase pass
over verified FAQ DB answers used by node_faq's deterministic strict
pre-check (see nodes/faq.py::node_faq)."""
from __future__ import annotations

import asyncio

import pytest
from langchain_core.messages import AIMessage


def _run(coro):
    return asyncio.run(coro)


_SOURCE = (
    "Эскроу-счёт — это специальный банковский счёт, предназначенный для "
    "безопасного проведения сделок между сторонами. Комиссия за открытие "
    "составляет 0.5% от суммы, срок открытия — 3 дня."
)


class _FakeRephraseLLM:
    def __init__(self, content):
        self._content = content

    async def ainvoke(self, msgs):
        return AIMessage(content=self._content)


class _BoomLLM:
    async def ainvoke(self, msgs):
        raise RuntimeError("provider down")


# ── _extract_numbers / _extract_urls / _passes_safety_guard ───────────────

class TestSafetyGuardHelpers:
    def test_extract_numbers_handles_space_grouped_thousands(self):
        from app.agent.faq_rephrase import _extract_numbers
        assert _extract_numbers("1 000 000 сум") == {"1000000"}
        assert _extract_numbers("1000000 сум") == {"1000000"}

    def test_extract_numbers_keeps_distinct_numbers_separate(self):
        from app.agent.faq_rephrase import _extract_numbers
        assert _extract_numbers("ставки 12% и 15%") == {"12", "15"}

    def test_extract_urls(self):
        from app.agent.faq_rephrase import _extract_urls
        assert _extract_urls("подробнее: https://asakabank.uz/escrow тут") == {
            "https://asakabank.uz/escrow"
        }

    def test_guard_accepts_faithful_rephrase(self):
        from app.agent.faq_rephrase import _passes_safety_guard
        rephrased = (
            "Эскроу-счёт нужен для безопасных расчётов между сторонами "
            "сделки. Комиссия за его открытие — 0.5% от суммы, а сам "
            "процесс занимает 3 дня."
        )
        assert _passes_safety_guard(_SOURCE, rephrased) is True

    def test_guard_rejects_dropped_number(self):
        from app.agent.faq_rephrase import _passes_safety_guard
        rephrased = "Эскроу-счёт нужен для безопасных расчётов между сторонами сделки."
        assert _passes_safety_guard(_SOURCE, rephrased) is False

    def test_guard_rejects_changed_number(self):
        from app.agent.faq_rephrase import _passes_safety_guard
        rephrased = _SOURCE.replace("0.5%", "1.5%")
        assert _passes_safety_guard(_SOURCE, rephrased) is False

    def test_guard_rejects_empty(self):
        from app.agent.faq_rephrase import _passes_safety_guard
        assert _passes_safety_guard(_SOURCE, "") is False
        assert _passes_safety_guard(_SOURCE, "   ") is False

    def test_guard_rejects_suspiciously_short(self):
        from app.agent.faq_rephrase import _passes_safety_guard
        assert _passes_safety_guard(_SOURCE, "Эскроу — это счёт.") is False

    def test_guard_rejects_dropped_url(self):
        from app.agent.faq_rephrase import _passes_safety_guard
        source = "Подробнее читайте тут: https://asakabank.uz/escrow"
        rephrased = "Подробнее можно почитать на нашем сайте."
        assert _passes_safety_guard(source, rephrased) is False


# ── rephrase_faq_answer: happy / guard / failure / disabled paths ─────────

class TestRephraseFaqAnswer:
    @pytest.mark.asyncio
    async def test_happy_path_returns_rephrased_text(self, monkeypatch):
        from app.agent import faq_rephrase as module
        from app.config import get_settings
        monkeypatch.setenv("FAQ_REPHRASE_ENABLED", "true")
        get_settings.cache_clear()

        good = (
            "Эскроу-счёт нужен для безопасных расчётов между сторонами "
            "сделки. Комиссия за открытие — 0.5% от суммы, срок — 3 дня."
        )
        monkeypatch.setattr(module, "_get_chat_openai", lambda role=None: _FakeRephraseLLM(good))

        result = await module.rephrase_faq_answer(_SOURCE, "что такое эскроу счет", "ru")
        assert result == good
        get_settings.cache_clear()

    @pytest.mark.asyncio
    async def test_guard_rejection_falls_back_to_source(self, monkeypatch):
        """The mocked LLM drops the commission percentage — the safety guard
        must discard the rephrase and return the verbatim source."""
        from app.agent import faq_rephrase as module
        from app.config import get_settings
        monkeypatch.setenv("FAQ_REPHRASE_ENABLED", "true")
        get_settings.cache_clear()

        bad = "Эскроу-счёт нужен для безопасных расчётов между сторонами сделки."
        monkeypatch.setattr(module, "_get_chat_openai", lambda role=None: _FakeRephraseLLM(bad))

        result = await module.rephrase_faq_answer(_SOURCE, "что такое эскроу счет", "ru")
        assert result == _SOURCE
        get_settings.cache_clear()

    @pytest.mark.asyncio
    async def test_llm_exception_falls_back_to_source(self, monkeypatch):
        from app.agent import faq_rephrase as module
        from app.config import get_settings
        monkeypatch.setenv("FAQ_REPHRASE_ENABLED", "true")
        get_settings.cache_clear()

        monkeypatch.setattr(module, "_get_chat_openai", lambda role=None: _BoomLLM())

        result = await module.rephrase_faq_answer(_SOURCE, "что такое эскроу счет", "ru")
        assert result == _SOURCE
        get_settings.cache_clear()

    @pytest.mark.asyncio
    async def test_llm_unavailable_falls_back_to_source(self, monkeypatch):
        from app.agent import faq_rephrase as module
        from app.config import get_settings
        monkeypatch.setenv("FAQ_REPHRASE_ENABLED", "true")
        get_settings.cache_clear()

        monkeypatch.setattr(module, "_get_chat_openai", lambda role=None: None)

        result = await module.rephrase_faq_answer(_SOURCE, "что такое эскроу счет", "ru")
        assert result == _SOURCE
        get_settings.cache_clear()

    @pytest.mark.asyncio
    async def test_disabled_by_setting_skips_llm_entirely(self, monkeypatch):
        from app.agent import faq_rephrase as module
        from app.config import get_settings
        monkeypatch.setenv("FAQ_REPHRASE_ENABLED", "false")
        get_settings.cache_clear()

        # If the disabled check didn't short-circuit, this would blow up —
        # proving the LLM factory was never even called.
        called = {"n": 0}

        def _boom_factory(role=None):
            called["n"] += 1
            raise AssertionError("must not be called when rephrasing is disabled")

        monkeypatch.setattr(module, "_get_chat_openai", _boom_factory)

        result = await module.rephrase_faq_answer(_SOURCE, "что такое эскроу счет", "ru")
        assert result == _SOURCE
        assert called["n"] == 0
        get_settings.cache_clear()

    @pytest.mark.asyncio
    async def test_empty_source_returns_immediately(self, monkeypatch):
        from app.agent import faq_rephrase as module
        result = await module.rephrase_faq_answer("", "что такое эскроу счет", "ru")
        assert result == ""

    @pytest.mark.asyncio
    async def test_timeout_falls_back_to_source(self, monkeypatch):
        from app.agent import faq_rephrase as module
        from app.config import get_settings
        monkeypatch.setenv("FAQ_REPHRASE_ENABLED", "true")
        get_settings.cache_clear()
        monkeypatch.setattr(module, "_REPHRASE_TIMEOUT_SECONDS", 0.01)

        class _SlowLLM:
            async def ainvoke(self, msgs):
                await asyncio.sleep(1)
                return AIMessage(content="too late")

        monkeypatch.setattr(module, "_get_chat_openai", lambda role=None: _SlowLLM())

        result = await module.rephrase_faq_answer(_SOURCE, "что такое эскроу счет", "ru")
        assert result == _SOURCE
        get_settings.cache_clear()
