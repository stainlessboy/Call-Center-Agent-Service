from __future__ import annotations

import contextvars
import html as _html
import logging as _logging
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Dict, List, Optional, Sequence, TypedDict

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph

from app.tools.data_loaders import (
    _normalize_language_code,
    _load_credit_product_offers_sync,
    _load_deposit_product_offers_sync,
    _load_card_product_offers_sync,
)
from app.tools.faq_tools import FAQ_FALLBACK_REPLY, _faq_lookup
from app.tools.pdf_generator import generate_amortization_pdf

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

def _get_products_by_category(category: str) -> list[dict]:
    """Return deduplicated list of products for given category from DB cache."""
    seen: set[str] = set()
    result: list[dict] = []

    if category in CREDIT_SECTION_MAP:
        section = CREDIT_SECTION_MAP[category]
        for offer in _load_credit_product_offers_sync():
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
        for offer in _load_deposit_product_offers_sync():
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
        for offer in _load_card_product_offers_sync():
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


def _fmt_rate(offer: dict) -> str:
    low = offer.get("rate_min_pct")
    high = offer.get("rate_max_pct")
    if low is not None and high is not None and abs(float(low) - float(high)) > 0.01:
        return f"{float(low):.1f}–{float(high):.1f}%"
    if low is not None:
        return f"{float(low):.1f}%"
    return str(offer.get("rate_text") or "уточняется")


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
# Number parsers (for calc_flow)
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

def _intent_llm_enabled() -> bool:
    flag = str(os.getenv("LOCAL_AGENT_INTENT_LLM_ENABLED", "1")).strip().lower()
    if flag in {"0", "false", "no", "off"}:
        return False
    return bool(os.getenv("OPENAI_API_KEY"))


@lru_cache(maxsize=1)
def _get_openai_client() -> Any:
    if not _intent_llm_enabled():
        return None
    try:
        from openai import OpenAI  # type: ignore
    except Exception:
        return None
    kwargs: dict[str, Any] = {"api_key": os.getenv("OPENAI_API_KEY")}
    base_url = os.getenv("OPENAI_BASE_URL")
    if base_url:
        kwargs["base_url"] = base_url
    try:
        return OpenAI(**kwargs)
    except Exception:
        return None


def _llm_finance_answer(text: str, lang: Optional[str] = None) -> Optional[str]:
    if not _intent_llm_enabled():
        return None
    client = _get_openai_client()
    if client is None:
        return None
    lang_code = (lang or "ru").lower()[:2]
    system = (
        "Ты консультант банка. Отвечай кратко и по делу. Не раскрывай, что ты ИИ. "
        "Не используй таблицы с символом |. Для сравнений используй маркированный список с эмодзи."
        + _LANG_INSTRUCTION.get(lang_code, "")
    )
    model = os.getenv("LOCAL_AGENT_INTENT_LLM_MODEL", "gpt-4o-mini")
    try:
        resp = client.chat.completions.create(
            model=model, temperature=0.3, max_tokens=350,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": text}],
        )
        if getattr(resp, "choices", None):
            content = str(resp.choices[0].message.content or "").strip()
            return _html.escape(content) if content else None
    except Exception:
        pass
    return None


def _llm_contextual_reply(
    user_text: str, prev_user: Optional[str], prev_ai: Optional[str], lang: Optional[str] = None
) -> Optional[str]:
    """Use LLM to answer follow-up questions using last dialogue context."""
    if not prev_ai or not _intent_llm_enabled():
        return None
    client = _get_openai_client()
    if client is None:
        return None
    lang_code = (lang or "ru").lower()[:2]
    system = (
        "Ты консультант банка. Пользователь уточняет или продолжает предыдущий ответ. "
        "Ответь по теме последнего обмена, коротко (1-3 предложения). "
        "Не используй таблицы с символом |."
        + _LANG_INSTRUCTION.get(lang_code, "")
    )
    model = os.getenv("LOCAL_AGENT_INTENT_LLM_MODEL", "gpt-4o-mini")
    msgs: list[dict[str, str]] = [{"role": "system", "content": system}]
    if prev_user:
        msgs.append({"role": "user", "content": prev_user})
    msgs.append({"role": "assistant", "content": prev_ai})
    msgs.append({"role": "user", "content": user_text})
    try:
        resp = client.chat.completions.create(model=model, temperature=0.3, max_tokens=300, messages=msgs)
        if getattr(resp, "choices", None):
            content = str(resp.choices[0].message.content or "").strip()
            return _html.escape(content) if content else None
    except Exception:
        pass
    return None


def _find_last_human_and_ai(messages: Sequence[Any]) -> tuple[Optional[str], Optional[str]]:
    last_human: Optional[str] = None
    last_ai: Optional[str] = None
    for msg in reversed(list(messages or [])):
        content = str(getattr(msg, "content", "") or "").strip()
        if not content:
            continue
        if last_ai is None and isinstance(msg, AIMessage):
            last_ai = content
        elif last_human is None and isinstance(msg, HumanMessage):
            last_human = content
        if last_human and last_ai:
            break
    return last_human, last_ai


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
    }


def _finalize_turn(
    state: BotState,
    answer: str,
    dialog: dict,
    keyboard_options: Optional[List[str]] = None,
) -> BotState:
    user_text = (state.get("last_user_text") or "").strip()
    msgs = list(state.get("messages") or [SystemMessage(content=SYSTEM_POLICY)])
    msgs.append(HumanMessage(content=user_text))
    msgs.append(AIMessage(content=answer))
    _max = int(os.getenv("MAX_DIALOG_MESSAGES", "50"))
    if len(msgs) > _max + 1:
        msgs = [msgs[0]] + msgs[-_max:]
    state["messages"] = msgs
    state["answer"] = answer
    state["dialog"] = dialog
    state["keyboard_options"] = keyboard_options
    return state


# ---------------------------------------------------------------------------
# NODE: classify
# ---------------------------------------------------------------------------

def node_classify_intent(state: BotState) -> BotState:
    """Route to: human / calc_flow / faq."""
    if state.get("human_mode"):
        state["_route"] = "human"
        return state
    dialog = state.get("dialog") or _default_dialog()
    if dialog.get("flow") == "calc_flow":
        state["_route"] = "calc_flow"
        return state
    state["_route"] = "faq"
    return state


def _route_turn(state: BotState) -> str:
    return state.get("_route") or "faq"


# ---------------------------------------------------------------------------
# NODE: faq
# Handles: greetings, FAQ, product listing, product detail, branches, currency
# ---------------------------------------------------------------------------

def node_faq(state: BotState) -> BotState:
    user_text = (state.get("last_user_text") or "").strip()
    dialog = dict(state.get("dialog") or _default_dialog())
    lang = _REQUEST_LANGUAGE.get()
    flow = dialog.get("flow")
    category = dialog.get("category")
    products: list[dict] = list(dialog.get("products") or [])
    prev_messages = list(state.get("messages") or [])

    # 1. Greeting
    if _is_greeting(user_text):
        buttons = ["🏠 Ипотека", "🚗 Автокредит", "💰 Микрозайм", "💳 Вклад", "🃏 Карта", "❓ Вопрос"]
        return _finalize_turn(state, _greeting_with_menu(lang), _default_dialog(), buttons)

    # 2. Thanks
    if _is_thanks(user_text):
        return _finalize_turn(state, "Пожалуйста! Если нужно — пишите.", dialog)

    # 3. Back to product list
    if flow in ("show_products", "product_detail") and _is_back_trigger(user_text) and products:
        new_dialog = {**dialog, "flow": "show_products", "selected_product": None}
        return _finalize_turn(
            state, _format_product_list_text(products, category or ""),
            new_dialog, [p["name"] for p in products],
        )

    # 4. "Рассчитать" / "Подать заявку" button clicked
    if flow == "product_detail" and _is_calc_trigger(user_text):
        calc_qs = CALC_QUESTIONS.get(category or "", [])
        if not calc_qs:
            # Cards: instant submit, no calc needed
            return _finalize_turn(
                state,
                "✅ Ваша заявка принята! Наш специалист свяжется с вами в ближайшее время.",
                _default_dialog(),
            )
        first_step, first_q = calc_qs[0]
        new_dialog = {**dialog, "flow": "calc_flow", "calc_step": first_step, "calc_slots": {}}
        return _finalize_turn(state, first_q, new_dialog)

    # 4.5. Comparison request — must come before product selection to avoid misrouting
    if _is_comparison_request(user_text):
        cmp_products: list[dict] = list(products) if flow == "show_products" else []
        if not cmp_products:
            # Auto-detect category from the comparison text and load products
            detected_cmp = _detect_product_category(user_text)
            if detected_cmp and detected_cmp != "credit_menu":
                cmp_products = _get_products_by_category(detected_cmp)
        cmp_prompt = user_text
        if cmp_products:
            prods_text = "\n".join(
                f"• {p['name']}: ставка {p.get('rate') or '—'}, "
                f"сумма {p.get('amount') or p.get('min_amount') or '—'}, "
                f"срок {p.get('term') or '—'}"
                for p in cmp_products
            )
            cmp_prompt = (
                f"{user_text}\n\n"
                f"Продукты нашего банка:\n{prods_text}\n\n"
                "Сравни только продукты нашего банка из списка выше. Не упоминай другие банки."
            )
        answer = _llm_finance_answer(cmp_prompt, lang)
        if answer:
            keyboard = [p["name"] for p in cmp_products] if cmp_products else None
            return _finalize_turn(state, answer, dialog, keyboard)

    # 5. User selected a product from list
    if flow == "show_products" and products:
        matched = _find_product_by_name(user_text, products)
        if matched:
            card = _format_product_card(matched, category or "")
            new_dialog = {**dialog, "flow": "product_detail", "selected_product": matched}
            if category in ("debit_card", "fx_card"):
                buttons = ["📋 Подать заявку", "◀ Все продукты"]
            else:
                buttons = ["✅ Рассчитать платёж", "◀ Все продукты"]
            return _finalize_turn(state, card, new_dialog, buttons)

    # 6. Product category intent detected → show product list
    detected_category = _detect_product_category(user_text)
    if detected_category == "credit_menu":
        return _finalize_turn(
            state,
            "Выберите вид кредита:",
            _default_dialog(),
            ["🏠 Ипотека", "🚗 Автокредит", "💰 Микрозайм", "📚 Образовательный кредит"],
        )
    if detected_category:
        prods = _get_products_by_category(detected_category)
        new_dialog = {
            **_default_dialog(),
            "flow": "show_products",
            "category": detected_category,
            "products": prods,
        }
        if prods:
            return _finalize_turn(
                state, _format_product_list_text(prods, detected_category),
                new_dialog, [p["name"] for p in prods],
            )
        label = CATEGORY_LABELS.get(detected_category, "этим продуктам")
        return _finalize_turn(
            state,
            f"Информация по {label} уточняется. Обратитесь в ближайшее отделение.",
            _default_dialog(),
        )

    # 7. Branch question
    if _is_branch_question(user_text):
        return _finalize_turn(
            state,
            "В банке есть отделения по всему Узбекистану.\nНапишите ваш город или район — подскажу ближайший адрес.",
            dialog,
        )

    # 8. Currency rates
    if _is_currency_question(user_text):
        return _finalize_turn(
            state,
            "Актуальные курсы валют смотрите на сайте банка или в мобильном приложении AsakaBank.\nТам же можно открыть валютный вклад или заказать карту.",
            dialog,
        )

    # 9. FAQ DB lookup
    answer = _faq_lookup(user_text, lang)

    # 10. LLM contextual reply for follow-up questions
    if not answer:
        prev_user, prev_ai = _find_last_human_and_ai(prev_messages)
        answer = _llm_contextual_reply(user_text, prev_user, prev_ai, lang)

    # 11. LLM finance fallback
    if not answer:
        answer = _llm_finance_answer(user_text, lang)

    # 12. Static fallback
    if not answer:
        answer = FAQ_FALLBACK_REPLY

    # Re-attach product flow keyboard so user can continue browsing
    keyboard: Optional[List[str]] = None
    if flow == "product_detail":
        if category in ("debit_card", "fx_card"):
            keyboard = ["📋 Подать заявку", "◀ Все продукты"]
        else:
            keyboard = ["✅ Рассчитать платёж", "◀ Все продукты"]
    elif flow == "show_products" and products:
        keyboard = [p["name"] for p in products]

    return _finalize_turn(state, answer, dialog, keyboard)


# ---------------------------------------------------------------------------
# Lead persistence
# ---------------------------------------------------------------------------

async def _save_lead_async(data: dict) -> None:
    from app.db.session import AsyncSessionLocal
    from app.db.models import Lead
    async with AsyncSessionLocal() as session:
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


def _save_lead_sync(data: dict) -> None:
    try:
        import asyncio
        asyncio.run(_save_lead_async(data))
    except Exception as e:
        _agent_logger.warning("_save_lead_sync error: %s", e)


# ---------------------------------------------------------------------------
# NODE: calc_flow
# Collects 2-3 answers then generates PDF (credit) or text calc (deposit)
# ---------------------------------------------------------------------------

def node_calc_flow(state: BotState) -> BotState:
    user_text = (state.get("last_user_text") or "").strip()
    dialog = dict(state.get("dialog") or _default_dialog())
    category = dialog.get("category") or ""
    calc_step = dialog.get("calc_step")
    calc_slots = dict(dialog.get("calc_slots") or {})
    selected_product = dialog.get("selected_product") or {}

    # ---- Lead capture mini-flow (runs after calc result is shown) ----------
    lead_step = dialog.get("lead_step")
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
        _save_lead_sync({
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
        return _finalize_turn(
            state,
            "✅ Отлично! Менеджер свяжется с вами в ближайшее время. Спасибо за обращение!",
            _default_dialog(),
        )
    # ------------------------------------------------------------------------

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
            calc_slots["downpayment"] = val  # key matches slot_key in loop below
            parsed_value = True

    # Off-topic or unrecognised input during calc
    if calc_step and not parsed_value:
        lang = _REQUEST_LANGUAGE.get()
        if _looks_like_question(user_text):
            # Answer the side question, then re-ask current step
            faq_ans = _faq_lookup(user_text, lang) or _llm_finance_answer(user_text, lang) or ""
            current_q = next((q for k, q in CALC_QUESTIONS.get(category, []) if k == calc_step), "")
            prefix = f"{faq_ans}\n\n↩️ " if faq_ans else "↩️ "
            return _finalize_turn(state, prefix + current_q, {**dialog, "calc_slots": calc_slots})
        else:
            # Invalid format — give a helpful hint
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
        pdf_path = generate_amortization_pdf(
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

def node_human_mode_turn(state: BotState) -> BotState:
    """Pause graph and wait for operator reply via interrupt()."""
    from langgraph.types import interrupt as langgraph_interrupt
    user_text = (state.get("last_user_text") or "").strip()
    operator_reply = langgraph_interrupt({"user_message": user_text, "reason": "human_mode_active"})
    answer = str(operator_reply) if operator_reply else ""
    return _finalize_turn(state, answer, dict(state.get("dialog") or _default_dialog()))


# ---------------------------------------------------------------------------
# Graph builder
# ---------------------------------------------------------------------------

def build_graph(checkpointer=None, store=None):
    graph = StateGraph(BotState)
    graph.add_node("classify", node_classify_intent)
    graph.add_node("faq", node_faq)
    graph.add_node("calc_flow", node_calc_flow)
    graph.add_node("human_mode", node_human_mode_turn)
    graph.set_entry_point("classify")
    graph.add_conditional_edges(
        "classify", _route_turn,
        {"faq": "faq", "calc_flow": "calc_flow", "human": "human_mode"},
    )
    graph.add_edge("faq", END)
    graph.add_edge("calc_flow", END)
    graph.add_edge("human_mode", END)
    return graph.compile(checkpointer=checkpointer or MemorySaver(), store=store)


# ---------------------------------------------------------------------------
# Checkpointer factory
# ---------------------------------------------------------------------------

_agent_logger = _logging.getLogger(__name__)


async def _create_async_checkpointer(backend: str, url: Optional[str]) -> tuple[Any, Any]:
    _lg = _logging.getLogger(__name__)

    if backend in ("postgres", "pg"):
        if url:
            try:
                from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
                cm = AsyncPostgresSaver.from_conn_string(url)
                saver = await cm.__aenter__()
                try:
                    await saver.setup()
                except Exception:
                    pass
                return saver, cm
            except Exception as e:
                _lg.warning("Postgres checkpointer failed (%s), falling back to SQLite", e)
        else:
            _lg.warning("LANGGRAPH_CHECKPOINT_URL not set for postgres, falling back to SQLite")

    if backend not in ("memory",):
        path = url or ".langgraph_checkpoints.sqlite3"
        try:
            from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
            cm = AsyncSqliteSaver.from_conn_string(path)
            saver = await cm.__aenter__()
            try:
                await saver.setup()
            except Exception:
                pass
            return saver, cm
        except Exception as e:
            _lg.warning("SQLite checkpointer failed (%s), using MemorySaver", e)

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
