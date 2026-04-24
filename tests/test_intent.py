"""Tests for app.agent.intent.

Focus: ``_is_operator_request`` — this function gates the
``show_operator_button`` flag in ``_finalize_turn``, so a false positive
shows an unwanted handoff button and a false negative hides it when the
user genuinely asked for a human.

The main risk is substring collisions (the same class of bug that caused
"курс" to match inside the Uzbek word "курсатолисизме" in
``bot/handlers/commands.py``). The audit below locks in both directions.
"""
from __future__ import annotations

import pytest

from app.agent.intent import _is_operator_request


class TestOperatorRequestNegatives:
    """Words that merely contain 'оператор' / 'operator' as a prefix inside
    a longer, unrelated word must NOT trigger operator handoff."""

    @pytest.mark.parametrize(
        "text",
        [
            "операция по карте",
            "кооператив",
            "телеоператор",
            "operation on card",
            "cooperator",
            "cooperation",
            "operative",
            "cooperative",
            "operatsiya",
            "kooperativ",
        ],
    )
    def test_not_triggered(self, text: str) -> None:
        assert _is_operator_request(text) is False, text


class TestOperatorRequestPositives:
    """Genuine operator requests — both base forms and morphological
    variants (dative, instrumental, plural) — must trigger."""

    @pytest.mark.parametrize(
        "text",
        [
            "хочу оператора",
            "соедини с оператором",
            "оператор",
            "operator",
            "оператор нужен",
            "живой оператор",
            "позовите оператора",
            # Morphological variants that the prefix-regex covers.
            "оператором пожалуйста",
            "оператору скажите",
            "операторы где",
            # Uzbek forms.
            "operator kerak",
            "operatorga ulang",
            "operator bilan gaplashish",
            "jonli operator",
            "operatorni chaqir",
            "operatorni chaqiring",
            # English synonyms.
            "human agent",
            "speak to human",
        ],
    )
    def test_triggered(self, text: str) -> None:
        assert _is_operator_request(text) is True, text
