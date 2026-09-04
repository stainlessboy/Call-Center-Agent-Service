from __future__ import annotations

import html as _html
from typing import Annotated, Literal, Optional

from langchain_core.tools import tool as lc_tool
from langgraph.prebuilt import InjectedState

from app.agent.i18n import (
    _localized_name,
    at,
    category_label,
    get_calc_questions,
)
from app.agent.intent import _detect_product_category
from app.agent.products import (
    _find_product_by_name,
    _format_product_card,
    _format_product_list_text,
    _get_products_by_category,
)
from app.agent.rate_rules import income_type_from_dialog, resolve_effective_rate
from app.agent.recommend import rank_products
from app.agent.ui_blocks import credit_calc_result_block
from app.config import get_settings
from app.utils.amortization import amortize, dti_ratio
from app.utils.faq_tools import faq_search

# All supported product categories, ordered from most common to least.
# Used in the 3-tier fallback search in select_product.
_ALL_CATEGORIES = [
    "mortgage",
    "autoloan",
    "microloan",
    "education_credit",
    "deposit",
    "debit_card",
    "fx_card",
]

# custom_loan_calculator's default rate now lives on Settings
# (`default_custom_loan_rate_pct`, `DEFAULT_CUSTOM_LOAN_RATE_PCT` env var) —
# read live via get_settings() at call time instead of once at import time,
# so a changed env value takes effect without a process restart in any
# context that already calls `get_settings.cache_clear()` (e.g. tests).

# Sanity caps for custom_loan_calculator's free-form inputs — the LLM/user
# can supply arbitrary numbers, and the annuity formula's `(1 + r) ** term`
# term can raise OverflowError for pathological terms. No natural per-product
# bound applies here (this calculator isn't tied to a specific product), so
# these are conservative fixed ceilings.
_MAX_CUSTOM_LOAN_TERM_MONTHS: int = 600  # 50 years
_MAX_CUSTOM_LOAN_AMOUNT: float = 10_000_000_000.0  # 10 billion UZS

# Sentinels returned by faq_lookup — explicit strings so the LLM can detect
# and handle each case without hallucinating an answer. Thresholds and tier
# logic live in app/utils/faq_tools.py (FAQ_SEM_*/FAQ_LEX_* env vars).
NO_MATCH_IN_FAQ = "NO_MATCH_IN_FAQ"
# Returned (as a prefix, possibly followed by candidate questions) when the
# best FAQ match is plausible but not confident.
FAQ_LOW_CONFIDENCE = "FAQ_LOW_CONFIDENCE"


def is_faq_sentinel(content: str | None) -> bool:
    """True when a tool output is a faq_lookup sentinel (possibly with an
    appended candidate list) rather than a user-facing answer."""
    t = (content or "").strip()
    return t.startswith(NO_MATCH_IN_FAQ) or t.startswith(FAQ_LOW_CONFIDENCE)


def _lang_from_state(state: dict | None) -> str:
    """Pull `lang` from InjectedState. Falls back to dialog.last_lang, then 'ru'."""
    if not state:
        return "ru"
    lang = state.get("lang")
    if lang in ("ru", "en", "uz"):
        return lang
    dialog = state.get("dialog") or {}
    return dialog.get("last_lang") or "ru"


async def _find_offices_impl(office_type: str, query: str, lang: str) -> tuple[str, list]:
    """Returns (display_text, offices) — offices is the raw ORM object list
    (possibly empty) so the tool wrapper can build the office_list artifact
    without a second DB round-trip."""
    from app.agent.branches import format_branches_list, search_offices

    offices = await search_offices(query=query, office_types=[office_type], limit=5)
    if not offices:
        return at("branch_none_found", lang, query=query or "—"), []

    header = at("branch_found_header", lang, count=len(offices))
    return f"{header}\n\n{format_branches_list(offices, lang)}", offices


@lc_tool(response_format="content_and_artifact")
async def find_office(
    office_type: Literal["filial", "sales_office", "sales_point"],
    query: str = "",
    state: Annotated[dict, InjectedState] = None,
) -> tuple[str, Optional[dict]]:
    """Find a bank office by type and optional location/name query.

    OFFICE TYPES:
    - "filial" — full-service branch (Центр банковских услуг / ЦБУ / БХМ). Has ALL services
      including legal-entity accounts, business loans, IP/yakka tadbirkor services.
      Default choice for vague "ближайшее отделение" queries.
    - "sales_office" — mini-office (офис продаж / savdo ofisi). Individuals only:
      consumer/auto/micro/education loans, cards, cashier, currency exchange.
      NO legal-entity services.
    - "sales_point" — car-dealership point (точка продаж / savdo nuqtasi).
      ONLY auto loans, consultations, ATM. Nothing else.

    EXAMPLES:
    - "где ближайший филиал?" → find_office(office_type="filial", query="")
    - "покажи все филиалы" / "дай информацию по филиалам" / "список филиалов" /
      "филиалы банка" → find_office(office_type="filial", query="")
    - "филиал в Андижане" → find_office(office_type="filial", query="Андижан")
    - "мне нужен счёт для юрлица в Ташкенте" → find_office(office_type="filial", query="Ташкент")
    - "где мини-офис в Самарканде" → find_office(office_type="sales_office", query="Самарканд")
    - "авто кредит в KIA Андижан" → find_office(office_type="sales_point", query="KIA Andijon")
    - "BYD Tashkent" → find_office(office_type="sales_point", query="BYD Tashkent")
    - "where is the nearest branch" / "show me all branches" / "list of branches"
      → find_office(office_type="filial", query="")
    - "Toshkentdagi filial" / "barcha filiallar" / "filiallar ro'yxati"
      → find_office(office_type="filial", query="")

    IMPORTANT: vague "all branches / показать филиалы" messages DO call this tool
    with `query=""` — do NOT ask the user to clarify. The tool returns up to 5
    offices by default; the user can then narrow down by city.

    PARAMETERS:
      office_type: one of "filial" / "sales_office" / "sales_point".
      query: free-form city / region / office-name / car-dealer as the user wrote it.
             Empty string = list first 5.
    """
    text, offices = await _find_offices_impl(office_type, query, _lang_from_state(state))
    if not offices:
        return text, None
    from app.agent.branches import office_public_dict
    artifact = {
        "type": "office_list",
        "data": {
            "office_type": office_type,
            "query": query,
            "offices": [office_public_dict(o) for o in offices],
        },
    }
    return text, artifact


@lc_tool
async def get_office_types_info(
    state: Annotated[dict, InjectedState] = None,
) -> str:
    """Explain the difference between the three bank office types
    (filial / sales_office / sales_point) and which services each provides.

    EXAMPLES:
    - "чем отличается филиал от мини-офиса" → get_office_types_info()
    - "что можно сделать в точке продаж" → get_office_types_info()
    - "где можно получить карту" → get_office_types_info()
    - "filial va mini-ofis farqi nima" → get_office_types_info()
    - "what's the difference between offices" → get_office_types_info()
    """
    return at("office_types_info", _lang_from_state(state))


def _currency_numeric(value) -> Optional[float]:
    """Best-effort float parse of a CBU JSON field ("12 690.15" style
    strings, or already-numeric values) for the rate_table artifact. Returns
    None rather than raising — a display-string field that doesn't parse
    just means the ui_block carries a null instead of blocking the tool."""
    if value is None:
        return None
    try:
        return float(str(value).replace(" ", "").replace(",", "."))
    except (TypeError, ValueError):
        return None


@lc_tool(response_format="content_and_artifact")
async def get_currency_info(
    state: Annotated[dict, InjectedState] = None,
) -> tuple[str, Optional[dict]]:
    """Get the latest currency exchange rates (USD, EUR, RUB, GBP, KZT, CNY vs UZS).

    HARD RULE: call ONLY if the message contains at least ONE explicit currency token
    (USD, EUR, RUB, GBP, KZT, CNY, UZS, доллар, евро, рубль, фунт, тенге, юань,
    сум/so'm, валюта, обмен, currency, exchange, valyuta, ayirboshlash). If none of
    these tokens are present — route via `faq_lookup` or `find_office` instead.

    EXAMPLES:
    - "курс доллара" / "сколько сейчас евро" / "обменный курс" → get_currency_info()
    - "какой сегодня курс валют" → get_currency_info()
    - "USD rate today" / "exchange rate" → get_currency_info()
    - "dollar narxi" / "valyuta kursi qancha" → get_currency_info()

    Edge cases for the 'курс/kursat...' homonym are described in the system policy — follow that guidance.
    """
    from app.utils.cbu_rates import fetch_cbu_rates

    lang = _lang_from_state(state)
    rates = await fetch_cbu_rates(("USD", "EUR", "RUB", "GBP", "KZT", "CNY"))
    if not rates:
        return at("currency_info", lang), None
    lines = []
    for r in rates:
        nominal = r["nominal"]
        nom_str = f"{nominal} " if str(nominal) != "1" else ""
        diff = float(r["diff"]) if r["diff"] else 0
        arrow = "↑" if diff > 0 else ("↓" if diff < 0 else "")
        lines.append(f"{r['icon']} {nom_str}{r['code']} = {r['rate']} UZS {arrow}")
    header = {"ru": "Курс ЦБ Узбекистана", "en": "CBU exchange rates", "uz": "O'zbekiston MB kursi"}[lang]
    date_str = rates[0].get("date", "")
    text = f"{header} ({date_str}):\n" + "\n".join(lines)
    artifact = {
        "type": "rate_table",
        "data": {
            "date": date_str,
            "rates": [
                {
                    "code": r["code"],
                    "name_ru": r["name_ru"],
                    "name_en": r["name_en"],
                    "name_uz": r["name_uz"],
                    "nominal": _currency_numeric(r["nominal"]) or 1,
                    "rate": _currency_numeric(r["rate"]),
                    "diff": _currency_numeric(r["diff"]),
                    "icon": r["icon"],
                }
                for r in rates
            ],
        },
    }
    return text, artifact


@lc_tool
async def show_credit_menu(
    state: Annotated[dict, InjectedState] = None,
) -> str:
    """Show the credit-type selection menu. Use ONLY when user wants credit but has NOT specified which type.

    EXAMPLES (correct use — type is genuinely unknown):
    - "хочу кредит" / "мне нужен кредит" / "какие есть кредиты" → show_credit_menu()
    - "I need a loan" / "what loans do you offer" → show_credit_menu()
    - "kredit olmoqchiman" / "kredit turlari" → show_credit_menu()

    DO NOT call when the user names a goal that uniquely implies a category — call get_products instead:
    - "хочу купить квартиру" / "квартира в кредит" / "хочу квартиру" / "жильё" → get_products(category="mortgage")
    - "хочу машину" / "куплю авто" / "нужен автомобиль" → get_products(category="autoloan")
    - "оплатить учёбу" / "контракт" / "оплата обучения" → get_products(category="education_credit")
    - "buy a house" / "buy an apartment" / "home loan" → get_products(category="mortgage")
    - "buy a car" / "car loan" → get_products(category="autoloan")
    - "pay tuition" / "student loan" → get_products(category="education_credit")
    - "kvartira olmoqchiman" / "uy-joy olish" → get_products(category="mortgage")
    - "mashina olmoqchiman" / "avtomobil sotib olmoqchiman" → get_products(category="autoloan")
    - "o'qish puli" / "kontrakt to'lovi" → get_products(category="education_credit")
    """
    return at("credit_menu_prompt", _lang_from_state(state))


@lc_tool(response_format="content_and_artifact")
async def get_products(
    category: Literal[
        "mortgage", "autoloan", "microloan", "education_credit",
        "deposit", "debit_card", "fx_card",
    ],
    state: Annotated[dict, InjectedState] = None,
) -> tuple[str, Optional[dict]]:
    """Get list of bank products for a specific category.
    Returns pre-formatted text — pass to the user AS-IS.

    CATEGORIES: mortgage, autoloan, microloan, education_credit, deposit, debit_card, fx_card.

    EXAMPLES — direct product/category names:
    - "хочу ипотеку" → get_products(category="mortgage")
    - "покажи автокредиты" → get_products(category="autoloan")
    - "какие у вас вклады" → get_products(category="deposit")
    - "микрозайм" → get_products(category="microloan")
    - "дебетовые карты" → get_products(category="debit_card")
    - "валютные карты" → get_products(category="fx_card")
    - "all products" / "◀ Все продукты" when state has category → get_products(category=<state.category>)
    - "deposit products" → get_products(category="deposit")
    - "ipoteka" → get_products(category="mortgage")

    EXAMPLES — "what X do you have" / "qanday X bor" — list-show questions go HERE,
    not to faq_lookup. The user wants to see the catalogue, not read a help article:
    - "какие у вас ипотеки есть?" / "какие ипотеки бывают?" → get_products(category="mortgage")
    - "какие у вас автокредиты?" → get_products(category="autoloan")
    - "какие вклады у вас есть?" → get_products(category="deposit")
    - "какие карты у вас есть?" → get_products(category="debit_card")
    - "what mortgages do you have?" → get_products(category="mortgage")
    - "what deposits are available?" → get_products(category="deposit")
    - "qanday ipoteka bor?" / "qanday ipoteka turlari bor?" → get_products(category="mortgage")
    - "qanday avtokredit bor?" → get_products(category="autoloan")
    - "qanday omonat bor?" / "qanday omonatlar mavjud?" → get_products(category="deposit")
    - "qanday karta bor?" / "qanday kartalar mavjud?" → get_products(category="debit_card")
    - "qanday mikrokredit bor?" → get_products(category="microloan")

    EXAMPLES — goal-based phrasings that imply a specific category (prefer this over show_credit_menu):
    - "хочу купить квартиру" / "хочу квартиру" / "куплю жильё" / "квартира в кредит" / "недвижимость" → get_products(category="mortgage")
    - "I want to buy an apartment" / "buy a home" / "home loan" → get_products(category="mortgage")
    - "kvartira olmoqchiman" / "uy-joy olish" / "ipoteka kerak" → get_products(category="mortgage")
    - "хочу купить машину" / "куплю авто" / "нужен автомобиль" → get_products(category="autoloan")
    - "I want to buy a car" / "car financing" → get_products(category="autoloan")
    - "mashina olmoqchiman" / "avtomobil sotib olmoqchiman" → get_products(category="autoloan")
    - "оплатить учёбу" / "контракт в университете" / "оплата обучения" → get_products(category="education_credit")
    - "pay tuition" / "student loan for university" → get_products(category="education_credit")
    - "o'qish puli" / "kontrakt to'lovi" → get_products(category="education_credit")
    - "хочу копить" / "хочу накопить" / "куда положить деньги под процент" → get_products(category="deposit")
    - "I want to save money" / "where to invest" → get_products(category="deposit")
    - "pul yig'moqchiman" / "jamg'arish" → get_products(category="deposit")

    Also use when the user clicks a "back to products" button while a category is in state —
    call with the state's current category to re-render the list.
    """
    lang = _lang_from_state(state)
    products = await _get_products_by_category(category)
    if not products:
        label = category_label(category, lang)
        return at("product_unavailable", lang, label=label), None
    text = _format_product_list_text(products, category, lang)
    from app.agent.products import _product_public_dict
    artifact = {
        "type": "product_list",
        "data": {
            "category": category,
            "products": [_product_public_dict(p) for p in products],
        },
    }
    return text, artifact


_RECOMMEND_REASON_KEYS = {
    "best_rate": "recommend_reason_best_rate",
    "age_fit": "recommend_reason_age_fit",
}


def _format_recommend_line(product: dict, lang: str) -> str:
    name = _localized_name(product, lang)
    rate = product.get("rate") or ""
    reasons = product.get("_recommend_reasons") or []
    phrases = [at(_RECOMMEND_REASON_KEYS[r], lang) for r in reasons if r in _RECOMMEND_REASON_KEYS]
    line = f"• {name}"
    if rate:
        line += f" — {rate}"
    if phrases:
        line += f" ({', '.join(phrases)})"
    return line


@lc_tool(response_format="content_and_artifact")
async def recommend_product(
    goal: str = "",
    state: Annotated[dict, InjectedState] = None,
) -> tuple[str, Optional[dict]]:
    """Recommend a bank product based on a life/financial GOAL the customer
    described, when they have NOT named a specific product or category.

    EXAMPLES (call when the customer describes what they want to achieve or
    worry about, without naming a product type — "ипотека"/"автокредит"/"вклад"/etc):
    - "хочу накопить на старость" / "думаю о пенсии" → recommend_product(goal="накопить на старость")
    - "мечтаю о своей квартире, но не знаю с чего начать" → recommend_product(goal="своя квартира")
    - "хочу создать финансовую подушку на всякий случай" → recommend_product(goal="финансовая подушка")
    - "думаю, как накопить дочке на учёбу" → recommend_product(goal="накопить на учёбу ребёнку")
    - "I want to build up retirement savings" → recommend_product(goal="retirement savings")
    - "farzandimni xorijda o'qitishni orzu qilaman" → recommend_product(goal="bolani xorijda o'qitish")

    DO NOT call when the customer already named a specific product/category —
    call `get_products(category=...)` instead:
    - "хочу ипотеку" / "покажи вклады" / "какие у вас автокредиты" → get_products(...)
    DO NOT call for a generic "хочу кредит" with no goal at all — that's
    `show_credit_menu()`.

    Ranking is deterministic (rate, and — if the client's age is known from
    their profile — how well it fits the product's age-dependent rate tiers).
    Returns a short pitch for the best match plus one alternative — pass the
    tool output to the customer AS-IS, do not reformat.

    Parameters:
        goal: the customer's goal in their own words (used to pick a product
              category). Empty string falls back to the category already in
              state, if any.
    """
    lang = _lang_from_state(state)
    dialog = (state or {}).get("dialog") or {}
    profile = (state or {}).get("user_profile") or None

    category = _detect_product_category(goal or "")
    if not category or category == "credit_menu":
        category = dialog.get("category") or None
    if not category:
        return at("recommend_no_goal", lang), None

    products = await _get_products_by_category(category)
    label = category_label(category, lang)
    if not products:
        return at("product_unavailable", lang, label=label), None

    ranked = rank_products(products, profile, category, top_n=2)
    if not ranked:
        return at("product_unavailable", lang, label=label), None

    lines = [at("recommend_top_header", lang, category=label), _format_recommend_line(ranked[0], lang)]
    if len(ranked) > 1:
        lines += ["", at("recommend_alt_header", lang), _format_recommend_line(ranked[1], lang)]
    lines += ["", at("recommend_footer", lang)]
    text = "\n".join(lines)

    from app.agent.products import _product_public_dict
    artifact = {
        "type": "product_list",
        "data": {
            "category": category,
            "kind": "recommend",
            "products": [_product_public_dict(p) for p in ranked],
        },
    }
    return text, artifact


def _product_card_result(matched: dict, category: str, lang: str, profile: Optional[dict]) -> tuple[str, dict]:
    """Shared (text, artifact) builder for a matched product — used by every
    tier of select_product's search below so the product_card artifact is
    built identically regardless of which tier found the match."""
    from app.agent.products import _personal_rate_pct, _product_public_dict

    text = _format_product_card(matched, category, lang, profile)
    personal_rate = None
    if category in ("mortgage", "autoloan", "microloan", "education_credit"):
        personal_rate = _personal_rate_pct(matched, profile)
    artifact = {
        "type": "product_card",
        "data": {
            "category": category,
            "product": _product_public_dict(matched),
            "personal_rate_pct": personal_rate,
        },
    }
    return text, artifact


@lc_tool(response_format="content_and_artifact")
async def select_product(
    product_name: str,
    state: Annotated[dict, InjectedState] = None,
) -> tuple[str, Optional[dict]]:
    """Show details of a specific product the user selected.
    Returns pre-formatted HTML text — pass AS-IS, do not reformat.

    EXAMPLES (assuming state.products = [1. "Ипотека Стандарт", 2. "Ипотека Лайт"]):
    - "Ипотека Стандарт" → select_product(product_name="Ипотека Стандарт")
    - "2" → select_product(product_name="Ипотека Лайт")  ← map the number to the product at that position
    - "первый" → select_product(product_name="Ипотека Стандарт")
    - "расскажи про стандарт" → select_product(product_name="Ипотека Стандарт")
    """
    lang = _lang_from_state(state)
    dialog = (state or {}).get("dialog") or {}
    dialog_products = list(dialog.get("products") or [])
    dialog_category = dialog.get("category", "")
    profile = (state or {}).get("user_profile")

    # Tier 1: search within the products already loaded in dialog state
    matched = _find_product_by_name(product_name, dialog_products)
    if matched:
        return _product_card_result(matched, dialog_category, lang, profile)

    # Tier 2: search in DB by dialog category
    if dialog_category:
        db_products = await _get_products_by_category(dialog_category)
        matched = _find_product_by_name(product_name, db_products)
        if matched:
            return _product_card_result(matched, dialog_category, lang, profile)

    # Tier 3: search across all known categories
    for cat in _ALL_CATEGORIES:
        if cat == dialog_category:
            continue
        cat_products = await _get_products_by_category(cat)
        matched = _find_product_by_name(product_name, cat_products)
        if matched:
            return _product_card_result(matched, cat, lang, profile)

    # No match found anywhere
    if dialog_products:
        names = ", ".join(p["name"] for p in dialog_products[:5])
        return at("product_not_found_suggest", lang, names=names), None
    return at("product_not_found", lang), None


_COMPARE_COLUMN_LABEL_KEYS = {
    "rate": "cmp_rate",
    "term": "cmp_term",
    "amount": "cmp_amount",
    "downpayment": "cmp_downpayment",
    "cashback": "cmp_cashback",
    "annual_fee": "cmp_annual_fee",
    "min_amount": "label_min_amount",
    "currency": "label_currency",
    "network": "label_network",
}


def _compare_column_label(col: str, lang: str) -> str:
    key = _COMPARE_COLUMN_LABEL_KEYS.get(col)
    return at(key, lang) if key else col


def _format_comparison_text(products: list[dict], category: str, lang: str) -> str:
    """Compact per-product breakdown for Telegram (no fixed-width table —
    values vary too much in length across categories/products for that to
    stay readable in a monospace-less chat client)."""
    from app.agent.products import _comparison_columns

    columns = _comparison_columns(category)
    lines = [at("compare_title", lang), ""]
    for i, p in enumerate(products, 1):
        name = _localized_name(p, lang) or p.get("name") or f"#{i}"
        lines.append(f"<b>{i}. {_html.escape(name)}</b>")
        for col in columns:
            val = p.get(col)
            if val in (None, "", []):
                continue
            lines.append(f"   {_compare_column_label(col, lang)}: {val}")
        lines.append("")
    lines.append(at("compare_footer", lang))
    return "\n".join(lines).strip()


@lc_tool(response_format="content_and_artifact")
async def compare_products(
    product_names: list[str],
    state: Annotated[dict, InjectedState] = None,
) -> tuple[str, Optional[dict]]:
    """Compare 2-4 bank products side by side (same category — the ones
    already shown to the customer, or looked up by the dialog's current
    category).

    EXAMPLES (assuming state.products = [1. "Ипотека Стандарт", 2. "Ипотека Лайт"]):
    - "чем отличаются стандарт и лайт" → compare_products(product_names=["Ипотека Стандарт", "Ипотека Лайт"])
    - "какой лучше — первый или второй" → compare_products(product_names=["1", "2"])  ← map numbers to positions, like select_product
    - "сравни ипотеку стандарт, лайт и семейную" → compare_products(product_names=["Ипотека Стандарт", "Ипотека Лайт", "Семейная ипотека"])
    - "what's the difference between standard and light" → compare_products(product_names=["Standard", "Light"])
    - "solishtir birinchi va ikkinchisini" → compare_products(product_names=["1", "2"])

    DO NOT call when:
    - only ONE product is named — use select_product instead.
    - the customer hasn't seen or named any specific products yet — show a
      list first (get_products), then compare once they mention 2+ of them.

    Parameters:
        product_names: 2-4 product names/numbers/positions, as the customer
            wrote them (or as shown in the last product list) — resolved
            the same way select_product resolves a single name.
    """
    lang = _lang_from_state(state)
    dialog = (state or {}).get("dialog") or {}
    category = dialog.get("category", "")

    candidates = list(dialog.get("products") or [])
    if not candidates and category:
        candidates = await _get_products_by_category(category)
    if not candidates:
        return at("compare_no_products", lang), None

    resolved: list[dict] = []
    seen_names: set[str] = set()
    for name in (product_names or [])[:4]:
        matched = _find_product_by_name(name, candidates)
        if matched and matched.get("name") not in seen_names:
            resolved.append(matched)
            seen_names.add(matched.get("name"))

    if len(resolved) < 2:
        names = ", ".join(p["name"] for p in candidates[:5])
        return at("compare_not_enough_products", lang, names=names), None

    text = _format_comparison_text(resolved, category, lang)
    from app.agent.products import _comparison_columns, _product_public_dict
    artifact = {
        "type": "comparison_table",
        "data": {
            "category": category,
            "columns": _comparison_columns(category),
            "products": [_product_public_dict(p) for p in resolved],
        },
    }
    return text, artifact


@lc_tool
async def start_calculator(
    state: Annotated[dict, InjectedState] = None,
) -> str:
    """Start payment/application calculator for the currently selected bank product.
    Return the tool output AS-IS without rephrasing.

    EXAMPLES (when a product is selected):
    - "рассчитать" / "подать заявку" / "хочу оформить" → start_calculator()
    - "calculate" → start_calculator()
    - "hisoblab bering" / "ariza topshirmoqchiman" → start_calculator()

    DO NOT call when:
    - no product is selected yet — call get_products first
    - the user gives their OWN numbers free-form — use custom_loan_calculator instead
    """
    lang = _lang_from_state(state)
    dialog = (state or {}).get("dialog") or {}
    category = dialog.get("category", "")
    calc_qs = get_calc_questions(category, lang)
    if not calc_qs:
        return at("calc_no_questions", lang)
    _, first_q = calc_qs[0]
    cat_label = category_label(category, lang)
    return at("calc_intro", lang, category=cat_label) + "\n\n" + first_q


@lc_tool(response_format="content_and_artifact")
async def custom_loan_calculator(
    amount: float,
    term_months: int,
    downpayment: float = 0.0,
    state: Annotated[dict, InjectedState] = None,
) -> tuple[str, Optional[dict]]:
    """Calculate a generic annuity loan payment using the customer's OWN numbers.
    NOT tied to a specific bank product.

    A conservative default interest rate (see DEFAULT_CUSTOM_LOAN_RATE_PCT env,
    default 20% p.a.) is applied, and the output clearly discloses this.
    The LLM MUST NOT invent a rate — that's why the tool does not accept one.

    EXAMPLES:
    - "если я возьму 50 млн на 5 лет" → custom_loan_calculator(amount=50000000, term_months=60, downpayment=0)
    - "посчитай 100 млн на 3 года с первоначальным 20 млн" → custom_loan_calculator(amount=100000000, term_months=36, downpayment=20000000)
    - "calculate 30m over 24 months" → custom_loan_calculator(amount=30000000, term_months=24, downpayment=0)

    PARSING HINTS:
    - "3 года"→36, "полтора года"→18, "5 лет"→60, "24 месяца"→24, "10 йил"→120
    - "без первоначального"/"0"/not mentioned → downpayment=0.0

    DO NOT call when the user wants a specific bank product — use get_products + start_calculator instead.

    Sanity limits: amount up to 10 billion UZS, term_months up to 600 (50
    years) — the tool returns a message with the allowed range instead of
    computing when either is exceeded.

    Parameters:
        amount: Total loan amount in UZS BEFORE deducting downpayment (e.g. 50_000_000).
        term_months: Integer number of months (e.g. 36 for 3 years).
        downpayment: Absolute downpayment in UZS (0.0 if none).
    """
    lang = _lang_from_state(state)
    rate_pct = get_settings().default_custom_loan_rate_pct

    def fmt(v: float) -> str:
        return f"{v:,.0f}".replace(",", " ")

    principal = amount - downpayment
    if principal <= 0:
        _err = {
            "ru": "Укажите корректные суммы: сумма кредита должна быть больше первоначального взноса.",
            "en": "Please provide valid amounts: the loan amount must exceed the down payment.",
            "uz": "Iltimos, to'g'ri summalarni kiriting: kredit summasi boshlang'ich to'lovdan katta bo'lishi kerak.",
        }
        return (_err.get(lang) or _err["ru"]), None
    if amount > _MAX_CUSTOM_LOAN_AMOUNT:
        return at("custom_calc_amount_too_large", lang, max_amount=fmt(_MAX_CUSTOM_LOAN_AMOUNT)), None
    if term_months <= 0:
        _err = {
            "ru": "Укажите корректный срок (в месяцах, больше нуля).",
            "en": "Please provide a valid term (in months, greater than zero).",
            "uz": "Iltimos, to'g'ri muddatni kiriting (oyda, noldan katta).",
        }
        return (_err.get(lang) or _err["ru"]), None
    if term_months > _MAX_CUSTOM_LOAN_TERM_MONTHS:
        return at("custom_calc_term_too_large", lang, max_term=_MAX_CUSTOM_LOAN_TERM_MONTHS), None

    # Shared annuity math (app/utils/amortization.py) — the single source of
    # truth also used by the PDF schedule and the Mini App calculator, so all
    # three surfaces round/compute identically. `amortize()` gives us the full
    # schedule too (monthly/total/overpayment match the plain annuity_payment
    # math used before Phase 3 — see ui_blocks.py module docstring).
    amort = amortize(principal, rate_pct, term_months)
    monthly = amort.monthly_payment
    total = amort.total_payment
    overpayment = amort.overpayment

    text = at(
        "custom_calc_result",
        lang,
        amount=fmt(amount),
        downpayment=fmt(downpayment),
        principal=fmt(principal),
        term=term_months,
        rate=rate_pct,
        monthly=fmt(monthly),
        total=fmt(total),
        overpayment=fmt(overpayment),
    )
    artifact = credit_calc_result_block(
        amort, product_name=None, amount=amount, downpayment=downpayment,
    )
    return text, artifact


def _fmt_money(v: float) -> str:
    return f"{v:,.0f}".replace(",", " ")


_WHAT_IF_PRODUCT_SUFFIX = {
    "ru": lambda name: f" по «{name}»",
    "en": lambda name: f' for "{name}"',
    "uz": lambda name: f" — «{name}» bo'yicha",
}


async def _resolve_calc_product(
    product_name: str, dialog: dict,
) -> Optional[dict]:
    """Shared product resolution for what_if_scenario/affordability_check:
    an explicit *product_name* (matched the same way select_product does)
    wins, otherwise fall back to the product already selected in dialog."""
    category = dialog.get("category", "")
    if product_name:
        candidates = list(dialog.get("products") or [])
        matched = _find_product_by_name(product_name, candidates)
        if not matched and category:
            db_products = await _get_products_by_category(category)
            matched = _find_product_by_name(product_name, db_products)
        if matched:
            return matched
    return dialog.get("selected_product") or None


@lc_tool(response_format="content_and_artifact")
async def what_if_scenario(
    amount: Optional[float] = None,
    term_months: Optional[int] = None,
    downpayment_pct: Optional[float] = None,
    product_name: str = "",
    state: Annotated[dict, InjectedState] = None,
) -> tuple[str, Optional[dict]]:
    """Recalculate a HYPOTHETICAL "what if" variant of a credit product —
    e.g. "what if the term were longer" or "what if I put down more" —
    WITHOUT touching the customer's actual in-progress calculator answers.

    IMPORTANT: this is a side-hypothesis, not a flow step. It never mutates
    `dialog.calc_slots` — call it as many times as the customer wants to
    play with numbers, their real calculator progress (if any) is untouched.
    When they're ready to actually proceed, they still go through the normal
    product button → calculator flow (start_calculator / the calc_flow node).

    EXAMPLES (a product is selected or was just shown):
    - "а если на 5 лет вместо 3?" → what_if_scenario(term_months=60)
    - "что если внести не 20%, а 30%?" → what_if_scenario(downpayment_pct=30)
    - "а если взять 80 миллионов по Ипотеке Лайт?" → what_if_scenario(amount=80000000, product_name="Ипотека Лайт")
    - "what if I take it for 7 years instead" → what_if_scenario(term_months=84)

    DO NOT call when:
    - no product is selected AND none is named — ask which product, or use
      custom_loan_calculator for a product-free free-form estimate instead.
    - the customer wants to actually commit to new numbers for their real
      application — that goes through start_calculator/the calculator flow,
      not this tool.

    Parameters (all optional — unset ones fall back to the customer's
    current calc_slots, i.e. "everything the same EXCEPT what changed"):
        amount: hypothetical loan amount in UZS.
        term_months: hypothetical term in months.
        downpayment_pct: hypothetical down payment, in percent (0-100).
        product_name: product to use for this scenario if different from
            the one currently selected — resolved like select_product.
    """
    lang = _lang_from_state(state)
    dialog = (state or {}).get("dialog") or {}
    calc_slots = dialog.get("calc_slots") or {}

    if dialog.get("category") == "deposit":
        return at("what_if_not_for_deposit", lang), None

    product = await _resolve_calc_product(product_name, dialog)
    if not product:
        return at("what_if_no_product", lang), None

    eff_amount = amount if amount is not None else calc_slots.get("amount")
    eff_term = term_months if term_months is not None else calc_slots.get("term_months")
    eff_dp_pct = downpayment_pct if downpayment_pct is not None else calc_slots.get("downpayment")

    if eff_amount is None or eff_term is None:
        return at("what_if_missing_params", lang), None
    if eff_amount > _MAX_CUSTOM_LOAN_AMOUNT:
        return at("custom_calc_amount_too_large", lang, max_amount=_fmt_money(_MAX_CUSTOM_LOAN_AMOUNT)), None
    if eff_term <= 0 or eff_term > _MAX_CUSTOM_LOAN_TERM_MONTHS:
        return at("custom_calc_term_too_large", lang, max_term=_MAX_CUSTOM_LOAN_TERM_MONTHS), None

    dp_pct = float(eff_dp_pct or 0)
    dp_abs = int(float(eff_amount) * dp_pct / 100)
    principal = float(eff_amount) - dp_abs
    if principal <= 0:
        return at("what_if_invalid_amounts", lang), None

    rate_pct = resolve_effective_rate(
        product,
        age=calc_slots.get("age"),
        amount=eff_amount,
        term_months=eff_term,
        downpayment_pct=dp_pct,
        income_type=income_type_from_dialog(dialog),
    )
    amort = amortize(principal, rate_pct, int(eff_term))

    product_display_name = _localized_name(product, lang) or product.get("name")
    suffix = ""
    if product_display_name:
        suffix_fn = _WHAT_IF_PRODUCT_SUFFIX.get(lang, _WHAT_IF_PRODUCT_SUFFIX["ru"])
        suffix = suffix_fn(product_display_name)

    text = at(
        "what_if_result", lang,
        product_suffix=suffix,
        amount=_fmt_money(eff_amount),
        downpayment=_fmt_money(dp_abs),
        dp_pct=f"{dp_pct:.0f}",
        term=int(eff_term),
        rate=f"{rate_pct:.1f}",
        monthly=_fmt_money(amort.monthly_payment),
        total=_fmt_money(amort.total_payment),
        overpayment=_fmt_money(amort.overpayment),
    )
    artifact = credit_calc_result_block(
        amort, product_name=product_display_name, amount=float(eff_amount), downpayment=dp_abs,
    )
    # Ephemeral marker (Phase 4, see docs/MINIAPP.md "UI blocks"): this is a
    # side-hypothesis, not the customer's active calc_result — the Mini App
    # should render it distinctly (e.g. not replace the last real result).
    artifact["data"]["is_hypothetical"] = True
    return text, artifact


@lc_tool(response_format="content_and_artifact")
async def affordability_check(
    monthly_payment: Optional[float] = None,
    loan_amount: Optional[float] = None,
    term_months: Optional[int] = None,
    state: Annotated[dict, InjectedState] = None,
) -> tuple[str, Optional[dict]]:
    """Check whether a monthly payment (given directly, or derived from a
    loan amount + term) is comfortable relative to the customer's income.

    Resolution order: an explicit `monthly_payment` wins; otherwise it is
    derived from `loan_amount`/`term_months` (falling back to the
    customer's current `dialog.calc_slots` for whichever of those two is
    missing) using the same rate lookup as the calculator. Income comes
    ONLY from the client's stored profile (`state.user_profile.facts.income_monthly`,
    learned from earlier conversation, never asked for here) — if unknown,
    give the general 40-50%-of-income rule and softly invite the customer
    to share their income, WITHOUT pressing for it (same tone as the PII
    policy: optional, never a requirement to proceed).

    EXAMPLES:
    - "потяну ли я платёж в 3 миллиона?" → affordability_check(monthly_payment=3000000)
    - "хватит ли моего дохода на этот кредит?" (product/amount/term already in context) → affordability_check()
    - "потяну ли 50 млн на 3 года?" → affordability_check(loan_amount=50000000, term_months=36)
    - "can I afford a 2.5M payment?" → affordability_check(monthly_payment=2500000)

    DO NOT call when the customer hasn't given a number and there is no
    calculator context at all (dialog.calc_slots empty, no product) — ask
    them for a payment or an amount+term first instead of guessing.

    Parameters (all optional):
        monthly_payment: a specific monthly payment to check, in UZS.
        loan_amount: loan amount to derive a payment from, in UZS.
        term_months: term to derive a payment from, in months.
    """
    lang = _lang_from_state(state)
    dialog = (state or {}).get("dialog") or {}
    calc_slots = dialog.get("calc_slots") or {}
    profile = (state or {}).get("user_profile") or {}
    income = (profile.get("facts") or {}).get("income_monthly")

    eff_payment = monthly_payment
    eff_amount = loan_amount if loan_amount is not None else calc_slots.get("amount")
    eff_term = term_months if term_months is not None else calc_slots.get("term_months")

    if eff_payment is None:
        if not eff_amount or not eff_term:
            return at("affordability_need_more_info", lang), None
        selected_product = dialog.get("selected_product") or {}
        rate_pct = resolve_effective_rate(
            selected_product,
            age=calc_slots.get("age"),
            amount=eff_amount,
            term_months=eff_term,
            downpayment_pct=calc_slots.get("downpayment"),
            income_type=income_type_from_dialog(dialog),
        )
        dp_pct = float(calc_slots.get("downpayment") or 0)
        principal = float(eff_amount) * (1 - dp_pct / 100)
        eff_payment = amortize(principal, rate_pct, int(eff_term)).monthly_payment

    if not eff_payment or eff_payment <= 0:
        return at("affordability_need_more_info", lang), None

    ratio = dti_ratio(eff_payment, income)
    warn_ratio = get_settings().dti_warn_ratio
    if ratio is None:
        text = at("affordability_general_rule", lang, payment=_fmt_money(eff_payment))
    else:
        verdict = at(
            "affordability_verdict_high" if ratio > warn_ratio else "affordability_verdict_ok",
            lang,
        )
        text = at(
            "affordability_result_known", lang,
            payment=_fmt_money(eff_payment),
            pct=f"{ratio * 100:.0f}",
            verdict=verdict,
        )

    artifact = {
        "type": "calc_result",
        "data": {
            "kind": "affordability",
            "monthly_payment": round(float(eff_payment), 2),
            "loan_amount": eff_amount,
            "term_months": eff_term,
            "income_monthly": income,
            "dti_ratio": round(ratio, 4) if ratio is not None else None,
            "dti_warn_ratio": warn_ratio,
        },
    }
    return text, artifact


@lc_tool
async def faq_lookup(
    query: str,
    state: Annotated[dict, InjectedState] = None,
) -> str:
    """Look up the FAQ knowledge base for banking questions about services, products or procedures.

    Call this for ANY "how do I X with the bank" question — including ones that
    sound like generic financial advice. The bank-specific FAQ is the source of
    truth; do not reply from general knowledge before checking it.

    EXAMPLES:
    - "как обновить паспорт в приложении" → faq_lookup(query="обновить паспорт в приложении")
    - "можно ли досрочно погасить кредит" → faq_lookup(query="досрочное погашение кредита")
    - "как кредит быстрее погасить" → faq_lookup(query="досрочное погашение кредита")
    - "как закрыть кредит" → faq_lookup(query="закрытие кредита")
    - "как заблокировать карту" → faq_lookup(query="блокировка карты")
    - "какие комиссии за перевод" → faq_lookup(query="комиссии переводы")
    - "сколько процентов при досрочном закрытии вклада" → faq_lookup(query="досрочное закрытие вклада проценты")
    - "есть ли обслуживание без очереди в филиалах" → faq_lookup(query="обслуживание без очереди")
    - "Филиалларингда навбатсиз хизмат курсатолисизме" → faq_lookup(query="навбатсиз хизмат")
    - "how to change phone number" → faq_lookup(query="change phone number")
    - "parolni qanday tiklayman" → faq_lookup(query="parolni tiklash")

    RETURNS one of these values:
    - Answer text if the match is confident. Rephrase it naturally, in your
      own words, in the customer's language — but keep every fact, number,
      rate, term, condition and link EXACTLY as given in the source. Never
      add anything that isn't in it.
    - "FAQ_LOW_CONFIDENCE" followed by the closest FAQ entries, each with its
      question AND answer text. If ONE of them clearly answers the user's
      question, answer from it directly in THIS SAME turn (same rephrasing
      rule as above) — do NOT call faq_lookup again for it. If none of them
      fits, treat this as NO_MATCH_IN_FAQ (below).
    - "NO_MATCH_IN_FAQ" if nothing relevant was found (this also covers a
      FAQ_LOW_CONFIDENCE reply where none of the candidates fit). In that case:
      * If the question is GENERAL banking knowledge (what is annuity, a
        downpayment, how escrow works, what is APR, the typical flow of taking
        a loan, common banking terms / definitions) — answer it yourself,
        briefly, in the user's language. DO NOT add a disclaimer or note that
        "this is general info" — the system automatically wraps your answer
        with an "Assistant" header and an operator disclaimer. NEVER invent
        concrete Asaka Bank facts: no made-up rates, fees, terms, product
        names, branch addresses, or document lists.
      * If the question is bank-specific and you cannot answer it — say plainly
        that you don't have that info yet; the system surfaces an operator
        button automatically after a couple of unhelpful turns. Escalate with
        request_operator only if the user explicitly asks for a human.
    """
    lang = _lang_from_state(state)
    result = await faq_search(query, lang)
    if result.tier == "strict":
        return result.answer or NO_MATCH_IN_FAQ
    if result.tier == "low" and result.candidates:
        # Candidates carry their answer text too (not just the question) —
        # a bare question list required a SECOND faq_lookup round-trip to
        # fetch the answer, and the model frequently skipped that step and
        # fell back to general knowledge instead of the DB (see the escrow
        # incident this was fixed for). Shipping the answer text up front
        # lets the model pick and answer within the same round.
        lines = [
            FAQ_LOW_CONFIDENCE,
            "No confident match. Closest FAQ entries (question + answer):",
        ]
        for i, c in enumerate(result.candidates, 1):
            if not c.question:
                continue
            lines.append(f"{i}. Q: {c.question}")
            if c.answer:
                lines.append(f"   A: {c.answer}")
        lines.append(
            "If one of these clearly answers the user's question, answer "
            "from it directly now (rephrase naturally, keep every fact/"
            "number/rate/term exactly as given) — do not call faq_lookup "
            "again for it. Otherwise treat this as NO_MATCH_IN_FAQ."
        )
        return "\n".join(lines)
    if result.tier == "low":
        return FAQ_LOW_CONFIDENCE
    return NO_MATCH_IN_FAQ


@lc_tool(response_format="content_and_artifact")
async def select_office(
    office_name: str,
    state: Annotated[dict, InjectedState] = None,
) -> tuple[str, Optional[dict]]:
    """Show full details of an office the user selected from the previously-shown list.
    Returns pre-formatted HTML — pass AS-IS, do NOT rephrase, do NOT promise to fetch later.

    EXAMPLES (after find_office returned offices [1. KIA Axmad Donish, 2. NRG Iroteka, 3. Olamavto]):
    - "1" → select_office(office_name="KIA Axmad Donish")
    - "первый" / "birinchisi" → select_office(office_name="KIA Axmad Donish")
    - "KIA Axmad Donish" → select_office(office_name="KIA Axmad Donish")
    - "все" / "хаммаси" / "barchasi" / "all" / "hammasini" → select_office(office_name="all")

    Use ONLY when find_office was just called and the user picks one (or asks for all details).
    """
    lang = _lang_from_state(state)
    dialog = (state or {}).get("dialog") or {}
    offices_state = list(dialog.get("offices") or [])
    if not offices_state:
        return at("office_not_found", lang), None

    from app.agent.branches import format_branch_card, format_branches_list, office_public_dict
    from app.db.models import Filial, SalesOffice, SalesPoint
    from app.db.session import get_session
    from sqlalchemy import select as sql_select

    _MODEL = {"filial": Filial, "sales_office": SalesOffice, "sales_point": SalesPoint}

    async def _fetch(items):
        out = []
        async with get_session() as session:
            for item in items:
                model = _MODEL.get(item.get("office_type"))
                if not model:
                    continue
                obj = (
                    await session.execute(sql_select(model).where(model.id == item.get("id")))
                ).scalar_one_or_none()
                if obj:
                    out.append(obj)
        return out

    def _list_result(objs: list) -> tuple[str, Optional[dict]]:
        if not objs:
            return at("office_not_found", lang), None
        text = format_branches_list(objs, lang)
        artifact = {
            "type": "office_list",
            "data": {"office_type": None, "query": "", "offices": [office_public_dict(o) for o in objs]},
        }
        return text, artifact

    def _detail_result(objs: list) -> tuple[str, Optional[dict]]:
        if not objs:
            return at("office_not_found", lang), None
        text = format_branch_card(objs[0], lang)
        artifact = {"type": "office_detail", "data": {"office": office_public_dict(objs[0])}}
        return text, artifact

    norm = (office_name or "").strip().lower()
    if norm in ("all", "все", "всё", "хаммаси", "barchasi", "hammasini", "hammasi"):
        objs = await _fetch(offices_state)
        return _list_result(objs)

    if norm.isdigit():
        idx = int(norm) - 1
        if 0 <= idx < len(offices_state):
            objs = await _fetch([offices_state[idx]])
            return _detail_result(objs)

    _ORDINALS = {
        "первый": 0, "первое": 0, "первая": 0, "first": 0, "birinchisi": 0, "birinchi": 0,
        "второй": 1, "второе": 1, "вторая": 1, "second": 1, "ikkinchisi": 1, "ikkinchi": 1,
        "третий": 2, "третье": 2, "третья": 2, "third": 2, "uchinchisi": 2, "uchinchi": 2,
        "четвертый": 3, "четвёртый": 3, "fourth": 3, "to'rtinchisi": 3,
        "пятый": 4, "fifth": 4, "beshinchisi": 4,
    }
    if norm in _ORDINALS and _ORDINALS[norm] < len(offices_state):
        objs = await _fetch([offices_state[_ORDINALS[norm]]])
        return _detail_result(objs)

    matched_items = [it for it in offices_state if norm in (it.get("name") or "").lower()]
    if matched_items:
        objs = await _fetch([matched_items[0]])
        return _detail_result(objs)

    return at("office_not_found_in_list", lang), None


@lc_tool
async def request_operator(
    reason: str = "",
    state: Annotated[dict, InjectedState] = None,
) -> str:
    """Transfer the user to a live operator. LAST RESORT.

    GENERAL RULE: for ANY customer question, call `faq_lookup` FIRST. Only call
    `request_operator` in these three cases:
    1. The user EXPLICITLY asks for a live operator/human
       ("позови оператора", "оператор", "operatorga ulang", "live agent").
    2. The user asks you to PERFORM an action on their account that needs
       identity verification — "do it for me NOW" (block my card, transfer
       money, change my password). The signal is "do it for me", not "how do I".
    3. `faq_lookup` returned `NO_MATCH_IN_FAQ`, the question is bank-specific,
       and you cannot answer it from general knowledge.

    A "how do I X / what to do if Y" question is NEVER an operator case by
    itself — it goes to `faq_lookup`.

    EXAMPLES:
    - "позови оператора" → request_operator(reason="user_request")
    - "заблокируйте мою карту прямо сейчас" → request_operator(reason="identity_required")

    reason: short tag — "identity_required" / "unclear_message" / "user_request".
    """
    lang = _lang_from_state(state)
    reason_lower = (reason or "").lower()
    if "identity" in reason_lower or "верификац" in reason_lower or "операци" in reason_lower:
        return at("operator_identity_required", lang)
    if "unclear" in reason_lower or "непонятн" in reason_lower or "не понял" in reason_lower:
        return at("operator_unclear_message", lang)
    return at("operator_connecting", lang)


# ---------------------------------------------------------------------------
# clarify — RE-ENABLED (Phase 4 "Экспертиза", 2026-08-07)
#
# Originally disabled 2026-06-11: it caused tight loops — the LLM asked
# "which card — Uzcard/Humo?", the user answered "uzcard" in free text, and
# the model re-asked the same question instead of using the answer.
#
# Two fixes together close that loop for good:
#  1. `options` are now REQUIRED reply buttons (Telegram keyboard / Mini App
#     chips — see nodes/faq.py::_update_dialog_from_tools), not free text to
#     parse. A tap returns the option's exact text, so there is nothing left
#     to mis-parse on the happy path.
#  2. A programmatic anti-loop guard in nodes/faq.py's tool-call round loop:
#     if the PREVIOUS finalized turn was itself a clarify prompt (tracked via
#     `dialog["clarify_last_turn"]`) and the model tries to call `clarify`
#     again this turn, the guard drops that tool result and strips `clarify`
#     from the tools bound for the rest of this turn's rounds — the model
#     physically cannot call it a second time in a row, so it falls through
#     to faq_lookup / a direct answer instead. See nodes/faq.py for the
#     mechanics and tests/test_phase4_tools.py for the regression test.
# ---------------------------------------------------------------------------


@lc_tool
async def clarify(
    question: str,
    options: list[str],
    state: Annotated[dict, InjectedState] = None,
) -> str:
    """Ask the customer a short, structured clarifying question when their
    message is genuinely ambiguous between 2-4 SPECIFIC, DIFFERENT next
    steps and nothing else (state, history, defaults) can resolve it.

    The `options` become reply buttons (Telegram) / tap-chips (Mini App) —
    the customer taps one and you get back that EXACT text next turn, never
    free-form guessing. Ends the turn (question + buttons), same as any
    other tool whose output is shown to the user as-is.

    CALL ONLY when the request could reasonably branch into 2-4 concrete,
    materially different answers and you have no other signal to pick one.

    EXAMPLES:
    - "как заблокировать карту" when the customer has both an Uzcard and a
      Humo card and the procedure differs → clarify(question="Уточните, пожалуйста, какая у вас карта — Uzcard или Humo?", options=["Uzcard", "Humo"])
    - "хочу закрыть вклад досрочно" when both currency AND term change the
      answer and neither is known → clarify(question="По какому вкладу вопрос — в сумах или в валюте?", options=["В сумах", "В валюте"])
    - "I want to close my deposit early" (currency unknown, changes the
      answer) → clarify(question="Which deposit is this about — UZS or foreign currency?", options=["UZS", "Foreign currency"])

    DO NOT call when:
    - you can just answer and note an assumption at the end ("Предполагаю,
      вы про X — если не так, уточните.") — cheaper than a whole extra turn.
    - there's a sensible default — pick the most common case and say so.
    - the customer's PREVIOUS message was already a clarify question from
      you (check <state>/history) and they replied with free text instead
      of tapping a button — do NOT call clarify again on the same ambiguity.
      Take their free-text reply at face value and answer it directly
      (faq_lookup or your best answer) instead of re-asking. A programmatic
      guard also enforces this, but do not rely on it — get it right first.

    Parameters:
        question: the clarifying question, in the customer's language.
        options: 2-4 short, DISTINCT reply options (button labels, a few
                 words each) — not full sentences.
    """
    return question


_FAQ_TOOLS = [
    find_office,
    select_office,
    get_office_types_info,
    get_currency_info,
    show_credit_menu,
    get_products,
    select_product,
    compare_products,
    start_calculator,
    custom_loan_calculator,
    what_if_scenario,
    affordability_check,
    faq_lookup,
    request_operator,
    clarify,
    recommend_product,
]
