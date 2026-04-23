"""Tests for app.agent.lang_detect.

Only the deterministic pre-LLM pieces are covered here:
- `_fast_path_detect` — regex shortcuts for unambiguous Uzbek.
- `_should_skip_detection` — skip rules for empty / non-alphabetic input.
- `_normalize_detector_output` — parsing of the detector's raw reply.

The LLM call itself is not exercised (it would hit OpenAI); the fast-path
covers the most common bug class: Russian sentences mis-classified as
Uzbek because of shared banking vocabulary (`филиал`, `кредит`, `ипотека`).
"""
from __future__ import annotations

import pytest

from app.agent.lang_detect import (
    _fast_path_detect,
    _normalize_detector_output,
    _should_skip_detection,
)


class TestFastPathUz:
    """Fast-path must return 'uz' for unambiguous Uzbek input."""

    @pytest.mark.parametrize(
        "text",
        [
            "Менга ўқув кредит керак",
            "филиал қаерда",
            "қанча фоиз",
            "Ассалому алайкум",
            "рахмат",
            "катта рахмат",
            "Филиалларингда навбатсиз хизмат курсатолисизме",
            "ипотека олмокчиман",
            "кредит олмокчи",
            "менга кредит",
        ],
    )
    def test_returns_uz(self, text: str) -> None:
        assert _fast_path_detect(text) == "uz"


class TestFastPathNoMatch:
    """Fast-path must NOT trigger on Russian sentences that merely mention
    shared banking vocabulary — these must be routed to the LLM (return None).
    """

    @pytest.mark.parametrize(
        "text",
        [
            # The original bug: clear Russian sentences with "филиал" in them.
            "покажи все филиалы",
            "дай информацию по филиалам банка",
            "список филиалов",
            "филиал в Ташкенте",
            "мне нужен кредит",
            "хочу ипотеку",
            "какая ставка по кредиту",
            "сколько процент",
            "где ваш офис",
            "расскажи про депозиты",
            # English must also pass through.
            "show me branches",
            "what is the rate",
            "I need a loan",
        ],
    )
    def test_returns_none(self, text: str) -> None:
        assert _fast_path_detect(text) is None


class TestShouldSkip:
    @pytest.mark.parametrize("text", ["", "   ", "a", "?", "12345", "!!!", "🙂"])
    def test_skip(self, text: str) -> None:
        assert _should_skip_detection(text) is True

    @pytest.mark.parametrize("text", ["ok", "да", "hi", "покажи"])
    def test_no_skip(self, text: str) -> None:
        assert _should_skip_detection(text) is False


class TestNormalizeOutput:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("ru", "ru"),
            ("en", "en"),
            ("uz", "uz"),
            ("RU", "ru"),
            (" ru ", "ru"),
            ("ru.", "ru"),
            ("The language is uz", "uz"),
            ("I think it's Russian (ru)", "ru"),
        ],
    )
    def test_valid(self, raw: str, expected: str) -> None:
        assert _normalize_detector_output(raw) == expected

    @pytest.mark.parametrize("raw", ["", "french", "russian", "xx", "enough"])
    def test_invalid(self, raw: str) -> None:
        # "enough" contains "en" as substring but not as a standalone token,
        # so it must NOT match.
        assert _normalize_detector_output(raw) is None
