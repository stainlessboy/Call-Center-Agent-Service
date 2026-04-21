"""LLM-based value extractor for calculator flow.

Instead of blindly regex-parsing numbers from user text, we ask the LLM
to determine whether the user gave a concrete numeric answer or asked
a question / gave an ambiguous response.
"""
from __future__ import annotations

import asyncio
import json
import logging
from langchain_core.messages import HumanMessage, SystemMessage

from app.agent.llm import _get_chat_openai, extract_token_usage

_log = logging.getLogger(__name__)

_EXTRACT_SYSTEM_PROMPT = {
    "ru": (
        "Ты помощник, извлекающий данные из ответа клиента в процессе расчёта банковского продукта.\n"
        "Текущий шаг: {step_description}\n"
        "Продукт: {product_name}\n\n"
        "{context}"
        "Проанализируй ответ клиента и верни JSON (без markdown):\n"
        '{{"type": "value", "value": <число>}} — если клиент дал КОНКРЕТНОЕ числовое значение\n'
        '{{"type": "question", "text": "<суть вопроса>"}} — если клиент задаёт вопрос или рассуждает, '
        "а не даёт конкретный ответ\n\n"
        "Правила:\n"
        '- Если бот задал вопрос (например "На какой срок?") и клиент ответил конкретным значением (например "2 года") — '
        'это КОНКРЕТНЫЙ ответ. type=value\n'
        '- "70% от стоимости авто" — это НЕ конкретная сумма, это описание. type=question\n'
        '- "Если я прошу 70%..." — это вопрос/рассуждение. type=question\n'
        '- "200 млн" — конкретная сумма. type=value, value=200000000\n'
        "- ДЛЯ ШАГА term: ВСЕГДА возвращай value в МЕСЯЦАХ, никогда в годах. "
        '"3 года"/"3 yil"/"3 йил"/"3 years" → value=36; '
        '"10 лет"/"10 yil"/"10 йил" → value=120; '
        '"24 мес"/"24 oy"/"24 ой"/"24 months" → value=24. '
        "Всегда умножай годы на 12.\n"
        '- "20%" или "20" (в контексте первоначального взноса) — конкретный процент. type=value, value=20\n'
        '- Любое рассуждение, уточнение, встречный вопрос — type=question\n'
    ),
    "en": (
        "You are an assistant extracting data from a customer's response during a bank product calculation.\n"
        "Current step: {step_description}\n"
        "Product: {product_name}\n\n"
        "{context}"
        "Analyze the customer's response and return JSON (no markdown):\n"
        '{{"type": "value", "value": <number>}} — if the customer gave a CONCRETE numeric value\n'
        '{{"type": "question", "text": "<essence of the question>"}} — if the customer is asking '
        "a question or reasoning, not giving a direct answer\n\n"
        "Rules:\n"
        '- If the bot asked a question (e.g. "For what term?") and customer replied with a specific value '
        '(e.g. "2 years") — this is a CONCRETE answer. type=value\n'
        '- "70% of car cost" — NOT a concrete amount, it\'s a description. type=question\n'
        '- "200 mln" — concrete amount. type=value, value=200000000\n'
        "- FOR term STEP: ALWAYS return value in MONTHS, never in years. "
        '"3 years"/"3 yil"/"3 йил"/"3 года" → value=36; '
        '"10 years"/"10 yil"/"10 йил"/"10 лет" → value=120; '
        '"24 months"/"24 oy"/"24 ой"/"24 мес" → value=24. '
        "Always multiply years by 12.\n"
        '- Any reasoning, clarification, counter-question — type=question\n'
    ),
    "uz": (
        "Siz bank mahsulotini hisoblash jarayonida mijozning javobidan ma'lumot oluvchi yordamchisiz.\n"
        "Joriy qadam: {step_description}\n"
        "Mahsulot: {product_name}\n\n"
        "{context}"
        "Mijoz javobini tahlil qiling va JSON qaytaring (markdownsiz):\n"
        '{{"type": "value", "value": <raqam>}} — agar mijoz ANIQ raqamli qiymat bergan bo\'lsa\n'
        '{{"type": "question", "text": "<savol mohiyati>"}} — agar mijoz savol bergan yoki '
        "muhokama qilayotgan bo'lsa\n"
        "Qoida: bot savol bergan bo'lsa (masalan 'Qancha muddatga?') va mijoz aniq qiymat bilan javob bergan bo'lsa "
        "(masalan '2 yil') — bu ANIQ javob hisoblanadi. type=value\n"
        "Muddat (term) qadami uchun: HAR DOIM value ni OYDA qaytaring, yillarda emas. "
        "'3 yil'/'3 йил'/'3 года'/'3 years' → value=36; "
        "'10 yil'/'10 йил'/'10 лет' → value=120; "
        "'24 oy'/'24 ой'/'24 мес'/'24 months' → value=24. "
        "Yillarni har doim 12 ga ko'paytiring.\n"
    ),
}

_STEP_DESCRIPTIONS = {
    "amount": {
        "ru": "Запрашиваем сумму кредита/вклада в сумах",
        "en": "Asking for loan/deposit amount in UZS",
        "uz": "Kredit/omonat summasini so'mda so'ramoqdamiz",
    },
    "term": {
        "ru": "Запрашиваем срок (в месяцах или годах)",
        "en": "Asking for term (in months or years)",
        "uz": "Muddatni so'ramoqdamiz (oylar yoki yillarda)",
    },
    "downpayment": {
        "ru": "Запрашиваем первоначальный взнос в процентах",
        "en": "Asking for down payment percentage",
        "uz": "Boshlang'ich to'lovni foizda so'ramoqdamiz",
    },
}


def _format_recent_messages_context(recent_messages: list, lang: str) -> str:
    """Format last messages as conversation context string for the extraction prompt."""
    if not recent_messages:
        return ""
    _role_labels = {
        "ru": {"human": "Клиент", "ai": "Бот", "system": None},
        "en": {"human": "User", "ai": "Bot", "system": None},
        "uz": {"human": "Mijoz", "ai": "Bot", "system": None},
    }
    labels = _role_labels.get(lang, _role_labels["ru"])
    lines: list[str] = []
    for msg in recent_messages:
        role = getattr(msg, "type", None) or getattr(msg, "role", "")
        content = str(getattr(msg, "content", "") or "").strip()
        if not content:
            continue
        label = labels.get(role)
        if label is None:
            continue
        lines.append(f"{label}: {content}")
    if not lines:
        return ""
    header = {
        "ru": "Последние сообщения диалога:\n",
        "en": "Recent conversation:\n",
        "uz": "So'nggi xabarlar:\n",
    }.get(lang, "Recent conversation:\n")
    return header + "\n".join(lines) + "\n\n"


async def extract_calc_value(
    user_text: str,
    calc_step: str,
    product_name: str,
    lang: str = "ru",
    recent_messages: list = None,
) -> dict:
    """Use LLM to extract value or detect question from user input.

    Args:
        user_text: The raw user input to classify.
        calc_step: Current calculator step (amount/term/downpayment).
        product_name: Display name of the product being calculated.
        lang: Language code (ru/en/uz).
        recent_messages: Last N LangChain messages for conversation context.

    Returns:
        {"type": "value", "value": <number>} — parsed concrete value
        {"type": "question", "text": "..."} — user asked a question
        {"type": "unparsed"} — LLM unavailable, fallback to regex
    """
    llm = _get_chat_openai()
    if not llm:
        return {"type": "unparsed"}

    step_desc = _STEP_DESCRIPTIONS.get(calc_step, {}).get(lang, calc_step)
    system_tpl = _EXTRACT_SYSTEM_PROMPT.get(lang, _EXTRACT_SYSTEM_PROMPT["ru"])
    context_block = _format_recent_messages_context(recent_messages or [], lang)
    system_text = system_tpl.format(
        step_description=step_desc,
        product_name=product_name,
        context=context_block,
    )

    try:
        ai_msg = await asyncio.wait_for(
            llm.ainvoke([
                SystemMessage(content=system_text),
                HumanMessage(content=user_text),
            ]),
            timeout=10.0,
        )
        usage = extract_token_usage(ai_msg)
        raw = str(ai_msg.content or "").strip()
        # Strip markdown code fences if present
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        result = json.loads(raw)
        if result.get("type") == "value" and result.get("value") is not None:
            return {"type": "value", "value": result["value"], "_usage": usage}
        if result.get("type") == "question":
            return {"type": "question", "text": result.get("text", user_text), "_usage": usage}
        return {"type": "unparsed", "_usage": usage}
    except Exception as exc:
        _log.debug("LLM extraction failed: %s", exc)
        return {"type": "unparsed"}


_PREFILL_SYSTEM_PROMPT = {
    "ru": (
        "Ты помощник, анализирующий историю диалога с клиентом банка перед запуском калькулятора.\n"
        "Категория продукта: {category}\n\n"
        "Задача: извлечь из последних сообщений финансовые данные, которые можно использовать как начальные значения "
        "для калькулятора. Используй САМЫЕ ПОСЛЕДНИЕ упомянутые значения, если их было несколько.\n\n"
        "Важные правила:\n"
        "- Если бот задал вопрос (например 'На какой срок?') и клиент ответил конкретным значением (например '3 года') — "
        "извлеки term_months=36. Такие пары вопрос-ответ являются ОСНОВНЫМ источником данных.\n"
        "- Если клиент назвал зарплату/доход — можно приблизительно оценить подходящую сумму кредита/вклада: "
        "для кредита — до 2–3 зарплат (если срок до 6 мес.) или исходя из платежеспособности; "
        "для овердрафта — близко к размеру зарплаты.\n"
        "- Для суммы (amount): верни в сумах (целое число).\n"
        "- Для срока (term_months): верни в месяцах (целое число).\n"
        "- Включай поле только если оно явно упомянуто или уверенно выводится из контекста.\n"
        "- Если данных нет — верни пустой объект {}.\n\n"
        "Верни JSON (без markdown): {{\"amount\": <число или null>, \"term_months\": <число или null>}}\n"
        "Пропускай null-поля из ответа."
    ),
    "en": (
        "You are an assistant analyzing a customer's conversation history before starting the bank calculator.\n"
        "Product category: {category}\n\n"
        "Task: extract financial data from the recent messages that can be used as initial values for the calculator. "
        "Use the MOST RECENT values if multiple were mentioned.\n\n"
        "Important rules:\n"
        "- If the bot asked a question (e.g. 'For what term?') and the customer replied with a specific value "
        "(e.g. '3 years') — extract term_months=36. Such question-answer pairs are the PRIMARY data source.\n"
        "- If the customer mentioned salary/income — estimate a reasonable loan/deposit amount: "
        "for loans — up to 2–3 months' salary (short term) or based on affordability; "
        "for overdraft — close to the salary amount.\n"
        "- For amount: return in UZS (integer).\n"
        "- For term_months: return in months (integer).\n"
        "- Include a field only if explicitly mentioned or confidently inferred from context.\n"
        "- If no data found — return empty object {}.\n\n"
        "Return JSON (no markdown): {{\"amount\": <number or null>, \"term_months\": <number or null>}}\n"
        "Omit null fields from the response."
    ),
    "uz": (
        "Siz bank kalkulyatorini ishga tushirishdan oldin mijoz suhbati tarixini tahlil qiluvchi yordamchisiz.\n"
        "Mahsulot toifasi: {category}\n\n"
        "Vazifa: so'nggi xabarlardagi moliyaviy ma'lumotlarni kalkulyator uchun boshlang'ich qiymatlar sifatida chiqarish. "
        "Bir nechta qiymat eslatilgan bo'lsa, ENG SO'NGGI qiymatdan foydalaning.\n\n"
        "Muhim qoidalar:\n"
        "- Agar bot savol bergan bo'lsa (masalan 'Qancha muddatga?') va mijoz aniq qiymat bilan javob bergan bo'lsa "
        "(masalan '3 yil') — term_months=36 ni ajrating. Bunday savol-javob juftliklari ASOSIY ma'lumot manbai hisoblanadi.\n"
        "- Mijoz maosh/daromadini eslatgan bo'lsa — kredit/omonat summasini taxminan baholash mumkin: "
        "kredit uchun — 2–3 oylik maosh; overdraft uchun — maoshga yaqin.\n"
        "- Summa (amount): so'mda (butun son).\n"
        "- Muddat (term_months): oyda (butun son).\n"
        "- Faqat aniq eslatilgan yoki kontekstdan ishonchli chiqariladigan maydonlarni kiriting.\n"
        "- Ma'lumot bo'lmasa — bo'sh ob'ekt {} qaytaring.\n\n"
        "JSON qaytaring (markdownsiz): {{\"amount\": <son yoki null>, \"term_months\": <son yoki null>}}\n"
        "Null maydonlarni javobdan o'tkazib yuboring."
    ),
}

_CONTEXT_UPDATE_SYSTEM_PROMPT = {
    "ru": (
        "Ты помощник, определяющий суть ответа клиента в процессе сбора данных калькулятора.\n"
        "Текущий шаг калькулятора: {step_description}\n"
        "Продукт: {product_name}\n"
        "Уже собранные данные: {current_slots}\n\n"
        "Клиент написал: \"{user_text}\"\n\n"
        "Определи тип ответа и верни JSON (без markdown):\n"
        '- Если клиент дал КОНКРЕТНОЕ числовое значение для текущего шага: {{"type": "value", "value": <число>}}\n'
        '- Если клиент сообщает финансовый контекст (зарплата, доход, бюджет), из которого можно вывести значение: '
        '{{"type": "context_update", "updates": {{"amount": <число>}}}}\n'
        '- Если клиент задаёт вопрос или рассуждает: {{"type": "question", "text": "<суть>"}}\n'
        '- Если не удалось распознать: {{"type": "unparsed"}}\n\n'
        "Правило для context_update: используй только если можно уверенно вывести числовое значение. "
        "Например 'моя зарплата 15 млн' при шаге amount → amount=15000000."
    ),
    "en": (
        "You are an assistant determining the intent of a customer's response during calculator data collection.\n"
        "Current calculator step: {step_description}\n"
        "Product: {product_name}\n"
        "Already collected data: {current_slots}\n\n"
        "Customer wrote: \"{user_text}\"\n\n"
        "Determine the response type and return JSON (no markdown):\n"
        '- If customer gave a CONCRETE numeric value for the current step: {{"type": "value", "value": <number>}}\n'
        '- If customer provides financial context (salary, income, budget) from which a value can be inferred: '
        '{{"type": "context_update", "updates": {{"amount": <number>}}}}\n'
        '- If customer is asking a question or reasoning: {{"type": "question", "text": "<essence>"}}\n'
        '- If unrecognized: {{"type": "unparsed"}}\n\n'
        "Rule for context_update: only use if a numeric value can be confidently derived. "
        "E.g. 'my salary is 15 mln' at amount step → amount=15000000."
    ),
    "uz": (
        "Siz kalkulyator ma'lumotlarini yig'ish jarayonida mijoz javobining mohiyatini aniqlovchi yordamchisiz.\n"
        "Joriy kalkulyator bosqichi: {step_description}\n"
        "Mahsulot: {product_name}\n"
        "Allaqachon yig'ilgan ma'lumotlar: {current_slots}\n\n"
        "Mijoz yozdi: \"{user_text}\"\n\n"
        "Javob turini aniqlang va JSON qaytaring (markdownsiz):\n"
        '- Agar mijoz joriy bosqich uchun ANIQ raqamli qiymat bergan bo\'lsa: {{"type": "value", "value": <raqam>}}\n'
        '- Agar mijoz qiymatni chiqarish mumkin bo\'lgan moliyaviy kontekst bildirsa (maosh, daromad): '
        '{{"type": "context_update", "updates": {{"amount": <raqam>}}}}\n'
        '- Agar mijoz savol bersa yoki muhokama qilsa: {{"type": "question", "text": "<mohiyat>"}}\n'
        '- Agar tanib bo\'lmasa: {{"type": "unparsed"}}\n\n'
        "context_update qoidasi: faqat raqamli qiymatni ishonchli chiqarish mumkin bo'lsa foydalaning."
    ),
}


async def extract_prefill_from_history(
    messages: list,
    category: str,
    lang: str = "ru",
    last_n: int = 10,
) -> dict:
    """Extract pre-fill values for the calculator from recent conversation history.

    Uses LLM to identify amounts, terms, or salary context mentioned in the last N messages.
    Returns a dict with keys like {"amount": 15000000, "term_months": 12}.
    Falls back to empty dict if LLM is unavailable or nothing is found.
    """
    llm = _get_chat_openai()
    if not llm:
        return {}

    # Build a text summary of the last N messages
    recent = messages[-last_n:] if len(messages) > last_n else messages
    history_lines: list[str] = []
    for msg in recent:
        role = getattr(msg, "type", None) or getattr(msg, "role", "unknown")
        content = str(getattr(msg, "content", "") or "").strip()
        if content:
            history_lines.append(f"{role}: {content}")

    if not history_lines:
        return {}

    history_text = "\n".join(history_lines)
    system_tpl = _PREFILL_SYSTEM_PROMPT.get(lang, _PREFILL_SYSTEM_PROMPT["ru"])
    system_text = system_tpl.format(category=category)

    try:
        ai_msg = await asyncio.wait_for(
            llm.ainvoke([
                SystemMessage(content=system_text),
                HumanMessage(content=history_text),
            ]),
            timeout=10.0,
        )
        raw = str(ai_msg.content or "").strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        result = json.loads(raw)
        if not isinstance(result, dict):
            return {}
        # Only return keys with actual non-null integer/float values
        prefill = {}
        if result.get("amount") is not None:
            prefill["amount"] = int(float(result["amount"]))
        if result.get("term_months") is not None:
            prefill["term_months"] = int(float(result["term_months"]))
        return prefill
    except Exception as exc:
        _log.debug("prefill extraction failed: %s", exc)
        return {}


async def extract_updated_value(
    user_text: str,
    calc_step: str,
    current_slots: dict,
    product_name: str,
    lang: str = "ru",
) -> dict:
    """Determine whether user input is a direct value, context update, question, or unparsed.

    This is a richer version of extract_calc_value that also handles cases where
    the user provides financial context (salary/income) instead of a direct answer.

    Returns one of:
        {"type": "value", "value": <number>}
        {"type": "context_update", "updates": {"amount": <number>, ...}}
        {"type": "question", "text": "..."}
        {"type": "unparsed"}
    All may include an "_usage" key with token counts.
    """
    llm = _get_chat_openai()
    if not llm:
        return {"type": "unparsed"}

    step_desc = _STEP_DESCRIPTIONS.get(calc_step, {}).get(lang, calc_step)
    system_tpl = _CONTEXT_UPDATE_SYSTEM_PROMPT.get(lang, _CONTEXT_UPDATE_SYSTEM_PROMPT["ru"])

    slots_repr = ", ".join(f"{k}={v}" for k, v in current_slots.items()) or "нет"
    system_text = system_tpl.format(
        step_description=step_desc,
        product_name=product_name,
        current_slots=slots_repr,
        user_text=user_text,
    )

    try:
        ai_msg = await asyncio.wait_for(
            llm.ainvoke([
                SystemMessage(content=system_text),
                HumanMessage(content=user_text),
            ]),
            timeout=10.0,
        )
        usage = extract_token_usage(ai_msg)
        raw = str(ai_msg.content or "").strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        result = json.loads(raw)
        rtype = result.get("type")

        if rtype == "value" and result.get("value") is not None:
            return {"type": "value", "value": result["value"], "_usage": usage}
        if rtype == "context_update" and isinstance(result.get("updates"), dict):
            # Coerce all update values to int/float
            updates = {}
            for k, v in result["updates"].items():
                if v is not None:
                    try:
                        updates[k] = int(float(v))
                    except (ValueError, TypeError):
                        pass
            if updates:
                return {"type": "context_update", "updates": updates, "_usage": usage}
        if rtype == "question":
            return {"type": "question", "text": result.get("text", user_text), "_usage": usage}
        return {"type": "unparsed", "_usage": usage}
    except Exception as exc:
        _log.debug("context update extraction failed: %s", exc)
        return {"type": "unparsed"}


