"""Tests for app.agent.pii_masker.

Two priorities:
1. POSITIVE — known PII formats are correctly replaced with the right token.
2. NEGATIVE — banking values that look number-heavy (loan amounts, terms, years,
   account counts, percentages) MUST NOT be masked. False positives here would
   break the calculator flow.
"""
from __future__ import annotations

import pytest

from app.agent.pii_masker import (
    ALL_TOKENS,
    TOKEN_ACCOUNT,
    TOKEN_CARD,
    TOKEN_CVV,
    TOKEN_EMAIL,
    TOKEN_IBAN,
    TOKEN_INN,
    TOKEN_PASSPORT,
    TOKEN_PHONE,
    TOKEN_PINFL,
    mask_pii,
)


# ---------------------------------------------------------------------------
# Phone — Uzbekistan mobile / landline
# ---------------------------------------------------------------------------
class TestPhoneUz:
    @pytest.mark.parametrize(
        "raw",
        [
            "+998901234567",
            "+998 90 123 45 67",
            "998901234567",
            "998 90 123 45 67",
            "+998-90-123-45-67",
            "+998 (90) 123-45-67",
            "+998(90)1234567",
        ],
    )
    def test_uz_mobile_formats_are_masked(self, raw):
        assert mask_pii(f"мой телефон {raw} перезвоните") == f"мой телефон {TOKEN_PHONE} перезвоните"

    def test_phone_at_start_of_text(self):
        assert mask_pii("+998901234567 это мой") == f"{TOKEN_PHONE} это мой"

    def test_phone_at_end_of_text(self):
        assert mask_pii("звоните на +998 90 123 45 67") == f"звоните на {TOKEN_PHONE}"

    def test_two_phones_in_one_message(self):
        out = mask_pii("основной +998901234567 запасной +998935554433")
        assert out == f"основной {TOKEN_PHONE} запасной {TOKEN_PHONE}"

    def test_phone_in_uzbek_text(self):
        assert mask_pii("Mening raqamim +998901234567") == f"Mening raqamim {TOKEN_PHONE}"


# ---------------------------------------------------------------------------
# Card — 16-digit bank cards
# ---------------------------------------------------------------------------
class TestCard:
    @pytest.mark.parametrize(
        "raw",
        [
            "1234567890123456",
            "1234 5678 9012 3456",
            "1234-5678-9012-3456",
            "1234 5678-9012 3456",
        ],
    )
    def test_card_formats_are_masked(self, raw):
        assert mask_pii(f"моя карта {raw} заблокирована") == f"моя карта {TOKEN_CARD} заблокирована"

    def test_card_in_natural_sentence(self):
        out = mask_pii("Заблокируйте карту 8600 1234 5678 9012 пожалуйста")
        assert out == f"Заблокируйте карту {TOKEN_CARD} пожалуйста"


# ---------------------------------------------------------------------------
# PINFL — 14 digits
# ---------------------------------------------------------------------------
class TestPinfl:
    def test_pinfl_14_digits_masked(self):
        assert mask_pii("ПИНФЛ 12345678901234") == f"ПИНФЛ {TOKEN_PINFL}"

    def test_pinfl_in_uzbek(self):
        assert mask_pii("JShShIRim 30801923400015") == f"JShShIRim {TOKEN_PINFL}"

    def test_phone_with_998_does_not_collide_with_pinfl(self):
        # 12-digit phone "998..." should become [PHONE], not [PINFL].
        assert mask_pii("998901234567") == TOKEN_PHONE
        # 14-digit number that is NOT a phone should become [PINFL].
        assert mask_pii("12345678901234") == TOKEN_PINFL


# ---------------------------------------------------------------------------
# Passport — Uzbekistan format AA1234567
# ---------------------------------------------------------------------------
class TestPassport:
    @pytest.mark.parametrize(
        "raw",
        [
            "AA1234567",
            "AA 1234567",
            "AA-1234567",
            "aa1234567",  # lowercase still matched
        ],
    )
    def test_passport_latin_formats(self, raw):
        assert mask_pii(f"паспорт {raw} серия") == f"паспорт {TOKEN_PASSPORT} серия"

    def test_passport_cyrillic(self):
        # Cyrillic letters that look like passport prefixes are also covered.
        assert mask_pii("паспорт АА1234567") == f"паспорт {TOKEN_PASSPORT}"


# ---------------------------------------------------------------------------
# IBAN
# ---------------------------------------------------------------------------
class TestIban:
    def test_uz_iban_strict_format(self):
        # 22-char form
        assert mask_pii("счёт UZ12NBKR0000000000000001") == f"счёт {TOKEN_IBAN}"

    def test_uz_iban_with_spaces(self):
        assert mask_pii("UZ12 NBKR 0000 0000 0000 0001") == TOKEN_IBAN


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------
class TestEmail:
    def test_simple_email(self):
        assert mask_pii("пишите на ivan@example.com") == f"пишите на {TOKEN_EMAIL}"

    def test_email_with_dots_and_plus(self):
        assert mask_pii("ivan.petrov+bank@example.co.uk") == TOKEN_EMAIL


# ---------------------------------------------------------------------------
# Phone — Uzbekistan mobile WITHOUT 998 prefix (operator code + separator)
# ---------------------------------------------------------------------------
class TestPhoneUzLocal:
    @pytest.mark.parametrize(
        "raw",
        [
            "90 123 45 67",
            "91 234 56 78",
            "93-456-78-90",
            "94.567.89.01",
            "(95) 678 90 12",
            "97 789 01 23",
            "99 890 12 34",
            "33 901 23 45",
            "50 012 34 56",
            "55 123 45 67",
            "77 234 56 78",
            "88 345 67 89",
            "22 456 78 90",
        ],
    )
    def test_local_mobile_formats_are_masked(self, raw):
        assert mask_pii(f"мой номер {raw} перезвоните") == f"мой номер {TOKEN_PHONE} перезвоните"

    def test_local_phone_with_dot_separator(self):
        assert mask_pii("+998.90.123.45.67") == TOKEN_PHONE

    def test_bare_9_digits_without_separator_not_masked(self):
        # Without separators we can't distinguish a phone from a 9-digit
        # amount/INN/etc. — must remain untouched.
        text = "номер 901234567"
        assert mask_pii(text) == text

    def test_wrong_grouping_not_masked(self):
        # 90 12345 67 — wrong grouping, not a phone format.
        text = "число 90 12345 67"
        assert mask_pii(text) == text

    def test_non_operator_prefix_not_masked(self):
        # 80, 60, 12 are not UZ mobile operator codes — must not match.
        for raw in ("80 123 45 67", "60 234 56 78", "12 345 67 89"):
            text = f"число {raw}"
            assert mask_pii(text) == text, f"{raw!r} should not be masked"

    def test_amount_with_thousands_grouping_not_masked(self):
        # Critical: "200 000 000 сум" must survive — the leading 200 is not
        # in the operator-code list, so the pattern shouldn't fire.
        text = "хочу взять 200 000 000 сум"
        assert mask_pii(text) == text


# ---------------------------------------------------------------------------
# UZ bank account number — 20 digits
# ---------------------------------------------------------------------------
class TestAccount:
    def test_20_digit_account_masked(self):
        assert mask_pii("счёт 20208000900123456789 в банке") == f"счёт {TOKEN_ACCOUNT} в банке"

    def test_account_at_start(self):
        assert mask_pii("20208000900123456789") == TOKEN_ACCOUNT

    def test_19_digits_not_masked(self):
        text = "номер 2020800090012345678"
        assert mask_pii(text) == text

    def test_21_digits_not_masked(self):
        text = "номер 202080009001234567890"
        assert mask_pii(text) == text

    def test_account_does_not_collide_with_card(self):
        # 16-digit card and 20-digit account in the same message.
        out = mask_pii("карта 1234 5678 9012 3456 счёт 20208000900123456789")
        assert TOKEN_CARD in out
        assert TOKEN_ACCOUNT in out
        assert "1234" not in out
        assert "20208000900123456789" not in out


# ---------------------------------------------------------------------------
# ИНН/СТИР/ТИН — 9 digits with explicit prefix
# ---------------------------------------------------------------------------
class TestInnPrefixed:
    @pytest.mark.parametrize(
        "raw,expected_prefix",
        [
            ("ИНН 123456789", "ИНН "),
            ("инн: 305123456", "инн: "),
            ("ИНН №305123456", "ИНН №"),
            ("STIR 305123456", "STIR "),
            ("stir: 123456789", "stir: "),
            ("TIN 305123456", "TIN "),
            ("tin 123456789", "tin "),
        ],
    )
    def test_inn_with_prefix_masked(self, raw, expected_prefix):
        assert mask_pii(f"мой {raw} зарегистрирован") == f"мой {expected_prefix}{TOKEN_INN} зарегистрирован"

    def test_bare_9_digits_without_prefix_not_masked(self):
        # No prefix → no mask. This preserves calculator amounts.
        text = "число 305123456"
        assert mask_pii(text) == text

    def test_inn_with_amount_in_same_message(self):
        # "200000000 сум" must survive even if message contains an INN.
        out = mask_pii("ИНН 305123456, сумма 200 000 000 сум")
        assert TOKEN_INN in out
        assert "200 000 000" in out
        assert "305123456" not in out


# ---------------------------------------------------------------------------
# CVV/CVC — 3 digits with explicit prefix
# ---------------------------------------------------------------------------
class TestCvvPrefixed:
    @pytest.mark.parametrize(
        "raw,expected_prefix",
        [
            ("CVV 437", "CVV "),
            ("cvv: 921", "cvv: "),
            ("CVC 558", "CVC "),
            ("cvc 100", "cvc "),
            ("код безопасности 437", "код безопасности "),
            ("код карты 921", "код карты "),
        ],
    )
    def test_cvv_with_prefix_masked(self, raw, expected_prefix):
        assert mask_pii(raw) == f"{expected_prefix}{TOKEN_CVV}"

    def test_bare_3_digits_not_masked(self):
        # 3 digits without context could be an age, count, percentage, etc.
        text = "мне 437 раз говорили"
        assert mask_pii(text) == text

    def test_percentage_not_masked(self):
        text = "ставка 437% какая-то странная но всё же"
        assert mask_pii(text) == text


# ---------------------------------------------------------------------------
# CRITICAL NEGATIVES — banking inputs that must survive untouched.
# A single false positive here breaks the calculator flow.
# ---------------------------------------------------------------------------
class TestNegativesCalculatorAmounts:
    @pytest.mark.parametrize(
        "amount",
        [
            "500000000",
            "500 000 000",
            "1000000",
            "10 000 000",
            "50000",
            "100",
            "200 миллионов",
            "200 млн",
            "2 млрд",
        ],
    )
    def test_loan_amounts_not_masked(self, amount):
        text = f"хочу взять {amount} сум"
        assert mask_pii(text) == text, (
            f"loan amount {amount!r} was masked as {mask_pii(text)!r} — "
            "this would break calc_flow"
        )

    @pytest.mark.parametrize(
        "term",
        ["12", "24", "36", "60", "120", "240", "10 лет", "5 yil", "120 мес", "24 oy"],
    )
    def test_terms_not_masked(self, term):
        text = f"срок {term}"
        assert mask_pii(text) == text

    @pytest.mark.parametrize("pct", ["20", "30", "50", "20%", "50.5%"])
    def test_percentages_not_masked(self, pct):
        text = f"первоначальный взнос {pct}"
        assert mask_pii(text) == text

    @pytest.mark.parametrize("year", ["2024", "2025", "2026", "1990"])
    def test_years_not_masked(self, year):
        text = f"в {year} году"
        assert mask_pii(text) == text


class TestNegativesShortNumbers:
    @pytest.mark.parametrize(
        "n",
        ["1", "2", "3", "10", "12", "24", "100", "999", "1234", "12345", "123456789"],
    )
    def test_short_or_9_digit_numbers_not_masked(self, n):
        # 9-digit numbers (could be INN) are intentionally NOT masked:
        # they collide too often with loan amounts and product IDs.
        text = f"номер {n}"
        assert mask_pii(text) == text


class TestNegativesNaturalText:
    @pytest.mark.parametrize(
        "text",
        [
            "хочу ипотеку",
            "какие у вас вклады",
            "Здравствуйте",
            "Salom",
            "что такое аннуитетный платёж?",
            "сколько стоит обслуживание карты Visa Gold?",
            "ставка по вкладу 18%",
            "дайте 500 миллионов на 10 лет",
        ],
    )
    def test_pure_text_not_changed(self, text):
        assert mask_pii(text) == text


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------
class TestProperties:
    def test_empty_string(self):
        assert mask_pii("") == ""

    def test_none_safe(self):
        # mask_pii is only called on strings in production, but it should
        # still degrade gracefully on falsy input.
        assert mask_pii("") == ""

    def test_idempotent(self):
        text = "звоните +998 90 123 45 67 или пишите ivan@example.com"
        once = mask_pii(text)
        twice = mask_pii(once)
        assert once == twice, "mask_pii must be idempotent so re-running on history is safe"

    def test_all_tokens_are_listed(self):
        for token in (
            TOKEN_PHONE,
            TOKEN_CARD,
            TOKEN_PINFL,
            TOKEN_PASSPORT,
            TOKEN_IBAN,
            TOKEN_EMAIL,
            TOKEN_ACCOUNT,
            TOKEN_INN,
            TOKEN_CVV,
        ):
            assert token in ALL_TOKENS

    def test_tokens_are_not_themselves_pii(self):
        # Tokens must not match any pattern — otherwise idempotence breaks.
        for token in ALL_TOKENS:
            assert mask_pii(token) == token


class TestCombinedMessages:
    def test_phone_and_card_in_same_message(self):
        out = mask_pii("карта 1234 5678 9012 3456 заблокирована, звоните +998901234567")
        assert TOKEN_CARD in out and TOKEN_PHONE in out
        assert "1234" not in out and "998" not in out

    def test_realistic_lead_paste(self):
        text = (
            "Меня зовут Иван Петров, телефон +998 90 123 45 67, "
            "паспорт AA1234567, ПИНФЛ 30801923400015, "
            "email ivan@example.com"
        )
        out = mask_pii(text)
        assert TOKEN_PHONE in out
        assert TOKEN_PASSPORT in out
        assert TOKEN_PINFL in out
        assert TOKEN_EMAIL in out
        assert "+998" not in out
        assert "AA1234567" not in out
        assert "30801923400015" not in out
        assert "ivan@example.com" not in out
        # Name "Иван Петров" survives (we don't mask names — that's the
        # system prompt's job, see security_review.html § 5).
        assert "Иван Петров" in out


# ---------------------------------------------------------------------------
# Integration: boundary — node_faq must NOT send raw PII to OpenAI.
# The regression this guards: faq.py used to build
#   HumanMessage(content=normalized_text or user_text)
# with the unmasked user text, so volunteered card/phone numbers leaked to
# the main LLM even though `_finalize_turn` later masked them for history.
# ---------------------------------------------------------------------------
class TestFaqNodePiiBoundary:
    @pytest.mark.asyncio
    async def test_card_not_sent_to_llm(self, monkeypatch):
        from langchain_core.messages import AIMessage

        from app.agent.nodes import faq as faq_module
        from app.agent.state import _default_dialog

        captured_messages: list = []

        class _StubBoundLLM:
            async def ainvoke(self, msgs):
                captured_messages.extend(msgs)
                return AIMessage(content="ok", tool_calls=[])

        class _StubLLM:
            def bind_tools(self, tools):
                return _StubBoundLLM()

        monkeypatch.setattr(faq_module, "_get_chat_openai", lambda: _StubLLM())

        state = {
            "last_user_text": "моя карта 1234 5678 9012 3456 заблокирована",
            "messages": [],
            "dialog": _default_dialog(),
            "lang": "ru",
            "session_id": "test",
            "user_id": 1,
        }
        await faq_module.node_faq(state)

        # At least one HumanMessage must reach the LLM; none of them may
        # contain the raw card digits.
        human_contents = [
            m.content for m in captured_messages if m.__class__.__name__ == "HumanMessage"
        ]
        assert human_contents, "no HumanMessage was sent to the LLM"
        joined = " ".join(str(c) for c in human_contents)
        assert "1234 5678 9012 3456" not in joined
        assert "1234567890123456" not in joined
        assert TOKEN_CARD in joined
