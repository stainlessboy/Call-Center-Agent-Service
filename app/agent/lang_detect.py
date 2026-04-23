"""Dedicated LLM-based language detector.

Called once per user turn in `agent._ainvoke` BEFORE the main graph runs,
the result is written to `state["lang"]` and `dialog.last_lang`.

Why a separate detector instead of asking the main LLM:
- gpt-4o-mini sometimes guesses `lang` wrong when the user writes Uzbek
  words in Russian letters ("Менга кредит керак", "Ассалому алайкум").
- The main LLM's `lang` decision used to "stick" via last_lang.
- A small dedicated prompt with no tools is faster and more reliable.

Model is configurable via `LANG_DETECTOR_MODEL` env; defaults to `gpt-4o-mini`
(regardless of OPENAI_MODEL) because we want it cheap and fast.
"""
from __future__ import annotations

import logging
import os
from functools import lru_cache
from typing import Optional

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from app.agent.constants import VALID_LANGS
from app.agent.llm import extract_text_content
from app.agent.pii_masker import mask_pii

_logger = logging.getLogger(__name__)

_DETECTOR_SYSTEM_PROMPT = (
    "You are a language classifier for a Uzbekistan bank chatbot.\n"
    "Output EXACTLY ONE token: ru, en, or uz. No punctuation, no explanation.\n"
    "\n"
    "DECISION ORDER (check top-down, stop at first match):\n"
    "\n"
    "1. RUSSIAN GRAMMAR WINS over shared banking vocabulary.\n"
    "   Words like 'кредит', 'ипотека', 'филиал(ы/ов/ам/ах)', 'депозит', 'карта',\n"
    "   'банк', 'офис', 'процент', 'ставка', 'сум', 'год/месяц' exist in BOTH ru\n"
    "   and uz — they are NOT a uz signal by themselves.\n"
    "   If the message has Russian function words / verbs / pronouns, return 'ru':\n"
    "     pronouns: я, мне, меня, мы, вы, вам, ваш, мой, это, эта, эти, все/всё\n"
    "     verbs:    покажи(те), дай(те), расскажи(те), хочу, могу, нужен/нужна/\n"
    "               нужно, есть, знаю, помоги(те), можно\n"
    "     wh-words: где, когда, что, как, какой/какая/какие, почему, сколько\n"
    "               (note: 'сколько' is ru; 'қанча/qancha' is uz)\n"
    "     preps:    по, в, на, из, от, для, про, о/об\n"
    "     particles: ли, же, бы, не\n"
    "   EXAMPLES (all 'ru'):\n"
    "     'покажи все филиалы', 'дай информацию по филиалам банка',\n"
    "     'список филиалов', 'мне нужен кредит', 'хочу ипотеку',\n"
    "     'филиал в Ташкенте', 'какая ставка по кредиту', 'где ваш офис',\n"
    "     'расскажи про депозиты', 'сколько процент'.\n"
    "\n"
    "2. ENGLISH: ASCII Latin words that form an English sentence → 'en'.\n"
    "     'show me branches', 'what is the rate', 'I need a loan'.\n"
    "   Do NOT classify as 'en' if the Latin text is Uzbek (see rule 3).\n"
    "\n"
    "3. UZBEK — return 'uz' ONLY when you see clear Uzbek-specific signal,\n"
    "   not shared vocabulary:\n"
    "\n"
    "   a) Uzbek LATIN script with Uzbek morphology or particles:\n"
    "      'Menga kredit kerak', 'Assalomu alaykum', 'ipoteka olmoqchiman',\n"
    "      'filial qayerda', 'qancha foiz', 'bormi', 'yoʻq'.\n"
    "\n"
    "   b) Uzbek CYRILLIC characters that do not exist in Russian: ў, қ, ғ, ҳ.\n"
    "      'Менга ўқув кредит керак', 'филиал қаерда', 'қанча фоиз'.\n"
    "\n"
    "   c) Uzbek written in Russian letters (no ў/қ/ғ/ҳ) — require an unambiguous\n"
    "      Uzbek MORPHOLOGICAL marker, NOT just a banking noun:\n"
    "      suffix / ending:  -ман / -миз / -сиз / -сизме / -сизми / -мокчи /\n"
    "                        -мокчиман / -ингиз / -ларингда / -ларингиз /\n"
    "                        -олсиз / -олисиз / -олисизме / -олисизми /\n"
    "                        -гача, infinitive on -мок (олмок, бермок, тулаш)\n"
    "      function words:   керак (as copula 'need'), бор (exist), йук / йўк,\n"
    "                        менга, сенга, сизга, унга, биз, сиз, шу, бу\n"
    "                        (when used as Uzbek function words, not Russian)\n"
    "      greetings:        ассалому алайкум, салом алейкум, рахмат, раҳмат\n"
    "      verbs:            олмокчи(ман), бермокчи, курсатолисиз(ме/ми),\n"
    "                        хизмат курсат-, навбат-сиз\n"
    "      EXAMPLES (all 'uz'):\n"
    "        'Менга кредит керак'           (менга + керак — uz morphology)\n"
    "        'ипотека олмокчиман'           (-мокчиман suffix)\n"
    "        'Филиалларингда навбатсиз хизмат курсатолисизме'\n"
    "        'Ассалому алайкум', 'рахмат', 'катта рахмат'.\n"
    "\n"
    "   DO NOT classify as 'uz' just because the message mentions 'филиал',\n"
    "   'кредит', 'ипотека', 'депозит', 'фоиз', 'сум' — these are shared\n"
    "   loanwords. Require a morphological marker from list (c)."
)


@lru_cache(maxsize=1)
def _get_detector_llm() -> Optional[ChatOpenAI]:
    """Return a cached ChatOpenAI instance for language detection.

    Uses `LANG_DETECTOR_MODEL` env (default `gpt-4o-mini`) — always cheap,
    regardless of the main `OPENAI_MODEL`.
    """
    try:
        model = os.getenv("LANG_DETECTOR_MODEL") or "gpt-4o-mini"
        kwargs: dict = {
            "model": model,
            "temperature": 0,
            "max_tokens": 5,
            "api_key": os.getenv("OPENAI_API_KEY"),
        }
        base_url = os.getenv("OPENAI_BASE_URL")
        if base_url:
            kwargs["base_url"] = base_url
        return ChatOpenAI(**kwargs)
    except Exception as exc:
        _logger.warning("Failed to create language detector LLM: %s", exc)
        return None


def _should_skip_detection(text: str) -> bool:
    """Skip LLM call for inputs that carry no linguistic signal.

    Returns True for:
    - empty / whitespace-only
    - single character
    - only digits / punctuation / emoji (no alphabetic chars)
    """
    t = (text or "").strip()
    if len(t) < 2:
        return True
    if not any(ch.isalpha() for ch in t):
        return True
    return False


import re as _re

_TOKEN_RE = _re.compile(r"[a-z]{2,}")

# Uzbek-only Cyrillic chars that do NOT exist in Russian. Their presence
# alone is a deterministic signal → skip the LLM.
_UZ_ONLY_CYRILLIC_RE = _re.compile(r"[ўқғҳЎҚҒҲ]")

# Uzbek morphological markers when the text is in Russian letters.
# These are regex fragments; each must match as a whole word (or with Russian
# suffixes / punctuation around it). We deliberately avoid bare "керак" because
# it also appears inside Russian phrases transliterated from Uzbek only.
_UZ_MORPHOLOGY_RE = _re.compile(
    r"\b("
    r"ассалому|алайкум|алейкум|"
    r"рахмат|рахмет|"
    r"олмокчи(ман)?|бермокчи(ман)?|"
    r"курсатолисиз(ме|ми)?|курсатолсиз|курсатинг|"
    r"навбатсиз|"
    r"олмок|бермок|тулаш|"
    r"менга|сенга|сизга|"
    r"\w+ларингда|\w+ларингиз|\w+ингизда|"
    r"\w+мокчиман|\w+мокчимиз|\w+сизме|\w+сизми|"
    r"\w+олисизме|\w+олисизми"
    r")\b",
    _re.IGNORECASE,
)


def _fast_path_detect(text: str) -> Optional[str]:
    """Deterministic pre-LLM shortcuts for unambiguous Uzbek text.

    Returns 'uz' if the text contains:
    - Uzbek-only Cyrillic chars (ў, қ, ғ, ҳ), OR
    - Clear Uzbek morphological markers in Russian letters.

    Returns None otherwise (let the LLM decide between ru / en / uz-in-ru-letters).
    """
    if not text:
        return None
    if _UZ_ONLY_CYRILLIC_RE.search(text):
        return "uz"
    if _UZ_MORPHOLOGY_RE.search(text):
        return "uz"
    return None


def _normalize_detector_output(raw: str) -> Optional[str]:
    """Parse the detector's raw reply into a valid language code, or None.

    Direct match wins. Otherwise we look for a valid code as a standalone
    TOKEN in the output — not a substring (so 'french' doesn't match 'en').
    """
    out = (raw or "").strip().lower()
    if out in VALID_LANGS:
        return out
    tokens = _TOKEN_RE.findall(out)
    for tok in tokens:
        if tok in VALID_LANGS:
            return tok
    return None


async def detect_language(text: str, fallback: str = "ru") -> str:
    """Detect language of user's message via a dedicated small LLM call.

    Args:
        text: user's message.
        fallback: language to return on skip / error / invalid detector output.
                  Typically `dialog.last_lang` so we remember previous turn.

    Returns one of: 'ru', 'en', 'uz'. Never raises.
    """
    if fallback not in VALID_LANGS:
        fallback = "ru"

    if _should_skip_detection(text):
        return fallback

    fast = _fast_path_detect(text)
    if fast is not None:
        return fast

    llm = _get_detector_llm()
    if llm is None:
        return fallback

    try:
        resp = await llm.ainvoke([
            SystemMessage(content=_DETECTOR_SYSTEM_PROMPT),
            HumanMessage(content=mask_pii(text)),
        ])
        detected = _normalize_detector_output(extract_text_content(resp))
        if detected:
            return detected
        _logger.info("Language detector returned unexpected output: %r", getattr(resp, "content", None))
    except Exception as exc:
        _logger.warning("Language detector call failed: %s", exc)

    return fallback
