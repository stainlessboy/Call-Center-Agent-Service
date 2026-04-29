from __future__ import annotations

from typing import Annotated, Literal

from langchain_core.tools import tool as lc_tool
from langgraph.prebuilt import InjectedState

from app.agent.constants import (
    _greeting_with_menu,
)
from app.agent.i18n import (
    at,
    category_label,
    get_calc_questions,
)
from app.agent.products import (
    _find_product_by_name,
    _format_product_card,
    _format_product_list_text,
    _get_products_by_category,
)
from app.utils.faq_tools import _faq_lookup_with_score

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

# Fixed conservative default rate (in %) used by custom_loan_calculator when
# the user did NOT explicitly state one. Making it visible (and configurable
# via env) keeps the LLM from hallucinating a specific rate like "12%".
import os as _os
_DEFAULT_CUSTOM_LOAN_RATE_PCT: float = float(_os.getenv("DEFAULT_CUSTOM_LOAN_RATE_PCT", "20.0"))

# Sentinels returned by faq_lookup — explicit strings so the LLM can detect
# and handle each case without hallucinating an answer.
NO_MATCH_IN_FAQ = "NO_MATCH_IN_FAQ"
# Returned when the best FAQ match score is between LOW and STRICT thresholds.
# The LLM should call clarify() to disambiguate before answering.
FAQ_LOW_CONFIDENCE = "FAQ_LOW_CONFIDENCE"

# Tri-tier FAQ similarity thresholds (overridable via env).
import os as _os_thresh
_FAQ_STRICT_THRESHOLD: float = float(_os_thresh.getenv("FAQ_STRICT_THRESHOLD", "0.62"))
_FAQ_LOW_CONFIDENCE_THRESHOLD: float = float(_os_thresh.getenv("FAQ_LOW_CONFIDENCE_THRESHOLD", "0.45"))


def _lang_from_state(state: dict | None) -> str:
    """Pull `lang` from InjectedState. Falls back to dialog.last_lang, then 'ru'."""
    if not state:
        return "ru"
    lang = state.get("lang")
    if lang in ("ru", "en", "uz"):
        return lang
    dialog = state.get("dialog") or {}
    return dialog.get("last_lang") or "ru"


@lc_tool
async def greeting_response(
    state: Annotated[dict, InjectedState] = None,
) -> str:
    """Greet the user when they say hello.

    EXAMPLES:
    - "привет" → greeting_response()
    - "здравствуйте" → greeting_response()
    - "hi" / "hello" → greeting_response()
    - "salom" / "assalomu alaykum" → greeting_response()
    """
    return _greeting_with_menu(_lang_from_state(state))


@lc_tool
async def thanks_response(
    state: Annotated[dict, InjectedState] = None,
) -> str:
    """Reply to gratitude from the user.

    EXAMPLES:
    - "спасибо" / "благодарю" → thanks_response()
    - "thank you" → thanks_response()
    - "rahmat" / "katta rahmat" → thanks_response()
    """
    return at("thanks_reply", _lang_from_state(state))


async def _find_offices_impl(office_type: str, query: str, lang: str) -> str:
    from app.agent.branches import format_branches_list, search_offices

    offices = await search_offices(query=query, office_types=[office_type], limit=5)
    if not offices:
        return at("branch_none_found", lang, query=query or "—")

    header = at("branch_found_header", lang, count=len(offices))
    return f"{header}\n\n{format_branches_list(offices, lang)}"


@lc_tool
async def find_office(
    office_type: Literal["filial", "sales_office", "sales_point"],
    query: str = "",
    state: Annotated[dict, InjectedState] = None,
) -> str:
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
    return await _find_offices_impl(office_type, query, _lang_from_state(state))


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


@lc_tool
async def get_currency_info(
    state: Annotated[dict, InjectedState] = None,
) -> str:
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
        return at("currency_info", lang)
    lines = []
    for r in rates:
        nominal = r["nominal"]
        nom_str = f"{nominal} " if str(nominal) != "1" else ""
        diff = float(r["diff"]) if r["diff"] else 0
        arrow = "↑" if diff > 0 else ("↓" if diff < 0 else "")
        lines.append(f"{r['icon']} {nom_str}{r['code']} = {r['rate']} UZS {arrow}")
    header = {"ru": "Курс ЦБ Узбекистана", "en": "CBU exchange rates", "uz": "O'zbekiston MB kursi"}[lang]
    date_str = rates[0].get("date", "")
    return f"{header} ({date_str}):\n" + "\n".join(lines)


@lc_tool
async def show_credit_menu(
    state: Annotated[dict, InjectedState] = None,
) -> str:
    """Show the credit-type selection menu. Use when user asks about credit without specifying type.

    EXAMPLES:
    - "хочу кредит" / "мне нужен кредит" / "какие есть кредиты" → show_credit_menu()
    - "I need a loan" → show_credit_menu()
    - "kredit olmoqchiman" → show_credit_menu()

    DO NOT call when the user specifies the credit type (ипотека/автокредит/etc) — use get_products instead.
    """
    return at("credit_menu_prompt", _lang_from_state(state))


@lc_tool
async def get_products(
    category: str,
    state: Annotated[dict, InjectedState] = None,
) -> str:
    """Get list of bank products for a specific category.
    Returns pre-formatted text — pass to the user AS-IS.

    CATEGORIES: mortgage, autoloan, microloan, education_credit, deposit, debit_card, fx_card.

    EXAMPLES:
    - "хочу ипотеку" → get_products(category="mortgage")
    - "покажи автокредиты" → get_products(category="autoloan")
    - "какие у вас вклады" → get_products(category="deposit")
    - "микрозайм" → get_products(category="microloan")
    - "дебетовые карты" → get_products(category="debit_card")
    - "валютные карты" → get_products(category="fx_card")
    - "all products" / "◀ Все продукты" when state has category → get_products(category=<state.category>)
    - "deposit products" → get_products(category="deposit")
    - "ipoteka" → get_products(category="mortgage")

    Also use when the user clicks a "back to products" button while a category is in state —
    call with the state's current category to re-render the list.
    """
    lang = _lang_from_state(state)
    products = await _get_products_by_category(category)
    if not products:
        label = category_label(category, lang)
        return at("product_unavailable", lang, label=label)
    return _format_product_list_text(products, category, lang)


@lc_tool
async def select_product(
    product_name: str,
    state: Annotated[dict, InjectedState] = None,
) -> str:
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

    # Tier 1: search within the products already loaded in dialog state
    matched = _find_product_by_name(product_name, dialog_products)
    if matched:
        return _format_product_card(matched, dialog_category, lang)

    # Tier 2: search in DB by dialog category
    if dialog_category:
        db_products = await _get_products_by_category(dialog_category)
        matched = _find_product_by_name(product_name, db_products)
        if matched:
            return _format_product_card(matched, dialog_category, lang)

    # Tier 3: search across all known categories
    for cat in _ALL_CATEGORIES:
        if cat == dialog_category:
            continue
        cat_products = await _get_products_by_category(cat)
        matched = _find_product_by_name(product_name, cat_products)
        if matched:
            return _format_product_card(matched, cat, lang)

    # No match found anywhere
    if dialog_products:
        names = ", ".join(p["name"] for p in dialog_products[:5])
        return at("product_not_found_suggest", lang, names=names)
    return at("product_not_found", lang)


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


@lc_tool
async def custom_loan_calculator(
    amount: float,
    term_months: int,
    downpayment: float = 0.0,
    state: Annotated[dict, InjectedState] = None,
) -> str:
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

    Parameters:
        amount: Total loan amount in UZS BEFORE deducting downpayment (e.g. 50_000_000).
        term_months: Integer number of months (e.g. 36 for 3 years).
        downpayment: Absolute downpayment in UZS (0.0 if none).
    """
    lang = _lang_from_state(state)
    rate_pct = _DEFAULT_CUSTOM_LOAN_RATE_PCT

    principal = amount - downpayment
    if principal <= 0:
        _err = {
            "ru": "Укажите корректные суммы: сумма кредита должна быть больше первоначального взноса.",
            "en": "Please provide valid amounts: the loan amount must exceed the down payment.",
            "uz": "Iltimos, to'g'ri summalarni kiriting: kredit summasi boshlang'ich to'lovdan katta bo'lishi kerak.",
        }
        return _err.get(lang) or _err["ru"]
    if term_months <= 0:
        _err = {
            "ru": "Укажите корректный срок (в месяцах, больше нуля).",
            "en": "Please provide a valid term (in months, greater than zero).",
            "uz": "Iltimos, to'g'ri muddatni kiriting (oyda, noldan katta).",
        }
        return _err.get(lang) or _err["ru"]

    r = rate_pct / 100 / 12
    monthly = principal * r * (1 + r) ** term_months / ((1 + r) ** term_months - 1)
    total = monthly * term_months
    overpayment = total - principal

    def fmt(v: float) -> str:
        return f"{v:,.0f}".replace(",", " ")

    return at(
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


@lc_tool
async def faq_lookup(
    query: str,
    state: Annotated[dict, InjectedState] = None,
) -> str:
    """Look up the FAQ knowledge base for banking questions about services, products or procedures.

    EXAMPLES:
    - "как обновить паспорт в приложении" → faq_lookup(query="обновить паспорт в приложении")
    - "можно ли досрочно погасить кредит" → faq_lookup(query="досрочное погашение кредита")
    - "как заблокировать карту" → faq_lookup(query="блокировка карты")
    - "есть ли обслуживание без очереди в филиалах" → faq_lookup(query="обслуживание без очереди")
    - "Филиалларингда навбатсиз хизмат курсатолисизме" → faq_lookup(query="навбатсиз хизмат")
    - "how to change phone number" → faq_lookup(query="change phone number")
    - "parolni qanday tiklayman" → faq_lookup(query="parolni tiklash")

    RETURNS one of three values:
    - Answer text if score >= FAQ_STRICT_THRESHOLD (0.62 default) — pass it to the user.
    - The literal string "FAQ_LOW_CONFIDENCE" if LOW_CONFIDENCE_THRESHOLD <= score < STRICT.
      In that case call clarify(missing_info=...) to disambiguate before answering.
    - The literal string "NO_MATCH_IN_FAQ" if score < LOW_CONFIDENCE_THRESHOLD.
      In that case ask the user to rephrase OR call clarify. After 1-2 failed
      clarifications call request_operator(reason="unclear_message"). DO NOT fabricate an answer.
    """
    lang = _lang_from_state(state)
    answer, score = await _faq_lookup_with_score(query, lang)
    if score >= _FAQ_STRICT_THRESHOLD:
        return answer or NO_MATCH_IN_FAQ
    if score >= _FAQ_LOW_CONFIDENCE_THRESHOLD:
        return FAQ_LOW_CONFIDENCE
    return NO_MATCH_IN_FAQ


@lc_tool
async def select_office(
    office_name: str,
    state: Annotated[dict, InjectedState] = None,
) -> str:
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
        return at("office_not_found", lang)

    from app.agent.branches import format_branch_card, format_branches_list
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

    norm = (office_name or "").strip().lower()
    if norm in ("all", "все", "всё", "хаммаси", "barchasi", "hammasini", "hammasi"):
        objs = await _fetch(offices_state)
        return format_branches_list(objs, lang) if objs else at("office_not_found", lang)

    if norm.isdigit():
        idx = int(norm) - 1
        if 0 <= idx < len(offices_state):
            objs = await _fetch([offices_state[idx]])
            return format_branch_card(objs[0], lang) if objs else at("office_not_found", lang)

    _ORDINALS = {
        "первый": 0, "первое": 0, "первая": 0, "first": 0, "birinchisi": 0, "birinchi": 0,
        "второй": 1, "второе": 1, "вторая": 1, "second": 1, "ikkinchisi": 1, "ikkinchi": 1,
        "третий": 2, "третье": 2, "третья": 2, "third": 2, "uchinchisi": 2, "uchinchi": 2,
        "четвертый": 3, "четвёртый": 3, "fourth": 3, "to'rtinchisi": 3,
        "пятый": 4, "fifth": 4, "beshinchisi": 4,
    }
    if norm in _ORDINALS and _ORDINALS[norm] < len(offices_state):
        objs = await _fetch([offices_state[_ORDINALS[norm]]])
        return format_branch_card(objs[0], lang) if objs else at("office_not_found", lang)

    matched_items = [it for it in offices_state if norm in (it.get("name") or "").lower()]
    if matched_items:
        objs = await _fetch([matched_items[0]])
        return format_branch_card(objs[0], lang) if objs else at("office_not_found", lang)

    return at("office_not_found_in_list", lang)


@lc_tool
async def request_operator(
    reason: str = "",
    state: Annotated[dict, InjectedState] = None,
) -> str:
    """Transfer the user to a live operator.

    WHEN TO CALL:
    1. User explicitly asks for a live operator/human.
    2. User requests identity-required operations (SMS toggle, card block/unblock,
       personal-data change, account status check, transfers).
    3. You asked the user to rephrase but still cannot understand — after 2-3 attempts.

    EXAMPLES:
    - "позови оператора" / "хочу живого человека" → request_operator(reason="user_request")
    - "разблокируй мою карту" → request_operator(reason="identity_required")
    - "подключи смс к моей карте" → request_operator(reason="identity_required")
    - "connect me to support" → request_operator(reason="user_request")
    - "operatorga ulang" → request_operator(reason="user_request")

    reason: short tag — "identity_required" / "unclear_message" / "user_request".
    """
    lang = _lang_from_state(state)
    reason_lower = (reason or "").lower()
    if "identity" in reason_lower or "верификац" in reason_lower or "операци" in reason_lower:
        return at("operator_identity_required", lang)
    if "unclear" in reason_lower or "непонятн" in reason_lower or "не понял" in reason_lower:
        return at("operator_unclear_message", lang)
    return at("operator_connecting", lang)


@lc_tool
async def clarify(
    missing_info: str,
    options: list[str] = None,
    state: Annotated[dict, InjectedState] = None,
) -> str:
    """Ask the user a structured clarifying question when their message is ambiguous
    or incomplete, instead of a flat 'I don't understand'.

    PREFER this over a free-form 'please rephrase' answer when:
    - the user's intent is unclear but bank-related
    - faq_lookup returned FAQ_LOW_CONFIDENCE — disambiguate before answering
    - the user asked something that needs a missing parameter (which product? which city? which card type?)

    DO NOT use clarify() for:
    - greeting/thanks (use greeting_response/thanks_response)
    - clear product or branch requests (use get_products / find_office)
    - explicit operator request (use request_operator)

    After 1-2 unsuccessful clarifies in a row → call request_operator(reason="unclear_message").

    EXAMPLES:
    - user: "хочу что-то открыть" → clarify(missing_info="какой продукт", options=["Вклад", "Карта", "Кредит"])
    - user: "какая ставка" → clarify(missing_info="по какому продукту", options=["Ипотека", "Автокредит", "Микрозайм"])
    - user: "where is the office" → clarify(missing_info="city or district")
    - faq_lookup returned FAQ_LOW_CONFIDENCE for "карта" → clarify(missing_info="какая карта", options=["Дебетовая", "Валютная"])

    Parameters:
        missing_info: short phrase describing what's unclear (e.g. "тип кредита", "city").
        options: optional list of suggested answers shown as quick-reply buttons.
    """
    from app.agent.i18n import at as _at
    lang = _lang_from_state(state)
    prompt = _at("clarify_prompt", lang, info=missing_info)
    if options:
        header = _at("clarify_options_header", lang)
        bullet_list = "\n".join(f"• {opt}" for opt in options)
        return f"{prompt}\n\n{header}\n{bullet_list}"
    return prompt


_FAQ_TOOLS = [
    greeting_response,
    thanks_response,
    find_office,
    select_office,
    get_office_types_info,
    get_currency_info,
    show_credit_menu,
    get_products,
    select_product,
    start_calculator,
    custom_loan_calculator,
    faq_lookup,
    request_operator,
    clarify,
]
