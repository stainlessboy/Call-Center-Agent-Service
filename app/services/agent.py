from __future__ import annotations

import asyncio
import contextvars
import html as _html
import logging as _logging
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Dict, List, Optional, Sequence, TypedDict

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.tools import tool as lc_tool
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph
from langgraph.prebuilt import ToolNode
from langgraph.types import Command

from app.tools.data_loaders import (
    _normalize_language_code,
    _load_credit_product_offers,
    _load_deposit_product_offers,
    _load_card_product_offers,
)
from app.tools.faq_tools import FAQ_FALLBACK_REPLY, _faq_lookup
from app.tools.pdf_generator import generate_amortization_pdf

_agent_logger = _logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

class BotState(TypedDict):
    messages: List[Any]
    last_user_text: str
    answer: str
    human_mode: bool
    keyboard_options: Optional[List[str]]
    dialog: dict          # see _default_dialog()
    _route: str
    session_id: Optional[str]
    user_id: Optional[int]


@dataclass
class AgentTurnResult:
    """Structured result returned by Agent.send_message."""
    text: str
    keyboard_options: Optional[List[str]] = None
    show_operator_button: bool = False


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SYSTEM_POLICY = (
    "Ты опытный консультант банка. Общайся тепло, естественно и по делу — как живой человек. "
    "Отвечай коротко и конкретно, без канцелярских фраз. "
    "Не раскрывай, что ты бот — веди себя как живой консультант."
)

# category → section_name in CreditProductOffer table
CREDIT_SECTION_MAP: dict[str, str] = {
    "mortgage": "Ипотека",
    "autoloan": "Автокредит",
    "microloan": "Микрозайм",
    "education_credit": "Образовательный",
}

CATEGORY_LABELS: dict[str, str] = {
    "mortgage": "ипотечные программы",
    "autoloan": "программы автокредита",
    "microloan": "программы микрозайма",
    "education_credit": "образовательные кредиты",
    "deposit": "вклады",
    "debit_card": "дебетовые карты",
    "fx_card": "валютные карты",
}

# calc questions per category: list of (step_key, question_text)
CALC_QUESTIONS: dict[str, list[tuple[str, str]]] = {
    "mortgage": [
        ("amount", "Какую сумму кредита планируете взять (в сумах)?"),
        ("term", "На какой срок? (укажите в годах)"),
        ("downpayment", "Первоначальный взнос (в %)?"),
    ],
    "autoloan": [
        ("amount", "Какую сумму кредита планируете взять (в сумах)?"),
        ("term", "На какой срок? (укажите в месяцах)"),
        ("downpayment", "Первоначальный взнос (в %)?"),
    ],
    "microloan": [
        ("amount", "Какую сумму кредита планируете взять (в сумах)?"),
        ("term", "На какой срок? (укажите в месяцах)"),
    ],
    "education_credit": [
        ("amount", "Какую сумму кредита планируете взять (в сумах)?"),
        ("term", "На какой срок? (укажите в месяцах)"),
    ],
    "deposit": [
        ("amount", "Какую сумму планируете разместить (в сумах)?"),
        ("term", "На какой срок? (укажите в месяцах)"),
    ],
    "debit_card": [],
    "fx_card": [],
}

_REQUEST_LANGUAGE: contextvars.ContextVar[str] = contextvars.ContextVar("_REQUEST_LANGUAGE", default="ru")
_LANG_INSTRUCTION = {"en": " Reply in English.", "uz": " Javobni o'zbek tilida yoz.", "ru": ""}

# Dialog context passed to tools via contextvar
_CURRENT_DIALOG: contextvars.ContextVar[dict] = contextvars.ContextVar("_CURRENT_DIALOG", default={})

_MAIN_MENU_BUTTONS = ["🏠 Ипотека", "🚗 Автокредит", "💰 Микрозайм", "💳 Вклад", "🃏 Карта", "❓ Вопрос"]
_CREDIT_MENU_BUTTONS = ["🏠 Ипотека", "🚗 Автокредит", "💰 Микрозайм", "📚 Образовательный кредит"]


# ---------------------------------------------------------------------------
# Intent helpers
# ---------------------------------------------------------------------------

def _contains_any(text: str, tokens: Sequence[str]) -> bool:
    lower = text.lower()
    return any(t in lower for t in tokens)


def _is_greeting(text: str) -> bool:
    return _contains_any(text, ("привет", "здравств", "салом", "ассалом", "добрый", "hello", "hi "))


def _is_thanks(text: str) -> bool:
    return _contains_any(text, ("спасибо", "благодар", "рахмат", "thank"))


def _is_branch_question(text: str) -> bool:
    return _contains_any(text, ("филиал", "отделен", "офис", "цбу", "адрес", "ближайш", "режим работы", "часы работы"))


def _is_currency_question(text: str) -> bool:
    return _contains_any(text, ("курс", "доллар", "евро", "валют", "usd", "eur"))


def _is_calc_trigger(text: str) -> bool:
    lower = text.lower()
    return "рассчита" in lower or "✅" in text or "📋" in text or "подать заявку" in lower


def _is_back_trigger(text: str) -> bool:
    return "◀" in text or "все продукт" in text.lower() or "назад" in text.lower()


def _is_operator_request(text: str) -> bool:
    return _contains_any(text, (
        "оператор", "живой оператор", "подключи оператора",
        "соедини с оператором", "хочу оператора", "позовите оператора",
        "operator", "live agent", "human agent",
        "оператор билан", "операторга",
    ))


def _looks_like_question(text: str) -> bool:
    if "?" in text:
        return True
    lower = text.lower()
    return any(t in lower for t in (
        "забыл", "помоги", "не могу", "объясни", "расскажи",
        "где найти", "почему", "скажи", "как зайти", "как восстановить",
    ))


def _is_yes(text: str) -> bool:
    lower = text.lower()
    return any(t in lower for t in ("да", "yes", "✅", "позвоните", "хочу", "конечно", "ок", "ok", "ага"))


def _is_comparison_request(text: str) -> bool:
    lower = text.lower()
    return any(t in lower for t in (
        "разница между", "сравни", "сравнение", "чем отличается", "чем отличаются",
        "в чем разница", "отличие между", "что лучше",
    ))


def _detect_product_category(text: str) -> Optional[str]:
    """Rule-based product category detection. Returns category string or None."""
    lower = text.lower()
    if any(t in lower for t in ("ипотек", "квартир", "жиль", "недвижим", "новострой")):
        return "mortgage"
    if any(t in lower for t in ("автокредит", "авто кредит", "для машины", "для авто", "машин", "автомобил")):
        return "autoloan"
    if any(t in lower for t in ("образовательн", "учеб", "обучени", "контракт", "университет")):
        return "education_credit"
    if any(t in lower for t in ("микрозайм", "микро займ", "микрокредит")):
        return "microloan"
    if any(t in lower for t in ("вклад", "депозит", "накоп", "сбережени")):
        return "deposit"
    if any(t in lower for t in ("валютн", "за границ", "visa", "mastercard", "поездк")):
        if any(t in lower for t in ("карт", "карточ")):
            return "fx_card"
    if any(t in lower for t in ("карт", "карточ", "uzcard", "humo")):
        return "debit_card"
    # generic credit intent → show category selection menu
    if any(t in lower for t in ("кредит", "займ", "заём")):
        return "credit_menu"
    return None


# ---------------------------------------------------------------------------
# DB product tools
# ---------------------------------------------------------------------------

def _fmt_rate(offer: dict) -> str:
    low = offer.get("rate_min_pct")
    high = offer.get("rate_max_pct")
    if low is not None and high is not None and abs(float(low) - float(high)) > 0.01:
        return f"{float(low):.1f}–{float(high):.1f}%"
    if low is not None:
        return f"{float(low):.1f}%"
    return str(offer.get("rate_text") or "уточняется")


async def _get_products_by_category(category: str) -> list[dict]:
    """Return deduplicated list of products for given category from DB."""
    seen: set[str] = set()
    result: list[dict] = []

    if category in CREDIT_SECTION_MAP:
        section = CREDIT_SECTION_MAP[category]
        for offer in await _load_credit_product_offers():
            if offer.get("section_name") != section:
                continue
            name = str(offer.get("service_name") or "").strip()
            if not name or name in seen:
                continue
            seen.add(name)
            result.append({
                "name": name,
                "rate": _fmt_rate(offer),
                "term": offer.get("term_text") or "",
                "amount": offer.get("amount_text") or "",
                "downpayment": offer.get("downpayment_text") or "",
                "rate_min_pct": offer.get("rate_min_pct"),
                "rate_max_pct": offer.get("rate_max_pct"),
                "collateral": offer.get("collateral_text") or "",
                "purpose": offer.get("purpose_text") or "",
            })

    elif category == "deposit":
        for offer in await _load_deposit_product_offers():
            name = str(offer.get("service_name") or "").strip()
            if not name or name in seen:
                continue
            seen.add(name)
            result.append({
                "name": name,
                "rate": offer.get("rate_text") or "",
                "rate_pct": offer.get("rate_pct"),
                "term": offer.get("term_text") or "",
                "term_months": offer.get("term_months"),
                "min_amount": offer.get("min_amount_text") or "",
                "currency": offer.get("currency_code") or "UZS",
                "topup": offer.get("topup_text") or "",
                "payout": offer.get("payout_text") or "",
            })

    elif category in ("debit_card", "fx_card"):
        is_fx = category == "fx_card"
        for offer in await _load_card_product_offers():
            if bool(offer.get("is_fx_card")) != is_fx:
                continue
            name = str(offer.get("service_name") or "").strip()
            if not name or name in seen:
                continue
            seen.add(name)
            result.append({
                "name": name,
                "network": offer.get("card_network") or "",
                "currency": offer.get("currency_code") or "",
                "cashback": offer.get("cashback_text") or "",
                "issue_fee": offer.get("issue_fee_text") or "",
                "annual_fee": offer.get("annual_fee_text") or "",
                "delivery": offer.get("delivery_available"),
                "validity": offer.get("validity_text") or "",
            })

    return result


def _format_product_list_text(products: list[dict], category: str) -> str:
    label = CATEGORY_LABELS.get(category, "продукты")
    lines = [f"Вот наши {label}:\n"]
    for p in products:
        rate = p.get("rate") or ""
        line = f"• {_html.escape(p['name'])}"
        if rate:
            line += f" — {rate}"
        lines.append(line)
    lines.append("\nВыберите программу для подробной информации.")
    return "\n".join(lines)


def _format_product_card(product: dict, category: str) -> str:
    name = _html.escape(product["name"])
    lines = [f"<b>{name}</b>\n"]

    if category in ("mortgage", "autoloan", "microloan", "education_credit"):
        if product.get("rate"):
            lines.append(f"📊 Ставка: {product['rate']}")
        if product.get("amount"):
            lines.append(f"💰 Сумма: {product['amount']}")
        if product.get("term"):
            lines.append(f"📅 Срок: {product['term']}")
        if product.get("downpayment"):
            lines.append(f"💵 Первый взнос: {product['downpayment']}")
        if product.get("purpose"):
            lines.append(f"🎯 Цель: {product['purpose']}")
        if product.get("collateral"):
            lines.append(f"🔒 Обеспечение: {product['collateral']}")

    elif category == "deposit":
        if product.get("rate"):
            lines.append(f"📊 Ставка: {product['rate']}")
        if product.get("term"):
            lines.append(f"📅 Срок: {product['term']}")
        if product.get("min_amount"):
            lines.append(f"💰 Мин. сумма: {product['min_amount']}")
        if product.get("currency"):
            lines.append(f"💱 Валюта: {product['currency']}")
        if product.get("topup"):
            lines.append(f"➕ Пополнение: {product['topup']}")
        if product.get("payout"):
            lines.append(f"💸 Выплата %: {product['payout']}")

    elif category in ("debit_card", "fx_card"):
        if product.get("network"):
            lines.append(f"💳 Платёжная сеть: {product['network']}")
        if product.get("currency"):
            lines.append(f"💱 Валюта: {product['currency']}")
        if product.get("issue_fee"):
            lines.append(f"🏷 Выпуск: {product['issue_fee']}")
        if product.get("annual_fee"):
            lines.append(f"💰 Обслуживание: {product['annual_fee']}")
        if product.get("cashback"):
            lines.append(f"🎁 Кэшбэк: {product['cashback']}")
        if product.get("validity"):
            lines.append(f"📅 Срок карты: {product['validity']}")
        if product.get("delivery"):
            lines.append("🚚 Доставка: доступна")

    return "\n".join(lines)


def _find_product_by_name(user_text: str, products: list[dict]) -> Optional[dict]:
    """Find product by exact, contains, or word-overlap match."""
    lower = user_text.lower().strip()
    for p in products:
        if p["name"].lower().strip() == lower:
            return p
    for p in products:
        pname = p["name"].lower()
        if pname in lower or lower in pname:
            return p
    user_words = {w for w in lower.split() if len(w) > 3}
    for p in products:
        pwords = {w for w in p["name"].lower().split() if len(w) > 3}
        if user_words & pwords:
            return p
    return None


# ---------------------------------------------------------------------------
# Number parsers (for calc_step)
# ---------------------------------------------------------------------------

def _parse_amount(text: str) -> Optional[int]:
    """Parse amount from text, returns integer in UZS."""
    cleaned = text.replace(" ", "").replace(",", "").lower()
    m = re.search(r"(\d+(?:\.\d+)?)\s*(млрд|млн|тыс|тысяч|k)?", cleaned)
    if not m:
        return None
    value = float(m.group(1))
    suffix = m.group(2) or ""
    if "млрд" in suffix:
        value *= 1_000_000_000
    elif "млн" in suffix:
        value *= 1_000_000
    elif "тыс" in suffix or suffix == "k":
        value *= 1_000
    return int(value)


def _parse_term_months(text: str) -> Optional[int]:
    """Parse term from text, returns months."""
    lower = text.lower().strip()
    m = re.search(r"(\d+)\s*(лет|год|лет|years?|г\.?|months?|мес\.?|м\.?)?", lower)
    if not m:
        return None
    value = int(m.group(1))
    unit = (m.group(2) or "").lower()
    if any(u in unit for u in ("лет", "год", "year", "г.")):
        return value * 12
    return value  # assume months


def _parse_downpayment(text: str) -> Optional[float]:
    """Parse downpayment percentage from text."""
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*%?", text)
    if m:
        return float(m.group(1).replace(",", "."))
    return None


# ---------------------------------------------------------------------------
# LLM helpers
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _get_chat_openai() -> Optional[ChatOpenAI]:
    """Return a LangChain ChatOpenAI instance."""
    try:
        kwargs: dict[str, Any] = {
            "model": os.getenv("LOCAL_AGENT_INTENT_LLM_MODEL", "gpt-4o-mini"),
            "temperature": 0.3,
            "max_tokens": 512,
            "api_key": os.getenv("OPENAI_API_KEY"),
        }
        base_url = os.getenv("OPENAI_BASE_URL")
        if base_url:
            kwargs["base_url"] = base_url
        return ChatOpenAI(**kwargs)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Greeting
# ---------------------------------------------------------------------------

def _greeting_with_menu(lang: str = "ru") -> str:
    if lang == "en":
        return "Hello! What are you interested in?"
    if lang == "uz":
        return "Assalomu alaykum! Qiziqtirayotgan bo'limni tanlang:"
    return "Здравствуйте! Выберите раздел или напишите ваш вопрос:"


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------

FALLBACK_STREAK_THRESHOLD = 3  # show operator button after this many consecutive fallbacks


def _default_dialog() -> dict:
    return {
        "flow": None,
        "category": None,
        "products": [],
        "selected_product": None,
        "calc_step": None,
        "calc_slots": {},
        "lead_step": None,
        "lead_slots": {},
        "fallback_streak": 0,
    }


def _finalize_turn(
    state: BotState,
    answer: str,
    dialog: dict,
    keyboard_options: Optional[List[str]] = None,
) -> dict:
    user_text = (state.get("last_user_text") or "").strip()
    msgs = list(state.get("messages") or [SystemMessage(content=SYSTEM_POLICY)])
    msgs.append(HumanMessage(content=user_text))
    msgs.append(AIMessage(content=answer))
    _max = int(os.getenv("MAX_DIALOG_MESSAGES", "50"))
    if len(msgs) > _max + 1:
        msgs = [msgs[0]] + msgs[-_max:]

    # Track consecutive fallback answers
    is_fallback = (answer == FAQ_FALLBACK_REPLY)
    streak = dialog.get("fallback_streak", 0)
    streak = streak + 1 if is_fallback else 0

    show_operator = (
        streak >= FALLBACK_STREAK_THRESHOLD
        or _is_operator_request(user_text)
        or dialog.get("operator_requested", False)
    )
    dialog = {**dialog, "fallback_streak": streak, "operator_requested": False}

    return {
        "messages": msgs,
        "answer": answer,
        "dialog": dialog,
        "keyboard_options": keyboard_options,
        "show_operator_button": show_operator,
    }


# ---------------------------------------------------------------------------
# LangGraph Tools — called by LLM in node_faq via bind_tools / ToolNode
# ---------------------------------------------------------------------------

@lc_tool
async def greeting_response() -> str:
    """Greet the user. Use when user says hello/hi/привет/салом/здравствуйте or any greeting."""
    lang = _REQUEST_LANGUAGE.get()
    return _greeting_with_menu(lang)


@lc_tool
async def thanks_response() -> str:
    """Respond to gratitude. Use when user says спасибо/рахмат/thank you."""
    return "Пожалуйста! Если нужно — пишите."


@lc_tool
async def get_branch_info() -> str:
    """Get bank branch locations and working hours. Use when user asks about branches, offices, addresses."""
    return (
        "В банке есть отделения по всему Узбекистану.\n"
        "Напишите ваш город или район — подскажу ближайший адрес."
    )


@lc_tool
async def get_currency_info() -> str:
    """Get currency exchange rates. Use when user asks about USD, EUR, or currency rates."""
    return (
        "Актуальные курсы валют смотрите на сайте банка или в мобильном приложении AsakaBank.\n"
        "Там же можно открыть валютный вклад или заказать карту."
    )


@lc_tool
async def show_credit_menu() -> str:
    """Show credit type selection. Use when user asks about credit/кредит without specifying type."""
    return "Выберите вид кредита: Ипотека, Автокредит, Микрозайм, Образовательный кредит"


@lc_tool
async def get_products(category: str) -> str:
    """
    Get list of bank products for a category.
    Categories: mortgage, autoloan, microloan, education_credit, deposit, debit_card, fx_card.
    Use when user asks about a specific product type.
    """
    products = await _get_products_by_category(category)
    if not products:
        label = CATEGORY_LABELS.get(category, category)
        return f"Информация по {label} уточняется. Обратитесь в ближайшее отделение."
    return _format_product_list_text(products, category)


@lc_tool
async def select_product(product_name: str) -> str:
    """
    Show details for a specific product. Use when user selects a product by name.
    The product_name should match one of the products currently displayed.
    """
    dialog = _CURRENT_DIALOG.get()
    products = list(dialog.get("products") or [])
    category = dialog.get("category", "")
    matched = _find_product_by_name(product_name, products)
    if not matched and products:
        matched = products[0]
    if not matched:
        return "Продукт не найден. Выберите из списка."
    return _format_product_card(matched, category)


@lc_tool
async def compare_products(query: str) -> str:
    """
    Compare bank products. Use when user asks to compare or find differences.
    Returns product data for comparison — formulate the comparison in your response.
    """
    dialog = _CURRENT_DIALOG.get()
    flow = dialog.get("flow")
    products = list(dialog.get("products") or [])
    cmp_products = list(products) if flow == "show_products" else []
    if not cmp_products:
        detected = _detect_product_category(query)
        if detected and detected != "credit_menu":
            cmp_products = await _get_products_by_category(detected)
    if cmp_products:
        prods_text = "\n".join(
            f"• {p['name']}: ставка {p.get('rate') or '—'}, "
            f"сумма {p.get('amount') or p.get('min_amount') or '—'}, "
            f"срок {p.get('term') or '—'}"
            for p in cmp_products
        )
        return (
            f"Продукты нашего банка:\n{prods_text}\n\n"
            "Сравни только продукты из списка. Не упоминай другие банки."
        )
    return "Уточните, какие продукты вы хотите сравнить."


@lc_tool
async def back_to_product_list() -> str:
    """Go back to the product list. Use when user clicks '◀ Все продукты' or says 'назад'."""
    dialog = _CURRENT_DIALOG.get()
    products = list(dialog.get("products") or [])
    category = dialog.get("category", "")
    if products:
        return _format_product_list_text(products, category)
    return "Выберите категорию продукта."


@lc_tool
async def start_calculator() -> str:
    """Start payment calculator. Use when user clicks '✅ Рассчитать' or '📋 Подать заявку'."""
    dialog = _CURRENT_DIALOG.get()
    category = dialog.get("category", "")
    calc_qs = CALC_QUESTIONS.get(category, [])
    if not calc_qs:
        return "✅ Ваша заявка принята! Наш специалист свяжется с вами в ближайшее время."
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
    return "Сейчас подключу оператора. Нажмите кнопку ниже."


_FAQ_TOOLS = [
    greeting_response, thanks_response, get_branch_info, get_currency_info,
    show_credit_menu, get_products, select_product, compare_products,
    back_to_product_list, start_calculator, faq_lookup, request_operator,
]


# ---------------------------------------------------------------------------
# NODE: router — 3 routes (faq, calc_flow, human_mode)
# ---------------------------------------------------------------------------

async def node_router(state: BotState) -> Command:
    """
    Minimal router:
    - human_mode active → human_mode node
    - lead_step or calc_flow active → calc_flow node
    - everything else → faq node (LLM picks the right tool)
    """
    if state.get("human_mode"):
        return Command(goto="human_mode")
    dialog = state.get("dialog") or _default_dialog()
    if dialog.get("lead_step"):
        return Command(goto="calc_flow")
    if dialog.get("flow") == "calc_flow":
        return Command(goto="calc_flow")
    return Command(goto="faq")


# ---------------------------------------------------------------------------
# Dialog state update from tool calls
# ---------------------------------------------------------------------------

def _reattach_keyboard(dialog: dict) -> tuple[dict, Optional[List[str]]]:
    """Re-attach flow-appropriate keyboard."""
    flow = dialog.get("flow")
    products = list(dialog.get("products") or [])
    category = dialog.get("category", "")
    if flow == "product_detail":
        if category in ("debit_card", "fx_card"):
            return dict(dialog), ["📋 Подать заявку", "◀ Все продукты"]
        return dict(dialog), ["✅ Рассчитать платёж", "◀ Все продукты"]
    if flow == "show_products" and products:
        return dict(dialog), [p["name"] for p in products]
    return dict(dialog), None


async def _update_dialog_from_tools(
    dialog: dict, tool_calls: list, user_text: str,
) -> tuple[dict, Optional[List[str]]]:
    """Inspect which tools the LLM called and update dialog/keyboard accordingly."""
    if not tool_calls:
        return _reattach_keyboard(dialog)

    last_tc = tool_calls[-1]
    name = last_tc["name"]
    args = last_tc.get("args", {})

    if name == "greeting_response":
        return _default_dialog(), list(_MAIN_MENU_BUTTONS)

    if name == "thanks_response":
        return dict(dialog), None

    if name == "get_branch_info":
        return dict(dialog), None

    if name == "get_currency_info":
        return dict(dialog), None

    if name == "show_credit_menu":
        return dict(dialog), list(_CREDIT_MENU_BUTTONS)

    if name == "get_products":
        category = args.get("category", "")
        products = await _get_products_by_category(category)
        new_dialog = {
            **_default_dialog(),
            "flow": "show_products",
            "category": category,
            "products": products,
        }
        return new_dialog, [p["name"] for p in products] if products else None

    if name == "select_product":
        product_name = args.get("product_name", "")
        products = list(dialog.get("products") or [])
        category = dialog.get("category", "")
        matched = _find_product_by_name(product_name, products)
        if not matched and products:
            matched = products[0]
        new_dialog = {**dialog, "flow": "product_detail", "selected_product": matched}
        if category in ("debit_card", "fx_card"):
            return new_dialog, ["📋 Подать заявку", "◀ Все продукты"]
        return new_dialog, ["✅ Рассчитать платёж", "◀ Все продукты"]

    if name == "compare_products":
        products = list(dialog.get("products") or [])
        return dict(dialog), [p["name"] for p in products] if products else None

    if name == "back_to_product_list":
        products = list(dialog.get("products") or [])
        new_dialog = {**dialog, "flow": "show_products", "selected_product": None}
        return new_dialog, [p["name"] for p in products] if products else None

    if name == "start_calculator":
        category = dialog.get("category", "")
        calc_qs = CALC_QUESTIONS.get(category, [])
        if not calc_qs:
            return _default_dialog(), None
        first_step, _ = calc_qs[0]
        new_dialog = {**dialog, "flow": "calc_flow", "calc_step": first_step, "calc_slots": {}}
        return new_dialog, None

    if name == "faq_lookup":
        return _reattach_keyboard(dialog)

    if name == "request_operator":
        return {**dialog, "operator_requested": True}, None

    return _reattach_keyboard(dialog)


# ---------------------------------------------------------------------------
# NODE: faq — LLM with tools + heuristic fallback
# ---------------------------------------------------------------------------

async def node_faq(state: BotState) -> dict:
    """
    Main FAQ node. The LLM decides which tool to call based on user intent.
    Tools: greeting, thanks, branch_info, currency_info, credit_menu,
    get_products, select_product, compare_products, back_to_list,
    start_calculator, faq_lookup.
    """
    user_text = (state.get("last_user_text") or "").strip()
    dialog = dict(state.get("dialog") or _default_dialog())
    lang = _REQUEST_LANGUAGE.get()

    dialog_token = _CURRENT_DIALOG.set(dialog)
    try:
        llm = _get_chat_openai()

        # Build message list for LLM
        existing_msgs = list(state.get("messages") or [SystemMessage(content=SYSTEM_POLICY)])
        lang_instruction = _LANG_INSTRUCTION.get(lang, "")
        system_content = SYSTEM_POLICY + lang_instruction

        # Add current state context for better tool selection
        context_parts: list[str] = []
        flow = dialog.get("flow")
        products = list(dialog.get("products") or [])
        if flow:
            context_parts.append(f"Current flow: {flow}")
        if products:
            names = ", ".join(p["name"] for p in products[:10])
            context_parts.append(f"Products displayed: {names}")
        if dialog.get("selected_product"):
            context_parts.append(f"Selected: {dialog['selected_product'].get('name')}")
        if dialog.get("category"):
            context_parts.append(f"Category: {dialog['category']}")
        if context_parts:
            system_content += "\n\nCurrent state:\n" + "\n".join(context_parts)

        if existing_msgs and isinstance(existing_msgs[0], SystemMessage):
            chat_msgs = [SystemMessage(content=system_content)] + existing_msgs[1:]
        else:
            chat_msgs = [SystemMessage(content=system_content)] + existing_msgs
        chat_msgs.append(HumanMessage(content=user_text))

        _max = int(os.getenv("MAX_DIALOG_MESSAGES", "50"))
        if len(chat_msgs) > _max + 1:
            chat_msgs = [chat_msgs[0]] + chat_msgs[-_max:]

        answer = FAQ_FALLBACK_REPLY
        tool_calls_made: list[dict] = []

        llm_with_tools = llm.bind_tools(_FAQ_TOOLS)
        try:
            loop_msgs = list(chat_msgs)
            for _ in range(3):  # max 3 tool call rounds
                ai_msg = await llm_with_tools.ainvoke(loop_msgs)
                loop_msgs.append(ai_msg)

                tool_calls = getattr(ai_msg, "tool_calls", None) or []
                if not tool_calls:
                    # No more tool calls → final answer
                    content = str(getattr(ai_msg, "content", "") or "").strip()
                    if content:
                        answer = content
                    break

                tool_calls_made.extend(tool_calls)
                tool_node = ToolNode(_FAQ_TOOLS)
                tool_results = await tool_node.ainvoke({"messages": loop_msgs})
                loop_msgs.extend(tool_results.get("messages", []))
        except Exception as exc:
            _agent_logger.warning("node_faq LLM failed: %s", exc)
            # Fall through to FAQ lookup fallback
            faq_ans = await _faq_lookup(user_text, lang)
            if faq_ans:
                answer = faq_ans

        new_dialog, keyboard = await _update_dialog_from_tools(
            dialog, tool_calls_made, user_text,
        )
        return _finalize_turn(state, answer, new_dialog, keyboard)
    finally:
        _CURRENT_DIALOG.reset(dialog_token)


# ---------------------------------------------------------------------------
# Lead persistence
# ---------------------------------------------------------------------------

async def _save_lead_async(data: dict) -> None:
    from app.db.session import get_session
    from app.db.models import Lead
    async with get_session() as session:
        lead = Lead(
            session_id=data.get("session_id"),
            telegram_user_id=data.get("user_id"),
            product_category=data.get("category"),
            product_name=data.get("product_name"),
            amount=data.get("amount"),
            term_months=data.get("term_months"),
            rate_pct=data.get("rate_pct") or None,
            contact_name=data.get("name") or None,
            contact_phone=data.get("phone") or None,
        )
        session.add(lead)
        await session.commit()


# ---------------------------------------------------------------------------
# NODE: calc_flow — combined calculator + lead capture
# ---------------------------------------------------------------------------

async def node_calc_flow(state: BotState) -> dict:
    """Handles both calc_step (collecting calculator inputs) and lead_step (name/phone capture)."""
    user_text = (state.get("last_user_text") or "").strip()
    dialog = dict(state.get("dialog") or _default_dialog())

    if dialog.get("lead_step"):
        return await _handle_lead_step(state, user_text, dialog)
    return await _handle_calc_step(state, user_text, dialog)


async def _handle_lead_step(state: BotState, user_text: str, dialog: dict) -> dict:
    """Lead capture mini-flow: offer → name → phone → save."""
    lead_step = dialog.get("lead_step")
    category = dialog.get("category") or ""
    calc_slots = dict(dialog.get("calc_slots") or {})
    selected_product = dialog.get("selected_product") or {}

    if lead_step == "offer":
        if _is_yes(user_text):
            new_dialog = {**dialog, "lead_step": "name"}
            return _finalize_turn(state, "Как вас зовут?", new_dialog)
        return _finalize_turn(state, "Хорошо! Если понадобится помощь — пишите.", _default_dialog())

    if lead_step == "name":
        lead_slots = dict(dialog.get("lead_slots") or {})
        lead_slots["name"] = user_text
        new_dialog = {**dialog, "lead_step": "phone", "lead_slots": lead_slots}
        return _finalize_turn(state, "Укажите ваш номер телефона:", new_dialog)

    if lead_step == "phone":
        lead_slots = dict(dialog.get("lead_slots") or {})
        lead_slots["phone"] = user_text
        try:
            await _save_lead_async({
                "session_id": state.get("session_id"),
                "user_id": state.get("user_id"),
                "category": category,
                "product_name": selected_product.get("name"),
                "amount": calc_slots.get("amount"),
                "term_months": calc_slots.get("term_months"),
                "rate_pct": float(
                    selected_product.get("rate_min_pct")
                    or selected_product.get("rate_pct")
                    or 0
                ) or None,
                "name": lead_slots.get("name", ""),
                "phone": lead_slots.get("phone", user_text),
            })
        except Exception as exc:
            _agent_logger.warning("lead save failed: %s", exc)
        return _finalize_turn(
            state,
            "✅ Отлично! Менеджер свяжется с вами в ближайшее время. Спасибо за обращение!",
            _default_dialog(),
        )

    # Unexpected lead_step value — reset
    return _finalize_turn(state, "Если нужна помощь — напишите.", _default_dialog())


async def _handle_calc_step(state: BotState, user_text: str, dialog: dict) -> dict:
    """Calculator step: collect amount/term/downpayment, then generate result."""
    category = dialog.get("category") or ""
    calc_step = dialog.get("calc_step")
    calc_slots = dict(dialog.get("calc_slots") or {})
    selected_product = dialog.get("selected_product") or {}
    lang = _REQUEST_LANGUAGE.get()

    # Parse answer for current step
    parsed_value = False
    if calc_step == "amount":
        val = _parse_amount(user_text)
        if val is not None:
            calc_slots["amount"] = val
            parsed_value = True
    elif calc_step == "term":
        val = _parse_term_months(user_text)
        if val is not None:
            calc_slots["term_months"] = val
            parsed_value = True
    elif calc_step == "downpayment":
        val = _parse_downpayment(user_text)
        if val is not None:
            calc_slots["downpayment"] = val
            parsed_value = True

    # Off-topic or unrecognised input during calc
    if calc_step and not parsed_value:
        if _looks_like_question(user_text):
            # Answer the side question, then re-ask current step
            faq_ans = await _faq_lookup(user_text, lang) or ""
            if not faq_ans:
                llm = _get_chat_openai()
                if llm:
                    try:
                        ai_msg = await llm.ainvoke([
                            SystemMessage(content="Ты консультант банка. Отвечай кратко."),
                            HumanMessage(content=user_text),
                        ])
                        faq_ans = str(ai_msg.content or "").strip()
                    except Exception:
                        pass
            current_q = next((q for k, q in CALC_QUESTIONS.get(category, []) if k == calc_step), "")
            prefix = f"{faq_ans}\n\n↩️ " if faq_ans else "↩️ "
            return _finalize_turn(state, prefix + current_q, {**dialog, "calc_slots": calc_slots})
        else:
            _hints = {
                "amount": "Не понял сумму. Введите цифрами, например: <b>500 млн</b>",
                "term": "Не понял срок. Например: <b>10 лет</b> или <b>120 мес</b>",
                "downpayment": "Не понял взнос. Введите процент цифрами, например: <b>20</b>",
            }
            return _finalize_turn(
                state,
                _hints.get(calc_step, "Введите число."),
                {**dialog, "calc_slots": calc_slots},
            )

    # Find next unanswered question
    for step_key, step_q in CALC_QUESTIONS.get(category, []):
        slot_key = "term_months" if step_key == "term" else step_key
        if slot_key not in calc_slots:
            new_dialog = {**dialog, "calc_step": step_key, "calc_slots": calc_slots}
            return _finalize_turn(state, step_q, new_dialog)

    # All slots collected → generate result
    product_name = selected_product.get("name") or "Продукт"
    amount = int(calc_slots.get("amount") or 10_000_000)
    term_months = int(calc_slots.get("term_months") or 12)
    amount_fmt = f"{amount:,}".replace(",", " ")

    if category == "deposit":
        rate_pct = float(selected_product.get("rate_pct") or 15.0)
        total_interest = amount * rate_pct / 100 * term_months / 12
        interest_fmt = f"{total_interest:,.0f}".replace(",", " ")
        total_fmt = f"{(amount + total_interest):,.0f}".replace(",", " ")
        answer = (
            f"<b>Расчёт по вкладу «{_html.escape(product_name)}»</b>\n\n"
            f"💰 Сумма: {amount_fmt} сум\n"
            f"📅 Срок: {term_months} мес.\n"
            f"📊 Ставка: {rate_pct:.1f}%\n"
            f"💵 Доход за период: {interest_fmt} сум\n"
            f"🏦 Итого к получению: {total_fmt} сум\n\n"
            "Хотите, чтобы наш менеджер связался с вами для оформления?"
        )
        lead_dialog = {
            **_default_dialog(),
            "flow": "calc_flow",
            "category": category,
            "selected_product": selected_product,
            "calc_slots": calc_slots,
            "lead_step": "offer",
        }
        return _finalize_turn(state, answer, lead_dialog, ["✅ Да, позвоните мне", "❌ Нет, спасибо"])

    # Credit → PDF amortization schedule
    rate_pct = float(
        selected_product.get("rate_min_pct")
        or selected_product.get("rate_pct")
        or 20.0
    )
    try:
        pdf_path = await asyncio.to_thread(
            generate_amortization_pdf,
            product_name=product_name,
            principal=amount,
            annual_rate_pct=rate_pct,
            term_months=term_months,
            output_dir="/tmp",
        )
        answer = (
            f"<b>График платежей готов!</b>\n\n"
            f"📋 Продукт: {_html.escape(product_name)}\n"
            f"💰 Сумма: {amount_fmt} сум\n"
            f"📊 Ставка: {rate_pct:.1f}%\n"
            f"📅 Срок: {term_months} мес.\n\n"
            f"[[PDF:{pdf_path}]]\n"
            "Хотите, чтобы менеджер связался с вами для оформления?"
        )
    except Exception:
        answer = (
            f"По продукту «{_html.escape(product_name)}»:\n"
            f"Сумма: {amount_fmt} сум, ставка: {rate_pct:.1f}%, срок: {term_months} мес.\n\n"
            "Хотите, чтобы менеджер связался с вами для оформления?"
        )

    lead_dialog = {
        **_default_dialog(),
        "flow": "calc_flow",
        "category": category,
        "selected_product": selected_product,
        "calc_slots": calc_slots,
        "lead_step": "offer",
    }
    return _finalize_turn(state, answer, lead_dialog, ["✅ Да, позвоните мне", "❌ Нет, спасибо"])


# ---------------------------------------------------------------------------
# NODE: human_mode
# ---------------------------------------------------------------------------

async def node_human_mode_turn(state: BotState) -> dict:
    """Pause graph and wait for operator reply via interrupt()."""
    from langgraph.types import interrupt as langgraph_interrupt
    user_text = (state.get("last_user_text") or "").strip()
    operator_reply = langgraph_interrupt({"user_message": user_text, "reason": "human_mode_active"})
    answer = str(operator_reply) if operator_reply else ""
    return _finalize_turn(state, answer, dict(state.get("dialog") or _default_dialog()))


# ---------------------------------------------------------------------------
# Graph builder — 3 destination nodes + 1 router
# ---------------------------------------------------------------------------

def build_graph(checkpointer=None, store=None):
    graph = StateGraph(BotState)

    graph.add_node("router", node_router)
    graph.add_node("faq", node_faq)
    graph.add_node("calc_flow", node_calc_flow)
    graph.add_node("human_mode", node_human_mode_turn)

    graph.set_entry_point("router")

    # router uses Command(goto=...) — no explicit conditional edges needed
    for name in ("faq", "calc_flow", "human_mode"):
        graph.add_edge(name, END)

    return graph.compile(checkpointer=checkpointer or MemorySaver(), store=store)


# ---------------------------------------------------------------------------
# Checkpointer factory
# ---------------------------------------------------------------------------

def _derive_postgres_url(database_url: str) -> str | None:
    """Convert SQLAlchemy DATABASE_URL to a plain psycopg-compatible URL."""
    if not database_url:
        return None
    # postgresql+asyncpg://... → postgresql://...
    for prefix in ("postgresql+asyncpg://", "postgres+asyncpg://"):
        if database_url.startswith(prefix):
            return "postgresql://" + database_url[len(prefix):]
    if database_url.startswith(("postgresql://", "postgres://")):
        return database_url
    return None


async def _create_async_checkpointer(backend: str, url: Optional[str]) -> tuple[Any, Any]:
    _lg = _logging.getLogger(__name__)

    # For "auto" backend: try postgres first (derive URL from DATABASE_URL if needed)
    pg_url = url
    if backend in ("auto", "postgres", "pg"):
        if not pg_url and backend == "auto":
            from app.config import get_settings as _get_settings
            pg_url = _derive_postgres_url(_get_settings().database_url)
        if pg_url:
            try:
                from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
                cm = AsyncPostgresSaver.from_conn_string(pg_url)
                saver = await cm.__aenter__()
                try:
                    await saver.setup()
                except Exception:
                    pass
                _lg.info("Using Postgres checkpointer")
                return saver, cm
            except Exception as e:
                _lg.warning("Postgres checkpointer failed (%s), falling back to MemorySaver", e)
        else:
            _lg.warning("No postgres URL available for checkpointer, using MemorySaver")

    return MemorySaver(), None


# ---------------------------------------------------------------------------
# Agent class
# ---------------------------------------------------------------------------

class Agent:
    """Banking FAQ + product selection agent with LangGraph state persistence."""

    def __init__(self) -> None:
        from langgraph.store.memory import InMemoryStore
        self._store = InMemoryStore()
        self._graph = build_graph()
        self._checkpointer: Any = None
        self._checkpointer_cm: Any = None

    async def setup(self, backend: str = "auto", url: Optional[str] = None) -> None:
        """Initialize async checkpointer. Call once at startup."""
        checkpointer, cm = await _create_async_checkpointer(backend, url)
        self._checkpointer = checkpointer
        self._checkpointer_cm = cm
        self._graph = build_graph(checkpointer=checkpointer, store=self._store)

    def _build_config(self, session_id: str) -> Dict[str, Any]:
        return {"configurable": {"thread_id": session_id}}

    async def _aload_existing_state(self, config: Dict[str, Any]) -> dict:
        try:
            snapshot = await self._graph.aget_state(config)
            return dict(snapshot.values or {})
        except Exception:
            return {}

    def _save_user_preference(self, user_id: int, key: str, value: Any) -> None:
        try:
            self._store.put((str(user_id), "preferences"), key, {"value": value})
        except Exception:
            pass

    def _get_user_preference(self, user_id: int, key: str) -> Any:
        try:
            items = self._store.search((str(user_id), "preferences"), query=key, limit=1)
            if items:
                return items[0].value.get("value")
        except Exception:
            pass
        return None

    async def _ainvoke(
        self,
        session_id: str,
        user_text: str,
        language: Optional[str] = None,
        human_mode: bool = False,
        user_id: Optional[int] = None,
    ) -> AgentTurnResult:
        if user_id and language is None:
            language = self._get_user_preference(user_id, "language")
        lang_token = _REQUEST_LANGUAGE.set(_normalize_language_code(language))
        config = self._build_config(session_id)
        try:
            existing = await self._aload_existing_state(config)
            state_in: BotState = {
                "last_user_text": user_text,
                "messages": list(existing.get("messages") or [SystemMessage(content=SYSTEM_POLICY)]),
                "dialog": dict(existing.get("dialog") or _default_dialog()),
                "human_mode": human_mode,
                "session_id": session_id,
                "user_id": user_id,
            }
            out = await self._graph.ainvoke(state_in, config=config)
            return AgentTurnResult(
                text=str(out.get("answer") or "Уточните, пожалуйста, ваш вопрос."),
                keyboard_options=out.get("keyboard_options") or None,
                show_operator_button=bool(out.get("show_operator_button")),
            )
        finally:
            _REQUEST_LANGUAGE.reset(lang_token)

    async def send_message(
        self,
        session_id: str,
        user_id: int,
        text: str,
        language: Optional[str] = None,
        human_mode: bool = False,
    ) -> AgentTurnResult:
        if user_id and language:
            self._save_user_preference(user_id, "language", language)
        return await self._ainvoke(session_id, text, language, human_mode=human_mode, user_id=user_id)

    async def resume_human_mode(self, session_id: str, operator_reply: str) -> str:
        """Resume a graph interrupted in human_mode node, injecting operator reply."""
        try:
            from langgraph.types import Command
            config = self._build_config(session_id)
            out = await self._graph.ainvoke(Command(resume=operator_reply), config=config)
            return str(out.get("answer") or operator_reply)
        except Exception as e:
            _agent_logger.warning("resume_human_mode error for %s: %s", session_id, e)
            return operator_reply

    async def ensure_language(self, text: str, language: Optional[str] = None) -> str:
        return text

    async def sync_history(self, session_id: str, events: Sequence[dict[str, str]]) -> None:
        if not events:
            return
        config = self._build_config(session_id)
        existing = await self._aload_existing_state(config)
        msgs = list(existing.get("messages") or [SystemMessage(content=SYSTEM_POLICY)])
        for event in events:
            role = (event.get("role") or "").strip().lower()
            text = (event.get("text") or "").strip()
            if not text:
                continue
            if role in {"user", "human"}:
                msgs.append(HumanMessage(content=text))
            elif role in {"assistant", "agent", "operator", "bot", "ai"}:
                msgs.append(AIMessage(content=text))
        try:
            await self._graph.aupdate_state(config, {"messages": msgs})
        except Exception:
            pass

    def close(self) -> None:
        return None

    async def aclose(self) -> None:
        if self._checkpointer_cm is not None:
            try:
                await self._checkpointer_cm.__aexit__(None, None, None)
            except Exception:
                pass
