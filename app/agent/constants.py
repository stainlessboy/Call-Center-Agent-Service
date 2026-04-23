from __future__ import annotations

# category → section_name in CreditProductOffer table (DB values, NOT translated)
CREDIT_SECTION_MAP: dict[str, str] = {
    "mortgage": "Ипотека",
    "autoloan": "Автокредит",
    "microloan": "Микрозайм",
    "education_credit": "Образовательный",
}

VALID_LANGS: tuple[str, ...] = ("ru", "en", "uz")


def resolve_language(dialog: dict, default: str = "ru") -> str:
    """Return persisted language from dialog.last_lang, else default.

    Used by agent._ainvoke as the fallback for the LLM detector when the
    current user message is empty/ambiguous.
    """
    last_lang = (dialog or {}).get("last_lang")
    if last_lang in VALID_LANGS:
        return last_lang
    return default

FALLBACK_STREAK_THRESHOLD = 3  # show operator button after this many consecutive fallbacks

# ── Dialog flow names ────────────────────────────────────────
FLOW_SHOW_PRODUCTS = "show_products"
FLOW_PRODUCT_DETAIL = "product_detail"
FLOW_CALC = "calc_flow"
FLOW_SHOW_OFFICES = "show_offices"
FLOW_OFFICE_DETAIL = "office_detail"

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
