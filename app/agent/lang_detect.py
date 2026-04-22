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

_logger = logging.getLogger(__name__)

_DETECTOR_SYSTEM_PROMPT = (
    "You are a language classifier.\n"
    "Classify the user's message and respond with EXACTLY ONE WORD:\n"
    "- 'ru' = Russian (including Uzbekistan banking vocabulary in Russian)\n"
    "- 'en' = English\n"
    "- 'uz' = Uzbek in ANY form:\n"
    "    * Latin script: 'Menga kredit kerak', 'Assalomu alaykum'\n"
    "    * Uzbek Cyrillic: 'Менга ўқув кредит керак'\n"
    "    * Uzbek words written with RUSSIAN letters (no special chars): "
    "'Менга кредит керак', 'Ассалому алайкум', 'ипотека олмокчиман', "
    "'рахмат', 'керак'\n"
    "Output ONLY one of: ru, en, uz. No punctuation, no explanation."
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

    llm = _get_detector_llm()
    if llm is None:
        return fallback

    try:
        resp = await llm.ainvoke([
            SystemMessage(content=_DETECTOR_SYSTEM_PROMPT),
            HumanMessage(content=text),
        ])
        detected = _normalize_detector_output(extract_text_content(resp))
        if detected:
            return detected
        _logger.info("Language detector returned unexpected output: %r", getattr(resp, "content", None))
    except Exception as exc:
        _logger.warning("Language detector call failed: %s", exc)

    return fallback
