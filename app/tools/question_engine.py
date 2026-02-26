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


# ---------------------------------------------------------------------------
# Multilingual button labels for inline keyboards
# ---------------------------------------------------------------------------

QUESTION_BUTTON_LABELS: dict[str, dict[str, dict[str, str]]] = {
    "credit_category": {
        "mortgage":         {"ru": "🏠 Ипотека",         "en": "🏠 Mortgage",       "uz": "🏠 Ipoteka"},
        "autoloan":         {"ru": "🚗 Автокредит",      "en": "🚗 Auto loan",      "uz": "🚗 Avtokredit"},
        "microloan":        {"ru": "💰 Микрозайм",       "en": "💰 Microloan",      "uz": "💰 Mikroqarz"},
        "education_credit": {"ru": "🎓 Образовательный", "en": "🎓 Education loan", "uz": "🎓 Ta'lim krediti"},
    },
    "citizen_uz": {
        "yes": {"ru": "✅ Да",  "en": "✅ Yes", "uz": "✅ Ha"},
        "no":  {"ru": "❌ Нет", "en": "❌ No",  "uz": "❌ Yo'q"},
    },
    "gender": {
        "male":   {"ru": "👨 Мужской", "en": "👨 Male",   "uz": "👨 Erkak"},
        "female": {"ru": "👩 Женский", "en": "👩 Female", "uz": "👩 Ayol"},
    },
    "income_proof": {
        "yes": {"ru": "✅ Есть доход",  "en": "✅ Have income", "uz": "✅ Daromad bor"},
        "no":  {"ru": "❌ Нет дохода", "en": "❌ No income",   "uz": "❌ Daromad yo'q"},
    },
    "self_employed": {
        "yes": {"ru": "✅ Да, самозанятый", "en": "✅ Yes, self-employed", "uz": "✅ Ha, o'z-o'zini ish"},
        "no":  {"ru": "❌ Нет",            "en": "❌ No",                 "uz": "❌ Yo'q"},
    },
    "payroll_participant": {
        "yes": {"ru": "✅ Да", "en": "✅ Yes", "uz": "✅ Ha"},
        "no":  {"ru": "❌ Нет", "en": "❌ No", "uz": "❌ Yo'q"},
    },
    "purpose_keys": {
        "housing_primary":   {"ru": "🏗 Новостройка",     "en": "🏗 New building",   "uz": "🏗 Yangi qurilish"},
        "housing_secondary": {"ru": "🏘 Вторичный рынок", "en": "🏘 Secondary",      "uz": "🏘 Ikkilamchi bozor"},
        "personal_any":      {"ru": "🧑 Личные цели",     "en": "🧑 Personal",       "uz": "🧑 Shaxsiy"},
        "business_start":    {"ru": "🚀 Открыть бизнес",  "en": "🚀 Start business", "uz": "🚀 Biznes ochish"},
        "business_support":  {"ru": "📈 Развить бизнес",  "en": "📈 Grow business",  "uz": "📈 Biznesni rivojlantirish"},
    },
    "mortgage_program": {
        "bi_group": {"ru": "BI Group",   "en": "BI Group",   "uz": "BI Group"},
        "nrg_2_4":  {"ru": "NRG 2-4%",  "en": "NRG 2-4%",  "uz": "NRG 2-4%"},
        "daho":     {"ru": "DAFO",       "en": "DAFO",       "uz": "DAFO"},
        "standard": {"ru": "Стандарт",  "en": "Standard",   "uz": "Standart"},
        "none":     {"ru": "Не знаю",   "en": "Don't know", "uz": "Bilmayman"},
    },
    "region_code": {
        "tashkent": {"ru": "🏙 Ташкент", "en": "🏙 Tashkent", "uz": "🏙 Toshkent"},
        "regions":  {"ru": "🌍 Регионы", "en": "🌍 Regions",  "uz": "🌍 Hududlar"},
    },
    "purpose_segment": {
        "consumer": {"ru": "🧑 Личные цели", "en": "🧑 Personal",  "uz": "🧑 Shaxsiy"},
        "business": {"ru": "🏢 Бизнес",      "en": "🏢 Business",  "uz": "🏢 Biznes"},
    },
    "study_level": {
        "bachelor":   {"ru": "🎓 Бакалавриат",     "en": "🎓 Bachelor",  "uz": "🎓 Bakalavr"},
        "master":     {"ru": "🎓 Магистратура",    "en": "🎓 Master",    "uz": "🎓 Magistr"},
        "vocational": {"ru": "📚 Проф. образование", "en": "📚 Vocational", "uz": "📚 Kasbiy"},
    },
    "institution_type": {
        "state":   {"ru": "🏛 Государственный", "en": "🏛 State",   "uz": "🏛 Davlat"},
        "private": {"ru": "🏫 Частный",         "en": "🏫 Private", "uz": "🏫 Xususiy"},
    },
    "deposit_goal": {
        "save":   {"ru": "💰 Накопление",       "en": "💰 Save money",    "uz": "💰 Jamg'arish"},
        "income": {"ru": "📈 Ежемесячный доход", "en": "📈 Monthly income", "uz": "📈 Oylik daromad"},
    },
    "deposit_currency": {
        "UZS": {"ru": "🇺🇿 Сумы (UZS)", "en": "🇺🇿 UZS",        "uz": "🇺🇿 So'm (UZS)"},
        "USD": {"ru": "💵 Доллары (USD)", "en": "💵 USD",         "uz": "💵 Dollar (USD)"},
        "EUR": {"ru": "💶 Евро (EUR)",    "en": "💶 EUR",         "uz": "💶 Evro (EUR)"},
    },
    "deposit_topup_needed": {
        "yes": {"ru": "✅ Да", "en": "✅ Yes", "uz": "✅ Ha"},
        "no":  {"ru": "❌ Нет", "en": "❌ No", "uz": "❌ Yo'q"},
    },
    "deposit_payout_pref": {
        "monthly": {"ru": "📅 Ежемесячно",    "en": "📅 Monthly",     "uz": "📅 Har oy"},
        "end":     {"ru": "🏁 В конце срока", "en": "🏁 At maturity", "uz": "🏁 Muddati oxirida"},
    },
    "card_type": {
        "debit": {"ru": "💳 Дебетовая (UZS)",  "en": "💳 Debit (UZS)", "uz": "💳 Debet (so'm)"},
        "fx":    {"ru": "🌍 Валютная (за рубеж)", "en": "🌍 FX (travel)", "uz": "🌍 Valyuta (xorijga)"},
    },
    "card_purpose": {
        "shopping_transfers": {"ru": "🛒 Покупки и переводы", "en": "🛒 Shopping & transfers", "uz": "🛒 Xaridlar va o'tkazmalar"},
        "travel":             {"ru": "✈️ Поездки за рубеж",  "en": "✈️ Travel abroad",         "uz": "✈️ Xorijga sayohat"},
    },
    "card_network": {
        "visa":       {"ru": "Visa",       "en": "Visa",       "uz": "Visa"},
        "mastercard": {"ru": "Mastercard", "en": "Mastercard", "uz": "Mastercard"},
    },
    "card_currency": {
        "USD": {"ru": "💵 Доллары (USD)", "en": "💵 USD", "uz": "💵 Dollar"},
        "EUR": {"ru": "💶 Евро (EUR)",    "en": "💶 EUR", "uz": "💶 Evro"},
    },
}


def get_question_buttons(question_key: str, language: str = "ru") -> Optional[list[str]]:
    """
    Returns a list of button labels for a question with known discrete options.
    Returns None for free-form questions (age, amounts, terms).
    """
    labels_map = QUESTION_BUTTON_LABELS.get(question_key)
    if not labels_map:
        return None
    result = []
    for val_labels in labels_map.values():
        label = val_labels.get(language) or val_labels.get("ru") or next(iter(val_labels.values()), "")
        if label:
            result.append(label)
    return result or None


# ---------------------------------------------------------------------------
# Multilingual question texts
# ---------------------------------------------------------------------------

_QUESTION_TEXT: dict[str, dict[str, str]] = {
    "credit_category": {
        "ru": "Какой тип кредита вас интересует — ипотека, автокредит, микрозайм или образовательный кредит?",
        "en": "What type of loan are you interested in — mortgage, auto loan, microloan, or education loan?",
        "uz": "Qaysi turdagi kredit qiziqtiradi — ipoteka, avtokredit, mikroqarz yoki ta'lim krediti?",
    },
    "citizen_uz": {
        "ru": "Вы являетесь гражданином Республики Узбекистан?",
        "en": "Are you a citizen of the Republic of Uzbekistan?",
        "uz": "Siz O'zbekiston Respublikasi fuqarosimisiz?",
    },
    "age": {
        "ru": "Сколько вам полных лет?",
        "en": "What is your age?",
        "uz": "Yoshingiz nechi?",
    },
    "gender": {
        "ru": "Ваш пол по паспорту — мужской или женский?",
        "en": "Your gender by passport — male or female?",
        "uz": "Pasportdagi jinsingiz — erkak yoki ayol?",
    },
    "income_proof": {
        "ru": "Есть ли у вас официально подтверждённый доход (справка о зарплате, налоговая декларация)?",
        "en": "Do you have officially confirmed income (salary certificate, tax return)?",
        "uz": "Rasmiy tasdiqlangan daromadingiz bormi (ish haqi ma'lumotnomasi, soliq deklaratsiyasi)?",
    },
    "self_employed": {
        "ru": "Вы зарегистрированы как самозанятый (ИП или самозанятый)?",
        "en": "Are you registered as self-employed or as an individual entrepreneur?",
        "uz": "Siz mustaqil ishchi (IP yoki o'z-o'zini bandlik) sifatida ro'yxatdan o'tganmisiz?",
    },
    "requested_amount": {
        "ru": "Какую сумму кредита вы планируете взять (в сумах)?",
        "en": "What loan amount are you planning to take (in UZS)?",
        "uz": "Qancha miqdorda kredit olmoqchisiz (so'mda)?",
    },
    "requested_term_months": {
        "ru": "На какой срок планируете взять кредит? Укажите в годах или месяцах.",
        "en": "For how long do you need the loan? Please specify in years or months.",
        "uz": "Qancha muddatga kredit kerak? Yil yoki oyda ko'rsating.",
    },
    "downpayment_pct": {
        "ru": "Какой первоначальный взнос вы планируете (в процентах от стоимости)?",
        "en": "What is your planned down payment (as a percentage of the value)?",
        "uz": "Boshlang'ich to'lov qancha bo'ladi (qiymatning foizida)?",
    },
    "purpose_keys": {
        "ru": "Уточните цель:",
        "en": "Please specify the purpose:",
        "uz": "Maqsadni aniqlashtiring:",
    },
    "mortgage_program": {
        "ru": "Какую ипотечную программу рассматриваете? (BI Group, NRG 2-4%, DAFO, стандарт или ещё не знаю)",
        "en": "Which mortgage program are you considering? (BI Group, NRG 2-4%, DAFO, standard, or don't know yet)",
        "uz": "Qaysi ipoteka dasturini ko'rib chiqyapsiz? (BI Group, NRG 2-4%, DAFO, standart yoki hali bilmayman)",
    },
    "region_code": {
        "ru": "В каком регионе находится недвижимость — Ташкент или другой регион?",
        "en": "In which region is the property — Tashkent or another region?",
        "uz": "Ko'chmas mulk qaysi hududda — Toshkent yoki boshqa hudud?",
    },
    "purpose_segment": {
        "ru": "Микрозайм для личных целей или для бизнеса?",
        "en": "Is the microloan for personal purposes or for business?",
        "uz": "Mikroqarz shaxsiy maqsadlar uchunmi yoki biznes uchun?",
    },
    "payroll_participant": {
        "ru": "Вы получаете зарплату через наш банк?",
        "en": "Do you receive your salary through our bank?",
        "uz": "Ish haqi bizning bank orqali to'lanadimi?",
    },
    "study_level": {
        "ru": "Уровень обучения — бакалавриат, магистратура или профессиональное образование?",
        "en": "Study level — bachelor, master, or vocational?",
        "uz": "Ta'lim darajasi — bakalavr, magistr yoki kasbiy ta'lim?",
    },
    "institution_type": {
        "ru": "Государственный или частный вуз/учреждение?",
        "en": "State or private university/institution?",
        "uz": "Davlat yoki xususiy oliy ta'lim muassasasi?",
    },
    "deposit_goal": {
        "ru": "Цель вклада — накопление средств или получение ежемесячного дохода?",
        "en": "Deposit goal — saving money or earning monthly income?",
        "uz": "Omonat maqsadi — jamg'arish yoki oylik daromad olish?",
    },
    "deposit_currency": {
        "ru": "В какой валюте хотите открыть вклад — сумы (UZS), доллары (USD) или евро (EUR)?",
        "en": "In which currency do you want to open a deposit — UZS, USD, or EUR?",
        "uz": "Qaysi valyutada omonat ochmoqchisiz — so'm (UZS), dollar (USD) yoki evro (EUR)?",
    },
    "deposit_term_months": {
        "ru": "На какой срок планируете вклад? Укажите в месяцах.",
        "en": "For how long do you plan to keep the deposit? Specify in months.",
        "uz": "Omonat qancha muddatga mo'ljallangan? Oyda ko'rsating.",
    },
    "deposit_topup_needed": {
        "ru": "Нужна ли возможность пополнять вклад в течение срока?",
        "en": "Do you need the option to top up the deposit during the term?",
        "uz": "Muddat davomida omonatni to'ldirish imkoniyati kerakmi?",
    },
    "deposit_payout_pref": {
        "ru": "Как предпочитаете получать проценты — ежемесячно или в конце срока?",
        "en": "How do you prefer to receive interest — monthly or at the end of the term?",
        "uz": "Foizlarni qanday olishni afzal ko'rasiz — har oy yoki muddat oxirida?",
    },
    "card_type": {
        "ru": "Вам нужна обычная дебетовая карта (UZS) или валютная карта для поездок за рубеж?",
        "en": "Do you need a regular debit card (UZS) or an FX card for travel abroad?",
        "uz": "Oddiy debet karta (UZS) kerakmi yoki xorijga sayohat uchun valyuta kartasi?",
    },
    "card_purpose": {
        "ru": "Для каких целей в основном — покупки и переводы или поездки за границу?",
        "en": "What is the main purpose — shopping & transfers or travel abroad?",
        "uz": "Asosiy maqsad — xaridlar va o'tkazmalar yoki xorijga sayohat?",
    },
    "card_network": {
        "ru": "Какая платёжная система предпочтительнее — VISA или Mastercard?",
        "en": "Which payment system do you prefer — Visa or Mastercard?",
        "uz": "Qaysi to'lov tizimini afzal ko'rasiz — Visa yoki Mastercard?",
    },
    "card_currency": {
        "ru": "В какой валюте карта — доллары (USD) или евро (EUR)?",
        "en": "In which currency — dollars (USD) or euros (EUR)?",
        "uz": "Qaysi valyutada — dollar (USD) yoki evro (EUR)?",
    },
}


def get_question_text(question_key: str, language: str = "ru") -> Optional[str]:
    """Return the localized question text for a given question key."""
    texts = _QUESTION_TEXT.get(question_key, {})
    return texts.get(language) or texts.get("ru") or None
