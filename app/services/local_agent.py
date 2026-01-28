from __future__ import annotations

import asyncio
import json
import os
import re
import uuid
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, TypedDict

from dotenv import load_dotenv
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph
from pydantic import BaseModel
from raw_tool.microloan_products import (
    build_microloan_response_auto_term,
    build_microloan_response_known_term,
    load_products_from_yaml as load_microloan_products_from_yaml,
)
from raw_tool.mortgage_selector import (
    load_products_from_yaml as load_mortgage_products_from_yaml,
    match_mortgages,
)

load_dotenv()

CALL_CENTER_PHONE = (os.getenv("CALL_CENTER_PHONE") or "").strip()
PDF_MARKER_PREFIX = "[[PDF:"
PDF_MARKER_SUFFIX = "]]"

Intent = Literal["greeting", "qa", "mortgage", "auto_loan", "microloan", "service", "unknown"]


class IntentResult(BaseModel):
    intent: Intent = "unknown"
    confidence: float = 0.0
    reason: str = ""


class Lead(BaseModel):
    lead_id: str
    product_type: str
    payload: Dict[str, Any]
    summary_for_operator: str


class BotState(TypedDict, total=False):
    chat_id: str
    messages: List[Any]
    last_user_text: str
    intent: Intent
    intent_confidence: float
    active_flow: Optional[Intent]
    step: int
    form: Dict[str, Any]
    answer: Optional[str]
    lead: Optional[Dict[str, Any]]
    pending_question: Optional[str]
    pending_pdf: Optional[bool]
    last_quote: Optional[Dict[str, Any]]

GENERAL_QUESTIONS: Dict[str, Dict[str, Any]] = {
    "credit_category": {
        "possible_values": ["mortgage", "microloan", "autoloan"],
        "required": True,
        "question": "Какой тип кредита вас интересует: ипотека, микрозайм или автокредит?",
    },
    "citizen_uz": {
        "possible_values": ["True", "False"],
        "required": True,
        "question": "Вы являетесь гражданином Республики Узбекистан?",
    },
    "age": {
        "possible_values": "integer",
        "required": True,
        "question": "Сколько вам полных лет?",
    },
    "gender": {
        "possible_values": ["male", "female"],
        "required": True,
        "question": "Уточните ваш пол по паспорту: мужской или женский?",
    },
    "income_proof": {
        "possible_values": ["True", "False"],
        "required": True,
        "question": "Есть ли у вас официальный подтверждённый доход?",
    },
    "self_employed": {
        "possible_values": ["True", "False"],
        "required": True,
        "question": "Вы зарегистрированы как самозанятый?",
    },
    "requested_amount": {
        "possible_values": "integer",
        "required": True,
        "question": "Какую сумму кредита вы планируете взять?",
    },
    "requested_term_months": {
        "possible_values": "integer",
        "required": True,
        "question": "На какой срок планируете взять кредит (в годах или месяцах)?",
    },
    "downpayment_pct": {
        "possible_values": "integer",
        "required": False,
        "question": "Какой первоначальный взнос в процентах вы планируете?",
    },
}

MORTGAGE_QUESTIONS: Dict[str, Dict[str, Any]] = {
    "purpose_segment": {
        "possible_values": ["housing"],
        "required": True,
        "question": None,
    },
    "purpose_keys": {
        "possible_values": ["housing_primary", "housing_secondary"],
        "required": True,
        "question": "Новостройка или вторичный рынок?",
    },
    "mortgage_program": {
        "possible_values": ["bi_group", "nrg_2_4", "daho", "standard", "None"],
        "required": True,
        "question": "Какую ипотечную программу рассматриваете?",
    },
    "region_code": {
        "possible_values": ["tashkent", "regions"],
        "required": True,
        "question": "В каком регионе находится недвижимость?",
    },
}

MICROLOAN_QUESTIONS: Dict[str, Dict[str, Any]] = {
    "purpose_segment": {
        "possible_values": ["consumer", "business"],
        "required": True,
        "question": "Для личных целей или для бизнеса вы оформляете микрозайм?",
    },
    "purpose_keys": {
        "possible_values": ["personal_any", "business_start", "business_support"],
        "required": True,
        "question": "Для каких целей вам нужен микрозайм?",
    },
    "payroll_participant": {
        "possible_values": ["True", "False"],
        "required": True,
        "question": "Получаете зарплату через наш банк?",
    },
}

SERVICE_QUESTION_BLOCKS: Dict[str, Dict[str, Dict[str, Any]]] = {
    "mortgage": MORTGAGE_QUESTIONS,
    "microloan": MICROLOAN_QUESTIONS,
    # autoloan — без сервисных вопросов
}


BANK_FAQ = [
    {
        "q": "какие документы нужны для ипотеки",
        "a": "Обычно требуются: паспорт, справка о доходах, документы на объект, заявление-анкета. Точный список зависит от программы."
    },
    {
        "q": "какие акции есть",
        "a": "Текущие акции зависят от региона и продукта. Я могу проверить акции по вашему продукту (ипотека/авто/микро) и сумме."
    },
    {
        "q": "досрочное погашение",
        "a": "Досрочное погашение возможно. Комиссия и порядок зависят от договора и типа кредита."
    },
]

PRODUCTS = {
    "mortgage": {"rate_annual": 0.24, "min_term_months": 12, "max_term_months": 240},
    "auto_loan": {"rate_annual": 0.26, "min_term_months": 6, "max_term_months": 84},
    "microloan": {"rate_annual": 0.34, "min_term_months": 3, "max_term_months": 36},
}

PROMOS = [
    {"name": "Снижение ставки -1% для зарплатных клиентов", "applies_to": ["mortgage", "auto_loan"], "active": True},
    {"name": "Без комиссии за выдачу (акция)", "applies_to": ["microloan"], "active": True},
]

MORTGAGE_PROGRAM_LABELS = {
    "standard": "Стандартная (рыночные условия)",
    "bi_group": "BI Group",
    "nrg_2_4": "NRG 2-4",
    "daho": "DAHO",
}


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _normalize_text(text: str) -> str:
    lowered = text.lower()
    cleaned = re.sub(r"[^\w\s]+", " ", lowered, flags=re.UNICODE)
    return re.sub(r"\s+", " ", cleaned).strip()


@lru_cache(maxsize=1)
def _load_faq_json() -> List[Dict[str, Any]]:
    path = _project_root() / "app" / "data" / "faq.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    items = data.get("items") or []
    return [item for item in items if isinstance(item, dict)]


def _faq_lookup(query: str) -> Optional[str]:
    norm_query = _normalize_text(query)
    if not norm_query:
        return None
    best_answer = None
    best_len = 0
    for item in _load_faq_json():
        answer = item.get("a")
        if not answer:
            continue
        variants = []
        if item.get("q"):
            variants.append(str(item["q"]))
        for alias in item.get("aliases") or []:
            variants.append(str(alias))
        for variant in variants:
            norm_variant = _normalize_text(variant)
            if not norm_variant:
                continue
            if norm_variant in norm_query or norm_query in norm_variant:
                if len(norm_variant) > best_len:
                    best_answer = str(answer)
                    best_len = len(norm_variant)
    return best_answer


def _list_mortgage_program_labels() -> List[str]:
    try:
        products = _load_mortgage_products()
        codes = {p.get("mortgage_program") or "standard" for p in products}
    except Exception:
        codes = set(MORTGAGE_PROGRAM_LABELS.keys())
    labels = [MORTGAGE_PROGRAM_LABELS.get(code, code) for code in sorted(codes)]
    return labels


def _mortgage_programs_prompt() -> str:
    labels = _list_mortgage_program_labels()
    if labels:
        return (
            "Есть ипотечные программы: "
            + ", ".join(labels)
            + ". Если нет предпочтений, можно выбрать «любую». Какую программу рассматриваете?"
        )
    return "Есть стандартная и партнёрские программы. Какую программу рассматриваете?"


@lru_cache(maxsize=1)
def _load_microloan_products() -> List[Dict[str, Any]]:
    path = _project_root() / "raw_tool" / "microloan_products.yml"
    if not path.exists():
        raise FileNotFoundError(f"microloan_products.yml not found at {path}")
    return load_microloan_products_from_yaml(str(path))


@lru_cache(maxsize=1)
def _load_mortgage_products() -> List[Dict[str, Any]]:
    path = _project_root() / "raw_tool" / "mortgage_products.yml"
    if not path.exists():
        raise FileNotFoundError(f"mortgage_products.yml not found at {path}")
    return load_mortgage_products_from_yaml(str(path))


def run_microloan_selector(profile: Dict[str, Any]) -> Dict[str, Any]:
    products = _load_microloan_products()

    term = profile.get("requested_term_months")
    if term is not None:
        response = build_microloan_response_known_term(products, profile, top_k=3)
        mode = "known_term"
    else:
        response = build_microloan_response_auto_term(
            products, profile, top_k_products=3, term_options_per_product=3
        )
        mode = "auto_term"

    result = {"mode": mode, "response": response, "profile": profile}
    return result


def run_mortgage_selector(profile: Dict[str, Any]) -> Dict[str, Any]:
    """
    Обёртка над match_mortgages.
    Возвращает dict: {profile, best_offers, near_miss}.
    """
    products = _load_mortgage_products()
    result = match_mortgages(products, profile, top_k=3)
    return result


def _extract_numbers(text: str) -> List[float]:
    normalized = re.sub(r"(?<=\d)\s+(?=\d)", "", text)
    numbers: List[float] = []
    for chunk in re.findall(r"\d+(?:[.,]\d+)?", normalized):
        try:
            numbers.append(float(chunk.replace(",", ".").replace(" ", "")))
        except ValueError:
            continue
    return numbers


def _is_branch_or_office_question(text: str) -> bool:
    lower = text.lower()
    keywords = (
        "филиал",
        "отделен",
        "отделени",
        "отделение",
        "цбу",
        "офис",
        "адрес",
        "располож",
        "где находитесь",
        "ближай",
        "график",
        "режим работы",
        "часы работы",
    )
    return any(k in lower for k in keywords)


def _has_purchase_signal(text: str) -> bool:
    lower = text.lower()
    triggers = ("хочу", "нужно", "оформ", "подать", "заявк", "взять", "получить", "купить", "интересует")
    return any(t in lower for t in triggers)


def _is_bank_related(text: str) -> bool:
    lower = text.lower()
    keywords = (
        "банк",
        "ипот",
        "кредит",
        "займ",
        "loan",
        "квартир",
        "жиль",
        "новостро",
        "вторич",
        "недвиж",
        "дом",
        "карт",
        "перевод",
        "депозит",
        "вклад",
        "счет",
        "счёт",
        "оплат",
        "платеж",
        "платёж",
        "комисси",
        "отделен",
        "филиал",
        "цбу",
        "офис",
        "акци",
        "ставк",
        "услуг",
        "парол",
        "логин",
        "вход",
        "прилож",
        "смс",
        "код",
        "пин",
        "pin",
    )
    return any(k in lower for k in keywords)


def _is_pdf_request(text: str) -> bool:
    lower = text.lower()
    return "pdf" in lower or "пдф" in lower or "график" in lower


def _is_mortgage_programs_question(text: str) -> bool:
    lower = text.lower()
    triggers = (
        "какие программы",
        "какие есть",
        "какие варианты",
        "список программ",
        "что есть",
        "перечисли",
    )
    return any(t in lower for t in triggers)


def _is_housing_difference_question(text: str) -> bool:
    lower = text.lower()
    if "новостро" in lower and "вторич" in lower:
        return True
    if any(k in lower for k in ("разниц", "чем отлич", "что такое")):
        return "новостро" in lower or "вторич" in lower or "первич" in lower
    return False


def _explain_primary_secondary() -> str:
    return (
        "Новостройка — жильё от застройщика (новый дом, часто первичный рынок). "
        "Вторичный рынок — жильё, которое уже было в собственности и продаётся повторно. "
        "Условия ипотеки могут отличаться по ставке и первоначальному взносу. "
        "Что выбираете: новостройка или вторичный рынок?"
    )


def _parse_bool(text: str) -> Optional[bool]:
    lower = text.lower()
    true_tokens = ("да", "ага", "угу", "конечно", "yes", "true", "есть", "являюсь", "верно")
    false_tokens = ("нет", "не ", "false", "no", "неа", "не являюсь", "not ")
    if any(tok in lower for tok in true_tokens):
        return True
    if any(tok in lower for tok in false_tokens):
        return False
    return None


def _parse_gender(text: str) -> Optional[str]:
    lower = text.lower()
    if any(w in lower for w in ("жен", "female", "дев", "девуш", "woman")):
        return "female"
    if any(w in lower for w in ("муж", "male", "парень", "man")):
        return "male"
    return None


def _parse_credit_category(text: str) -> Optional[str]:
    lower = text.lower()
    if any(k in lower for k in ("ипот", "квартир", "жиль", "новостро")):
        return "mortgage"
    if any(k in lower for k in ("микро", "микроз", "микрокредит")):
        return "microloan"
    if any(k in lower for k in ("авто", "машин", "car", "auto")):
        return "auto_loan"
    return None


def _parse_age(text: str, allow_plain: bool = False) -> Optional[int]:
    nums = _extract_numbers(text)
    for num in nums:
        age = int(num)
        if 14 <= age <= 100:
            return age
    if allow_plain and nums:
        guess = int(nums[0])
        if 10 < guess < 120:
            return guess
    return None


def _parse_amount(text: str, allow_plain_number: bool = False) -> Optional[int]:
    lower = text.lower()
    keywords = (
        "сум",
        "uzs",
        "сом",
        "руб",
        "usd",
        "eur",
        "доллар",
        "сумма",
        "кредит",
        "заём",
        "заем",
        "loan",
        "млн",
        "миллион",
        "тыс",
    )
    if not allow_plain_number and not any(k in lower for k in keywords):
        return None

    nums = _extract_numbers(text)
    if not nums:
        return None

    amount = nums[0]
    if "млрд" in lower or "миллиард" in lower:
        amount *= 1_000_000_000
    elif "млн" in lower or "million" in lower:
        amount *= 1_000_000
    elif "тыс" in lower or "тысяч" in lower or lower.strip().endswith("k"):
        amount *= 1_000

    amount_int = int(amount)
    return amount_int if amount_int > 0 else None


def _parse_term_months(text: str, allow_plain_number: bool = False) -> Optional[int]:
    lower = text.lower()
    nums = _extract_numbers(text)
    if not nums:
        return None

    has_year = any(word in lower for word in ("год", "года", "лет", "year"))
    has_month = any(word in lower for word in ("мес", "месяц", "месяцев", "month"))

    if has_year:
        return int(nums[0] * 12)
    if has_month or allow_plain_number:
        return int(nums[0])
    return None


def _parse_downpayment_pct(text: str, allow_plain_number: bool = False) -> Optional[int]:
    nums = _extract_numbers(text)
    if not nums:
        return None
    pct = float(nums[0])
    if pct <= 0 or pct > 100:
        return None
    lower = text.lower()
    if "%" in text or "взнос" in lower or allow_plain_number:
        return int(pct)
    return None


def _parse_region_code(text: str, allow_plain: bool = False) -> Optional[str]:
    lower = text.lower()
    if "ташкент" in lower:
        return "tashkent"
    if any(k in lower for k in ("регион", "область", "обл", "регионы")) or allow_plain:
        return "regions"
    return None


def _parse_mortgage_program(text: str) -> Optional[str]:
    lower = text.lower()
    if "bi" in lower or "би" in lower:
        return "bi_group"
    if "nrg" in lower:
        return "nrg_2_4"
    if "daho" in lower or "дахо" in lower:
        return "daho"
    if "станд" in lower or "обыч" in lower or "classic" in lower:
        return "standard"
    if "любой" in lower or "любую" in lower or "не знаю" in lower or "без разницы" in lower:
        return "any"
    return None


def _parse_mortgage_purpose_keys(text: str, pending: bool = False) -> Optional[List[str]]:
    lower = text.lower()
    if "втор" in lower or "вторич" in lower:
        return ["housing_secondary"]
    if any(k in lower for k in ("новост", "первич", "новое", "застрой", "строй")):
        return ["housing_primary"]
    if pending and "люб" in lower:
        return ["housing_primary", "housing_secondary"]
    return None


def _parse_microloan_purpose_segment(text: str, pending: bool = False) -> Optional[str]:
    lower = text.lower()
    if any(k in lower for k in ("бизнес", "ип", "предприним", "фоп", "self-employed")):
        return "business"
    if pending and lower.strip():
        return "consumer"
    return None


def _parse_microloan_purpose_keys(text: str, pending: bool = False) -> Optional[List[str]]:
    lower = text.lower()
    if any(k in lower for k in ("старт", "открыть", "начать", "запустить")):
        return ["business_start"]
    if any(k in lower for k in ("расшир", "поддерж", "оборот", "поддержать", "развит")):
        return ["business_support"]
    if pending and lower.strip():
        return ["personal_any"]
    return None


def _set_if_absent(profile: Dict[str, Any], key: str, value: Any) -> None:
    if value is None:
        return
    if profile.get(key) is None:
        profile[key] = value


def _ensure_defaults(flow: Intent, profile: Dict[str, Any]) -> Dict[str, Any]:
    if profile.get("credit_category") is None and flow in ("mortgage", "microloan", "auto_loan"):
        profile["credit_category"] = flow if flow != "auto_loan" else "auto_loan"
    if flow == "mortgage" and profile.get("purpose_segment") is None:
        profile["purpose_segment"] = "housing"
    return profile


def _parse_field_by_key(
    key: str,
    text: str,
    flow: Intent,
    pending_key: Optional[str] = None,
) -> Any:
    allow_plain = pending_key == key
    if key == "credit_category":
        return _parse_credit_category(text)
    if key == "citizen_uz":
        lower = text.lower()
        if "граждан" in lower:
            return False if "не" in lower else True
        return _parse_bool(text)
    if key == "age":
        return _parse_age(text, allow_plain=allow_plain)
    if key == "gender":
        return _parse_gender(text)
    if key == "income_proof":
        return _parse_bool(text)
    if key == "self_employed":
        return _parse_bool(text)
    if key == "requested_amount":
        return _parse_amount(text, allow_plain_number=allow_plain)
    if key == "requested_term_months":
        return _parse_term_months(text, allow_plain_number=allow_plain)
    if key == "downpayment_pct":
        return _parse_downpayment_pct(text, allow_plain_number=allow_plain)
    if key == "purpose_segment":
        if flow == "mortgage":
            return "housing"
        if flow == "microloan":
            return _parse_microloan_purpose_segment(text, pending=allow_plain)
    if key == "purpose_keys":
        if flow == "mortgage":
            return _parse_mortgage_purpose_keys(text, pending=allow_plain)
        if flow == "microloan":
            return _parse_microloan_purpose_keys(text, pending=allow_plain)
    if key == "mortgage_program":
        return _parse_mortgage_program(text)
    if key == "region_code":
        return _parse_region_code(text, allow_plain=allow_plain)
    if key == "payroll_participant":
        return _parse_bool(text)
    return None


def _update_profile_from_text(
    flow: Intent,
    text: str,
    profile: Dict[str, Any],
    pending_key: Optional[str] = None,
) -> Dict[str, Any]:
    if pending_key:
        _set_if_absent(profile, pending_key, _parse_field_by_key(pending_key, text, flow, pending_key))

    if flow in ("mortgage", "auto_loan", "microloan"):
        for key in GENERAL_QUESTIONS:
            _set_if_absent(profile, key, _parse_field_by_key(key, text, flow))

    service_block = SERVICE_QUESTION_BLOCKS.get(flow) or {}
    for key in service_block:
        _set_if_absent(profile, key, _parse_field_by_key(key, text, flow))

    return profile


def _find_next_question(flow: Intent, profile: Dict[str, Any]) -> tuple[Optional[str], Optional[str]]:
    if flow in ("mortgage", "auto_loan", "microloan"):
        for key, cfg in GENERAL_QUESTIONS.items():
            if key == "downpayment_pct" and flow != "mortgage":
                continue
            if profile.get(key) is None and (cfg.get("required") or (flow == "mortgage" and key == "downpayment_pct")):
                return key, cfg.get("question")

    block = SERVICE_QUESTION_BLOCKS.get(flow) or {}
    for key, cfg in block.items():
        if profile.get(key) is None and cfg.get("required", False):
            if cfg.get("question") is None and len(cfg.get("possible_values", [])) == 1:
                profile[key] = cfg["possible_values"][0]
                continue
            return key, cfg.get("question")

    return None, None


def _format_microloan_result(result: Dict[str, Any]) -> str:
    resp = result.get("response", {})
    mode = result.get("mode", "known_term")
    if resp.get("status") == "rejected":
        reason = resp.get("reason_text") or "Нет подходящих микрозаймов по базовым требованиям."
        return f"По микрозаймам сейчас не могу предложить варианты: {reason}"

    best = resp.get("best_offers") or []
    near = resp.get("near_miss") or []
    lines: List[str] = []
    if best:
        lines.append("Подобрал варианты микрозайма по вашим ответам:")
        for idx, item in enumerate(best, start=1):
            product = item.get("product_data") or item.get("product") or {}
            name = product.get("name", "Продукт")
            channel = product.get("channel_text") or product.get("channel")
            apr = None
            term = None
            grace = None
            if mode == "known_term":
                offer = item.get("offer") or {}
                apr = offer.get("apr")
                term = offer.get("term_months")
                grace = offer.get("grace_months")
            else:
                offers = item.get("offers") or []
                if offers:
                    apr = offers[0].get("apr")
                    term = offers[0].get("term_months")
                    grace = offers[0].get("grace_months")
            parts = [f"{idx}) {name}"]
            if apr is not None and term is not None:
                parts.append(f"ставка ~{apr:.1f}% на {term} мес.")
            if grace:
                parts.append(f"льготный период: {grace} мес.")
            if channel:
                parts.append(f"канал: {channel}")
            lines.append(" — ".join(parts))
    else:
        lines.append("Прямых совпадений по микрозаймам не нашлось.")
        if near:
            lines.append("Ближайшие варианты:")
            for idx, item in enumerate(near, start=1):
                product = item.get("product_data") or item.get("product") or {}
                name = product.get("name", "Продукт")
                reason = item.get("main_fail_reason") or item.get("reason_text") or "есть ограничения"
                lines.append(f"{idx}) {name}")
                for reason_line in _split_reason_lines(reason):
                    lines.append(f"   - {reason_line}")
    return "\n".join(lines)


def _split_reason_lines(reason: str) -> List[str]:
    lines = []
    for line in (reason or "").splitlines():
        cleaned = line.strip()
        if not cleaned:
            continue
        if cleaned.startswith("-"):
            cleaned = cleaned.lstrip("-").strip()
        lines.append(cleaned)
    return lines or [reason.strip()] if reason else ["есть ограничения"]


def _format_mortgage_result(result: Dict[str, Any]) -> str:
    best = result.get("best_offers") or []
    near = result.get("near_miss") or []
    if best:
        lines = ["Подобрал ипотечные программы:"]
        for idx, item in enumerate(best, start=1):
            product = item.get("product") or {}
            name = product.get("name", "Программа")
            apr = item.get("apr")
            channel_text = item.get("channel_text") or product.get("channel_text") or item.get("channel")
            parts = [f"{idx}) {name}"]
            if apr is not None:
                parts.append(f"ставка ~{apr:.2f}%")
            if channel_text:
                parts.append(channel_text)
            lines.append(" — ".join(parts))
        return "\n".join(lines)

    if near:
        lines = ["Прямых совпадений по ипотеке нет, но есть близкие варианты:"]
        for idx, item in enumerate(near, start=1):
            product = item.get("product") or {}
            name = product.get("name", "Программа")
            reason = item.get("reason_text") or "есть ограничения"
            lines.append(f"{idx}) {name}")
            for reason_line in _split_reason_lines(reason):
                lines.append(f"   - {reason_line}")
        return "\n".join(lines)

    return "По указанным параметрам ипотечные программы не найдены. Могу передать запрос оператору для ручной проверки."


def _format_auto_loan_reply(profile: Dict[str, Any]) -> str:
    amount = profile.get("requested_amount")
    term = profile.get("requested_term_months")
    calc = None
    if amount and term:
        calc = annuity_payment.invoke({"principal": float(amount), "annual_rate": 0.26, "term_months": int(term)})
    reply = "Предварительная консультация по автокредиту."
    if amount:
        reply += f"\nСумма: {int(amount):,}"
    if term:
        reply += f"\nСрок: {int(term)} мес."
    if calc:
        reply += f"\n{calc}"
    reply += "\nАкции: " + get_active_promos.invoke({"product_type": "auto_loan"})
    reply += "\nМогу оформить обращение для детальной консультации."
    return reply


def _normalize_apr(apr: Optional[float]) -> Optional[float]:
    if apr is None:
        return None
    try:
        value = float(apr)
    except (TypeError, ValueError):
        return None
    return value / 100.0 if value > 1.0 else value


def _extract_mortgage_apr(result: Dict[str, Any]) -> Optional[float]:
    best = result.get("best_offers") or []
    if best:
        return _normalize_apr(best[0].get("apr"))
    return None


def _extract_microloan_apr(result: Dict[str, Any]) -> Optional[float]:
    resp = result.get("response") or {}
    best = resp.get("best_offers") or []
    if best:
        item = best[0]
        offer = item.get("offer") or {}
        apr = offer.get("apr")
        if apr is None:
            offers = item.get("offers") or []
            if offers:
                apr = offers[0].get("apr")
        return _normalize_apr(apr)
    return None


def _build_quote(
    flow: Intent,
    profile: Dict[str, Any],
    mortgage_result: Optional[Dict[str, Any]] = None,
    microloan_result: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    try:
        principal = int(float(profile.get("requested_amount"))) if profile.get("requested_amount") is not None else None
        term = int(float(profile.get("requested_term_months"))) if profile.get("requested_term_months") is not None else None
    except (TypeError, ValueError):
        return None

    if not principal or not term:
        return None

    annual_rate = None
    if flow == "mortgage":
        annual_rate = _extract_mortgage_apr(mortgage_result) or PRODUCTS["mortgage"]["rate_annual"]
    elif flow == "microloan":
        annual_rate = _extract_microloan_apr(microloan_result) or PRODUCTS["microloan"]["rate_annual"]
    elif flow == "auto_loan":
        annual_rate = PRODUCTS["auto_loan"]["rate_annual"]

    if annual_rate is None:
        return None

    return {
        "principal": principal,
        "term_months": term,
        "annual_rate": float(annual_rate),
        "flow": flow,
    }


def _format_amount(value: float) -> str:
    return f"{value:,.0f}"


def _build_payment_schedule_lines(principal: float, annual_rate: float, term_months: int) -> List[str]:
    if term_months <= 0:
        return ["Payment schedule", "Invalid term."]
    monthly_rate = annual_rate / 12.0
    if monthly_rate <= 0:
        payment = principal / term_months
    else:
        denom = 1 - (1 + monthly_rate) ** (-term_months)
        payment = principal * monthly_rate / denom if denom != 0 else principal / term_months

    balance = float(principal)
    lines = [
        "Loan payment schedule",
        f"Principal: {_format_amount(principal)}",
        f"Annual rate: {annual_rate * 100:.2f}%",
        f"Term: {int(term_months)} months",
        "",
        "Month    Payment    Interest   Principal     Balance",
    ]
    total_paid = 0.0
    total_interest = 0.0
    for month in range(1, term_months + 1):
        interest = balance * monthly_rate
        principal_paid = payment - interest
        if month == term_months:
            principal_paid = balance
            payment = interest + principal_paid
        balance = max(0.0, balance - principal_paid)
        total_paid += payment
        total_interest += interest
        lines.append(
            f"{month:>5} {_format_amount(payment):>11} {_format_amount(interest):>11} "
            f"{_format_amount(principal_paid):>11} {_format_amount(balance):>11}"
        )

    lines.append("")
    lines.append(f"Total paid: {_format_amount(total_paid)}")
    lines.append(f"Total interest: {_format_amount(total_interest)}")
    return lines


def _pdf_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _build_pdf_stream(lines: List[str], start_x: int = 50, start_y: int = 760, leading: int = 12) -> str:
    if not lines:
        lines = [""]
    escaped = [_pdf_escape(line) for line in lines]
    parts = [
        "BT",
        "/F1 10 Tf",
        f"{leading} TL",
        f"{start_x} {start_y} Td",
        f"({escaped[0]}) Tj",
    ]
    for line in escaped[1:]:
        parts.append(f"T* ({line}) Tj")
    parts.append("ET")
    return "\n".join(parts)


def _write_simple_pdf(lines: List[str], path: Path) -> None:
    start_y = 760
    leading = 12
    max_lines = max(1, int((start_y - 40) / leading))
    pages = [lines[i:i + max_lines] for i in range(0, len(lines), max_lines)]

    objects: List[str] = []
    objects.append("<< /Type /Catalog /Pages 2 0 R >>")
    page_numbers = [5 + i * 2 for i in range(len(pages))]
    objects.append(f"<< /Type /Pages /Kids [{' '.join(f'{n} 0 R' for n in page_numbers)}] /Count {len(pages)} >>")
    objects.append("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    for page_lines in pages:
        stream = _build_pdf_stream(page_lines, start_y=start_y, leading=leading)
        stream_bytes = stream.encode("latin-1")
        objects.append(f"<< /Length {len(stream_bytes)} >>\nstream\n{stream}\nendstream")
        objects.append(
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            "/Contents {content_id} 0 R /Resources << /Font << /F1 3 0 R >> >> >>"
        )

    content = bytearray()
    offsets = []
    for idx, body in enumerate(objects, start=1):
        offsets.append(len(content))
        if "/Contents {content_id}" in body:
            content_id = idx - 1
            body = body.replace("{content_id}", str(content_id))
        content.extend(f"{idx} 0 obj\n{body}\nendobj\n".encode("latin-1"))

    xref_pos = len(content)
    content.extend(f"xref\n0 {len(objects) + 1}\n".encode("latin-1"))
    content.extend(b"0000000000 65535 f \n")
    for offset in offsets:
        content.extend(f"{offset:010d} 00000 n \n".encode("latin-1"))
    content.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n".encode(
            "latin-1"
        )
    )
    path.write_bytes(content)


def _generate_payment_pdf(quote: Dict[str, Any]) -> str:
    principal = float(quote["principal"])
    annual_rate = float(quote["annual_rate"])
    term_months = int(quote["term_months"])
    lines = _build_payment_schedule_lines(principal, annual_rate, term_months)
    path = Path("/tmp") / f"loan_schedule_{uuid.uuid4().hex}.pdf"
    _write_simple_pdf(lines, path)
    return str(path)


def _with_pdf_marker(text: str, pdf_path: str) -> str:
    return f"{text}\n\n{PDF_MARKER_PREFIX}{pdf_path}{PDF_MARKER_SUFFIX}"


def _finalize_reply_for_flow(flow: Intent, profile: Dict[str, Any]) -> tuple[str, Dict[str, Any], Optional[Dict[str, Any]]]:
    try:
        profile_for_match = dict(profile)
        if profile_for_match.get("mortgage_program") == "any":
            profile_for_match["mortgage_program"] = None
        if flow == "microloan":
            microloan_result = run_microloan_selector(profile_for_match)
            reply = _format_microloan_result(microloan_result)
            quote = _build_quote(flow, profile, microloan_result=microloan_result)
        elif flow == "mortgage":
            mortgage_result = run_mortgage_selector(profile_for_match)
            reply = _format_mortgage_result(mortgage_result)
            quote = _build_quote(flow, profile, mortgage_result=mortgage_result)
        elif flow == "auto_loan":
            reply = _format_auto_loan_reply(profile)
            quote = _build_quote(flow, profile)
        else:
            reply = "Зафиксировал запрос по услуге. Могу передать оператору для уточнения деталей."
            quote = None
    except FileNotFoundError as exc:
        reply = f"Не нашёл справочник продуктов ({exc}). Передам запрос оператору."
        quote = None

    payload = {"flow": flow, "profile": profile}
    lead_json = create_lead.invoke({
        "product_type": flow,
        "payload_json": json.dumps(payload, ensure_ascii=False),
        "summary": f"Запрос клиента: {flow}. Профиль: {json.dumps(profile, ensure_ascii=False)}",
    })
    lead = json.loads(lead_json)
    reply = reply + f"\n\nНомер обращения (демо): {lead['lead_id']}"
    return reply, lead, quote


@tool
def bank_kb_search(query: str) -> str:
    """Search in bank FAQ/KB and return best answer snippet."""
    json_answer = _faq_lookup(query)
    if json_answer:
        return json_answer
    q = query.lower().strip()
    for item in BANK_FAQ:
        if item["q"] in q or q in item["q"]:
            return item["a"]
    contact = CALL_CENTER_PHONE or "колл-центр банка"
    return (
        "Пока не нашёл точного ответа на этот вопрос. "
        "Могу уточнить детали, либо вы можете позвонить в " + contact + "."
    )


@tool
def get_active_promos(product_type: str) -> str:
    """Return active promos for a product type."""
    pt = product_type.strip().lower()
    found = [p["name"] for p in PROMOS if p["active"] and pt in p["applies_to"]]
    if not found:
        return "Активных акций по выбранному продукту сейчас не найдено."
    return "Активные акции: " + "; ".join(found)


@tool
def annuity_payment(principal: float, annual_rate: float, term_months: int) -> str:
    """Calculate annuity monthly payment for loan."""
    if principal <= 0 or term_months <= 0 or annual_rate <= 0:
        return "Для расчёта нужны корректные значения: сумма > 0, срок > 0, ставка > 0."
    r = annual_rate / 12.0
    denom = 1 - (1 + r) ** (-term_months)
    payment = principal * r / denom if denom != 0 else principal / term_months
    return f"Оценка аннуитетного платежа: {payment:,.0f} в месяц (ставка {annual_rate*100:.2f}% годовых, срок {term_months} мес.)."


@tool
def create_lead(product_type: str, payload_json: str, summary: str) -> str:
    """Create a lead (CRM stub)."""
    lead_id = str(uuid.uuid4())[:8]
    lead = Lead(
        lead_id=lead_id,
        product_type=product_type,
        payload=json.loads(payload_json),
        summary_for_operator=summary,
    )
    return lead.model_dump_json()


def build_llm() -> ChatOpenAI:
    return ChatOpenAI(
        model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        temperature=0.2,
    )


SYSTEM_POLICY = """
Ты — сотрудник контакт-центра банка.
Твоя задача — вежливо и профессионально консультировать клиента и помогать оформить продукт или услугу.

Правила общения:
- Не упоминай, что ты ИИ/бот/модель/агент, не говори о “инструментах”, “графе”, “RAG” и т.п.
- Если клиент здоровается — поздоровайся и предложи варианты помощи.
- Если вопрос про документы/условия/акции/расчёт — ответь по базе или уточни недостающие параметры.
- Если клиент хочет оформить ипотеку/автокредит/микрозайм/услугу — задай уточняющие вопросы по сценарию.
- Если данных для точного ответа нет — честно скажи, что нужно уточнение, и задай 1–2 конкретных вопроса.
- Не запрашивай лишние персональные данные. Для предварительной консультации достаточно общих параметров.
"""


def node_classify_intent(state: BotState) -> BotState:
    llm = build_llm().with_structured_output(IntentResult)

    user_text = state["last_user_text"]
    msgs = [
        SystemMessage(content=SYSTEM_POLICY),
        SystemMessage(content=(
            "Определи намерение пользователя. Возможные intent: "
            "greeting, qa, mortgage, auto_loan, microloan, service, unknown.\n"
            "Если это приветствие/начало диалога (привет, здравствуйте, салом, ассалом, доброе утро и т.п.) — greeting.\n"
            "Если пользователь просит документы/акции/условия/расчёт без явного оформления — чаще qa.\n"
            "Вопросы про филиалы, отделения, режим работы, адреса — это qa, а не сценарий оформления.\n"
            "Вопросы про доступ в приложение, пароль/логин, блокировку карты, выписку/баланс — это qa.\n"
            "Если вопрос не относится к банку или его услугам — unknown.\n"
            "Если явно: 'хочу ипотеку/оформить/подать заявку' — mortgage.\n"
            "Если 'автокредит/машина' — auto_loan. 'микрозайм/микрокредит' — microloan.\n"
            "Если 'карта/перевод/депозит/услуга/счёт' — service."
        )),
        HumanMessage(content=user_text),
    ]
    res: IntentResult = llm.invoke(msgs)
    state["intent"] = res.intent
    state["intent_confidence"] = float(res.confidence)
    return state


def node_route(state: BotState) -> str:
    if state.get("active_flow"):
        return "flow"

    user_text = state.get("last_user_text") or ""
    if state.get("pending_pdf"):
        decision = _parse_bool(user_text)
        if decision is not None or _is_pdf_request(user_text):
            return "pdf"
        state["pending_pdf"] = False
    if _is_branch_or_office_question(user_text):
        return "qa"

    intent = state.get("intent", "unknown")
    confidence = float(state.get("intent_confidence") or 0.0)

    if intent == "greeting":
        return "greeting"

    if (
        intent in ("mortgage", "auto_loan", "microloan", "service")
        and (confidence >= 0.4 or _has_purchase_signal(user_text))
        and _is_bank_related(user_text)
    ):
        return "start_flow"

    return "qa"


def node_greeting(state: BotState) -> BotState:
    user_text = state["last_user_text"]
    reply = (
        "Здравствуйте. Я помогу с вопросами по продуктам и услугам банка.\n"
        "Подскажите, пожалуйста, что вас интересует: условия кредита (ипотека/авто/микро), "
        "необходимые документы, действующие акции или расчёт платежа?"
    )

    msgs = state.get("messages", [])
    msgs = msgs + [HumanMessage(content=user_text)]
    msgs.append(AIMessage(content=reply))
    state["messages"] = msgs
    state["answer"] = reply
    return state


def node_qa(state: BotState) -> BotState:
    user_text = state["last_user_text"]
    lower = user_text.lower()
    intent = state.get("intent", "unknown")
    faq_answer = _faq_lookup(user_text)

    if faq_answer:
        answer = faq_answer
    elif _is_branch_or_office_question(user_text):
        answer = (
            "Да, отделения и ЦБУ банка есть. "
            "Нажмите «🏢 Отделения», чтобы выбрать город или район, или отправьте геолокацию кнопкой "
            "«📍 Найти ближайший ЦБУ», и я подскажу ближайший офис."
        )
    elif _is_housing_difference_question(user_text):
        answer = _explain_primary_secondary()
    elif not _is_bank_related(user_text) and intent == "unknown":
        answer = "Я отвечаю только на вопросы по продуктам и услугам банка. Сформулируйте, пожалуйста, вопрос про кредиты, карты или отделения."
    elif "акци" in lower:
        pt = "mortgage" if "ипот" in lower else "auto_loan" if "авто" in lower else "microloan" if "микро" in lower else "mortgage"
        answer = get_active_promos.invoke({"product_type": pt})
    elif any(k in lower for k in ["посч", "платеж", "платёж", "расчет", "расчёт", "калькул"]):
        answer = "Для расчёта подскажите, пожалуйста: тип продукта (ипотека/авто/микро), сумму кредита и срок (в месяцах)."
    else:
        answer = bank_kb_search.invoke({"query": user_text})

    msgs = state.get("messages", [])
    msgs = msgs + [HumanMessage(content=user_text)]
    msgs.append(AIMessage(content=answer))
    state["messages"] = msgs
    state["answer"] = answer
    return state


def node_pdf(state: BotState) -> BotState:
    user_text = state["last_user_text"]
    decision = _parse_bool(user_text)
    if decision is None and _is_pdf_request(user_text):
        decision = True

    if decision is False:
        reply = "Хорошо, если понадобится PDF — скажите."
        state["pending_pdf"] = False
    elif decision is True:
        quote = state.get("last_quote")
        if not quote:
            reply = "Для PDF нужны сумма и срок кредита. Уточните их, и я подготовлю документ."
            state["pending_pdf"] = False
        else:
            try:
                pdf_path = _generate_payment_pdf(quote)
                reply = _with_pdf_marker("Готово. Отправляю PDF с графиком выплат.", pdf_path)
            except Exception:
                reply = "Не удалось подготовить PDF. Попробуйте позже."
            state["pending_pdf"] = False
    else:
        reply = "Если хотите PDF, напишите «да». Если не нужно — «нет»."
        state["pending_pdf"] = True

    msgs = state.get("messages", [])
    msgs = msgs + [HumanMessage(content=user_text)]
    msgs.append(AIMessage(content=reply))
    state["messages"] = msgs
    state["answer"] = reply
    return state


def node_start_flow(state: BotState) -> BotState:
    intent = state.get("intent", "unknown")
    flow: Intent = intent if intent in ("mortgage", "auto_loan", "microloan", "service") else None
    state["active_flow"] = flow
    state["step"] = 0
    state["form"] = _ensure_defaults(flow or "unknown", state.get("form", {}))
    state["pending_question"] = None
    state["pending_pdf"] = False
    return state


def node_flow(state: BotState) -> BotState:
    user_text = state["last_user_text"]
    flow: Intent = state.get("active_flow") or "unknown"
    pending_key = state.get("pending_question")
    if flow == "mortgage" and pending_key == "purpose_keys" and _is_housing_difference_question(user_text):
        reply = _explain_primary_secondary()
        msgs = state.get("messages", [])
        msgs = msgs + [HumanMessage(content=user_text)]
        msgs.append(AIMessage(content=reply))
        state["messages"] = msgs
        state["answer"] = reply
        return state
    if flow == "mortgage" and pending_key == "mortgage_program" and _is_mortgage_programs_question(user_text):
        reply = _mortgage_programs_prompt()
        msgs = state.get("messages", [])
        msgs = msgs + [HumanMessage(content=user_text)]
        msgs.append(AIMessage(content=reply))
        state["messages"] = msgs
        state["answer"] = reply
        return state

    profile = _ensure_defaults(flow, state.get("form", {}))
    profile = _update_profile_from_text(flow, user_text, profile, state.get("pending_question"))
    state["form"] = profile
    state["pending_question"] = None

    next_key, question = _find_next_question(flow, profile)

    msgs = state.get("messages", [])
    msgs = msgs + [HumanMessage(content=user_text)]

    if next_key and question:
        state["pending_question"] = next_key
        msgs.append(AIMessage(content=question))
        state["messages"] = msgs
        state["answer"] = question
        state["step"] = int(state.get("step") or 0) + 1
        return state

    reply, lead, quote = _finalize_reply_for_flow(flow, profile)
    if flow in ("mortgage", "auto_loan", "microloan"):
        reply += "\n\nМогу подготовить PDF с графиком выплат по кредиту. Отправить?"
        state["pending_pdf"] = True
        if quote:
            state["last_quote"] = quote

    msgs.append(AIMessage(content=reply))
    state["messages"] = msgs
    state["answer"] = reply
    state["lead"] = lead
    state["active_flow"] = None
    state["step"] = 0
    return state


def build_graph():
    graph = StateGraph(BotState)

    graph.add_node("classify_intent", node_classify_intent)
    graph.add_node("greeting", node_greeting)
    graph.add_node("qa", node_qa)
    graph.add_node("pdf", node_pdf)
    graph.add_node("start_flow", node_start_flow)
    graph.add_node("flow", node_flow)

    graph.set_entry_point("classify_intent")
    graph.add_conditional_edges("classify_intent", node_route, {
        "greeting": "greeting",
        "qa": "qa",
        "pdf": "pdf",
        "start_flow": "start_flow",
        "flow": "flow",
    })

    graph.add_edge("greeting", END)
    graph.add_edge("qa", END)
    graph.add_edge("pdf", END)
    graph.add_edge("start_flow", "flow")
    graph.add_edge("flow", END)

    checkpointer = MemorySaver()
    return graph.compile(checkpointer=checkpointer)


class LocalAgent:
    """
    In-process agent implementation based on the provided langgraph logic.
    """
    def __init__(self) -> None:
        self._graph = build_graph()

    def _invoke(self, session_id: str, user_text: str) -> str:
        state_in: BotState = {
            "chat_id": session_id,
            "last_user_text": user_text,
            "messages": [SystemMessage(content=SYSTEM_POLICY)],
        }
        out = self._graph.invoke(state_in, config={"configurable": {"thread_id": session_id}})
        return out.get("answer") or "Уточните, пожалуйста, ваш вопрос."

    async def send_message(self, session_id: str, user_id: int, text: str) -> str:
        # Run sync graph in a worker thread to avoid blocking event loop.
        return await asyncio.to_thread(self._invoke, session_id, text)


async def _cli_chat() -> None:
    """
    Simple CLI runner to test the agent without Telegram.
    """
    agent = LocalAgent()
    session_id = f"cli-{uuid.uuid4()}"
    print("CLI агент. Введите текст, 'exit' или Ctrl+C для выхода.")
    while True:
        try:
            user_text = input("Вы: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nВыход.")
            break
        if not user_text:
            continue
        if user_text.lower() in {"exit", "quit", "/exit", "/q"}:
            print("Выход.")
            break
        reply = await agent.send_message(session_id=session_id, user_id=0, text=user_text)
        print(f"Агент: {reply}")


if __name__ == "__main__":
    asyncio.run(_cli_chat())
