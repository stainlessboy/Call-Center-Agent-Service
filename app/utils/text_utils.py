"""Shared text normalization utilities for FAQ matching and intent classification."""
from __future__ import annotations

import re


def normalize_text(text: str) -> str:
    lowered = (text or "").lower()
    lowered = re.sub(r"[^\w\s]+", " ", lowered, flags=re.UNICODE)
    return re.sub(r"\s+", " ", lowered).strip()


def token_stem(token: str) -> str:
    token = token.strip()
    if len(token) <= 3:
        return token
    for suffix in (
        "ами", "ями", "ого", "ому", "ему", "ыми", "ими", "иях", "ах", "ях",
        "ов", "ев", "ей", "ой", "ый", "ий", "ая", "ое", "ые", "ую", "ам",
        "ям", "ом", "ем", "а", "я", "у", "ю", "е", "ы", "и",
    ):
        if len(token) > 4 and token.endswith(suffix):
            return token[: -len(suffix)]
    return token


def token_set(text: str) -> set[str]:
    return {t for t in (token_stem(x) for x in normalize_text(text).split()) if t}


# ---------------------------------------------------------------------------
# Stopword-aware content tokens (FAQ lexical scoring only — see
# app/utils/faq_tools.py::_faq_similarity). token_set() above is shared with
# intent classification and MUST stay untouched: it is a plain "every token"
# set with no stopword filtering.
#
# These are question-frame/filler words that dilute the token-F1 leg of FAQ
# matching without carrying any of the query's actual topic — e.g. "давайте
# мне интересно эскроу счет" vs FAQ "Что такое Эскроу?" only really agree on
# "эскроу"/"счет", but "что"/"такое"/"давайте"/"мне"/"интересно" pull the F1
# score down as if they were mismatches. Stored as STEMS (via token_stem)
# because token_set() stems its own tokens before we'd compare against this
# set — an unstemmed "хочу" would never match a stored token like "хоч".
# ---------------------------------------------------------------------------
_STOPWORDS_RAW: frozenset[str] = frozenset({
    # Russian — question frames, politeness, pronouns, connectives.
    "что", "такое", "как", "какой", "какая", "какое", "какие", "давайте",
    "дайте", "мне", "мной", "я", "интересно", "хочу", "хотим", "хотел",
    "хотела", "хотели", "узнать", "знать", "про", "о", "об", "это", "этот",
    "эта", "эти", "пожалуйста", "подскажите", "скажите", "можно", "можете",
    "ли", "есть", "нужно", "надо", "будьте", "добры", "добрые", "и",
    "в", "на", "у", "к", "с", "для", "по", "из", "от", "если", "то", "же",
    "бы", "б", "вы", "ты", "мы", "он", "она", "оно", "они", "меня", "нас",
    "вас", "их", "его", "её", "ее", "поясните", "объясните", "расскажите",
    "интересует", "нам", "будет", "было", "быть", "являет", "являются",
    # English
    "what", "is", "are", "the", "a", "an", "please", "tell", "me", "want",
    "wanted", "know", "about", "can", "could", "would", "how", "do", "does",
    "did", "i", "you", "we", "it", "this", "that", "to", "of", "in", "on",
    "for", "and", "or", "so", "my", "your", "our", "let", "us", "interested",
    "curious", "explain", "kindly", "will", "be",
    # Uzbek (Latin script — the agent's uz output is Latin-only, see i18n.py)
    "nima", "qanday", "haqida", "bilmoqchiman", "bilmoqchi", "iltimos",
    "aytingchi", "ayting", "meni", "menga", "biz", "bizga", "u", "bu", "shu",
    "kerak", "keraklimi", "bo'ladimi", "bormi", "qiziqaman", "qiziq",
    "uchun", "bilan", "va", "ham", "lekin", "tushuntiring",
})
_STOPWORD_STEMS: frozenset[str] = frozenset(token_stem(w) for w in _STOPWORDS_RAW)


def token_set_content(text: str) -> set[str]:
    """Like token_set(), minus question-frame/filler stopwords.

    Used only by the FAQ lexical token-F1 leg — intent classification and
    every other token_set() caller are unaffected.

    Guard: if removing stopwords would empty the set (a query made entirely
    of filler words, e.g. "что такое"), return the UNSTRIPPED set instead —
    an empty token set scores 0.0 against everything, which is strictly
    worse for matching than not filtering at all.
    """
    full = token_set(text)
    content = full - _STOPWORD_STEMS
    return content if content else full
