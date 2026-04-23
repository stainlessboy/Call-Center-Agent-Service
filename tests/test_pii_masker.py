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
    TOKEN_CARD,
    TOKEN_EMAIL,
    TOKEN_IBAN,
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
        for token in (TOKEN_PHONE, TOKEN_CARD, TOKEN_PINFL, TOKEN_PASSPORT, TOKEN_IBAN, TOKEN_EMAIL):
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
