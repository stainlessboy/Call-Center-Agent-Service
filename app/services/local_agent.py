from __future__ import annotations

import asyncio
import difflib
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
from sqlalchemy import select

from app.db.models import FaqItem
from app.db.session import get_session

load_dotenv()

CALL_CENTER_PHONE = (os.getenv("CALL_CENTER_PHONE") or "").strip()
PDF_MARKER_PREFIX = "[[PDF:"
PDF_MARKER_SUFFIX = "]]"

Intent = Literal["greeting", "qa", "mortgage", "auto_loan", "microloan", "education", "service", "unknown"]


class IntentResult(BaseModel):
    intent: Intent = "unknown"
    confidence: float = 0.0
    reason: str = ""


class FieldExtractionResult(BaseModel):
    value: Optional[str] = None


class FaqLookupResult(BaseModel):
    matched: bool = False
    answer: Optional[str] = None


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
    last_quote_options: Optional[List[Dict[str, Any]]]

MORTGAGE_FLOW_QUESTIONS: Dict[str, Dict[str, Any]] = {
    "mortgage_purpose": {
        "required": True,
        "question": "Что планируете: покупка на первичном рынке, вторичном или ремонт? Можно написать «любая».",
    },
    "region_code": {
        "required": True,
        "question": "В каком регионе находится недвижимость: Ташкент или регионы?",
    },
    "requested_amount": {
        "required": True,
        "question": "Какую сумму кредита планируете (в сумах)?",
    },
    "requested_term_months": {
        "required": True,
        "question": "На какой срок планируете взять ипотеку (в годах или месяцах)?",
    },
    "downpayment_pct": {
        "required": True,
        "question": "Какой первоначальный взнос в процентах вы планируете?",
    },
    "mortgage_program_name": {
        "required": True,
        "question": "Какая программа интересует: рыночная, Универсал, Семья, DAHO, BI Group, NRG 2.4? Если не знаете — напишите «любая».",
    },
}

MICROLOAN_FLOW_QUESTIONS: Dict[str, Dict[str, Any]] = {
    "purpose_segment": {
        "required": True,
        "question": "Для личных целей или для бизнеса вы оформляете микрозайм?",
    },
    "requested_amount": {
        "required": True,
        "question": "Какую сумму микрозайма планируете (в сумах)?",
    },
    "requested_term_months": {
        "required": True,
        "question": "На какой срок планируете микрозайм (в месяцах)?",
    },
}

AUTO_LOAN_FLOW_QUESTIONS: Dict[str, Dict[str, Any]] = {
    "auto_product_query": {
        "required": True,
        "question": "Какую машину хотите купить? Укажите модель или программу: 2.5, онлайн, KIA Sonet, Damas, Onix, Tracker.",
    },
    "requested_amount": {
        "required": True,
        "question": "Какую сумму автокредита планируете (в сумах)?",
    },
    "requested_term_months": {
        "required": True,
        "question": "На какой срок планируете автокредит (можно в месяцах или годах)?",
    },
    "downpayment_pct": {
        "required": True,
        "question": "Какой первоначальный взнос в процентах вы планируете?",
    },
    "auto_income_type": {
        "required": True,
        "question": "Вы зарплатный клиент, с официальным доходом или без официального дохода (оборот по карте)?",
    },
}

EDUCATION_FLOW_QUESTIONS: Dict[str, Dict[str, Any]] = {
    "education_student": {
        "required": True,
        "question": "Вы обучаетесь на дневной форме (бакалавриат или магистратура)?",
    },
}

FLOW_QUESTION_BLOCKS: Dict[str, Dict[str, Dict[str, Any]]] = {
    "mortgage": MORTGAGE_FLOW_QUESTIONS,
    "microloan": MICROLOAN_FLOW_QUESTIONS,
    "auto_loan": AUTO_LOAN_FLOW_QUESTIONS,
    "education": EDUCATION_FLOW_QUESTIONS,
}

QUESTION_EXPLANATIONS: Dict[str, str] = {
    "auto_product_query": (
        "Нужно понять, под какую программу попадает ваша машина. "
        "Например: Онлайн автокредит, KIA Sonet, Damas, Onix, Tracker или 2.5."
    ),
    "requested_term_months": (
        "Срок можно указать в месяцах или годах. "
        "Например: «36 месяцев» или «3 года»."
    ),
    "requested_amount": (
        "Укажите сумму кредита цифрами. "
        "Например: «120 млн сум» или «120000000»."
    ),
    "downpayment_pct": "Это ваш первоначальный взнос в процентах от стоимости автомобиля или жилья.",
    "auto_income_type": (
        "Нужно выбрать один вариант: зарплатный клиент, официальный доход, "
        "или без официального дохода (оборот по карте)."
    ),
    "mortgage_purpose": "Для ипотеки важно направление: первичный рынок, вторичный рынок или ремонт.",
    "mortgage_program_name": "Можно выбрать конкретную программу или написать «любая».",
    "education_student": "Образовательный кредит предварительно доступен для очной формы обучения.",
}

LLM_FIELD_SPECS: Dict[str, Dict[str, Any]] = {
    "mortgage_purpose": {"type": "enum", "values": ["primary", "secondary", "repair", "any"]},
    "region_code": {"type": "enum", "values": ["tashkent", "regions"]},
    "requested_amount": {"type": "int"},
    "requested_term_months": {"type": "int_months"},
    "downpayment_pct": {"type": "int_pct"},
    "mortgage_program_name": {
        "type": "enum",
        "values": ["market", "universal", "family", "daho", "bi_group", "nrg_2_4", "any"],
    },
    "purpose_segment": {"type": "enum", "values": ["consumer", "business"]},
    "auto_product_query": {"type": "enum", "values": ["2_5", "online", "kia_sonet", "damas", "onix", "tracker"]},
    "auto_income_type": {"type": "enum", "values": ["payroll", "official", "no_official"]},
    "education_student": {"type": "bool"},
}


BANK_FAQ = [
    {
        "q": "какие документы нужны для ипотеки",
        "a": "Обычно требуются: паспорт, справка о доходах, документы на объект, заявление-анкета. Точный список зависит от программы."
    },
    {
        "q": "какие акции есть",
        "a": "Текущие акции зависят от региона и продукта. Я могу проверить акции по вашему продукту (ипотека/авто/микро/образовательный) и сумме."
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


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _normalize_text(text: str) -> str:
    lowered = text.lower()
    cleaned = re.sub(r"[^\w\s]+", " ", lowered, flags=re.UNICODE)
    return re.sub(r"\s+", " ", cleaned).strip()


def _token_stem(token: str) -> str:
    token = token.strip()
    if not token:
        return ""
    suffixes = (
        "ами",
        "ями",
        "ого",
        "ему",
        "ому",
        "ыми",
        "ими",
        "иях",
        "ах",
        "ях",
        "ов",
        "ев",
        "ей",
        "ой",
        "ый",
        "ий",
        "ая",
        "ое",
        "ые",
        "ую",
        "юю",
        "ам",
        "ям",
        "ом",
        "ем",
        "ах",
        "ях",
        "а",
        "я",
        "у",
        "ю",
        "е",
        "ы",
        "и",
    )
    for suffix in suffixes:
        if len(token) > 4 and token.endswith(suffix):
            return token[: -len(suffix)]
    return token


def _token_set(text: str) -> set[str]:
    normalized = _normalize_text(text)
    return {stem for stem in (_token_stem(t) for t in normalized.split()) if stem}


def _faq_similarity_score(query: str, variant: str) -> float:
    norm_query = _normalize_text(query)
    norm_variant = _normalize_text(variant)
    if not norm_query or not norm_variant:
        return 0.0
    if norm_variant in norm_query or norm_query in norm_variant:
        return 1.0

    seq_score = difflib.SequenceMatcher(a=norm_query, b=norm_variant).ratio()

    q_tokens = _token_set(norm_query)
    v_tokens = _token_set(norm_variant)
    if not q_tokens or not v_tokens:
        return seq_score
    overlap = len(q_tokens & v_tokens) / max(1, len(v_tokens))
    return max(seq_score, overlap)


@lru_cache(maxsize=1)
def _load_ai_chat_info() -> Dict[str, Any]:
    path = _project_root() / "app" / "data" / "ai_chat_info.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _get_credit_section_rows(section_name: str) -> List[List[Any]]:
    data = _load_ai_chat_info()
    try:
        section = data["workbook"]["credit_products"]["sections"][section_name]
    except (KeyError, TypeError):
        return []
    rows = section.get("rows_normalized") or []
    return [list(row) for row in rows if isinstance(row, list)]


async def _load_faq_db() -> List[Dict[str, str]]:
    async with get_session() as session:
        result = await session.execute(select(FaqItem.question, FaqItem.answer))
        rows = result.all()
    items: List[Dict[str, str]] = []
    for question, answer in rows:
        if not question or not answer:
            continue
        items.append({"q": str(question), "a": str(answer)})
    return items


def _load_faq_db_sync() -> List[Dict[str, str]]:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        try:
            return asyncio.run(_load_faq_db())
        except Exception:
            return []
    return []


def _faq_lookup_in_items(query: str, items: List[Dict[str, Any]]) -> Optional[str]:
    norm_query = _normalize_text(query)
    if not norm_query:
        return None
    best_answer = None
    best_score = 0.0
    best_len = 0
    for item in items:
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
            score = _faq_similarity_score(norm_query, norm_variant)
            if score < 0.58:
                continue
            if score > best_score or (abs(score - best_score) <= 0.03 and len(norm_variant) > best_len):
                best_answer = str(answer)
                best_score = score
                best_len = len(norm_variant)
    return best_answer


def _faq_lookup(query: str) -> Optional[str]:
    db_items = _load_faq_db_sync()
    if not db_items:
        return None
    answer = _faq_lookup_in_items(query, db_items)
    if answer:
        return answer
    return _faq_lookup_with_llm(query, db_items)


def _faq_lookup_with_llm(query: str, items: List[Dict[str, Any]]) -> Optional[str]:
    if not query.strip() or not items:
        return None

    prepared: List[str] = []
    for idx, item in enumerate(items, start=1):
        q = str(item.get("q") or "").strip()
        a = str(item.get("a") or "").strip()
        if not q or not a:
            continue
        aliases = item.get("aliases") or []
        alias_text = ", ".join(str(x).strip() for x in aliases if str(x).strip())
        if alias_text:
            prepared.append(f"{idx}. Вопрос: {q}\nСинонимы: {alias_text}\nОтвет: {a}")
        else:
            prepared.append(f"{idx}. Вопрос: {q}\nОтвет: {a}")

    if not prepared:
        return None

    prompt = (
        "Ты подбираешь один ответ только из FAQ банка. "
        "Если вопрос клиента соответствует одному из FAQ по смыслу, верни matched=true и ответ из FAQ. "
        "Если соответствия нет, верни matched=false. "
        "Не придумывай новые ответы."
    )
    faq_blob = "\n\n".join(prepared)

    try:
        llm = build_llm().with_structured_output(FaqLookupResult)
        result: FaqLookupResult = llm.invoke(
            [
                SystemMessage(content=prompt),
                SystemMessage(content=f"FAQ:\n{faq_blob}"),
                HumanMessage(content=f"Вопрос клиента: {query}"),
            ]
        )
    except Exception:
        return None

    if not result.matched:
        return None
    answer = (result.answer or "").strip()
    return answer or None


def _build_contextual_qa_query(state: BotState, user_text: str) -> str:
    current = (user_text or "").strip()
    if not current:
        return current
    lower = current.lower()
    short_or_followup = len(current) <= 50 or any(
        token in lower for token in ("это", "этого", "так", "такое", "такой", "подробнее", "а если", "а как")
    )
    if not short_or_followup:
        return current
    messages = state.get("messages", [])
    prev_human: Optional[str] = None
    for msg in reversed(messages):
        if isinstance(msg, HumanMessage):
            content = str(getattr(msg, "content", "")).strip()
            if content:
                prev_human = content
                break
    if not prev_human:
        return current
    return f"{prev_human}. {current}"


def _list_mortgage_program_labels() -> List[str]:
    return [
        "рыночная",
        "Универсал",
        "Семья",
        "DAHO",
        "BI Group",
        "NRG 2.4",
    ]


def _mortgage_programs_prompt() -> str:
    labels = _list_mortgage_program_labels()
    if labels:
        return (
            "Есть ипотечные программы: "
            + ", ".join(labels)
            + ". Если нет предпочтений, можно выбрать «любую». Какую программу рассматриваете?"
        )
    return "Какую ипотечную программу рассматриваете?"


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
        "авто",
        "машин",
        "автомоб",
        "автокредит",
    )
    return any(k in lower for k in keywords)


def _is_clarification_question(text: str) -> bool:
    lower = text.lower().strip()
    if any(phrase in lower for phrase in ("что значит", "а что значит", "что это значит", "можете объяснить")):
        return True
    if "?" in lower and any(
        token in lower
        for token in ("что", "как", "какой", "какая", "какие", "почему", "зачем", "то есть", "в смысле")
    ):
        return True
    starters = (
        "что",
        "как",
        "какой",
        "какая",
        "какие",
        "почему",
        "зачем",
        "то есть",
        "в смысле",
        "сколько",
    )
    return any(lower.startswith(s) for s in starters)


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
    if any(k in lower for k in ("образоват", "учеб", "контракт", "стипен")):
        return "education"
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


def _parse_mortgage_purpose(text: str, pending: bool = False) -> Optional[str]:
    lower = text.lower()
    if any(k in lower for k in ("ремонт", "ремонтир", "ремонтировать")):
        return "repair"
    if any(k in lower for k in ("вторич", "вторичка", "вторичный")):
        return "secondary"
    if any(k in lower for k in ("первич", "новост", "застрой", "новый дом")):
        return "primary"
    if pending and "люб" in lower:
        return "any"
    return None


def _parse_mortgage_program_name(text: str, pending: bool = False) -> Optional[str]:
    lower = text.lower()
    if "daho" in lower or "дахо" in lower:
        return "daho"
    if "bi" in lower or "би" in lower:
        return "bi_group"
    if "nrg" in lower:
        return "nrg_2_4"
    if "семь" in lower:
        return "family"
    if "универ" in lower:
        return "universal"
    if "рыноч" in lower or "стандарт" in lower or "обычн" in lower:
        return "market"
    if pending and "люб" in lower:
        return "any"
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


def _parse_auto_product_query(text: str, pending: bool = False) -> Optional[str]:
    lower = text.lower()
    if "sonet" in lower or "сонет" in lower or "kia" in lower or "киа" in lower:
        return "kia_sonet"
    if "damas" in lower or "дамас" in lower:
        return "damas"
    if "onix" in lower or "оникс" in lower:
        return "onix"
    if "tracker" in lower or "тракер" in lower:
        return "tracker"
    if "онлайн" in lower:
        return "online"
    if "2.5" in lower or "2,5" in lower or "2 5" in lower:
        return "2_5"
    if pending and lower.strip():
        return lower.strip()
    return None


def _parse_auto_income_type(text: str, pending: bool = False) -> Optional[str]:
    lower = text.lower()
    if "зарплат" in lower or "зп" in lower:
        return "payroll"
    if "без официаль" in lower or "без офиц" in lower or "оборот" in lower:
        return "no_official"
    if "официаль" in lower or "справка" in lower:
        return "official"
    if pending and lower.strip():
        return None
    return None


def _parse_education_student(text: str, pending: bool = False) -> Optional[bool]:
    lower = text.lower()
    if any(k in lower for k in ("бакалавр", "магистр", "студент", "обучаюсь", "дневн", "очная", "очное")):
        return True
    result = _parse_bool(text)
    if result is None and pending and lower.strip():
        return True
    return result


def _coerce_llm_field_value(key: str, raw: str) -> Any:
    spec = LLM_FIELD_SPECS.get(key) or {}
    value_type = spec.get("type")
    cleaned = raw.strip().lower()
    if not cleaned:
        return None

    if value_type == "enum":
        allowed = set(spec.get("values") or [])
        return cleaned if cleaned in allowed else None
    if value_type == "bool":
        if cleaned in {"true", "yes", "1", "да"}:
            return True
        if cleaned in {"false", "no", "0", "нет"}:
            return False
        return None
    if value_type == "int":
        return _parse_amount(cleaned, allow_plain_number=True)
    if value_type == "int_months":
        return _parse_term_months(cleaned, allow_plain_number=True)
    if value_type == "int_pct":
        return _parse_downpayment_pct(cleaned, allow_plain_number=True)
    return None


def _llm_parse_pending_answer(key: str, text: str, flow: Intent) -> Any:
    spec = LLM_FIELD_SPECS.get(key)
    if not spec:
        return None
    values = spec.get("values") or []
    values_text = ", ".join(values)
    prompt = (
        "Нормализуй ответ клиента в одно значение для поля анкеты. "
        f"flow={flow}, field={key}, field_type={spec.get('type')}. "
        "Если данных недостаточно или это встречный вопрос клиента, верни null. "
        "Не добавляй пояснений."
    )
    if values:
        prompt += f" Разрешенные значения: {values_text}."
    try:
        llm = build_llm().with_structured_output(FieldExtractionResult)
        res: FieldExtractionResult = llm.invoke([
            SystemMessage(content=prompt),
            HumanMessage(content=text),
        ])
    except Exception:
        return None
    if res.value is None:
        return None
    return _coerce_llm_field_value(key, str(res.value))


def _set_if_absent(profile: Dict[str, Any], key: str, value: Any) -> None:
    if value is None:
        return
    if profile.get(key) is None:
        profile[key] = value


def _ensure_defaults(flow: Intent, profile: Dict[str, Any]) -> Dict[str, Any]:
    if profile.get("credit_category") is None and flow in ("mortgage", "microloan", "auto_loan", "education"):
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
    if allow_plain:
        llm_value = _llm_parse_pending_answer(key, text, flow)
        if llm_value is not None:
            return llm_value
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
    if key == "mortgage_purpose":
        return _parse_mortgage_purpose(text, pending=allow_plain)
    if key == "mortgage_program_name":
        return _parse_mortgage_program_name(text, pending=allow_plain)
    if key == "region_code":
        return _parse_region_code(text, allow_plain=allow_plain)
    if key == "payroll_participant":
        return _parse_bool(text)
    if key == "auto_product_query":
        return _parse_auto_product_query(text, pending=allow_plain)
    if key == "auto_income_type":
        return _parse_auto_income_type(text, pending=allow_plain)
    if key == "education_student":
        return _parse_education_student(text, pending=allow_plain)
    return None


def _update_profile_from_text(
    flow: Intent,
    text: str,
    profile: Dict[str, Any],
    pending_key: Optional[str] = None,
) -> Dict[str, Any]:
    if pending_key:
        _set_if_absent(profile, pending_key, _parse_field_by_key(pending_key, text, flow, pending_key))
        if profile.get(pending_key) is None:
            return profile

    flow_block = FLOW_QUESTION_BLOCKS.get(flow) or {}
    for key in flow_block:
        _set_if_absent(profile, key, _parse_field_by_key(key, text, flow))

    return profile


def _find_next_question(flow: Intent, profile: Dict[str, Any]) -> tuple[Optional[str], Optional[str]]:
    block = FLOW_QUESTION_BLOCKS.get(flow) or {}
    for key, cfg in block.items():
        if profile.get(key) is None and cfg.get("required", False):
            return key, cfg.get("question")

    return None, None


def _clarification_reply(flow: Intent, key: str) -> Optional[str]:
    question = (FLOW_QUESTION_BLOCKS.get(flow) or {}).get(key, {}).get("question")
    explanation = QUESTION_EXPLANATIONS.get(key)
    if not question:
        return None
    if explanation:
        return f"{explanation}\n\n{question}"
    return question


def _flow_intro_text(flow: Intent) -> Optional[str]:
    if flow == "auto_loan":
        return "Запускаю подбор автокредита по вашим ответам. Задам несколько коротких вопросов."
    if flow == "mortgage":
        return "Запускаю подбор ипотеки по вашим ответам. Задам несколько коротких вопросов."
    if flow == "microloan":
        return "Запускаю подбор микрозайма по вашим ответам. Задам несколько коротких вопросов."
    if flow == "education":
        return "Запускаю подбор образовательного кредита по вашим ответам."
    return None


def _profile_summary_text(flow: Intent, profile: Dict[str, Any]) -> str:
    lines: List[str] = []
    if flow == "auto_loan":
        product = {
            "2_5": "Автокредит 2.5",
            "online": "Онлайн автокредит",
            "kia_sonet": "KIA Sonet",
            "damas": "Chevrolet Damas",
            "onix": "Chevrolet Onix",
            "tracker": "Chevrolet Tracker",
        }.get(str(profile.get("auto_product_query") or ""), str(profile.get("auto_product_query") or ""))
        income = {
            "payroll": "зарплатный клиент",
            "official": "официальный доход",
            "no_official": "без официального дохода (оборот по карте)",
        }.get(str(profile.get("auto_income_type") or ""), str(profile.get("auto_income_type") or ""))
        if product:
            lines.append(f"- Программа/модель: {product}")
        if profile.get("requested_amount") is not None:
            lines.append(f"- Сумма: {int(profile['requested_amount']):,}")
        if profile.get("requested_term_months") is not None:
            lines.append(f"- Срок: {int(profile['requested_term_months'])} мес.")
        if profile.get("downpayment_pct") is not None:
            lines.append(f"- Первоначальный взнос: {int(profile['downpayment_pct'])}%")
        if income:
            lines.append(f"- Доход: {income}")
    elif flow == "mortgage":
        purpose = {
            "primary": "первичный рынок",
            "secondary": "вторичный рынок",
            "repair": "ремонт",
            "any": "любой вариант",
        }.get(str(profile.get("mortgage_purpose") or ""), str(profile.get("mortgage_purpose") or ""))
        region = {"tashkent": "Ташкент", "regions": "Регионы"}.get(
            str(profile.get("region_code") or ""), str(profile.get("region_code") or "")
        )
        if purpose:
            lines.append(f"- Цель: {purpose}")
        if region:
            lines.append(f"- Регион: {region}")
        if profile.get("requested_amount") is not None:
            lines.append(f"- Сумма: {int(profile['requested_amount']):,}")
        if profile.get("requested_term_months") is not None:
            lines.append(f"- Срок: {int(profile['requested_term_months'])} мес.")
        if profile.get("downpayment_pct") is not None:
            lines.append(f"- Первоначальный взнос: {int(profile['downpayment_pct'])}%")
    elif flow == "microloan":
        segment = {"consumer": "личные цели", "business": "бизнес"}.get(
            str(profile.get("purpose_segment") or ""), str(profile.get("purpose_segment") or "")
        )
        if segment:
            lines.append(f"- Назначение: {segment}")
        if profile.get("requested_amount") is not None:
            lines.append(f"- Сумма: {int(profile['requested_amount']):,}")
        if profile.get("requested_term_months") is not None:
            lines.append(f"- Срок: {int(profile['requested_term_months'])} мес.")
    elif flow == "education":
        if profile.get("education_student") is not None:
            lines.append(f"- Очная форма: {'да' if profile.get('education_student') else 'нет'}")
    return "\n".join(lines)


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _parse_amount_range(text: str) -> Optional[tuple[int, int]]:
    if not text:
        return None
    lower = text.lower()
    if "%" in lower:
        return None
    nums = _extract_numbers(lower)
    if not nums:
        return None

    def to_amount(num: float, ctx: str) -> int:
        if "млн" in ctx and num < 1000:
            return int(num * 1_000_000)
        if "тыс" in ctx and num < 1000:
            return int(num * 1_000)
        return int(num)

    if len(nums) >= 2 and any(sep in lower for sep in ("-", "—")):
        return to_amount(nums[0], lower), to_amount(nums[1], lower)
    if "до" in lower:
        return 0, to_amount(nums[0], lower)
    value = to_amount(nums[0], lower)
    return value, value


def _parse_term_range(text: str) -> Optional[tuple[int, int]]:
    if not text:
        return None
    lower = text.lower()
    nums = _extract_numbers(lower)
    if not nums:
        return None
    unit = 12 if any(w in lower for w in ("год", "лет", "year")) else 1
    nums = [int(n * unit) for n in nums]
    if len(nums) >= 2 and any(sep in lower for sep in ("-", "—")):
        return nums[0], nums[1]
    if "до" in lower:
        return 0, nums[0]
    return nums[0], nums[0]


def _parse_percent_value(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        pct = float(value)
        if 0 < pct <= 1:
            pct *= 100
        return pct
    text = str(value).strip().lower()
    nums = _extract_numbers(text)
    if not nums:
        return None
    pct = float(nums[0])
    if pct <= 1 and ("%" not in text):
        pct *= 100
    return pct


def _rate_to_annual(rate_value: Any) -> Optional[float]:
    if rate_value is None:
        return None
    if isinstance(rate_value, str) and rate_value.strip() in {"-", "—"}:
        return None
    if isinstance(rate_value, (int, float)):
        val = float(rate_value)
    else:
        nums = _extract_numbers(str(rate_value))
        if not nums:
            return None
        val = min(nums)
    if val == 0:
        return None
    return val / 100.0 if val > 1 else val


def _format_rate(rate_value: Any) -> Optional[str]:
    if isinstance(rate_value, str):
        cleaned = " ".join([part.strip() for part in rate_value.splitlines() if part.strip()])
        if cleaned:
            return cleaned
    rate = _rate_to_annual(rate_value)
    if rate is None:
        return None
    return f"{rate * 100:.2f}%"


def _format_downpayment(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        pct = float(value)
        if 0 < pct <= 1:
            pct *= 100
        return f"{pct:.0f}%"
    text = str(value).strip()
    nums = _extract_numbers(text)
    if nums:
        pct = float(nums[0])
        if pct <= 1 and "%" not in text:
            pct *= 100
        return f"{pct:.0f}%"
    return text or None


def _matches_business(text: str) -> bool:
    lower = text.lower()
    return any(k in lower for k in ("предприним", "бизнес", "самозан", "ип"))


def _select_microloan_offers(profile: Dict[str, Any]) -> Dict[str, Any]:
    amount = profile.get("requested_amount")
    term = profile.get("requested_term_months")
    purpose = profile.get("purpose_segment")
    rows = _get_credit_section_rows("Микрозайм")
    offers: List[Dict[str, Any]] = []
    near_offers: List[Dict[str, Any]] = []
    for row in rows:
        name = _clean_text(row[0] if len(row) > 0 else None)
        if not name or name.lower() == "тип кредита":
            continue
        purpose_text = f"{_clean_text(row[2] if len(row) > 2 else '')} {name}"
        fail_reasons: List[str] = []
        if purpose == "business" and not _matches_business(purpose_text):
            fail_reasons.append("программа не относится к бизнес-целям")
        if purpose == "consumer" and _matches_business(purpose_text):
            fail_reasons.append("программа ориентирована на бизнес")
        amount_range = _parse_amount_range(_clean_text(row[3] if len(row) > 3 else ""))
        if amount is not None and amount_range:
            if not (amount_range[0] <= int(amount) <= amount_range[1]):
                fail_reasons.append(f"сумма должна быть в диапазоне {amount_range[0]:,} - {amount_range[1]:,}")
        term_range = _parse_term_range(_clean_text(row[4] if len(row) > 4 else ""))
        if term is not None and term_range:
            if not (term_range[0] <= int(term) <= term_range[1]):
                fail_reasons.append(f"срок должен быть в диапазоне {term_range[0]} - {term_range[1]} мес.")
        offer = {
            "name": name,
            "amount": _clean_text(row[3] if len(row) > 3 else None),
            "term": _clean_text(row[4] if len(row) > 4 else None),
            "rate": row[5] if len(row) > 5 else None,
            "collateral": _clean_text(row[7] if len(row) > 7 else None),
        }
        if fail_reasons:
            near_offers.append({"offer": offer, "reasons": fail_reasons})
            continue
        offers.append(offer)
    rate_annual = _rate_to_annual(offers[0].get("rate")) if offers else None
    return {"offers": offers, "near_offers": near_offers[:3], "rate_annual": rate_annual}


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


def _extract_region_limit(text: str, region_code: Optional[str], purpose: Optional[str]) -> Optional[int]:
    if not text:
        return None
    lower = text.lower()
    if "%" in lower:
        return None
    if purpose == "repair":
        if "ремонт" in lower:
            nums = _extract_numbers(lower)
            if nums:
                return int(nums[-1] * 1_000_000) if "млн" in lower and nums[-1] < 1000 else int(nums[-1])
        return None
    if region_code == "tashkent":
        idx = lower.find("ташкент")
        if idx != -1:
            nums = _extract_numbers(lower[idx: idx + 80])
            if nums:
                return int(nums[0] * 1_000_000) if "млн" in lower and nums[0] < 1000 else int(nums[0])
    if region_code == "regions":
        for key in ("област", "кара"):
            idx = lower.find(key)
            if idx != -1:
                nums = _extract_numbers(lower[idx: idx + 80])
                if nums:
                    return int(nums[0] * 1_000_000) if "млн" in lower and nums[0] < 1000 else int(nums[0])
    return None


def _match_mortgage_program(name: str, program: Optional[str]) -> bool:
    if not program or program == "any":
        return True
    lower = name.lower()
    mapping = {
        "daho": "daho",
        "bi_group": "bi",
        "nrg_2_4": "nrg",
        "family": "семья",
        "universal": "универс",
        "market": "рыноч",
    }
    token = mapping.get(program)
    return token is None or token in lower


def _match_mortgage_purpose(purpose_text: str, purpose: Optional[str]) -> bool:
    if not purpose or purpose == "any":
        return True
    lower = purpose_text.lower()
    if purpose == "primary":
        return "первич" in lower
    if purpose == "secondary":
        return "вторич" in lower
    if purpose == "repair":
        return "ремонт" in lower
    return True


def _select_mortgage_offers(profile: Dict[str, Any]) -> Dict[str, Any]:
    amount = profile.get("requested_amount")
    term = profile.get("requested_term_months")
    downpayment = profile.get("downpayment_pct")
    region_code = profile.get("region_code")
    purpose = profile.get("mortgage_purpose")
    program = profile.get("mortgage_program_name")

    rows = _get_credit_section_rows("Ипотека")
    offers: List[Dict[str, Any]] = []
    near_offers: List[Dict[str, Any]] = []
    for row in rows:
        name = _clean_text(row[0] if len(row) > 0 else None)
        if not name or name.lower() == "тип кредита":
            continue
        purpose_text = _clean_text(row[2] if len(row) > 2 else None)
        fail_reasons: List[str] = []
        if not _match_mortgage_purpose(purpose_text, purpose):
            fail_reasons.append("цель кредита не совпадает с выбранной программой")
        if not _match_mortgage_program(name, program):
            fail_reasons.append("другая ипотечная программа")

        amount_text = _clean_text(row[3] if len(row) > 3 else None)
        limit = _extract_region_limit(amount_text, region_code, purpose)
        if amount is not None and limit is not None:
            if int(amount) > limit:
                fail_reasons.append(f"максимальная сумма для условий программы: {limit:,}")

        term_range = _parse_term_range(_clean_text(row[4] if len(row) > 4 else None))
        if term is not None and term_range:
            if int(term) > term_range[1]:
                fail_reasons.append(f"максимальный срок: {term_range[1]} мес.")

        min_down = _parse_percent_value(row[5] if len(row) > 5 else None)
        if downpayment is not None and min_down is not None:
            if float(downpayment) < float(min_down):
                fail_reasons.append(f"минимальный взнос: {min_down:.0f}%")

        offer = {
            "name": name,
            "purpose": purpose_text,
            "amount": amount_text,
            "term": _clean_text(row[4] if len(row) > 4 else None),
            "downpayment": _clean_text(row[5] if len(row) > 5 else None),
            "rate": row[7] if len(row) > 7 else None,
            "collateral": _clean_text(row[8] if len(row) > 8 else None),
        }
        if fail_reasons:
            near_offers.append({"offer": offer, "reasons": fail_reasons})
            continue
        offers.append(offer)
    rate_annual = _rate_to_annual(offers[0].get("rate")) if offers else None
    return {"offers": offers, "near_offers": near_offers[:3], "rate_annual": rate_annual}


def _match_auto_product(name: str, query: Optional[str]) -> bool:
    if not query:
        return True
    lower = name.lower()
    mapping = {
        "kia_sonet": "sonet",
        "damas": "damas",
        "onix": "onix",
        "tracker": "tracker",
        "online": "онлайн",
        "2_5": "2.5",
    }
    token = mapping.get(query)
    if token:
        return token in lower
    return query.lower() in lower


def _select_auto_loan_offers(profile: Dict[str, Any]) -> Dict[str, Any]:
    term = profile.get("requested_term_months")
    downpayment = profile.get("downpayment_pct")
    income_type = profile.get("auto_income_type")
    query = profile.get("auto_product_query")
    rows = _get_credit_section_rows("Автокредит")
    offers: List[Dict[str, Any]] = []
    near_offers: List[Dict[str, Any]] = []
    for row in rows:
        name = _clean_text(row[0] if len(row) > 0 else None)
        if not name or name.lower() == "тип кредита":
            continue
        if not _match_auto_product(name, query):
            continue

        fail_reasons: List[str] = []
        term_range = _parse_term_range(_clean_text(row[4] if len(row) > 4 else None))
        if term is not None and term_range:
            if not (term_range[0] <= int(term) <= term_range[1]):
                fail_reasons.append(f"доступный срок по варианту: {term_range[0]} мес.")

        row_down = _parse_percent_value(row[5] if len(row) > 5 else None)
        if downpayment is not None and row_down is not None:
            if float(downpayment) < float(row_down):
                fail_reasons.append(f"минимальный взнос: {row_down:.0f}%")

        rate_value = None
        if income_type == "payroll":
            rate_value = row[6] if len(row) > 6 else None
        elif income_type == "official":
            rate_value = row[7] if len(row) > 7 else None
        elif income_type == "no_official":
            rate_value = row[8] if len(row) > 8 else None
        if income_type and _rate_to_annual(rate_value) is None:
            fail_reasons.append("для выбранного типа дохода ставка по этому варианту не указана")

        offer = {
            "name": name,
            "purpose": _clean_text(row[2] if len(row) > 2 else None),
            "amount": _clean_text(row[3] if len(row) > 3 else None),
            "term": _clean_text(row[4] if len(row) > 4 else None),
            "downpayment": _clean_text(row[5] if len(row) > 5 else None),
            "rate": rate_value,
            "collateral": _clean_text(row[9] if len(row) > 9 else None),
        }
        if fail_reasons:
            near_offers.append({"offer": offer, "reasons": fail_reasons})
            continue
        offers.append(offer)
    rate_annual = _rate_to_annual(offers[0].get("rate")) if offers else None
    return {"offers": offers, "near_offers": near_offers[:3], "rate_annual": rate_annual}


def _select_education_offer(profile: Dict[str, Any]) -> Dict[str, Any]:
    eligible = profile.get("education_student")
    rows = _get_credit_section_rows("Образовательный")
    offers: List[Dict[str, Any]] = []
    if eligible:
        for row in rows:
            name = _clean_text(row[0] if len(row) > 0 else None)
            if not name or name.lower() == "тип кредита":
                continue
            offers.append({
                "name": name,
                "purpose": _clean_text(row[2] if len(row) > 2 else None),
                "amount": _clean_text(row[3] if len(row) > 3 else None),
                "term": _clean_text(row[4] if len(row) > 4 else None),
                "rate": row[5] if len(row) > 5 else None,
                "collateral": _clean_text(row[7] if len(row) > 7 else None),
            })
    rate_annual = _rate_to_annual(offers[0].get("rate")) if offers else None
    return {"offers": offers, "rate_annual": rate_annual}


def _format_microloan_result(result: Dict[str, Any]) -> str:
    offers = result.get("offers") or []
    near_offers = result.get("near_offers") or []
    if not offers:
        if near_offers:
            lines = ["Точного совпадения по микрозайму нет. Ближайшие варианты:"]
            for idx, item in enumerate(near_offers[:3], start=1):
                offer = item.get("offer") or {}
                reasons = item.get("reasons") or []
                lines.append(f"{idx}) {offer.get('name', 'Программа')}")
                lines.extend([f"   - {reason}" for reason in reasons[:2]])
            return "\n".join(lines)
        return "По вашим параметрам микрозайм не найден. Могу передать запрос оператору."
    lines = ["Подобрал варианты микрозайма:"]
    for idx, offer in enumerate(offers[:3], start=1):
        parts = [f"{idx}) {offer['name']}"]
        if offer.get("amount"):
            parts.append(f"сумма: {offer['amount']}")
        if offer.get("term"):
            parts.append(f"срок: {offer['term']}")
        rate = _format_rate(offer.get("rate"))
        if rate:
            parts.append(f"ставка: {rate}")
        if offer.get("collateral"):
            parts.append(f"обеспечение: {offer['collateral']}")
        lines.append(" — ".join(parts))
    return "\n".join(lines)


def _format_mortgage_result(result: Dict[str, Any]) -> str:
    offers = result.get("offers") or []
    near_offers = result.get("near_offers") or []
    if not offers:
        if near_offers:
            lines = ["Точного совпадения по ипотеке нет. Ближайшие варианты:"]
            for idx, item in enumerate(near_offers[:3], start=1):
                offer = item.get("offer") or {}
                reasons = item.get("reasons") or []
                lines.append(f"{idx}) {offer.get('name', 'Программа')}")
                lines.extend([f"   - {reason}" for reason in reasons[:2]])
            return "\n".join(lines)
        return "По указанным параметрам ипотечные программы не найдены. Могу передать запрос оператору."
    lines = ["Подобрал ипотечные программы:"]
    for idx, offer in enumerate(offers[:3], start=1):
        parts = [f"{idx}) {offer['name']}"]
        if offer.get("amount"):
            parts.append(f"сумма: {offer['amount']}")
        if offer.get("term"):
            parts.append(f"срок: {offer['term']}")
        downpayment = _format_downpayment(offer.get("downpayment"))
        if downpayment:
            parts.append(f"взнос: {downpayment}")
        rate = _format_rate(offer.get("rate"))
        if rate:
            parts.append(f"ставка: {rate}")
        if offer.get("collateral"):
            parts.append(f"обеспечение: {offer['collateral']}")
        lines.append(" — ".join(parts))
    return "\n".join(lines)


def _format_auto_loan_result(result: Dict[str, Any], income_type: Optional[str]) -> str:
    offers = result.get("offers") or []
    near_offers = result.get("near_offers") or []
    if not offers:
        if near_offers:
            lines = ["Точного совпадения по автокредиту нет. Ближайшие варианты:"]
            for idx, item in enumerate(near_offers[:3], start=1):
                offer = item.get("offer") or {}
                reasons = item.get("reasons") or []
                lines.append(f"{idx}) {offer.get('name', 'Программа')}")
                if offer.get("term"):
                    lines.append(f"   - срок в варианте: {offer['term']}")
                downpayment = _format_downpayment(offer.get("downpayment"))
                if downpayment:
                    lines.append(f"   - взнос в варианте: {downpayment}")
                lines.extend([f"   - {reason}" for reason in reasons[:2]])
            return "\n".join(lines)
        return "По указанным параметрам автокредиты не найдены. Могу передать запрос оператору."
    lines = ["Подобрал варианты автокредита:"]
    income_label = {
        "payroll": "зарплатные клиенты",
        "official": "официальный доход",
        "no_official": "без официального дохода",
    }.get(income_type or "", None)
    for idx, offer in enumerate(offers[:3], start=1):
        parts = [f"{idx}) {offer['name']}"]
        if offer.get("term"):
            parts.append(f"срок: {offer['term']}")
        downpayment = _format_downpayment(offer.get("downpayment"))
        if downpayment:
            parts.append(f"взнос: {downpayment}")
        if income_label:
            rate = _format_rate(offer.get("rate"))
            if rate:
                parts.append(f"ставка ({income_label}): {rate}")
        if offer.get("collateral"):
            parts.append(f"обеспечение: {offer['collateral']}")
        lines.append(" — ".join(parts))
    return "\n".join(lines)


def _format_education_result(result: Dict[str, Any]) -> str:
    offers = result.get("offers") or []
    if not offers:
        return "Образовательный кредит доступен для студентов очной формы обучения. Если вы обучаетесь очно, уточните, пожалуйста."
    offer = offers[0]
    parts = [offer["name"]]
    if offer.get("amount"):
        parts.append(f"сумма: {offer['amount']}")
    if offer.get("term"):
        parts.append(f"срок: {offer['term']}")
    rate = _format_rate(offer.get("rate"))
    if rate:
        parts.append(f"ставка: {rate}")
    if offer.get("collateral"):
        parts.append(f"обеспечение: {offer['collateral']}")
    return "Образовательный кредит:\n" + " — ".join(parts)


def _collect_quote_options(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    options: List[Dict[str, Any]] = []
    offers = result.get("offers") or []
    for offer in offers[:3]:
        rate = _rate_to_annual(offer.get("rate"))
        if rate is None:
            continue
        label = str(offer.get("name") or "Вариант")
        term = _clean_text(offer.get("term"))
        down = _format_downpayment(offer.get("downpayment"))
        details: List[str] = []
        if term:
            details.append(f"срок {term}")
        if down:
            details.append(f"взнос {down}")
        if details:
            label = f"{label} ({', '.join(details)})"
        options.append({"label": label, "annual_rate": rate})
    return options


def _materialize_quote_options(flow: Intent, profile: Dict[str, Any], raw_options: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    try:
        principal = int(float(profile.get("requested_amount")))
        term_months = int(float(profile.get("requested_term_months")))
    except (TypeError, ValueError):
        return []

    options: List[Dict[str, Any]] = []
    for item in raw_options[:3]:
        rate = _rate_to_annual(item.get("annual_rate"))
        if rate is None:
            continue
        options.append(
            {
                "label": item.get("label") or "Вариант",
                "principal": principal,
                "term_months": term_months,
                "annual_rate": rate,
                "flow": flow,
            }
        )
    return options


def _build_quote(
    flow: Intent,
    profile: Dict[str, Any],
    rate_override: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    try:
        principal = int(float(profile.get("requested_amount"))) if profile.get("requested_amount") is not None else None
        term = int(float(profile.get("requested_term_months"))) if profile.get("requested_term_months") is not None else None
    except (TypeError, ValueError):
        return None

    if not principal or not term:
        return None

    annual_rate = rate_override
    if annual_rate is None:
        if flow == "mortgage":
            annual_rate = PRODUCTS["mortgage"]["rate_annual"]
        elif flow == "microloan":
            annual_rate = PRODUCTS["microloan"]["rate_annual"]
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


def _finalize_reply_for_flow(
    flow: Intent,
    profile: Dict[str, Any],
) -> tuple[str, Dict[str, Any], Optional[Dict[str, Any]], List[Dict[str, Any]]]:
    try:
        quote_options: List[Dict[str, Any]] = []
        if flow == "microloan":
            tool_json = microloan_selector.invoke({
                "requested_amount": profile.get("requested_amount"),
                "requested_term_months": profile.get("requested_term_months"),
                "purpose_segment": profile.get("purpose_segment"),
            })
            tool_data = json.loads(tool_json)
            reply = tool_data.get("text", "")
            has_offers = bool(tool_data.get("has_offers"))
            quote_options = _materialize_quote_options(flow, profile, tool_data.get("quote_options") or [])
            quote = _build_quote(flow, profile, rate_override=tool_data.get("rate_annual")) if has_offers else None
        elif flow == "mortgage":
            tool_json = mortgage_selector.invoke({
                "requested_amount": profile.get("requested_amount"),
                "requested_term_months": profile.get("requested_term_months"),
                "downpayment_pct": profile.get("downpayment_pct"),
                "region_code": profile.get("region_code"),
                "mortgage_purpose": profile.get("mortgage_purpose"),
                "mortgage_program_name": profile.get("mortgage_program_name"),
            })
            tool_data = json.loads(tool_json)
            reply = tool_data.get("text", "")
            has_offers = bool(tool_data.get("has_offers"))
            quote_options = _materialize_quote_options(flow, profile, tool_data.get("quote_options") or [])
            quote = _build_quote(flow, profile, rate_override=tool_data.get("rate_annual")) if has_offers else None
        elif flow == "auto_loan":
            tool_json = auto_loan_selector.invoke({
                "requested_term_months": profile.get("requested_term_months"),
                "downpayment_pct": profile.get("downpayment_pct"),
                "auto_income_type": profile.get("auto_income_type"),
                "auto_product_query": profile.get("auto_product_query"),
            })
            tool_data = json.loads(tool_json)
            reply = tool_data.get("text", "")
            has_offers = bool(tool_data.get("has_offers"))
            quote_options = _materialize_quote_options(flow, profile, tool_data.get("quote_options") or [])
            quote = _build_quote(flow, profile, rate_override=tool_data.get("rate_annual")) if has_offers else None
        elif flow == "education":
            tool_json = education_selector.invoke({
                "education_student": profile.get("education_student"),
            })
            tool_data = json.loads(tool_json)
            reply = tool_data.get("text", "")
            quote = None
            quote_options = []
        else:
            reply = "Зафиксировал запрос по услуге. Могу передать оператору для уточнения деталей."
            quote = None
            quote_options = []
    except FileNotFoundError as exc:
        reply = f"Не нашёл справочник продуктов ({exc}). Передам запрос оператору."
        quote = None
        quote_options = []

    payload = {"flow": flow, "profile": profile}
    lead_json = create_lead.invoke({
        "product_type": flow,
        "payload_json": json.dumps(payload, ensure_ascii=False),
        "summary": f"Запрос клиента: {flow}. Профиль: {json.dumps(profile, ensure_ascii=False)}",
    })
    lead = json.loads(lead_json)
    reply = reply + f"\n\nНомер обращения (демо): {lead['lead_id']}"
    if quote and not quote_options:
        quote_options = [dict(quote, label="Вариант 1")]
    return reply, lead, quote, quote_options


def _tool_response(
    text: str,
    rate_annual: Optional[float] = None,
    has_offers: bool = False,
    quote_options: Optional[List[Dict[str, Any]]] = None,
) -> str:
    return json.dumps(
        {
            "text": text,
            "rate_annual": rate_annual,
            "has_offers": has_offers,
            "quote_options": quote_options or [],
        },
        ensure_ascii=False,
    )


@tool
def microloan_selector(
    requested_amount: Optional[int] = None,
    requested_term_months: Optional[int] = None,
    purpose_segment: Optional[str] = None,
) -> str:
    """Select microloan offers from AI chat info."""
    profile = {
        "requested_amount": requested_amount,
        "requested_term_months": requested_term_months,
        "purpose_segment": purpose_segment,
    }
    result = _select_microloan_offers(profile)
    text = _format_microloan_result(result)
    quote_options = _collect_quote_options(result)
    return _tool_response(
        text,
        result.get("rate_annual"),
        has_offers=bool(result.get("offers")),
        quote_options=quote_options,
    )


@tool
def mortgage_selector(
    requested_amount: Optional[int] = None,
    requested_term_months: Optional[int] = None,
    downpayment_pct: Optional[int] = None,
    region_code: Optional[str] = None,
    mortgage_purpose: Optional[str] = None,
    mortgage_program_name: Optional[str] = None,
) -> str:
    """Select mortgage offers from AI chat info."""
    profile = {
        "requested_amount": requested_amount,
        "requested_term_months": requested_term_months,
        "downpayment_pct": downpayment_pct,
        "region_code": region_code,
        "mortgage_purpose": mortgage_purpose,
        "mortgage_program_name": mortgage_program_name,
    }
    result = _select_mortgage_offers(profile)
    text = _format_mortgage_result(result)
    quote_options = _collect_quote_options(result)
    return _tool_response(
        text,
        result.get("rate_annual"),
        has_offers=bool(result.get("offers")),
        quote_options=quote_options,
    )


@tool
def auto_loan_selector(
    requested_term_months: Optional[int] = None,
    downpayment_pct: Optional[int] = None,
    auto_income_type: Optional[str] = None,
    auto_product_query: Optional[str] = None,
) -> str:
    """Select auto loan offers from AI chat info."""
    profile = {
        "requested_term_months": requested_term_months,
        "downpayment_pct": downpayment_pct,
        "auto_income_type": auto_income_type,
        "auto_product_query": auto_product_query,
    }
    result = _select_auto_loan_offers(profile)
    text = _format_auto_loan_result(result, auto_income_type)
    quote_options = _collect_quote_options(result)
    return _tool_response(
        text,
        result.get("rate_annual"),
        has_offers=bool(result.get("offers")),
        quote_options=quote_options,
    )


@tool
def education_selector(education_student: Optional[bool] = None) -> str:
    """Select education loan offer from AI chat info."""
    profile = {"education_student": education_student}
    result = _select_education_offer(profile)
    text = _format_education_result(result)
    quote_options = _collect_quote_options(result)
    return _tool_response(
        text,
        result.get("rate_annual"),
        has_offers=bool(result.get("offers")),
        quote_options=quote_options,
    )


@tool
def bank_kb_search(query: str) -> str:
    """Search in bank FAQ/KB and return best answer snippet."""
    faq_answer = _faq_lookup(query)
    if faq_answer:
        return faq_answer
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
- Если клиент хочет оформить ипотеку/автокредит/микрозайм/образовательный кредит/услугу — задай уточняющие вопросы по сценарию.
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
            "greeting, qa, mortgage, auto_loan, microloan, education, service, unknown.\n"
            "Если это приветствие/начало диалога (привет, здравствуйте, салом, ассалом, доброе утро и т.п.) — greeting.\n"
            "Если пользователь просит документы/акции/условия/расчёт без явного оформления — чаще qa.\n"
            "Вопросы про филиалы, отделения, режим работы, адреса — это qa, а не сценарий оформления.\n"
            "Вопросы про доступ в приложение, пароль/логин, блокировку карты, выписку/баланс — это qa.\n"
            "Если вопрос не относится к банку или его услугам — unknown.\n"
            "Если явно: 'хочу ипотеку/оформить/подать заявку' — mortgage.\n"
            "Если 'автокредит/машина' — auto_loan. 'микрозайм/микрокредит' — microloan.\n"
            "Если 'образовательный кредит/контракт/учёба' — education.\n"
            "Если 'карта/перевод/депозит/услуга/счёт' — service."
        )),
        HumanMessage(content=user_text),
    ]
    res: IntentResult = llm.invoke(msgs)
    heuristic_intent = _parse_credit_category(user_text)
    resolved_intent = res.intent
    resolved_confidence = float(res.confidence)
    if heuristic_intent in ("mortgage", "auto_loan", "microloan", "education"):
        if resolved_intent in ("unknown", "qa", "service") and _has_purchase_signal(user_text):
            resolved_intent = heuristic_intent  # type: ignore[assignment]
            resolved_confidence = max(resolved_confidence, 0.65)
    state["intent"] = resolved_intent
    state["intent_confidence"] = resolved_confidence
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
    inferred_credit = _parse_credit_category(user_text)
    if (
        intent in ("unknown", "qa", "service")
        and inferred_credit in ("mortgage", "auto_loan", "microloan", "education")
        and _has_purchase_signal(user_text)
    ):
        state["intent"] = inferred_credit  # type: ignore[assignment]
        state["intent_confidence"] = max(confidence, 0.6)
        intent = inferred_credit
        confidence = float(state["intent_confidence"])

    if intent == "greeting":
        return "greeting"

    if (
        intent in ("mortgage", "auto_loan", "microloan", "education", "service")
        and (confidence >= 0.4 or _has_purchase_signal(user_text))
        and _is_bank_related(user_text)
    ):
        return "start_flow"

    return "qa"


def node_greeting(state: BotState) -> BotState:
    user_text = state["last_user_text"]
    reply = (
        "Здравствуйте. Я помогу с вопросами по продуктам и услугам банка.\n"
        "Подскажите, пожалуйста, что вас интересует: условия кредита (ипотека/авто/микро/образовательный), "
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
    contextual_query = _build_contextual_qa_query(state, user_text)
    faq_answer = _faq_lookup(user_text) or _faq_lookup(contextual_query)

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
        pt = (
            "mortgage"
            if "ипот" in lower
            else "auto_loan"
            if "авто" in lower
            else "microloan"
            if "микро" in lower
            else "education"
            if "образов" in lower
            else "mortgage"
        )
        answer = get_active_promos.invoke({"product_type": pt})
    elif any(k in lower for k in ["посч", "платеж", "платёж", "расчет", "расчёт", "калькул"]):
        answer = "Для расчёта подскажите, пожалуйста: тип продукта (ипотека/авто/микро/образовательный), сумму кредита и срок (в месяцах)."
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
    quote_options = state.get("last_quote_options") or []
    choice_nums = _extract_numbers(user_text)
    selected_index: Optional[int] = None
    if quote_options and choice_nums:
        candidate = int(choice_nums[0])
        if 1 <= candidate <= len(quote_options):
            selected_index = candidate - 1
            state["last_quote"] = quote_options[selected_index]
    form = state.get("form", {})
    if form.get("requested_amount") is None:
        parsed_amount = _parse_amount(user_text, allow_plain_number=True)
        if parsed_amount is not None:
            form["requested_amount"] = parsed_amount
    if form.get("requested_term_months") is None:
        parsed_term = _parse_term_months(user_text, allow_plain_number=True)
        if parsed_term is not None:
            form["requested_term_months"] = parsed_term
    state["form"] = form
    decision = True if selected_index is not None else _parse_bool(user_text)
    if decision is None and _is_pdf_request(user_text):
        decision = True

    if decision is False:
        reply = "Хорошо, если понадобится PDF — скажите."
        state["pending_pdf"] = False
    elif decision is True:
        quote = state.get("last_quote")
        if not quote:
            flow = form.get("credit_category")
            if flow in ("mortgage", "auto_loan", "microloan"):
                quote = _build_quote(flow, form)
            if quote:
                state["last_quote"] = quote
        if not quote and quote_options:
            quote = quote_options[0]
            state["last_quote"] = quote
        if not quote:
            missing: List[str] = []
            if form.get("requested_amount") is None:
                missing.append("сумма кредита")
            if form.get("requested_term_months") is None:
                missing.append("срок кредита")
            if missing:
                reply = "Для PDF уточните, пожалуйста: " + " и ".join(missing) + "."
            else:
                reply = "Не удалось рассчитать параметры для PDF. Уточните сумму и срок кредита."
            state["pending_pdf"] = True
        else:
            try:
                pdf_path = _generate_payment_pdf(quote)
                reply = _with_pdf_marker("Готово. Отправляю PDF с графиком выплат.", pdf_path)
            except Exception:
                reply = "Не удалось подготовить PDF. Попробуйте позже."
            state["pending_pdf"] = False
    else:
        if quote_options:
            choices = [f"{idx}) {opt.get('label', 'Вариант')}" for idx, opt in enumerate(quote_options, start=1)]
            reply = (
                "Укажите номер варианта для PDF:\n"
                + "\n".join(choices)
                + "\n\nИли напишите «да», чтобы взять первый вариант."
            )
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
    flow: Intent = intent if intent in ("mortgage", "auto_loan", "microloan", "education", "service") else None
    state["active_flow"] = flow
    state["step"] = 0
    state["form"] = _ensure_defaults(flow or "unknown", state.get("form", {}))
    state["pending_question"] = None
    state["pending_pdf"] = False
    state["last_quote_options"] = []
    return state


def node_flow(state: BotState) -> BotState:
    user_text = state["last_user_text"]
    flow: Intent = state.get("active_flow") or "unknown"
    pending_key = state.get("pending_question")
    is_special_mortgage_question = (
        flow == "mortgage"
        and pending_key == "mortgage_purpose"
        and _is_housing_difference_question(user_text)
    )
    if pending_key and _is_clarification_question(user_text) and not is_special_mortgage_question:
        clarification = _clarification_reply(flow, pending_key)
        if clarification:
            msgs = state.get("messages", [])
            msgs = msgs + [HumanMessage(content=user_text)]
            msgs.append(AIMessage(content=clarification))
            state["messages"] = msgs
            state["answer"] = clarification
            return state
    if flow == "mortgage" and pending_key == "mortgage_purpose" and _is_housing_difference_question(user_text):
        reply = _explain_primary_secondary()
        msgs = state.get("messages", [])
        msgs = msgs + [HumanMessage(content=user_text)]
        msgs.append(AIMessage(content=reply))
        state["messages"] = msgs
        state["answer"] = reply
        return state
    if flow == "mortgage" and pending_key == "mortgage_program_name" and _is_mortgage_programs_question(user_text):
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
        step_num = int(state.get("step") or 0)
        intro = _flow_intro_text(flow) if step_num == 0 else None
        question_text = f"{intro}\n\n{question}" if intro else question
        msgs.append(AIMessage(content=question_text))
        state["messages"] = msgs
        state["answer"] = question_text
        state["step"] = step_num + 1
        return state

    reply, lead, quote, quote_options = _finalize_reply_for_flow(flow, profile)
    if flow in ("mortgage", "auto_loan", "microloan", "education"):
        summary = _profile_summary_text(flow, profile)
        if summary:
            reply = "Основываясь на ваших ответах:\n" + summary + "\n\n" + reply
    if flow in ("mortgage", "auto_loan", "microloan") and quote:
        if quote_options and len(quote_options) > 1:
            choice_lines = [f"{idx}) {opt.get('label', 'Вариант')}" for idx, opt in enumerate(quote_options, start=1)]
            reply += "\n\nДля PDF выберите вариант:\n" + "\n".join(choice_lines)
            reply += "\n\nНапишите номер варианта (1, 2, 3) или «да» для первого."
        else:
            reply += "\n\nМогу подготовить PDF с графиком выплат по кредиту. Отправить?"
        state["pending_pdf"] = True
        state["last_quote"] = quote
        state["last_quote_options"] = quote_options
    else:
        state["pending_pdf"] = False
        state["last_quote_options"] = []

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
