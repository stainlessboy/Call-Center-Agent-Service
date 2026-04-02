from __future__ import annotations

import contextvars

# category → section_name in CreditProductOffer table (DB values, NOT translated)
CREDIT_SECTION_MAP: dict[str, str] = {
    "mortgage": "Ипотека",
    "autoloan": "Автокредит",
    "microloan": "Микрозайм",
    "education_credit": "Образовательный",
}

_REQUEST_LANGUAGE: contextvars.ContextVar[str] = contextvars.ContextVar("_REQUEST_LANGUAGE", default="ru")
_LANG_INSTRUCTION = {"en": " Reply in English.", "uz": " Javobni o'zbek tilida yoz.", "ru": ""}

# Dialog context passed to tools via contextvar
_CURRENT_DIALOG: contextvars.ContextVar[dict] = contextvars.ContextVar("_CURRENT_DIALOG", default={})

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
