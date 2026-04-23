"""Regex-based PII masking applied to user text before it reaches OpenAI.

This is a defense-in-depth layer on top of the explicit lead-step masking in
``app/agent/nodes/calc_flow.py``. It covers cases where a customer voluntarily
sends sensitive data in free-form chat (e.g. asking "my card 1234... is blocked")
or pastes a phone/passport into the calculator.

Conservative by design: only patterns that are very unlikely to collide with
legitimate banking input (loan amounts, terms, years) are matched. ``ИНН`` (9
digits) and dates are deliberately NOT masked — they collide too often with
amounts like ``500000000`` or years like ``2026``.

Order of substitution matters: more specific patterns (with prefixes like
``998`` or ``UZ``) run first; bare-digit patterns (``[CARD]``, ``[PINFL]``)
run last so they cannot eat parts of phones or IBANs.
"""
from __future__ import annotations

import re
from typing import Final

# ---------------------------------------------------------------------------
# Tokens — what each PII category becomes in the masked text.
# ---------------------------------------------------------------------------
TOKEN_PHONE: Final = "[PHONE]"
TOKEN_CARD: Final = "[CARD]"
TOKEN_PINFL: Final = "[PINFL]"
TOKEN_PASSPORT: Final = "[PASSPORT]"
TOKEN_IBAN: Final = "[IBAN]"
TOKEN_EMAIL: Final = "[EMAIL]"

# Public list of all tokens — used by the system-prompt builder to remind the
# LLM that these placeholders mean "PII, do not echo".
ALL_TOKENS: Final[tuple[str, ...]] = (
    TOKEN_PHONE,
    TOKEN_CARD,
    TOKEN_PINFL,
    TOKEN_PASSPORT,
    TOKEN_IBAN,
    TOKEN_EMAIL,
)

# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------

# Email — straightforward.
_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")

# Uzbekistan IBAN: starts with "UZ" + 2 check digits + up to 22 alphanumerics,
# possibly chunked by spaces. We match a generous tail so both the strict
# 22-char form and the legacy 24-char form are caught.
_IBAN_RE = re.compile(
    r"\bUZ\d{2}[\s\-]?(?:[\dA-Z][\s\-]?){17,22}\b",
    re.IGNORECASE,
)

# Uzbekistan mobile / landline numbers. We REQUIRE the country prefix 998
# (with an optional leading +) — without it the pattern would collide with
# every 9-digit amount in a calculator. Allowed separators between digit
# groups: spaces, hyphens, parentheses.
_PHONE_UZ_RE = re.compile(
    r"\+?998[\s\-()]*\d{2}[\s\-()]*\d{3}[\s\-()]*\d{2}[\s\-()]*\d{2}",
)

# Bank card — 16 digits in 4-4-4-4 grouping (with optional spaces or hyphens
# between groups). Word boundaries prevent eating digits from longer numbers.
_CARD_RE = re.compile(
    r"\b\d{4}[\s\-]?\d{4}[\s\-]?\d{4}[\s\-]?\d{4}\b",
)

# PINFL — 14 digits in a row. Runs AFTER phone, so a 12-digit phone with the
# 998 prefix never gets misclassified. 14-digit numbers in casual banking
# chat are almost always PINFL or birth-record IDs (= still PII).
_PINFL_RE = re.compile(r"\b\d{14}\b")

# Uzbekistan passport — 2 letters (Latin or Cyrillic) followed by 7 digits.
# Common formats: "AA1234567", "АА 1234567" (Cyrillic), "AA-1234567".
_PASSPORT_RE = re.compile(
    r"\b[A-ZА-ЯЎҚҒҲ]{2}[\s\-]?\d{7}\b",
    re.IGNORECASE,
)

# Ordered pipeline. Each (regex, token) pair is applied in sequence. Order is
# load-bearing — read the module docstring before changing it.
_PIPELINE: Final[tuple[tuple[re.Pattern[str], str], ...]] = (
    (_EMAIL_RE, TOKEN_EMAIL),
    (_IBAN_RE, TOKEN_IBAN),
    (_PHONE_UZ_RE, TOKEN_PHONE),
    (_CARD_RE, TOKEN_CARD),
    (_PINFL_RE, TOKEN_PINFL),
    (_PASSPORT_RE, TOKEN_PASSPORT),
)


def mask_pii(text: str) -> str:
    """Replace likely-PII substrings in *text* with category tokens.

    Returns *text* unchanged if it is empty or contains no matches. Idempotent:
    running ``mask_pii(mask_pii(x))`` is the same as ``mask_pii(x)`` because
    tokens like ``[PHONE]`` do not match any pattern themselves.
    """
    if not text:
        return text
    for pattern, token in _PIPELINE:
        text = pattern.sub(token, text)
    return text
