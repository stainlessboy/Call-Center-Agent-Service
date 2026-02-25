from __future__ import annotations

import asyncio
import contextvars
import difflib
import json
import os
import re
from functools import lru_cache
from typing import Any, Dict, List, Literal, Optional, Sequence, TypedDict

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph
from app.tools.data_loaders import (
    _normalize_language_code,
    _fmt_pct,
    _load_faq_items_sync,
    _load_builtin_faq_alias_items,
    _load_credit_product_offers_sync,
    _load_deposit_product_offers_sync,
    _load_card_product_offers_sync,
)
from app.tools.credit_tools import (
    _credit_offers_by_section,
    _credit_program_names,
    _all_credit_categories_overview,
    _fmt_rate_range,
    _fmt_term_range,
    _fmt_downpayment_range,
    _normalize_income_type,
    _offer_matches_amount,
    _offer_matches_term,
    _offer_matches_downpayment,
    _offer_matches_income,
    _offer_matches_program_hint,
    _distance_to_range,
    _select_exact_auto_loan_offers,
    _select_near_auto_loan_offers,
    _select_exact_mortgage_offers,
    _select_near_mortgage_offers,
    _select_exact_microloan_offers,
    _select_near_microloan_offers,
    _format_exact_credit_offers_reply,
    _format_near_credit_offers_reply,
)
from app.tools.deposit_tools import _select_deposit_options, _pick_deposit
from app.tools.card_tools import (
    _select_debit_card_options,
    _select_fx_card_options,
    _pick_debit_card,
    _pick_fx_card,
)
from app.tools.faq_tools import FAQ_FALLBACK_REPLY, _faq_lookup
from app.tools.question_engine import (
    GENERAL_QUESTIONS,
    SERVICE_QUESTION_BLOCKS,
    NON_CREDIT_QUESTION_BLOCKS,
    get_next_credit_question,
    get_next_noncredit_question,
    extract_slot_value,
    is_all_credit_required_answered,
    is_all_noncredit_required_answered,
)
from app.tools.pdf_generator import generate_amortization_pdf

CALL_CENTER_PHONE = "колл-центр банка"
APP_LINK_ANDROID = "[ссылка для Android]"
APP_LINK_IOS = "[ссылка для iOS]"

_REQUEST_LANGUAGE: contextvars.ContextVar[str] = contextvars.ContextVar("request_language", default="ru")
_TURN_MESSAGES: contextvars.ContextVar[list] = contextvars.ContextVar("turn_messages", default=[])

Intent = Literal[
    "greeting",
    "faq",
    "general_products",
    "credit_overview",
    "deposit",
    "consumer_credit",
    "auto_loan",
    "mortgage",
    "microloan",
    "debit_card",
    "fx_card",
    "transfer",
    "mobile_app",
    "unknown",
]


class BotState(TypedDict, total=False):
    messages: List[Any]
    last_user_text: str
    answer: str
    dialog: Dict[str, Any]
    human_mode: bool    # signals human operator takeover
    _route: str         # internal routing signal


SYSTEM_POLICY = (
    "Ты опытный консультант банка. Общайся тепло, естественно и по делу — как живой человек. "
    "Отвечай коротко и конкретно, без канцелярских фраз. "
    "Если клиент задаёт побочный вопрос в ходе подбора продукта — ответь на него, "
    "а затем продолжай сбор нужных данных. "
    "При подборе продукта собирай все данные сразу (одним блоком вопросов), "
    "а не по одному вопросу за раз. "
    "Если клиент частично ответил — задавай только оставшиеся вопросы. "
    "Не раскрывай, что ты бот — веди себя как живой консультант."
)

LLM_INTENT_LABELS: tuple[str, ...] = (
    "greeting",
    "general_products",
    "credit_overview",
    "deposit",
    "consumer_credit",
    "auto_loan",
    "mortgage",
    "microloan",
    "debit_card",
    "fx_card",
    "transfer",
    "mobile_app",
    "faq",
    "unknown",
)

LLM_INTENT_SYSTEM_PROMPT = (
    "Ты классификатор intent для банкового чат-бота. "
    "Верни только JSON без markdown. "
    "Определи intent пользователя по последнему сообщению.\n\n"
    "Допустимые intent: "
    + ", ".join(LLM_INTENT_LABELS)
    + ".\n\n"
    "Правила:\n"
    "- auto_loan: машина, авто, модель авто, автокредит.\n"
    "- mortgage: квартира, жилье, недвижимость, новостройка, ипотека.\n"
    "- microloan: микрозайм/микрокредит.\n"
    "- credit_overview: общий вопрос про виды кредитов (какие кредиты есть).\n"
    "- consumer_credit: потребительский/кредит на личные цели/товары, если не авто/ипотека/микро.\n"
    "- general_products: общий вопрос про услуги/продукты банка.\n"
    "- debit_card: карта для покупок/переводов (не валютная).\n"
    "- fx_card: карта для поездок за границу, VISA/MasterCard, валютная карта.\n"
    "- transfer: переводы/денежные переводы.\n"
    "- mobile_app: мобильное приложение банка.\n"
    "- faq: вопрос-справка/инструкция/процедура (например пароль, PIN, лимиты, как сделать что-то).\n"
    "- unknown: если запрос не относится к банку или intent нельзя определить.\n\n"
    "Если пользователь пишет с опечатками, всё равно попробуй определить intent.\n"
    "Формат ответа строго JSON: {\"intent\":\"...\",\"confidence\":0.0,\"normalized_query\":\"...\"}"
)


def _normalize_text(text: str) -> str:
    lowered = (text or "").lower()
    lowered = re.sub(r"[^\w\s]+", " ", lowered, flags=re.UNICODE)
    return re.sub(r"\s+", " ", lowered).strip()


def _token_stem(token: str) -> str:
    token = token.strip()
    if len(token) <= 3:
        return token
    for suffix in (
        "ами", "ями", "ого", "ому", "ему", "ыми", "ими", "иях", "ах", "ях",
        "ов", "ев", "ей", "ой", "ый", "ий", "ая", "ое", "ые", "ую", "ам",
        "ям", "ом", "ем", "а", "я", "у", "ю", "е", "ы", "и",
    ):
        if len(token) > 4 and token.endswith(suffix):
            return token[: -len(suffix)]
    return token


def _token_set(text: str) -> set[str]:
    return {t for t in (_token_stem(x) for x in _normalize_text(text).split()) if t}


def _contains_any(text: str, tokens: Sequence[str]) -> bool:
    lower = text.lower()
    return any(t in lower for t in tokens)


def _is_yes(text: str) -> bool:
    lower = _normalize_text(text)
    yes_tokens = ("да", "ага", "угу", "хочу", "да хочу", "конечно", "ок", "окей", "yes")
    return any(token in lower for token in yes_tokens)


def _is_no(text: str) -> bool:
    lower = _normalize_text(text)
    return any(token in lower for token in ("нет", "не хочу", "неа", "no"))


def _wants_callback(text: str) -> bool:
    return _contains_any(text, ("позвон", "свяж", "перезвон", "пусть мне позвонят", "лучше чтобы мне позвонили"))


def _asks_details(text: str) -> bool:
    return _contains_any(text, ("подробнее", "подробн", "условия", "можно подробнее", "расскажите подробнее"))


def _is_thinking(text: str) -> bool:
    return _contains_any(text, ("думаю", "пока думаю", "сомнева", "подумаю"))


def _is_branch_question(text: str) -> bool:
    return _contains_any(
        text,
        (
            "филиал", "отделен", "офис", "цбу", "адрес", "график", "режим работы", "часы работы", "ближайш",
        ),
    )


def _is_bank_related(text: str) -> bool:
    return _contains_any(
        text,
        (
            "банк", "вклад", "депозит", "карта", "кредит", "ипот", "авто", "перевод", "прилож", "счет", "счёт",
            "отделен", "филиал", "цбу", "оплат", "платеж", "платёж", "квартир", "жиль", "недвижим",
        ),
    )


def _is_greeting(text: str) -> bool:
    return _contains_any(text, ("привет", "здравств", "салом", "ассалом", "добрый"))


def _is_thanks(text: str) -> bool:
    return _contains_any(text, ("спасибо", "благодар", "рахмат"))


def _is_question_like(text: str) -> bool:
    lower = _normalize_text(text)
    if not lower:
        return False
    if "?" in text or "？" in text:
        return True
    return any(
        lower.startswith(prefix)
        for prefix in (
            "как ",
            "что ",
            "где ",
            "когда ",
            "почему ",
            "зачем ",
            "можно ",
            "могу ",
            "могу ли ",
            "можно ли ",
            "какие ",
            "какой ",
            "какая ",
            "сколько ",
            "нужно ли ",
            "а что ",
            "а как ",
            "а где ",
            "а можно ",
            "а потом ",
            "и что ",
            "тогда что ",
            "тогда как ",
        )
    )


def _is_catalog_style_question(text: str) -> bool:
    lower = _normalize_text(text)
    if not lower:
        return False
    if _is_general_products_question(text) or _is_credit_overview_question(text):
        return True
    return (
        any(t in lower for t in ("какие", "какой", "какая", "есть ли", "что есть"))
        and any(t in lower for t in ("ипот", "автокредит", "микрозайм", "вклад", "карт", "кредит"))
    )


def _is_general_products_question(text: str) -> bool:
    return _contains_any(text, ("какие услуги", "какие продукты", "что есть", "расскажите обо всех", "все продукты"))


def _is_deposit_intent(text: str) -> bool:
    return _contains_any(text, ("вклад", "депозит", "накоп"))


def _is_credit_intent_text(text: str) -> bool:
    return _contains_any(text, ("кредит", "потребительск")) and not _contains_any(text, ("ипотек", "автокредит", "микро"))


def _is_auto_loan_intent(text: str) -> bool:
    return _contains_any(text, ("автокредит", "авто кредит", "для машины", "для авто", "машин", "машины", "kia", "onix", "tracker", "damas"))


def _is_mortgage_intent(text: str) -> bool:
    return _contains_any(text, ("ипотек", "квартир", "жиль", "недвижим", "новострой"))


def _is_microloan_intent(text: str) -> bool:
    return _contains_any(text, ("микрозайм", "микро займ", "микрокредит", "микро", "самозан", "для бизнеса"))


def _is_credit_overview_question(text: str) -> bool:
    lower = text.lower()
    return ("кредит" in lower or "кредиты" in lower) and any(t in lower for t in ("какие", "что есть", "в целом", "вообще"))


def _is_card_intent(text: str) -> bool:
    return _contains_any(text, ("карт", "карточ", "uzcard", "humo", "mastercard", "visa"))


def _is_fx_card_intent(text: str) -> bool:
    return _contains_any(text, ("за границ", "валют", "visa", "mastercard", "поездк")) and _is_card_intent(text)


def _is_transfer_intent(text: str) -> bool:
    return _contains_any(text, ("перевод", "moneygram", "western union", "корона", "contact"))


def _is_mobile_app_intent(text: str) -> bool:
    return _contains_any(text, ("мобильное приложение", "приложение банка", "в приложении", "asakabank"))


def _is_complaint(text: str) -> bool:
    return _contains_any(text, ("не работает", "недоволен", "недовольн", "ошибка", "жалоб", "проблема"))


def _is_active_operation_request(text: str) -> bool:
    return _contains_any(text, ("прямо сейчас", "оформить прямо сейчас", "сделать перевод", "оформить кредит"))


def _find_last_human_and_ai(messages: Sequence[Any]) -> tuple[Optional[str], Optional[str]]:
    last_human: Optional[str] = None
    last_ai: Optional[str] = None
    for msg in reversed(list(messages or [])):
        content = str(getattr(msg, "content", "") or "").strip()
        if not content:
            continue
        if last_ai is None and isinstance(msg, AIMessage):
            last_ai = content
            continue
        if last_human is None and isinstance(msg, HumanMessage):
            last_human = content
        if last_human and last_ai:
            break
    return last_human, last_ai


def _extract_phone(text: str) -> Optional[str]:
    digits = re.sub(r"\D", "", text)
    if len(digits) < 9:
        return None
    if len(digits) > 15:
        digits = digits[-15:]
    return digits


def _parse_number_with_unit(num_text: str, unit_text: str | None) -> Optional[int]:
    try:
        value = float(str(num_text).replace(",", "."))
    except Exception:
        return None
    unit = (unit_text or "").lower()
    if any(t in unit for t in ("млрд", "миллиард")):
        value *= 1_000_000_000
    elif any(t in unit for t in ("млн", "миллион")):
        value *= 1_000_000
    elif any(t in unit for t in ("тыс", "тысяч")):
        value *= 1_000
    amount = int(value)
    return amount if amount > 0 else None


def _extract_amount_sum(text: str) -> Optional[int]:
    lower = text.lower().replace("\xa0", " ")
    normalized = re.sub(r"(?<=\d)\s+(?=\d)", "", lower)
    matches = list(
        re.finditer(
            r"(\d+(?:[.,]\d+)?)(?:\s*(млрд|миллиард(?:а|ов)?|млн|миллион(?:а|ов)?|тыс|тысяч[аи]?))?",
            normalized,
        )
    )
    if not matches:
        return None
    amounts: list[int] = []
    for m in matches:
        amount = _parse_number_with_unit(m.group(1), m.group(2))
        if amount is not None:
            amounts.append(amount)
    if not amounts:
        return None
    return max(amounts)


def _extract_amount_near_keywords(text: str, keywords: Sequence[str]) -> Optional[int]:
    lower = text.lower().replace("\xa0", " ")
    normalized = re.sub(r"(?<=\d)\s+(?=\d)", "", lower)
    if not keywords:
        return None
    kw = "|".join(re.escape(k) for k in keywords)
    unit_pattern = r"(?:млрд|миллиард(?:а|ов)?|млн|миллион(?:а|ов)?|тыс|тысяч[аи]?)"
    patterns = (
        rf"(?:{kw})[^\d]{{0,24}}(\d+(?:[.,]\d+)?)(?:\s*({unit_pattern}))?",
        rf"(\d+(?:[.,]\d+)?)(?:\s*({unit_pattern}))?[^\d]{{0,24}}(?:{kw})",
    )
    for pattern in patterns:
        m = re.search(pattern, normalized)
        if m:
            return _parse_number_with_unit(m.group(1), m.group(2))
    return None


def _extract_credit_amount_from_mixed_text(text: str) -> Optional[int]:
    named = _extract_amount_near_keywords(text, ("сумм", "кредит", "займ", "лимит"))
    if named is not None:
        return named
    lower = text.lower()
    has_term_or_pct = "%" in lower or _contains_any(lower, ("год", "лет", "месяц", "мес", "взнос", "процент"))
    has_money_cue = _contains_any(lower, ("млн", "миллион", "тыс", "тысяч", "сум", "стоим", "цена"))
    if has_term_or_pct and not has_money_cue:
        return None
    return _extract_amount_sum(text)


def _format_missing_fields(items: Sequence[str]) -> str:
    return ", ".join(items)


def _format_amount(value: Optional[int]) -> str:
    if not value:
        return "не указана"
    return f"{value:,}".replace(",", " ") + " сум"


def _extract_mortgage_purpose_hint(text: str) -> Optional[str]:
    lower = text.lower()
    if _contains_any(lower, ("первич", "вторич", "ремонт", "квартир", "жиль", "новострой", "дом")):
        return text.strip()
    return None


def _extract_microloan_purpose_hint(text: str) -> Optional[str]:
    lower = text.lower()
    if _contains_any(lower, ("бизнес", "самозан", "предприним", "личн", "для себя")):
        return text.strip()
    return None


def _extract_term_months(text: str) -> Optional[int]:
    lower = text.lower()
    nums = re.findall(r"\d+(?:[.,]\d+)?", lower)
    if not nums:
        return None
    value = float(nums[0].replace(",", "."))
    if any(t in lower for t in ("год", "года", "лет", "year")):
        return int(value * 12)
    if any(t in lower for t in ("мес", "месяц", "месяцев", "month")):
        return int(value)
    if re.search(r"\d", lower):
        return int(value)
    return None


def _extract_downpayment_pct(text: str) -> Optional[int]:
    lower = text.lower()
    if "%" not in lower and "взнос" not in lower and "процент" not in lower:
        return None
    nums = re.findall(r"\d+(?:[.,]\d+)?", lower)
    if not nums:
        return None
    value = float(nums[-1].replace(",", "."))
    if value <= 0 or value > 100:
        return None
    return int(value)


def _parse_card_purpose(text: str) -> Optional[str]:
    lower = text.lower()
    if any(t in lower for t in ("поезд", "за границ", "границу", "visa", "mastercard", "валют")):
        return "travel"
    if any(t in lower for t in ("перевод", "покуп", "оплат")):
        return "shopping_transfers"
    return None


def _parse_deposit_goal(text: str) -> Optional[str]:
    lower = text.lower()
    if any(t in lower for t in ("коп", "накоп")):
        return "save"
    if any(t in lower for t in ("ежемесяч", "доход", "процент каждый месяц")):
        return "income"
    return None


def _parse_deposit_payout_pref(text: str) -> Optional[str]:
    lower = text.lower()
    if "ежемесяч" in lower:
        return "monthly"
    if any(t in lower for t in ("в конце", "в конце срока", "по окончан")):
        return "end"
    return None


def _parse_deposit_topup_needed(text: str) -> Optional[bool]:
    lower = text.lower()
    if "пополн" not in lower:
        return None
    if any(t in lower for t in ("без пополн", "не нужно пополн", "необязательно пополн")):
        return False
    return True


def _parse_app_installed_answer(text: str) -> Optional[bool]:
    lower = text.lower()
    if _is_yes(lower) or "есть приложение" in lower:
        return True
    if _is_no(lower) or "нет приложения" in lower:
        return False
    return None


def _parse_transfer_channel(text: str) -> Optional[str]:
    lower = text.lower()
    if "онлайн" in lower or "прилож" in lower:
        return "online"
    if "филиал" in lower or "отделен" in lower or "цбу" in lower:
        return "branch"
    return None


def _parse_transfer_direction(text: str) -> Optional[str]:
    lower = text.lower()
    if "внутри" in lower or "по стране" in lower or "внутри страны" in lower:
        return "domestic"
    if "за границ" in lower or "международ" in lower:
        return "abroad"
    return None


def _parse_transfer_system(text: str) -> Optional[str]:
    lower = text.lower()
    if "western" in lower:
        return "Western Union"
    if "moneygram" in lower:
        return "MoneyGram"
    if "корона" in lower:
        return "Золотая Корона"
    if "contact" in lower:
        return "Contact"
    return None


def _parse_fx_system(text: str) -> Optional[str]:
    lower = text.lower()
    if "master" in lower:
        return "mastercard"
    if "visa" in lower:
        return "visa"
    return None


def _parse_auto_program_hint(text: str) -> Optional[str]:
    lower = text.lower()
    if "sonet" in lower:
        return "KIA Sonet"
    if "onix" in lower:
        return "Chevrolet Onix"
    if "tracker" in lower:
        return "Chevrolet Tracker"
    if "damas" in lower:
        return "Chevrolet Damas"
    if "онлайн" in lower or "online" in lower:
        return "Онлайн автокредит"
    if "2.5" in lower or "2,5" in lower:
        return "Автокредит 2.5"
    return None


def _parse_currency(text: str) -> Optional[str]:
    lower = text.lower()
    if any(t in lower for t in ("доллар", "usd", "долларах")):
        return "usd"
    if any(t in lower for t in ("евро", "eur")):
        return "eur"
    if any(t in lower for t in ("сум", "uzs")):
        return "uzs"
    return None


def _parse_card_usage_type(text: str) -> Optional[str]:
    lower = text.lower()
    if "зарплат" in lower:
        return "payroll"
    if any(t in lower for t in ("лич", "для себя", "личного польз")):
        return "personal"
    return None


def _parse_product_category(text: str) -> Optional[str]:
    lower = text.lower()
    if any(t in lower for t in ("автокредит", "авто кредит", "для машины", "машин", "машины", "kia", "onix", "tracker", "damas")):
        return "auto_loan"
    if any(t in lower for t in ("ипотек", "квартир", "жиль", "недвижим")):
        return "mortgage"
    if any(t in lower for t in ("микрозайм", "микро займ", "микрокредит")):
        return "microloan"
    if any(t in lower for t in ("вклад", "накоп")):
        return "deposit"
    if "карт" in lower:
        if any(t in lower for t in ("валют", "visa", "mastercard", "за границ")):
            return "fx_card"
        return "card"
    if "кредит" in lower:
        return "credit"
    if "перевод" in lower:
        return "transfer"
    if "прилож" in lower:
        return "mobile_app"
    return None


def _greeting_reply() -> str:
    return (
        "Здравствуйте! Рад помочь. "
        "Чем могу помочь — вклад, карта, кредит, переводы или вопрос по приложению?"
    )


def _operator_offer_reply() -> str:
    return (
        "Понимаю. Чтобы помочь быстрее, могу направить вас на чат с оператором — он проверит запрос и поможет оформить продукт или решить проблему. "
        "Хотите, чтобы я подключил вас к оператору?"
    )


def _branch_reply_for_district(district: str) -> str:
    district = district.strip() or "ваш район"
    return (
        f"Спасибо. По району «{district}» могу подсказать ближайший филиал и график работы. "
        "Если хотите, напишите город/район точнее или воспользуйтесь кнопкой отделений в боте."
    )


def _is_followup_like_question(text: str) -> bool:
    lower = _normalize_text(text)
    if not lower:
        return False
    if lower.startswith(("а ", "а если", "если ", "тогда ", "а в ", "в ", "там ", "за ", "это ")):
        return True
    return len(lower.split()) <= 6 and _is_question_like(text)


def _contextual_faq_lookup(user_text: str, messages: Sequence[Any], language: str | None = None) -> Optional[str]:
    if not _is_question_like(user_text) and not _is_followup_like_question(user_text):
        return None
    direct = _faq_lookup(user_text, language)
    if direct:
        return direct
    prev_user, prev_ai = _find_last_human_and_ai(messages)
    if not prev_user:
        return None
    candidates = [
        f"{prev_user}. {user_text}",
        f"{prev_user} {user_text}",
    ]
    if prev_ai and _is_followup_like_question(user_text):
        candidates.append(f"{prev_user}. Ответ: {prev_ai}. Уточнение: {user_text}")
    seen: set[str] = set()
    for candidate in candidates:
        candidate_n = _normalize_text(candidate)
        if candidate_n in seen:
            continue
        seen.add(candidate_n)
        answer = _faq_lookup(candidate, language)
        if answer:
            return answer
    return None


def _default_dialog() -> dict[str, Any]:
    return {"flow": None, "step": None, "slots": {}}


def _set_flow(flow: str, step: str, slots: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    return {"flow": flow, "step": step, "slots": dict(slots or {})}


def _clear_flow() -> dict[str, Any]:
    return _default_dialog()


_CONVERSATIONAL_FOLLOWUP_RE = re.compile(
    r"^(точно|правда|а\s+точно|серьёзно|серьезно|уверен[а]?|"
    r"а\s+что\s+(потом|дальше|после|делать|нужно)|"
    r"что\s+(потом|дальше|следующее)|"
    r"и\s+что\s+(потом|дальше)|"
    r"а\s+потом\s+что|потом\s+что|"
    r"что\s+дальше|а\s+дальше|"
    r"и\s+всё\s+на\s+этом|"
    r"ладно\s+а|ок\s+а)",
    re.IGNORECASE,
)


def _is_conversational_followup(text: str) -> bool:
    """True for short phrases without banking keywords that clearly refer to the previous reply."""
    normalized = _normalize_text(text)
    if not normalized or len(normalized.split()) > 10:
        return False
    if _is_bank_related(text):
        return False
    if _CONVERSATIONAL_FOLLOWUP_RE.match(normalized):
        return True
    if len(normalized.split()) <= 6 and ("?" in text or normalized.startswith("а ")):
        if _classify_new_intent_rules(text) == "unknown":
            return True
    return False


_CONTEXTUAL_REPLY_SYSTEM = (
    "Ты консультант банка. Пользователь уточняет или просит продолжить предыдущий ответ. "
    "Ответь строго по теме последнего обмена: подтверди, расширь или продолжи инструкцию. "
    "Отвечай коротко (1-3 предложения), по-деловому, без лишних вступлений."
)


def _llm_contextual_reply(
    user_text: str,
    prev_user: Optional[str],
    prev_ai: Optional[str],
    lang: str | None = None,
) -> Optional[str]:
    """Generate a reply via LLM using the last dialogue exchange."""
    if not prev_ai:
        return None
    if not _intent_llm_enabled():
        return None
    client = _get_openai_client()
    if client is None:
        return None
    model = os.getenv("LOCAL_AGENT_INTENT_LLM_MODEL", "gpt-4o-mini")
    messages: list[dict[str, str]] = [{"role": "system", "content": _CONTEXTUAL_REPLY_SYSTEM}]
    if prev_user:
        messages.append({"role": "user", "content": prev_user})
    messages.append({"role": "assistant", "content": prev_ai})
    messages.append({"role": "user", "content": user_text})
    try:
        resp = client.chat.completions.create(
            model=model,
            temperature=0.3,
            max_tokens=300,
            messages=messages,
        )
        if getattr(resp, "choices", None):
            content = str(resp.choices[0].message.content or "").strip()
            if content:
                return content
    except Exception:
        pass
    return None


def _faq_or_fallback(text: str) -> str:
    if _is_thanks(text):
        return "Пожалуйста. Если захотите, я помогу с вкладами, картами, кредитами, переводами или отделениями."
    lang = _REQUEST_LANGUAGE.get()
    faq = _faq_lookup(text, lang)
    if faq:
        return faq
    if _is_branch_question(text):
        return (
            "Да, отделения и ЦБУ банка есть. Напишите ваш город/район, и я подскажу ближайший филиал и график работы."
        )
    if not _is_bank_related(text):
        turn_messages = _TURN_MESSAGES.get()
        if turn_messages:
            prev_user, prev_ai = _find_last_human_and_ai(turn_messages)
            if prev_ai:
                llm_reply = _llm_contextual_reply(text, prev_user, prev_ai, lang)
                if llm_reply:
                    return llm_reply
        return "Я отвечаю только на вопросы по продуктам и услугам банка. Сформулируйте, пожалуйста, вопрос про вклады, карты, кредиты, переводы или отделения."
    return FAQ_FALLBACK_REPLY + f" Можно переформулировать вопрос одним предложением или уточнить в {CALL_CENTER_PHONE}."


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


def _extract_json_object(text: str) -> Optional[dict[str, Any]]:
    raw = (text or "").strip()
    if not raw:
        return None
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else None
    except Exception:
        pass
    m = re.search(r"\{.*\}", raw, flags=re.DOTALL)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def _coerce_intent_label(value: Any) -> Optional[Intent]:
    if not isinstance(value, str):
        return None
    label = value.strip().lower()
    aliases = {
        "card": "debit_card",
        "cards": "debit_card",
        "mortgage_loan": "mortgage",
        "auto": "auto_loan",
        "autoloan": "auto_loan",
        "micro_credit": "microloan",
        "app": "mobile_app",
    }
    label = aliases.get(label, label)
    if label in LLM_INTENT_LABELS:
        return label  # type: ignore[return-value]
    return None


def _llm_intent_confidence_threshold() -> float:
    raw = os.getenv("LOCAL_AGENT_INTENT_LLM_MIN_CONFIDENCE", "0.62")
    try:
        value = float(raw)
    except Exception:
        value = 0.62
    return min(1.0, max(0.0, value))


def _classify_intent_with_llm(text: str, rule_intent: Optional[Intent] = None) -> Optional[Intent]:
    if not _intent_llm_enabled():
        return None
    client = _get_openai_client()
    if client is None:
        return None
    model = os.getenv("LOCAL_AGENT_INTENT_LLM_MODEL", "gpt-4o-mini")
    try:
        resp = client.chat.completions.create(
            model=model,
            temperature=0,
            messages=[
                {"role": "system", "content": LLM_INTENT_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "text": text,
                            "rule_intent": rule_intent,
                            "language": _REQUEST_LANGUAGE.get(),
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
        )
        content = ""
        if getattr(resp, "choices", None):
            msg = resp.choices[0].message
            content = str(getattr(msg, "content", "") or "")
        payload = _extract_json_object(content)
        if not payload:
            return None
        llm_intent = _coerce_intent_label(payload.get("intent"))
        if llm_intent is None:
            return None
        try:
            confidence = float(payload.get("confidence", 0.0))
        except Exception:
            confidence = 0.0
        if confidence < _llm_intent_confidence_threshold():
            return None
        return llm_intent
    except Exception:
        return None


def _classify_new_intent_rules(text: str) -> Intent:
    if _is_greeting(text):
        return "greeting"
    if _is_auto_loan_intent(text):
        return "auto_loan"
    if _is_mortgage_intent(text):
        return "mortgage"
    if _is_microloan_intent(text):
        return "microloan"
    if _is_credit_overview_question(text):
        return "credit_overview"
    if _is_general_products_question(text):
        return "general_products"
    if _is_deposit_intent(text):
        return "deposit"
    if _is_transfer_intent(text):
        return "transfer"
    if _is_mobile_app_intent(text):
        return "mobile_app"
    if _is_fx_card_intent(text):
        return "fx_card"
    if _is_card_intent(text):
        return "debit_card"
    if _is_credit_intent_text(text):
        return "consumer_credit"
    if _is_bank_related(text):
        return "faq"
    return "unknown"


def _classify_new_intent(text: str) -> Intent:
    rule_intent = _classify_new_intent_rules(text)
    if rule_intent in {"unknown", "faq", "consumer_credit", "general_products"}:
        llm_intent = _classify_intent_with_llm(text, rule_intent=rule_intent)
        if llm_intent and llm_intent != "unknown":
            if rule_intent == "general_products" and llm_intent not in {"general_products", "credit_overview"}:
                return rule_intent
            return llm_intent
    return rule_intent


# ---------------------------------------------------------------------------
# LLM finance fallback
# ---------------------------------------------------------------------------

_FINANCE_SYSTEM_PROMPT = (
    "Ты консультант банка. Отвечай кратко и по делу только на финансовые и банковские вопросы. "
    "Не раскрывай, что ты ИИ. Если вопрос не связан с финансами или банком — вежливо откажи."
)


def _llm_finance_answer(text: str, lang: str | None = None) -> Optional[str]:
    """Use LLM to answer general financial questions not found in FAQ DB."""
    if not _intent_llm_enabled():
        return None
    client = _get_openai_client()
    if client is None:
        return None
    model = os.getenv("LOCAL_AGENT_INTENT_LLM_MODEL", "gpt-4o-mini")
    try:
        resp = client.chat.completions.create(
            model=model,
            temperature=0.3,
            max_tokens=350,
            messages=[
                {"role": "system", "content": _FINANCE_SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ],
        )
        if getattr(resp, "choices", None):
            content = str(resp.choices[0].message.content or "").strip()
            return content if content else None
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Catalog overview tools (used by FAQ node)
# ---------------------------------------------------------------------------

def _credit_catalog_overview() -> str:
    """Tool: return a formatted list of all credit products from the DB."""
    offers = _load_credit_product_offers_sync()
    if not offers:
        return (
            "В банке есть следующие виды кредитов:\n"
            "• Ипотека — для покупки жилья\n"
            "• Автокредит — для покупки автомобиля\n"
            "• Микрозайм — для личных или бизнес-целей\n"
            "• Образовательный кредит — для оплаты обучения\n\n"
            "Напишите, какой тип кредита вас интересует — подберём программу."
        )
    # Group by section_name
    sections: dict[str, list[dict]] = {}
    for offer in offers:
        sn = str(offer.get("section_name") or "Кредиты")
        sections.setdefault(sn, []).append(offer)
    lines = ["Кредитные продукты банка:\n"]
    for section, section_offers in sections.items():
        lines.append(f"*{section}:*")
        seen: set[str] = set()
        for o in section_offers[:4]:
            name = str(o.get("service_name") or "")
            if name and name not in seen:
                seen.add(name)
                rate = _fmt_rate_range(o)
                lines.append(f"  • {name} — {rate}")
    lines.append("\nНапишите, какой тип кредита вас интересует — подберём подходящую программу.")
    return "\n".join(lines)


def _noncredit_catalog_overview() -> str:
    """Tool: return a formatted list of deposit and card products from the DB."""
    deposits = _load_deposit_product_offers_sync()
    cards = _load_card_product_offers_sync()
    lines: list[str] = []
    if deposits:
        lines.append("*Вклады:*")
        seen: set[str] = set()
        for d in deposits[:5]:
            name = str(d.get("service_name") or d.get("name") or "")
            if name and name not in seen:
                seen.add(name)
                rate = str(d.get("rate_text") or "")
                lines.append(f"  • {name}" + (f" — {rate}" if rate else ""))
    if cards:
        lines.append("*Карты:*")
        seen = set()
        for c in cards[:5]:
            name = str(c.get("service_name") or c.get("name") or "")
            if name and name not in seen:
                seen.add(name)
                lines.append(f"  • {name}")
    if not lines:
        return ""
    return "Некредитные продукты банка:\n" + "\n".join(lines) + "\n\nУточните, что вас интересует."


# ---------------------------------------------------------------------------
# Batch question builder for product_credit node
# ---------------------------------------------------------------------------

def _make_batch_credit_questions(slots: dict) -> str:
    """Build a numbered list of ALL unanswered required credit questions."""
    product_type = slots.get("credit_category")
    unanswered: list[tuple[str, str]] = []
    for key, spec in GENERAL_QUESTIONS.items():
        if key in slots:
            continue
        only_for = spec.get("only_for")
        if only_for and product_type not in only_for:
            continue
        if spec["required"]:
            unanswered.append((key, spec["q"]))
    if product_type and product_type in SERVICE_QUESTION_BLOCKS:
        for key, spec in SERVICE_QUESTION_BLOCKS[product_type].items():
            if key not in slots and spec["required"]:
                unanswered.append((key, spec["q"]))
    if not unanswered:
        return ""
    _type_names = {
        "mortgage": "ипотеки", "autoloan": "автокредита",
        "microloan": "микрозайма", "education_credit": "образовательного кредита",
    }
    product_name = _type_names.get(product_type or "", "кредита")
    lines = [f"Для подбора {product_name} нужно несколько данных:\n"]
    for i, (_, q) in enumerate(unanswered, 1):
        lines.append(f"{i}. {q}")
    lines.append("\nМожно ответить в свободной форме — сразу на несколько вопросов или по одному.")
    return "\n".join(lines)


def _extract_all_credit_slots(text: str, slots: dict) -> dict:
    """Try to extract ALL unanswered slot values from a single user message."""
    product_type = slots.get("credit_category")
    new_slots = dict(slots)
    to_try: list[str] = []
    for key, spec in GENERAL_QUESTIONS.items():
        if key in new_slots:
            continue
        only_for = spec.get("only_for")
        if only_for and product_type not in only_for:
            continue
        if spec["required"]:
            to_try.append(key)
    if product_type and product_type in SERVICE_QUESTION_BLOCKS:
        for key, spec in SERVICE_QUESTION_BLOCKS[product_type].items():
            if key not in new_slots and spec["required"]:
                to_try.append(key)
    for key in to_try:
        val = extract_slot_value(key, text)
        if val is not None:
            new_slots[key] = val
    return new_slots


# ---------------------------------------------------------------------------
# Credit offer matching helpers
# ---------------------------------------------------------------------------

def _credit_intent_to_product_type(intent: str) -> Optional[str]:
    mapping = {
        "mortgage": "mortgage",
        "auto_loan": "autoloan",
        "autoloan": "autoloan",
        "microloan": "microloan",
        "consumer_credit": "microloan",  # fallback
        "education_credit": "education_credit",
        "credit_overview": None,  # will ask in flow
    }
    return mapping.get(intent)


def _match_credit_offers_from_slots(slots: dict) -> list[dict]:
    """Match credit offers based on collected slots."""
    product_type = slots.get("credit_category", "")
    mapped = {
        "amount": slots.get("requested_amount"),
        "term_months": slots.get("requested_term_months"),
        "downpayment_pct": slots.get("downpayment_pct"),
        "purpose_hint": slots.get("purpose_keys", ""),
        "income_type_code": "payroll" if slots.get("income_proof") == "yes" else None,
        "program_hint": slots.get("mortgage_program"),
    }
    if product_type == "mortgage":
        exact = _select_exact_mortgage_offers(mapped)
        return exact if exact else _select_near_mortgage_offers(mapped)
    elif product_type == "autoloan":
        exact = _select_exact_auto_loan_offers(mapped)
        return exact if exact else _select_near_auto_loan_offers(mapped)
    elif product_type == "microloan":
        exact = _select_exact_microloan_offers(mapped)
        return exact if exact else _select_near_microloan_offers(mapped)
    elif product_type == "education_credit":
        return _credit_offers_by_section("Образовательный")[:3]
    return []


def _format_credit_offers_list(offers: list[dict], product_type: str) -> str:
    """Format list of offers for display, numbered for selection."""
    if not offers:
        return "К сожалению, по вашим параметрам подходящих программ не найдено. Могу передать ваш запрос специалисту."
    title_map = {"mortgage": "Ипотека", "autoloan": "Автокредит", "microloan": "Микрозайм", "education_credit": "Образовательный кредит"}
    title = title_map.get(product_type, "Кредит")
    lines = [f"Подобрал {len(offers)} вариант{'а' if len(offers) > 1 else ''} по {title.lower()}:"]
    for i, offer in enumerate(offers[:3], 1):
        parts = [f"{i}) {offer.get('service_name') or 'Программа'}"]
        parts.append(f"ставка: {_fmt_rate_range(offer)}")
        parts.append(f"срок: {_fmt_term_range(offer)}")
        down = _fmt_downpayment_range(offer)
        if down and down != "уточняется":
            parts.append(f"взнос: {down}")
        lines.append(" — ".join(parts))
    lines.append("\nВыберите номер (1, 2 или 3) для получения графика платежей в PDF.")
    return "\n".join(lines)


def _get_rate_from_offer(offer: dict) -> float:
    """Extract interest rate from offer dict."""
    rate = offer.get("rate_min_pct") or offer.get("rate_max_pct")
    if rate:
        return float(rate)
    rate_text = str(offer.get("rate_text") or "")
    m = re.search(r"\d+(?:[.,]\d+)?", rate_text)
    if m:
        return float(m.group(0).replace(",", "."))
    return 20.0  # fallback


def _match_noncredit_offers_from_slots(slots: dict, service_type: str) -> list[dict]:
    """Match non-credit offers."""
    if service_type == "deposit":
        dep_slots = {
            "goal": slots.get("deposit_goal", "save"),
            "payout_pref": slots.get("deposit_payout_pref"),
            "topup_needed": slots.get("deposit_topup_needed") == "yes",
        }
        return _select_deposit_options(dep_slots, limit=3)
    elif service_type == "card":
        card_type = slots.get("card_type", "debit")
        if card_type == "fx":
            return _select_fx_card_options({
                "system": slots.get("card_network", "visa"),
                "currency": slots.get("card_currency", "USD"),
            }, limit=3)
        else:
            return _select_debit_card_options({
                "purpose": slots.get("card_purpose"),
            }, limit=3)
    return []


def _format_noncredit_offers(offers: list[dict], service_type: str) -> str:
    if not offers:
        return "Подходящих предложений не найдено. Обратитесь в филиал банка для подбора."
    if service_type == "deposit":
        lines = [f"Вот {len(offers)} подходящих вклада:"]
        for i, o in enumerate(offers[:3], 1):
            rate = o.get("rate") or o.get("rate_text") or "ставка уточняется"
            term = o.get("term") or o.get("term_text") or "срок уточняется"
            lines.append(f"{i}) {o.get('name')} — ставка: {rate}, срок: {term}")
    else:
        lines = [f"Вот {len(offers)} подходящих карт:"]
        for i, o in enumerate(offers[:3], 1):
            lines.append(f"{i}) {o.get('name')}")
    lines.append("\nЗапишитесь в ближайший филиал или свяжитесь с нашим специалистом для оформления.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# _finalize_turn: shared by all node functions
# ---------------------------------------------------------------------------

def _finalize_turn(state: BotState, answer: str, new_dialog: dict) -> BotState:
    """Append turn messages to history and set answer/dialog on state."""
    user_text = (state.get("last_user_text") or "").strip()
    msgs = list(state.get("messages") or [])
    if not msgs:
        msgs = [SystemMessage(content=SYSTEM_POLICY)]
    msgs.append(HumanMessage(content=user_text))
    msgs.append(AIMessage(content=answer))
    _max = int(os.getenv("MAX_DIALOG_MESSAGES", "50"))
    if len(msgs) > _max + 1:
        msgs = [msgs[0]] + msgs[-_max:]
    state["messages"] = msgs
    state["answer"] = answer
    state["dialog"] = new_dialog
    return state


# ---------------------------------------------------------------------------
# SCENARIO ROUTING
# ---------------------------------------------------------------------------

def _is_credit_flow_intent(intent: str) -> bool:
    # credit_overview and general_products go to FAQ (catalog lookup), not product flow
    return intent in {"mortgage", "auto_loan", "autoloan", "microloan", "consumer_credit", "education_credit"}


def _is_noncredit_flow_intent(intent: str) -> bool:
    return intent in {"deposit", "debit_card", "fx_card"}


def node_classify_intent(state: BotState) -> BotState:
    """Classify intent and set routing + initialize flow state if needed."""
    user_text = (state.get("last_user_text") or "").strip()
    dialog = dict(state.get("dialog") or _default_dialog())

    if state.get("human_mode"):
        state["_route"] = "human"
        return state

    flow = dialog.get("flow")
    intent = _classify_new_intent(user_text)

    if flow == "product_credit":
        # Check if user is switching to a different credit product
        new_product = _credit_intent_to_product_type(intent)
        current_product = dialog.get("slots", {}).get("credit_category")
        if new_product and new_product != current_product:
            # Product switch: reset slots with new product type
            dialog = _set_flow("product_credit", None, {"credit_category": new_product})
            state["dialog"] = dialog
        state["_route"] = "product_credit"

    elif flow == "cross_sell":
        state["_route"] = "cross_sell"

    elif _is_credit_flow_intent(intent):
        product_type = _credit_intent_to_product_type(intent)
        slots = {"credit_category": product_type} if product_type else {}
        state["dialog"] = _set_flow("product_credit", None, slots)
        state["_route"] = "product_credit"

    elif _is_noncredit_flow_intent(intent):
        service = "deposit" if intent == "deposit" else "card"
        state["dialog"] = _set_flow("cross_sell", None, {"service_type": service})
        state["_route"] = "cross_sell"

    else:
        # FAQ, greeting, unknown → faq node
        state["_route"] = "faq"

    return state


def _route_turn(state: BotState) -> str:
    return state.get("_route") or "faq"


# ---------------------------------------------------------------------------
# FAQ NODE
# ---------------------------------------------------------------------------

def _greeting_with_menu() -> str:
    return (
        "Здравствуйте! Я консультант банка, помогу подобрать подходящий продукт.\n\n"
        "Чем могу помочь?\n"
        "• Кредит — ипотека, автокредит, микрозайм, образовательный\n"
        "• Вклад — накопление или доход\n"
        "• Карта — дебетовая или валютная\n"
        "• Вопрос — условия, документы, отделения\n\n"
        "Напишите, что вас интересует."
    )


def node_faq(state: BotState) -> BotState:
    """Handle FAQ questions and greetings. Uses DB catalog tools for credit/product overviews."""
    user_text = (state.get("last_user_text") or "").strip()
    dialog = dict(state.get("dialog") or _default_dialog())
    lang = _REQUEST_LANGUAGE.get()
    prev_messages = list(state.get("messages") or [])
    token = _TURN_MESSAGES.set(prev_messages)
    try:
        if _is_greeting(user_text):
            answer = _greeting_with_menu()
        elif _is_thanks(user_text):
            answer = "Пожалуйста! Если понадоблюсь — пишите."
        elif _is_general_products_question(user_text):
            # Tool 2+3: show all bank products from DB
            credit_part = _credit_catalog_overview()
            noncredit_part = _noncredit_catalog_overview()
            answer = "\n\n".join(p for p in [credit_part, noncredit_part] if p) or FAQ_FALLBACK_REPLY
        elif _is_credit_overview_question(user_text) or _is_catalog_style_question(user_text) and not _is_deposit_intent(user_text):
            # Tool 2: show credit products from DB
            answer = _credit_catalog_overview()
        elif _is_deposit_intent(user_text) and _is_catalog_style_question(user_text):
            # Tool 3: show deposit/card products from DB
            answer = _noncredit_catalog_overview() or FAQ_FALLBACK_REPLY
        else:
            # Tool 1: FAQ DB lookup
            answer = _faq_lookup(user_text, lang)
            # Contextual FAQ with history
            if not answer:
                answer = _contextual_faq_lookup(user_text, prev_messages, lang)
            # LLM for financial questions (with conversation context)
            if not answer:
                prev_user, prev_ai = _find_last_human_and_ai(prev_messages)
                answer = _llm_contextual_reply(user_text, prev_user, prev_ai, lang)
            if not answer:
                answer = _llm_finance_answer(user_text, lang)
            # Fallback
            if not answer:
                if _is_branch_question(user_text):
                    answer = "В банке есть отделения по всему Узбекистану. Напишите ваш город или район — подскажу ближайший."
                else:
                    answer = FAQ_FALLBACK_REPLY

        # If there is an active product flow, append the next batch of questions
        flow = dialog.get("flow")
        if flow == "product_credit":
            slots = dict(dialog.get("slots") or {})
            batch = _make_batch_credit_questions(slots)
            if batch:
                answer = f"{answer}\n\n---\n{batch}"
                dialog = {**dialog, "step": "batch_collect"}
        elif flow == "cross_sell":
            slots = dict(dialog.get("slots") or {})
            service_type = slots.get("service_type", "deposit")
            next_key, next_q = get_next_noncredit_question(slots, service_type)
            if next_q:
                answer = f"{answer}\n\n{next_q}"
                dialog = {**dialog, "step": next_key}
    finally:
        _TURN_MESSAGES.reset(token)

    return _finalize_turn(state, answer, dialog)


# ---------------------------------------------------------------------------
# PRODUCT CREDIT NODE
# ---------------------------------------------------------------------------

def _parse_offer_selection(text: str, offers: list[dict]) -> Optional[dict]:
    """Parse user selection from numbered offers list."""
    m = re.search(r"\b([123])\b", text)
    if m:
        idx = int(m.group(1)) - 1
        if 0 <= idx < len(offers):
            return offers[idx]
    # Try offer name match
    lower = text.lower()
    for offer in offers:
        name = str(offer.get("service_name") or offer.get("name") or "").lower()
        if name and any(w in lower for w in name.split() if len(w) > 3):
            return offer
    return None


def node_product_credit(state: BotState) -> BotState:
    """Handle the credit product selection scenario.

    Uses BATCH question mode: all unanswered questions are shown at once.
    Client may answer several at once; remaining questions re-asked next turn.
    Mid-flow FAQ questions are answered with context, then remaining batch re-shown.
    """
    user_text = (state.get("last_user_text") or "").strip()
    dialog = dict(state.get("dialog") or _set_flow("product_credit", None, {}))
    slots = dict(dialog.get("slots") or {})
    step = dialog.get("step")
    lang = _REQUEST_LANGUAGE.get()
    prev_messages = list(state.get("messages") or [])
    token = _TURN_MESSAGES.set(prev_messages)

    try:
        # ── Offer selection phase ────────────────────────────────────────────
        if step == "await_selection":
            stored_offers = slots.get("_matched_offers", [])
            selected = _parse_offer_selection(user_text, stored_offers)
            if selected:
                product_name = str(selected.get("service_name") or "Кредит")
                principal = int(slots.get("requested_amount") or 10_000_000)
                rate = _get_rate_from_offer(selected)
                term = int(slots.get("requested_term_months") or 12)
                try:
                    pdf_path = generate_amortization_pdf(
                        product_name=product_name,
                        principal=principal,
                        annual_rate_pct=rate,
                        term_months=term,
                        output_dir="/tmp",
                    )
                    amount_fmt = f"{principal:,}".replace(",", " ")
                    answer = (
                        f"Отлично! Вы выбрали «{product_name}».\n"
                        f"Ставка: {rate:.1f}%, срок: {term} мес., сумма: {amount_fmt} сум.\n\n"
                        f"[[PDF:{pdf_path}]]\n"
                        "График платежей сформирован. Для оформления обратитесь в ближайший филиал или позвоните в колл-центр."
                    )
                    return _finalize_turn(state, answer, _clear_flow())
                except Exception:
                    amount_fmt = f"{principal:,}".replace(",", " ")
                    answer = (
                        f"Вы выбрали «{product_name}».\n"
                        f"Ставка: {rate:.1f}%, срок: {term} мес., сумма: {amount_fmt} сум.\n\n"
                        "Для оформления кредита обратитесь в ближайший филиал или позвоните в колл-центр. "
                        "Менеджер подготовит полный график платежей."
                    )
                    return _finalize_turn(state, answer, _clear_flow())
            else:
                # User may be asking a side question (e.g., "а какие вклады есть?")
                # — answer it and re-show the offer list.
                faq_answer: Optional[str] = None
                if _is_deposit_intent(user_text) and _is_catalog_style_question(user_text):
                    faq_answer = _noncredit_catalog_overview() or None
                elif _is_credit_overview_question(user_text):
                    faq_answer = _credit_catalog_overview()
                elif _is_question_like(user_text) or _is_followup_like_question(user_text):
                    faq_answer = _faq_lookup(user_text, lang)
                    if not faq_answer:
                        prev_user, prev_ai = _find_last_human_and_ai(prev_messages)
                        faq_answer = _llm_contextual_reply(user_text, prev_user, prev_ai, lang)
                    if not faq_answer:
                        faq_answer = _llm_finance_answer(user_text, lang)
                if faq_answer:
                    offers_again = _format_credit_offers_list(stored_offers, slots.get("credit_category", ""))
                    answer = f"{faq_answer}\n\n---\n{offers_again}"
                    return _finalize_turn(state, answer, dialog)
                answer = (
                    "Укажите номер варианта (1, 2 или 3) для получения графика платежей.\n\n"
                    + _format_credit_offers_list(stored_offers, slots.get("credit_category", ""))
                )
                return _finalize_turn(state, answer, dialog)

        # ── BATCH slot extraction: try to pull ALL unanswered slots at once ─
        new_slots = _extract_all_credit_slots(user_text, slots)

        # ── Mid-flow FAQ / contextual question handling ──────────────────────
        # If user asks a question mid-flow, answer it (with conversation context)
        # then re-show the SAME remaining batch (don't skip any questions).
        if _is_question_like(user_text) or _is_followup_like_question(user_text):
            # 1. Direct FAQ DB lookup
            faq_answer = _faq_lookup(user_text, lang)
            # 2. Contextual lookup using history
            if not faq_answer:
                faq_answer = _contextual_faq_lookup(user_text, prev_messages, lang)
            # 3. LLM with conversation context (knows the last bot question = batch)
            if not faq_answer:
                prev_user, prev_ai = _find_last_human_and_ai(prev_messages)
                faq_answer = _llm_contextual_reply(user_text, prev_user, prev_ai, lang)
            # 4. General finance LLM
            if not faq_answer:
                faq_answer = _llm_finance_answer(user_text, lang)

            if faq_answer:
                # Re-show remaining batch (updated with any newly extracted slots)
                batch = _make_batch_credit_questions(new_slots)
                combined = f"{faq_answer}\n\n---\n{batch}" if batch else faq_answer
                return _finalize_turn(state, combined, {**dialog, "slots": new_slots, "step": "batch_collect"})

        # ── Update slots ─────────────────────────────────────────────────────
        slots = new_slots

        # ── Check if all required questions are answered ─────────────────────
        next_key, next_q = get_next_credit_question(slots)
        if next_q is None:
            offers = _match_credit_offers_from_slots(slots)
            product_type = slots.get("credit_category", "")
            slots["_matched_offers"] = offers
            answer = _format_credit_offers_list(offers, product_type)
            return _finalize_turn(state, answer, {**dialog, "slots": slots, "step": "await_selection"})

        # ── Show remaining batch questions ───────────────────────────────────
        batch = _make_batch_credit_questions(slots)
        return _finalize_turn(state, batch or next_q, {**dialog, "slots": slots, "step": "batch_collect"})

    finally:
        _TURN_MESSAGES.reset(token)


# ---------------------------------------------------------------------------
# CROSS-SELL NODE (deposits & cards)
# ---------------------------------------------------------------------------

def node_cross_sell(state: BotState) -> BotState:
    """Handle non-credit product scenario (deposits, cards)."""
    user_text = (state.get("last_user_text") or "").strip()
    dialog = dict(state.get("dialog") or _set_flow("cross_sell", None, {}))
    slots = dict(dialog.get("slots") or {})
    step = dialog.get("step")
    lang = _REQUEST_LANGUAGE.get()
    prev_messages = list(state.get("messages") or [])
    token = _TURN_MESSAGES.set(prev_messages)
    service_type = slots.get("service_type", "deposit")

    try:
        # Extract slot value for current question
        if step and step not in ("present_offers",):
            value = extract_slot_value(step, user_text)
            if value is not None:
                slots[step] = value

        # Check for FAQ interruption
        if _is_question_like(user_text):
            faq_answer = _faq_lookup(user_text, lang)
            if not faq_answer:
                faq_answer = _llm_finance_answer(user_text, lang)
            if faq_answer:
                next_key, next_q = get_next_noncredit_question(slots, service_type)
                combined = f"{faq_answer}\n\n{next_q}" if next_q else faq_answer
                new_step = next_key if next_key else step
                return _finalize_turn(state, combined, {**dialog, "slots": slots, "step": new_step})

        # Next question
        next_key, next_q = get_next_noncredit_question(slots, service_type)

        if next_q is None:
            # All questions answered -> match and present
            offers = _match_noncredit_offers_from_slots(slots, service_type)
            answer = _format_noncredit_offers(offers, service_type)
            return _finalize_turn(state, answer, _clear_flow())

        return _finalize_turn(state, next_q, {**dialog, "slots": slots, "step": next_key})

    finally:
        _TURN_MESSAGES.reset(token)


# ---------------------------------------------------------------------------
# HUMAN MODE NODE
# ---------------------------------------------------------------------------

def node_human_mode_turn(state: BotState) -> BotState:
    """Pause graph execution and wait for operator reply via interrupt()."""
    from langgraph.types import interrupt as langgraph_interrupt
    user_text = (state.get("last_user_text") or "").strip()
    operator_reply = langgraph_interrupt({
        "user_message": user_text,
        "reason": "human_mode_active",
    })
    answer = str(operator_reply) if operator_reply else ""
    return _finalize_turn(state, answer, dict(state.get("dialog") or _default_dialog()))


# ---------------------------------------------------------------------------
# GRAPH BUILDER
# ---------------------------------------------------------------------------

def build_graph(checkpointer=None, store=None):
    graph = StateGraph(BotState)
    graph.add_node("classify", node_classify_intent)
    graph.add_node("faq", node_faq)
    graph.add_node("product_credit", node_product_credit)
    graph.add_node("cross_sell", node_cross_sell)
    graph.add_node("human_mode", node_human_mode_turn)
    graph.set_entry_point("classify")
    graph.add_conditional_edges(
        "classify",
        _route_turn,
        {
            "faq": "faq",
            "product_credit": "product_credit",
            "cross_sell": "cross_sell",
            "human": "human_mode",
        },
    )
    graph.add_edge("faq", END)
    graph.add_edge("product_credit", END)
    graph.add_edge("cross_sell", END)
    graph.add_edge("human_mode", END)
    return graph.compile(checkpointer=checkpointer or MemorySaver(), store=store)


# ---------------------------------------------------------------------------
# CHECKPOINTER FACTORY
# ---------------------------------------------------------------------------

import logging as _logging

_agent_logger = _logging.getLogger(__name__)


async def _create_async_checkpointer(
    backend: str, url: str | None
) -> tuple[Any, Any]:
    """Create and initialize the appropriate async checkpointer.

    Returns (checkpointer, context_manager_or_None).
    The caller must call __aexit__ on the context_manager at shutdown.
    """
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
# AGENT CLASS
# ---------------------------------------------------------------------------

class Agent:
    """Scripted FAQ + cross-sell agent with LangGraph state persistence."""

    def __init__(self) -> None:
        from langgraph.store.memory import InMemoryStore
        self._store = InMemoryStore()
        self._graph = build_graph()  # MemorySaver until setup() is called
        self._checkpointer: Any = None
        self._checkpointer_cm: Any = None  # context manager for cleanup

    async def setup(self, backend: str = "auto", url: str | None = None) -> None:
        """Initialize async checkpointer based on config. Call once at startup."""
        checkpointer, cm = await _create_async_checkpointer(backend, url)
        self._checkpointer = checkpointer
        self._checkpointer_cm = cm
        self._graph = build_graph(checkpointer=checkpointer, store=self._store)

    def _build_config(self, session_id: str) -> Dict[str, Any]:
        return {"configurable": {"thread_id": session_id}}

    async def _aload_existing_state(self, config: Dict[str, Any]) -> dict[str, Any]:
        try:
            if hasattr(self._graph, "aget_state"):
                snapshot = await self._graph.aget_state(config)
            else:
                snapshot = self._graph.get_state(config)
            values = snapshot.values or {}
            return dict(values)
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
        language: str | None = None,
        human_mode: bool = False,
        user_id: Optional[int] = None,
    ) -> str:
        # Load user language preference from store if not provided
        if user_id and language is None:
            preferred_lang = self._get_user_preference(user_id, "language")
            if preferred_lang:
                language = preferred_lang
        lang_token = _REQUEST_LANGUAGE.set(_normalize_language_code(language))
        config = self._build_config(session_id)
        try:
            existing = await self._aload_existing_state(config)
            state_in: BotState = {
                "last_user_text": user_text,
                "messages": list(existing.get("messages") or [SystemMessage(content=SYSTEM_POLICY)]),
                "dialog": dict(existing.get("dialog") or _default_dialog()),
                "human_mode": human_mode,
            }
            out = await self._graph.ainvoke(state_in, config=config)
            return str(out.get("answer") or "Уточните, пожалуйста, ваш вопрос.")
        finally:
            _REQUEST_LANGUAGE.reset(lang_token)

    async def send_message(
        self,
        session_id: str,
        user_id: int,
        text: str,
        language: str | None = None,
        human_mode: bool = False,
    ) -> str:
        if user_id and language:
            self._save_user_preference(user_id, "language", language)
        return await self._ainvoke(session_id, text, language, human_mode=human_mode, user_id=user_id)

    async def resume_human_mode(self, session_id: str, operator_reply: str) -> str:
        """Resume a graph interrupted in human_mode_node, injecting operator reply."""
        try:
            from langgraph.types import Command
            config = self._build_config(session_id)
            out = await self._graph.ainvoke(Command(resume=operator_reply), config=config)
            return str(out.get("answer") or operator_reply)
        except Exception as e:
            _agent_logger.warning("resume_human_mode error for %s: %s", session_id, e)
            return operator_reply

    async def ensure_language(self, text: str, language: str | None = None) -> str:
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
        updater = getattr(self._graph, "update_state", None)
        if callable(updater):
            try:
                updater(config, {"messages": msgs})
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
