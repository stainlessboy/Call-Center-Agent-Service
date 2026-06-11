from __future__ import annotations

from typing import Annotated, Literal

from langchain_core.tools import tool as lc_tool
from langgraph.prebuilt import InjectedState

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
_FAQ_STRICT_THRESHOLD: float = float(_os_thresh.getenv("FAQ_STRICT_THRESHOLD", "0.42"))
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


@lc_tool
async def get_products(
    category: str,
    state: Annotated[dict, InjectedState] = None,
) -> str:
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
    - Answer text if the match is confident — pass it to the user.
    - The literal string "FAQ_LOW_CONFIDENCE" or "NO_MATCH_IN_FAQ" if nothing
      confident was found. In BOTH cases:
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
    answer, score = await _faq_lookup_with_score(query, lang)
    print('__query__',query)
    print('__score__',score)
    print('__answer__',answer)
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
# clarify — TEMPORARILY DISABLED (2026-06-11)
#
# The clarify tool caused tight loops: the LLM asked "which card — Uzcard/Humo?",
# the user answered "uzcard", and the model re-asked the same question instead
# of using the answer. We removed it from `_FAQ_TOOLS` so the LLM can no longer
# call it. The simpler flow is now: answer from faq_lookup, and if nothing is
# found, fall through to the generic fallback (which surfaces the operator
# button after a couple of unhelpful turns — see helpers._finalize_turn).
#
# Kept here commented out so it can be restored quickly if we decide structured
# disambiguation is worth re-adding. If you re-enable it, also re-add it to
# `_FAQ_TOOLS` below and restore the policy/docstring references.
#
# @lc_tool
# async def clarify(
#     missing_info: str,
#     options: list[str] = None,
#     state: Annotated[dict, InjectedState] = None,
# ) -> str:
#     """Ask the user a structured clarifying question when their message is ambiguous
#     or incomplete, instead of a flat 'I don't understand'.
#     """
#     from app.agent.i18n import at as _at
#     lang = _lang_from_state(state)
#     prompt = _at("clarify_prompt", lang, info=missing_info)
#     if options:
#         header = _at("clarify_options_header", lang)
#         bullet_list = "\n".join(f"• {opt}" for opt in options)
#         return f"{prompt}\n\n{header}\n{bullet_list}"
#     return prompt


_FAQ_TOOLS = [
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
    # clarify,  # TEMPORARILY DISABLED — see comment above
]
