"""Tests for text-shortcut routing in `app.bot.handlers.commands`.

These shortcuts let the Telegram handler answer common menu intents
(«курс валют», «язык») without invoking the agent. The original version
used naive substring matching, which caused Uzbek messages containing
`курсат-` (verb stem for "to show / offer") to be misrouted to currency
rates. The fix is word-level matching, which is what these tests guard.
"""
from __future__ import annotations

import pytest

from app.bot.handlers.commands import (
    _CURRENCY_SHORTCUT_TOKENS,
    _LANGUAGE_SHORTCUT_TOKENS,
    _has_shortcut_token,
    _normalize_for_match,
)


class TestCurrencyShortcut:
    @pytest.mark.parametrize(
        "text",
        [
            "курс",
            "курсы",
            "курс доллара",
            "какой курс евро сегодня",
            "курсы валют",
            "валюта",
            "сколько стоит валюта",
            "exchange rate",
            "what is the rate",
            "valyuta kursi",
            "dollar kursi",
            "КУРС ВАЛЮТ",
        ],
    )
    def test_match(self, text: str) -> None:
        assert _has_shortcut_token(
            _normalize_for_match(text), _CURRENCY_SHORTCUT_TOKENS
        ), text

    @pytest.mark.parametrize(
        "text",
        [
            # Original bug — Uzbek verb «курсатолисизме» contains "курс" as substring.
            "Филиалларингда навбатсиз хизмат курсатолисизме",
            "курсатиш мумкинми",
            "хизмат курсатинг",
            # Nouns with "курс" substring that are NOT currency.
            "курсант военного училища",
            "курсовая работа",
            # English words containing "rate" as a substring.
            "accurate translation please",
            "I need to integrate with your API",
            # Empty / neutral.
            "",
            "привет",
            "покажи все филиалы",
        ],
    )
    def test_no_match(self, text: str) -> None:
        assert not _has_shortcut_token(
            _normalize_for_match(text), _CURRENCY_SHORTCUT_TOKENS
        ), text


class TestLanguageShortcut:
    @pytest.mark.parametrize(
        "text",
        [
            "язык",
            "сменить язык",
            "поменяй язык на английский",
            "change language",
            "tilni o'zgartir",
            "til tanlash",
        ],
    )
    def test_match(self, text: str) -> None:
        assert _has_shortcut_token(
            _normalize_for_match(text), _LANGUAGE_SHORTCUT_TOKENS
        ), text

    @pytest.mark.parametrize(
        "text",
        [
            # "язык" as substring of an unrelated word.
            "английский языковой курс",
            "stencil design",  # contains "til" substring
            "until tomorrow",
            # Uzbek morphology that coincidentally contains "til".
            "tilingizni bilaman",  # "tiling" prefix but not standalone "til"
            "",
            "привет",
        ],
    )
    def test_no_match(self, text: str) -> None:
        assert not _has_shortcut_token(
            _normalize_for_match(text), _LANGUAGE_SHORTCUT_TOKENS
        ), text


class TestNormalizeForMatch:
    """Sanity checks for the tokenizer feeding the shortcut matcher."""

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("Курс ВАЛЮТ", "курс валют"),
            ("  курс,   доллара!!! ", "курс доллара"),
            ("Курс-валют?", "курс валют"),
            ("курсатолисизме", "курсатолисизме"),
        ],
    )
    def test_normalize(self, raw: str, expected: str) -> None:
        assert _normalize_for_match(raw) == expected
