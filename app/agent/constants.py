from __future__ import annotations

import contextvars
from typing import Optional

# category → section_name in CreditProductOffer table (DB values, NOT translated)
CREDIT_SECTION_MAP: dict[str, str] = {
    "mortgage": "Ипотека",
    "autoloan": "Автокредит",
    "microloan": "Микрозайм",
    "education_credit": "Образовательный",
}

VALID_LANGS: tuple[str, ...] = ("ru", "en", "uz")

_REQUEST_LANGUAGE: contextvars.ContextVar[str] = contextvars.ContextVar("_REQUEST_LANGUAGE", default="ru")
_LANG_INSTRUCTION = {"en": " Reply in English.", "uz": " Javobni o'zbek tilida yoz.", "ru": ""}


def resolve_language(
    dialog: dict,
    tool_calls: Optional[list] = None,
    default: str = "ru",
) -> str:
    """Resolve current turn language.

    Priority: last valid `lang` arg from tool_calls > dialog.last_lang > default.
    The LLM passes `lang` in every tool call; the last valid one wins
    (reflects the final tool in a multi-round turn).
    """
    if tool_calls:
        detected: Optional[str] = None
        for tc in tool_calls:
            tc_lang = tc.get("args", {}).get("lang")
            if tc_lang in VALID_LANGS:
                detected = tc_lang
        if detected:
            return detected
    last_lang = dialog.get("last_lang")
    if last_lang in VALID_LANGS:
        return last_lang
    return default

FALLBACK_STREAK_THRESHOLD = 3  # show operator button after this many consecutive fallbacks

# ── Dialog flow names ────────────────────────────────────────
FLOW_SHOW_PRODUCTS = "show_products"
FLOW_PRODUCT_DETAIL = "product_detail"
FLOW_CALC = "calc_flow"

# ── Calculator step names ────────────────────────────────────
STEP_AMOUNT = "amount"
STEP_TERM = "term"
STEP_DOWNPAYMENT = "downpayment"


def _greeting_with_menu(lang: str = "ru") -> str:
    if lang == "en":
        return "Hello! What are you interested in?"
    if lang == "uz":
        return "Assalomu alaykum! Qiziqtirayotgan bo'limni tanlang:"
    return "Здравствуйте! Выберите раздел или напишите ваш вопрос:"
