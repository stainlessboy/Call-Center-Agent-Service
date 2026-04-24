"""Tests for app.agent.lang_detect.

Only the deterministic pre-LLM pieces are covered here:
- `_should_skip_detection` — skip rules for empty / non-alphabetic input.
- `_normalize_detector_output` — parsing of the detector's raw reply.

The LLM call itself is not exercised (it would hit OpenAI). Language
classification accuracy (including the tricky "Uzbek in Russian letters"
case) is the LLM's job — see the decision rules in
`app.agent.lang_detect._DETECTOR_SYSTEM_PROMPT`.
"""
from __future__ import annotations

import pytest

from app.agent.lang_detect import (
    _normalize_detector_output,
    _should_skip_detection,
)


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
