"""LLM-based value extractor for calculator flow.

Instead of blindly regex-parsing numbers from user text, we ask the LLM
to determine whether the user gave a concrete numeric answer or asked
a question / gave an ambiguous response.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

from langchain_core.messages import HumanMessage, SystemMessage

from app.agent.llm import _get_chat_openai, extract_token_usage
from app.agent.parsers import _parse_amount, _parse_downpayment, _parse_term_months

_log = logging.getLogger(__name__)

_EXTRACT_SYSTEM_PROMPT = {
    "ru": (
        "Ты помощник, извлекающий данные из ответа клиента в процессе расчёта банковского продукта.\n"
        "Текущий шаг: {step_description}\n"
        "Продукт: {product_name}\n\n"
        "Проанализируй ответ клиента и верни JSON (без markdown):\n"
        '{{"type": "value", "value": <число>}} — если клиент дал КОНКРЕТНОЕ числовое значение\n'
        '{{"type": "question", "text": "<суть вопроса>"}} — если клиент задаёт вопрос или рассуждает, '
        "а не даёт конкретный ответ\n\n"
        "Правила:\n"
        '- "70% от стоимости авто" — это НЕ конкретная сумма, это описание. type=question\n'
        '- "Если я прошу 70%..." — это вопрос/рассуждение. type=question\n'
        '- "200 млн" — конкретная сумма. type=value, value=200000000\n'
        '- "36 мес" — конкретный срок. type=value, value=36\n'
        '- "20%" или "20" (в контексте первоначального взноса) — конкретный процент. type=value, value=20\n'
        '- Любое рассуждение, уточнение, встречный вопрос — type=question\n'
    ),
    "en": (
        "You are an assistant extracting data from a customer's response during a bank product calculation.\n"
        "Current step: {step_description}\n"
        "Product: {product_name}\n\n"
        "Analyze the customer's response and return JSON (no markdown):\n"
        '{{"type": "value", "value": <number>}} — if the customer gave a CONCRETE numeric value\n'
        '{{"type": "question", "text": "<essence of the question>"}} — if the customer is asking '
        "a question or reasoning, not giving a direct answer\n\n"
        "Rules:\n"
        '- "70% of car cost" — NOT a concrete amount, it\'s a description. type=question\n'
        '- "200 mln" — concrete amount. type=value, value=200000000\n'
        '- "36 months" — concrete term. type=value, value=36\n'
        '- Any reasoning, clarification, counter-question — type=question\n'
    ),
    "uz": (
        "Siz bank mahsulotini hisoblash jarayonida mijozning javobidan ma'lumot oluvchi yordamchisiz.\n"
        "Joriy qadam: {step_description}\n"
        "Mahsulot: {product_name}\n\n"
        "Mijoz javobini tahlil qiling va JSON qaytaring (markdownsiz):\n"
        '{{"type": "value", "value": <raqam>}} — agar mijoz ANIQ raqamli qiymat bergan bo\'lsa\n'
        '{{"type": "question", "text": "<savol mohiyati>"}} — agar mijoz savol bergan yoki '
        "muhokama qilayotgan bo'lsa\n"
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


async def extract_calc_value(
    user_text: str,
    calc_step: str,
    product_name: str,
    lang: str = "ru",
) -> dict:
    """Use LLM to extract value or detect question from user input.

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
    system_text = system_tpl.format(step_description=step_desc, product_name=product_name)

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


def regex_fallback(user_text: str, calc_step: str) -> Optional[int | float]:
    """Regex fallback when LLM is unavailable."""
    if calc_step == "amount":
        return _parse_amount(user_text)
    elif calc_step == "term":
        return _parse_term_months(user_text)
    elif calc_step == "downpayment":
        return _parse_downpayment(user_text)
    return None
