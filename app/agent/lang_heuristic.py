"""Cheap regex/script-based language mismatch detector.

Used in `Agent._ainvoke` to decide whether to *suggest* the user switch
language. We do NOT switch automatically — we surface a one-time inline
keyboard ("Switch to UZ? [Ha] [Yo'q]") and let the user confirm.

This module costs zero tokens and ~no latency. The previous LLM-based
`detect_language` is kept only as a fallback for first-contact users where
`User.language` is still null.

Decision logic mirrors the LLM detector but is intentionally conservative:
we only return a suggestion when the signal is unambiguous. False positives
annoy the user with a switch prompt for a single Latin word in a Russian
sentence — so we err on the side of staying silent.
"""
from __future__ import annotations

import re
from typing import Optional

from app.agent.constants import VALID_LANGS

# ── Uzbek-specific signals ──────────────────────────────────────────────────
# Cyrillic glyphs unique to Uzbek, never used in modern Russian.
_UZ_CYRILLIC_CHARS = re.compile(r"[ўқғҳЎҚҒҲ]")

# Latin Uzbek apostrophe digraphs (o', g') — straight ' or curly ʼ ’.
_UZ_LATIN_APOSTROPHE = re.compile(r"\b\w*[oʻ'’g][ʻ'’]\w*", re.IGNORECASE)

# Common Uzbek function words / morphology, in either Latin or Cyrillic.
# These are STRONG signals — they almost never appear in Russian or English.
_UZ_MARKERS = re.compile(
    r"\b("
    r"qancha|менга|сенга|сизга|керак|kerak|"
    r"assalomu|salom|ассалому|"
    r"rahmat|раҳмат|рахмат|"
    r"yo['ʻ’]?q|йўқ|йук|"
    r"bormi|бормикин|"
    r"ko['ʻ’]?rsat|курсат|"
    r"olmoqchi|олмокчи|олмокчиман|"
    r"ipoteka olmoq|kredit kerak|"
    r"qayerda|qaerda|қаерда|"
    r"birinchisi|hammasi|barchasi|hammasini|хаммаси"
    r")\b",
    re.IGNORECASE,
)

# ── Russian-specific signals ────────────────────────────────────────────────
# Russian function words / pronouns that don't appear in Uzbek transliteration.
_RU_MARKERS = re.compile(
    r"\b("
    r"я|мне|меня|мы|вы|вам|ваш|мой|это|эти|"
    r"покажи|дай|расскажи|хочу|могу|нужен|нужна|нужно|"
    r"где|когда|что|как|какой|какая|какие|почему|сколько|"
    r"который|которая|которое|которые|"
    r"спасибо|пожалуйста|здравствуйте|привет"
    r")\b",
    re.IGNORECASE,
)

# Cyrillic block detection (any Cyrillic letter).
_CYRILLIC = re.compile(r"[Ѐ-ӿ]")

# ── English signals ─────────────────────────────────────────────────────────
_EN_MARKERS = re.compile(
    r"\b("
    r"the|and|please|hello|hi|thanks|thank you|"
    r"what|where|how|when|why|who|which|"
    r"i|me|my|you|your|we|us|"
    r"show|give|tell|want|need|can|could|would"
    r")\b",
    re.IGNORECASE,
)

# Latin-script block (basic ASCII letters).
_LATIN = re.compile(r"[A-Za-z]")


def _classify(text: str) -> Optional[str]:
    """Cheap classifier. Returns 'ru' / 'en' / 'uz' or None on no signal."""
    s = (text or "").strip()
    if len(s) < 2 or not any(ch.isalpha() for ch in s):
        return None

    # Strongest signal first: Uzbek-specific Cyrillic glyphs.
    if _UZ_CYRILLIC_CHARS.search(s):
        return "uz"

    # Latin apostrophe digraphs (o', g') — definitive Uzbek marker.
    if _UZ_LATIN_APOSTROPHE.search(s):
        return "uz"

    # Uzbek morphological markers (Latin or Cyrillic).
    if _UZ_MARKERS.search(s):
        return "uz"

    has_cyr = bool(_CYRILLIC.search(s))
    has_lat = bool(_LATIN.search(s))

    # Russian function words — Uzbek written in Russian letters does not use these.
    if _RU_MARKERS.search(s) and has_cyr:
        return "ru"

    # English markers in a predominantly Latin text → en.
    if _EN_MARKERS.search(s) and has_lat and not has_cyr:
        return "en"

    # Predominantly Cyrillic without Russian markers — could be transliterated
    # Uzbek or short Russian. Don't guess; return None to stay silent.
    if has_cyr and not has_lat:
        return None

    # Predominantly Latin without English markers — could be transliterated
    # Uzbek without obvious markers, or unrelated. Don't guess.
    return None


def check_lang_mismatch(text: str, current_lang: Optional[str]) -> Optional[str]:
    """Return a suggested language code if the message clearly differs from
    the user's current language; otherwise None.

    Args:
        text: user's raw message.
        current_lang: the language we believe the user prefers (User.language
            normalized to ru/en/uz). If None or unknown, we never suggest a
            switch — the calling code should fall back to LLM detection in
            that case.

    Returns one of {"ru", "en", "uz"} or None.
    """
    if current_lang not in VALID_LANGS:
        return None
    detected = _classify(text)
    if detected is None or detected == current_lang:
        return None
    return detected
