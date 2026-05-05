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
TOKEN_ACCOUNT: Final = "[ACCOUNT]"
TOKEN_INN: Final = "[INN]"
TOKEN_CVV: Final = "[CVV]"

# Public list of all tokens — used by the system-prompt builder to remind the
# LLM that these placeholders mean "PII, do not echo".
ALL_TOKENS: Final[tuple[str, ...]] = (
    TOKEN_PHONE,
    TOKEN_CARD,
    TOKEN_PINFL,
    TOKEN_PASSPORT,
    TOKEN_IBAN,
    TOKEN_EMAIL,
    TOKEN_ACCOUNT,
    TOKEN_INN,
    TOKEN_CVV,
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
# groups: spaces, hyphens, parentheses, dots.
_PHONE_UZ_RE = re.compile(
    r"\+?998[\s\-.()]*\d{2}[\s\-.()]*\d{3}[\s\-.()]*\d{2}[\s\-.()]*\d{2}",
)

# Same number written WITHOUT the 998 prefix (e.g. "90 123 45 67"). To avoid
# colliding with calculator amounts ("200 000 000 сум") we anchor on the
# closed list of Uzbekistan mobile operator codes AND require at least one
# explicit separator between the operator-code chunk and the rest. Bare
# 9-digit runs ("901234567") are intentionally NOT matched.
_PHONE_UZ_LOCAL_RE = re.compile(
    r"\(?\b(?:22|33|50|55|71|77|88|90|91|93|94|95|97|98|99)\)?"
    r"[\s\-.()]+\d{3}[\s\-.()]+\d{2}[\s\-.()]+\d{2}\b",
)

# Bank card — 16 digits in 4-4-4-4 grouping (with optional spaces or hyphens
# between groups). Word boundaries prevent eating digits from longer numbers.
_CARD_RE = re.compile(
    r"\b\d{4}[\s\-]?\d{4}[\s\-]?\d{4}[\s\-]?\d{4}\b",
)

# UZ bank account number — 20 digits (e.g. 20208000900123456789). 20-digit
# runs are extremely rare in casual chat outside of account references, so
# the bare pattern is safe. Must run BEFORE 16-digit CARD and 14-digit PINFL
# for clarity (the trailing \b in those patterns already prevents eating
# part of a 20-digit number, but explicit ordering is clearer).
_ACCOUNT_RE = re.compile(r"\b\d{20}\b")

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

# ИНН/СТИР/ТИН — 9 digits with an explicit prefix word. Without the prefix
# the pattern would collide with calculator amounts (200000000), so we
# require "ИНН"/"STIR"/"TIN" immediately before the digits. The prefix is
# preserved in the output via backreference; only the digit run is replaced.
_INN_PREFIXED_RE = re.compile(
    r"(\b(?:инн|stir|tin)\b\s*[:#№]?\s*)\d{9}\b",
    re.IGNORECASE,
)

# CVV/CVC card security code — 3 digits with an explicit prefix. Bare
# 3-digit runs are far too common (counts, ages, percentages) to mask
# without context, so the prefix gate is mandatory.
_CVV_PREFIXED_RE = re.compile(
    r"(\b(?:cvv|cvc|код безопасности|код карты)\b\s*[:#№]?\s*)\d{3}\b",
    re.IGNORECASE,
)

# Ordered pipeline. Each (regex, repl) pair is applied in sequence. Order is
# load-bearing — read the module docstring before changing it. ``repl`` is
# either a literal token (e.g. ``[PHONE]``) or a backref-aware replacement
# string (e.g. ``r"\1[INN]"``) that preserves the leading prefix word.
_PIPELINE: Final[tuple[tuple[re.Pattern[str], str], ...]] = (
    (_EMAIL_RE, TOKEN_EMAIL),
    (_IBAN_RE, TOKEN_IBAN),
    (_ACCOUNT_RE, TOKEN_ACCOUNT),
    (_PHONE_UZ_RE, TOKEN_PHONE),
    (_PHONE_UZ_LOCAL_RE, TOKEN_PHONE),
    (_CARD_RE, TOKEN_CARD),
    (_PINFL_RE, TOKEN_PINFL),
    (_PASSPORT_RE, TOKEN_PASSPORT),
    (_INN_PREFIXED_RE, r"\1" + TOKEN_INN),
    (_CVV_PREFIXED_RE, r"\1" + TOKEN_CVV),
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
