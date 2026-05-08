"""Tests for app.agent.lang_heuristic.

Covers:
- `_classify` — the per-language pattern matcher (regex / script-based).
- `check_lang_mismatch` — the public entry point used by `agent._ainvoke`.
- `looks_worth_llm_recheck` — gating for the LLM fallback when regex stays silent.

The LLM fallback itself lives in `agent._ainvoke` and is exercised in
`tests/test_agent.py` (or via integration). Here we only verify the cheap
regex layer and the gating helper.
"""
from __future__ import annotations

import pytest

from app.agent.lang_heuristic import (
    _classify,
    check_lang_mismatch,
    looks_worth_llm_recheck,
)


class TestClassifyUzbek:
    @pytest.mark.parametrize(
        "text",
        [
            "qancha foiz",                      # qancha marker
            "menga kredit kerak",               # menga + kerak
            "Assalomu alaykum",                 # greeting
            "филиал қаерда",                    # Uzbek Cyrillic char + qaerda
            "рахмат катта",                     # rahmat
            "кредит олмокчиман",                # Cyrillic morphology
            "yo'q",                             # negation with apostrophe
            "ko'rsating",                       # apostrophe digraph
        ],
    )
    def test_detects_uz(self, text: str) -> None:
        assert _classify(text) == "uz"


class TestClassifyRussian:
    @pytest.mark.parametrize(
        "text",
        [
            "покажи все филиалы",
            "мне нужен кредит",
            "хочу ипотеку",
            "какая ставка по кредиту",
            "где ваш офис",
        ],
    )
    def test_detects_ru(self, text: str) -> None:
        assert _classify(text) == "ru"


class TestClassifyEnglish:
    @pytest.mark.parametrize(
        "text",
        [
            "I need a loan please",
            "show me branches",
            "what is the rate",
        ],
    )
    def test_detects_en(self, text: str) -> None:
        assert _classify(text) == "en"


class TestClassifyAmbiguous:
    """Inputs the regex must NOT classify — they need the LLM fallback."""

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "?",
            "12345",
            "🙂",
            "ok",                               # too short, no marker
            "ман уй омогчиман адамга",          # Cyrillic Uzbek typo, no marker
            "man uy omoqchiman",                # Latin Uzbek typo, no apostrophe, no marker
            "Qanday ipoteka bor?",              # Latin "qanday X bor", no apostrophe (qanday isn't in markers)
        ],
    )
    def test_returns_none(self, text: str) -> None:
        # The point of this test is to lock in the gap that motivated the LLM
        # fallback. If a future regex change starts catching these, the LLM
        # fallback is still safe — just slightly less needed.
        assert _classify(text) is None


class TestCheckLangMismatch:
    def test_returns_none_when_current_lang_invalid(self) -> None:
        assert check_lang_mismatch("Assalomu alaykum", None) is None
        assert check_lang_mismatch("Assalomu alaykum", "fr") is None

    def test_returns_none_when_same_lang(self) -> None:
        assert check_lang_mismatch("Assalomu alaykum", "uz") is None

    def test_returns_target_lang_on_mismatch(self) -> None:
        assert check_lang_mismatch("Assalomu alaykum", "ru") == "uz"
        assert check_lang_mismatch("show me branches", "ru") == "en"
        assert check_lang_mismatch("покажи филиалы", "uz") == "ru"

    def test_returns_none_on_unclassifiable_input(self) -> None:
        # Pure noise — no signal to suggest anything.
        assert check_lang_mismatch("12345", "ru") is None


class TestLooksWorthLlmRecheck:
    @pytest.mark.parametrize(
        "text",
        [
            "ман уй омогчиман адамга",
            "man uy omoqchiman",
            "Qanday ipoteka bor?",
            "salom akam",                       # transliterated greeting
            "hello there",
        ],
    )
    def test_yes_on_alpha_text(self, text: str) -> None:
        assert looks_worth_llm_recheck(text) is True

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "   ",
            "?",
            "ok",                               # < 4 chars
            "12345",                            # no alpha
            "🙂🙂🙂🙂",                          # no alpha
            "!!!",
        ],
    )
    def test_no_on_short_or_non_alpha(self, text: str) -> None:
        assert looks_worth_llm_recheck(text) is False
