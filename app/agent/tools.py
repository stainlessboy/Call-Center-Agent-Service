from __future__ import annotations

from langchain_core.tools import tool as lc_tool

from app.agent.constants import (
    _CURRENT_DIALOG,
    _REQUEST_LANGUAGE,
    _greeting_with_menu,
)
from app.agent.i18n import (
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
from app.utils.faq_tools import _faq_lookup


@lc_tool
async def greeting_response() -> str:
    """Greet the user. Use when user says hello/hi/привет/салом/здравствуйте or any greeting."""
    lang = _REQUEST_LANGUAGE.get()
    return _greeting_with_menu(lang)


@lc_tool
async def thanks_response() -> str:
    """Respond to gratitude. Use when user says спасибо/рахмат/thank you."""
    lang = _REQUEST_LANGUAGE.get()
    return at("thanks_reply", lang)


@lc_tool
async def get_branch_info() -> str:
    """Get bank branch locations and working hours. Use when user asks about branches, offices, addresses."""
    lang = _REQUEST_LANGUAGE.get()
    return at("branch_info", lang)


@lc_tool
async def get_currency_info() -> str:
    """Get currency exchange rates. Use when user asks about USD, EUR, or currency rates."""
    lang = _REQUEST_LANGUAGE.get()
    from app.utils.cbu_rates import fetch_cbu_rates

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
async def show_credit_menu() -> str:
    """Show credit type selection. Use when user asks about credit/кредит without specifying type."""
    lang = _REQUEST_LANGUAGE.get()
    return at("credit_menu_prompt", lang)


@lc_tool
async def get_products(category: str) -> str:
    """
    Get list of bank products for a category.
    Categories: mortgage, autoloan, microloan, education_credit, deposit, debit_card, fx_card.
    Use when user asks about a specific product type.
    """
    lang = _REQUEST_LANGUAGE.get()
    products = await _get_products_by_category(category)
    if not products:
        label = category_label(category, lang)
        return at("product_unavailable", lang, label=label)
    return _format_product_list_text(products, category, lang)


@lc_tool
async def select_product(product_name: str) -> str:
    """
    Show details for a specific product. Use when user selects a product by name.
    The product_name should match one of the products currently displayed.
    """
    lang = _REQUEST_LANGUAGE.get()
    dialog = _CURRENT_DIALOG.get()
    products = list(dialog.get("products") or [])
    category = dialog.get("category", "")
    matched = _find_product_by_name(product_name, products)
    if not matched and products:
        matched = products[0]
    if not matched:
        return at("product_not_found", lang)
    return _format_product_card(matched, category, lang)


@lc_tool
async def compare_products(query: str) -> str:
    """
    Compare bank products. Use when user asks to compare or find differences.
    Returns product data for comparison — formulate the comparison in your response.
    """
    lang = _REQUEST_LANGUAGE.get()
    dialog = _CURRENT_DIALOG.get()
    flow = dialog.get("flow")
    products = list(dialog.get("products") or [])
    cmp_products = list(products) if flow == "show_products" else []
    if not cmp_products:
        detected = _detect_product_category(query)
        if detected and detected != "credit_menu":
            cmp_products = await _get_products_by_category(detected)
    if cmp_products:
        cmp_lines = []
        for p in cmp_products:
            line = (
                f"• {p['name']}: {at('cmp_rate', lang)} {p.get('rate') or '—'}, "
                f"{at('cmp_amount', lang)} {p.get('amount') or p.get('min_amount') or '—'}, "
                f"{at('cmp_term', lang)} {p.get('term') or '—'}"
            )
            if p.get("cashback"):
                line += f", {at('cmp_cashback', lang)} {p['cashback']}"
            if p.get("annual_fee"):
                line += f", {at('cmp_annual_fee', lang)} {p['annual_fee']}"
            if p.get("downpayment"):
                line += f", {at('cmp_downpayment', lang)} {p['downpayment']}"
            cmp_lines.append(line)
        prods_text = "\n".join(cmp_lines)
        return at("compare_header", lang, products=prods_text)
    return at("compare_clarify", lang)


@lc_tool
async def back_to_product_list() -> str:
    """Go back to the product list. Use when user clicks '◀ Все продукты' or says 'назад'."""
    lang = _REQUEST_LANGUAGE.get()
    dialog = _CURRENT_DIALOG.get()
    products = list(dialog.get("products") or [])
    category = dialog.get("category", "")
    if products:
        return _format_product_list_text(products, category, lang)
    return at("choose_category", lang)


@lc_tool
async def start_calculator() -> str:
    """Start payment calculator. Use when user clicks '✅ Рассчитать' or '📋 Подать заявку'."""
    lang = _REQUEST_LANGUAGE.get()
    dialog = _CURRENT_DIALOG.get()
    category = dialog.get("category", "")
    calc_qs = get_calc_questions(category, lang)
    if not calc_qs:
        return at("calc_no_questions", lang)
    _, first_q = calc_qs[0]
    return first_q


@lc_tool
async def faq_lookup(query: str) -> str:
    """Look up FAQ database for banking questions about services, products, or procedures."""
    lang = _REQUEST_LANGUAGE.get()
    result = await _faq_lookup(query, lang)
    return result or ""


@lc_tool
async def request_operator() -> str:
    """Transfer the user to a live operator. Use when the user explicitly asks
    to speak with a human operator, support agent, or live person."""
    lang = _REQUEST_LANGUAGE.get()
    return at("operator_connecting", lang)


_FAQ_TOOLS = [
    greeting_response, thanks_response, get_branch_info, get_currency_info,
    show_credit_menu, get_products, select_product, compare_products,
    back_to_product_list, start_calculator, faq_lookup, request_operator,
]
