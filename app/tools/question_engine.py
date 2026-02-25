from __future__ import annotations

import re
from collections import OrderedDict
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Credit product question schemas
# ---------------------------------------------------------------------------

GENERAL_QUESTIONS: OrderedDict[str, dict[str, Any]] = OrderedDict([
    ("credit_category", {
        "values": ["mortgage", "microloan", "autoloan", "education_credit"],
        "required": True,
        "q": "Какой тип кредита вас интересует — ипотека, автокредит, микрозайм или образовательный кредит?",
    }),
    ("citizen_uz", {
        "values": ["yes", "no"],
        "required": True,
        "q": "Вы являетесь гражданином Республики Узбекистан?",
    }),
    ("age", {
        "values": "integer",
        "required": True,
        "q": "Сколько вам полных лет?",
    }),
    ("gender", {
        "values": ["male", "female"],
        "required": True,
        "q": "Ваш пол по паспорту — мужской или женский?",
    }),
    ("income_proof", {
        "values": ["yes", "no"],
        "required": True,
        "q": "Есть ли у вас официально подтверждённый доход (справка о зарплате, налоговая декларация)?",
    }),
    ("self_employed", {
        "values": ["yes", "no"],
        "required": True,
        "q": "Вы зарегистрированы как самозанятый (ИП или самозанятый)?",
    }),
    ("requested_amount", {
        "values": "integer",
        "required": True,
        "q": "Какую сумму кредита вы планируете взять (в сумах)?",
    }),
    ("requested_term_months", {
        "values": "integer",
        "required": True,
        "q": "На какой срок планируете взять кредит? Укажите в годах или месяцах.",
    }),
    ("downpayment_pct", {
        "values": "integer",
        "required": False,
        "only_for": ["mortgage", "autoloan"],
        "q": "Какой первоначальный взнос вы планируете (в процентах от стоимости)?",
    }),
])

MORTGAGE_QUESTIONS: OrderedDict[str, dict[str, Any]] = OrderedDict([
    ("purpose_keys", {
        "values": ["housing_primary", "housing_secondary"],
        "required": True,
        "q": "Вас интересует новостройка (первичный рынок) или вторичный рынок?",
    }),
    ("mortgage_program", {
        "values": ["bi_group", "nrg_2_4", "daho", "standard", "none"],
        "required": True,
        "q": "Какую ипотечную программу рассматриваете? (BI Group, NRG 2-4%, DAFO, стандарт или ещё не знаю)",
    }),
    ("region_code", {
        "values": ["tashkent", "regions"],
        "required": True,
        "q": "В каком регионе находится недвижимость — Ташкент или другой регион?",
    }),
])

MICROLOAN_QUESTIONS: OrderedDict[str, dict[str, Any]] = OrderedDict([
    ("purpose_segment", {
        "values": ["consumer", "business"],
        "required": True,
        "q": "Микрозайм для личных целей или для бизнеса?",
    }),
    ("purpose_keys", {
        "values": ["personal_any", "business_start", "business_support"],
        "required": True,
        "q": "Уточните цель: личные нужды, открытие бизнеса или поддержка действующего бизнеса?",
    }),
    ("payroll_participant", {
        "values": ["yes", "no"],
        "required": True,
        "q": "Вы получаете зарплату через наш банк?",
    }),
])

EDUCATION_QUESTIONS: OrderedDict[str, dict[str, Any]] = OrderedDict([
    ("study_level", {
        "values": ["bachelor", "master", "vocational"],
        "required": True,
        "q": "Уровень обучения — бакалавриат, магистратура или профессиональное образование?",
    }),
    ("institution_type", {
        "values": ["state", "private"],
        "required": True,
        "q": "Государственный или частный вуз/учреждение?",
    }),
])

# autoloan uses only GENERAL_QUESTIONS (no additional block)
SERVICE_QUESTION_BLOCKS: dict[str, OrderedDict[str, dict[str, Any]]] = {
    "mortgage": MORTGAGE_QUESTIONS,
    "microloan": MICROLOAN_QUESTIONS,
    "education_credit": EDUCATION_QUESTIONS,
}

# ---------------------------------------------------------------------------
# Non-credit question schemas
# ---------------------------------------------------------------------------

DEPOSIT_QUESTIONS: OrderedDict[str, dict[str, Any]] = OrderedDict([
    ("deposit_goal", {
        "values": ["save", "income"],
        "required": True,
        "q": "Цель вклада — накопление средств или получение ежемесячного дохода?",
    }),
    ("deposit_currency", {
        "values": ["UZS", "USD", "EUR"],
        "required": True,
        "q": "В какой валюте хотите открыть вклад — сумы (UZS), доллары (USD) или евро (EUR)?",
    }),
    ("deposit_term_months", {
        "values": "integer",
        "required": True,
        "q": "На какой срок планируете вклад? Укажите в месяцах.",
    }),
    ("deposit_topup_needed", {
        "values": ["yes", "no"],
        "required": True,
        "q": "Нужна ли возможность пополнять вклад в течение срока?",
    }),
    ("deposit_payout_pref", {
        "values": ["monthly", "end"],
        "required": True,
        "q": "Как предпочитаете получать проценты — ежемесячно или в конце срока?",
    }),
])

CARD_QUESTIONS: OrderedDict[str, dict[str, Any]] = OrderedDict([
    ("card_type", {
        "values": ["debit", "fx"],
        "required": True,
        "q": "Вам нужна обычная дебетовая карта (UZS) или валютная карта для поездок за рубеж?",
    }),
    ("card_purpose", {
        "values": ["shopping_transfers", "travel"],
        "required": True,
        "q": "Для каких целей в основном — покупки и переводы или поездки за границу?",
    }),
    ("card_network", {
        "values": ["visa", "mastercard"],
        "required": False,
        "only_for_card_type": ["fx"],
        "q": "Какая платёжная система предпочтительнее — VISA или Mastercard?",
    }),
    ("card_currency", {
        "values": ["USD", "EUR"],
        "required": False,
        "only_for_card_type": ["fx"],
        "q": "В какой валюте карта — доллары (USD) или евро (EUR)?",
    }),
])

NON_CREDIT_QUESTION_BLOCKS: dict[str, OrderedDict[str, dict[str, Any]]] = {
    "deposit": DEPOSIT_QUESTIONS,
    "card": CARD_QUESTIONS,
}


# ---------------------------------------------------------------------------
# Question engine logic
# ---------------------------------------------------------------------------

def _is_applicable(key: str, spec: dict[str, Any], slots: dict[str, Any]) -> bool:
    """Check if a question is applicable given current slots."""
    only_for = spec.get("only_for")
    if only_for:
        product_type = slots.get("credit_category")
        if product_type not in only_for:
            return False
    only_for_card = spec.get("only_for_card_type")
    if only_for_card:
        card_type = slots.get("card_type")
        if card_type not in only_for_card:
            return False
    return True


def get_next_credit_question(slots: dict[str, Any]) -> tuple[Optional[str], Optional[str]]:
    """
    Returns (question_key, question_text) for the next unanswered required question.
    Returns (None, None) if all required questions are answered.
    """
    product_type = slots.get("credit_category")

    # First pass: GENERAL_QUESTIONS
    for key, spec in GENERAL_QUESTIONS.items():
        if key in slots:
            continue
        if not _is_applicable(key, spec, slots):
            continue
        if spec["required"] or (not spec["required"] and _is_applicable(key, spec, slots)):
            if spec["required"]:
                return key, spec["q"]

    # Second pass: SERVICE_QUESTION_BLOCKS for the product type
    if product_type and product_type in SERVICE_QUESTION_BLOCKS:
        for key, spec in SERVICE_QUESTION_BLOCKS[product_type].items():
            if key in slots:
                continue
            if spec["required"]:
                return key, spec["q"]

    return None, None


def get_next_noncredit_question(slots: dict[str, Any], service_type: str) -> tuple[Optional[str], Optional[str]]:
    """Returns (question_key, question_text) for the next unanswered required question for deposits/cards."""
    questions = NON_CREDIT_QUESTION_BLOCKS.get(service_type, OrderedDict())
    for key, spec in questions.items():
        if key in slots:
            continue
        if not _is_applicable(key, spec, slots):
            continue
        if spec["required"]:
            return key, spec["q"]
    return None, None


# ---------------------------------------------------------------------------
# Slot extraction from user text
# ---------------------------------------------------------------------------

def _extract_yes_no(text: str) -> Optional[str]:
    lower = text.lower()
    yes_tokens = ("да", "yes", "конечно", "являюсь", "есть", "имею", "зарегистрир", "получаю", "гражданин")
    no_tokens = ("нет", "no", "не являюсь", "не имею", "не зарегистр", "не получаю", "не гражданин")
    for t in yes_tokens:
        if t in lower:
            return "yes"
    for t in no_tokens:
        if t in lower:
            return "no"
    return None


def _extract_integer(text: str) -> Optional[int]:
    """Extract first meaningful integer from text (handles units like млн, тыс)."""
    lower = text.lower().replace(",", ".").replace(" ", "")
    # Handle million
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:млн|миллион)", lower)
    if m:
        return int(float(m.group(1)) * 1_000_000)
    # Handle thousand
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:тыс|тысяч)", lower)
    if m:
        return int(float(m.group(1)) * 1_000)
    # Handle years → months conversion
    m = re.search(r"(\d+)\s*(?:год|лет)", lower)
    if m:
        return int(m.group(1)) * 12
    # Plain integer
    m = re.search(r"\d+", lower)
    if m:
        return int(m.group(0))
    return None


def extract_slot_value(question_key: str, text: str) -> Any:
    """
    Extract slot value from user message text for a given question key.
    Returns extracted value or None if cannot extract.
    """
    lower = text.lower()

    if question_key == "credit_category":
        if any(t in lower for t in ("ипотек", "жильё", "квартир", "недвижим", "жилье")):
            return "mortgage"
        if any(t in lower for t in ("автокредит", "авто", "машин", "автомобил")):
            return "autoloan"
        if any(t in lower for t in ("микрозайм", "микрокредит", "микро")):
            return "microloan"
        if any(t in lower for t in ("образоват", "учеб", "вуз", "контракт", "студент")):
            return "education_credit"
        return None

    elif question_key in ("citizen_uz", "income_proof", "self_employed", "payroll_participant",
                          "deposit_topup_needed"):
        return _extract_yes_no(text)

    elif question_key == "age":
        # Use direct regex to avoid _extract_integer's years->months conversion
        m = re.search(r"\b(\d{1,3})\b", text.lower())
        if m:
            val = int(m.group(1))
            if 14 <= val <= 100:
                return val
        return None

    elif question_key == "requested_amount":
        lower2 = text.lower()
        m = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:млн|миллион)", lower2)
        if m:
            return int(float(m.group(1).replace(",", ".")) * 1_000_000)
        m = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:тыс|тысяч)", lower2)
        if m:
            return int(float(m.group(1).replace(",", ".")) * 1_000)
        m = re.search(r"\d[\d\s]{4,}", lower2)  # Large number with spaces
        if m:
            return int(re.sub(r"\s", "", m.group(0)))
        m = re.search(r"\d+", lower2)
        if m:
            v = int(m.group(0))
            if v >= 100:
                return v
        return None

    elif question_key == "requested_term_months":
        m = re.search(r"(\d+)\s*(?:год|лет)", lower)
        if m:
            return int(m.group(1)) * 12
        m = re.search(r"(\d+)\s*(?:мес|месяц)", lower)
        if m:
            return int(m.group(1))
        m = re.search(r"\d+", lower)
        if m:
            v = int(m.group(0))
            # Heuristic: if <= 30, treat as years
            if v <= 30:
                return v * 12
            return v
        return None

    elif question_key == "deposit_term_months":
        m = re.search(r"(\d+)\s*(?:год|лет)", lower)
        if m:
            return int(m.group(1)) * 12
        m = re.search(r"(\d+)\s*(?:мес|месяц)", lower)
        if m:
            return int(m.group(1))
        m = re.search(r"\d+", lower)
        if m:
            return int(m.group(0))
        return None

    elif question_key == "downpayment_pct":
        m = re.search(r"(\d+)\s*%", lower)
        if m:
            return int(m.group(1))
        m = re.search(r"\d+", lower)
        if m:
            v = int(m.group(0))
            if 0 <= v <= 100:
                return v
        return None

    elif question_key == "gender":
        if any(t in lower for t in ("мужск", "мужчин", "male", "м")):
            return "male"
        if any(t in lower for t in ("женск", "женщин", "female", "ж")):
            return "female"
        return None

    elif question_key == "purpose_keys":
        if any(t in lower for t in ("первич", "новостройк", "новострой")):
            return "housing_primary"
        if any(t in lower for t in ("вторич",)):
            return "housing_secondary"
        if any(t in lower for t in ("личн", "себе", "нужды")):
            return "personal_any"
        if any(t in lower for t in ("открыт", "стартап", "начать бизнес")):
            return "business_start"
        if any(t in lower for t in ("поддержк", "действующ", "развит")):
            return "business_support"
        return None

    elif question_key == "mortgage_program":
        if any(t in lower for t in ("bi group", "bi-group", "бигруп", "бигруп")):
            return "bi_group"
        if any(t in lower for t in ("nrg", "2-4", "2.4", "нрг")):
            return "nrg_2_4"
        if any(t in lower for t in ("daho", "dafo", "дахо", "дафо")):
            return "daho"
        if any(t in lower for t in ("стандарт", "обычн", "стандартн")):
            return "standard"
        if any(t in lower for t in ("не знаю", "нет", "любой", "любую")):
            return "none"
        return None

    elif question_key == "region_code":
        if any(t in lower for t in ("ташкент", "tashkent", "столиц", "capital")):
            return "tashkent"
        if any(t in lower for t in ("регион", "область", "другой", "самарканд", "бухар",
                                     "андижан", "фергана", "наманган", "нукус", "провинц")):
            return "regions"
        return None

    elif question_key == "purpose_segment":
        if any(t in lower for t in ("бизнес", "предприним", "компани", "фирм")):
            return "business"
        if any(t in lower for t in ("личн", "себе", "потребит", "нужд")):
            return "consumer"
        return None

    elif question_key == "study_level":
        if any(t in lower for t in ("бакалавр", "bachelor")):
            return "bachelor"
        if any(t in lower for t in ("магистр", "master")):
            return "master"
        if any(t in lower for t in ("профессиональн", "колледж", "профтех", "vocational")):
            return "vocational"
        return None

    elif question_key == "institution_type":
        if any(t in lower for t in ("государств", "гос.", "публичн")):
            return "state"
        if any(t in lower for t in ("частн", "платн", "коммерч")):
            return "private"
        return None

    elif question_key == "deposit_goal":
        if any(t in lower for t in ("накопл", "копить", "сохранить", "накапл")):
            return "save"
        if any(t in lower for t in ("доход", "проценты", "зарабат", "пассивн")):
            return "income"
        return None

    elif question_key == "deposit_currency":
        if any(t in lower for t in ("доллар", "usd", "$")):
            return "USD"
        if any(t in lower for t in ("евро", "eur", "€")):
            return "EUR"
        if any(t in lower for t in ("сум", "uzs", "национальн")):
            return "UZS"
        return None

    elif question_key == "deposit_payout_pref":
        if any(t in lower for t in ("ежемесяч", "каждый месяц", "monthly")):
            return "monthly"
        if any(t in lower for t in ("конце", "в конце", "срока", "end")):
            return "end"
        return None

    elif question_key == "card_type":
        if any(t in lower for t in ("валютн", "fx", "загранич", "поездк", "за рубеж")):
            return "fx"
        if any(t in lower for t in ("дебетов", "обычн", "uzs", "uzcard", "humo")):
            return "debit"
        return None

    elif question_key == "card_purpose":
        if any(t in lower for t in ("поездк", "загранич", "за рубеж", "travel")):
            return "travel"
        if any(t in lower for t in ("покуп", "перевод", "оплат", "shopping")):
            return "shopping_transfers"
        return None

    elif question_key == "card_network":
        if "visa" in lower or "виза" in lower:
            return "visa"
        if "mastercard" in lower or "мастер" in lower:
            return "mastercard"
        return None

    elif question_key == "card_currency":
        if any(t in lower for t in ("доллар", "usd", "$")):
            return "USD"
        if any(t in lower for t in ("евро", "eur", "€")):
            return "EUR"
        return None

    return None


def is_all_credit_required_answered(slots: dict[str, Any]) -> bool:
    """Check if all required credit questions are answered."""
    key, _ = get_next_credit_question(slots)
    return key is None


def is_all_noncredit_required_answered(slots: dict[str, Any], service_type: str) -> bool:
    """Check if all required non-credit questions are answered."""
    key, _ = get_next_noncredit_question(slots, service_type)
    return key is None
