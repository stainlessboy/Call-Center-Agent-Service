# bank_callcenter_bot.py
from __future__ import annotations

import os
import json
import uuid
from typing import Any, Dict, List, Literal, Optional, TypedDict

from dotenv import load_dotenv
from pydantic import BaseModel

from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage
from langchain_core.tools import tool

from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import MemorySaver


# =========================
# 0) ENV
# =========================
load_dotenv()


# =========================
# 1) DOMAIN MODELS
# =========================

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


# =========================
# 2) GRAPH STATE
# =========================

class BotState(TypedDict, total=False):
    chat_id: str
    messages: List[Any]              # HumanMessage / AIMessage / SystemMessage
    last_user_text: str

    intent: Intent
    intent_confidence: float

    # scenario context
    active_flow: Optional[Intent]    # mortgage/auto_loan/microloan/service or None
    step: int                        # current step index in flow
    form: Dict[str, Any]             # collected fields

    # outputs
    answer: Optional[str]
    lead: Optional[Dict[str, Any]]   # Lead serialized


# =========================
# 3) BANK DATA (STUBS)
# =========================

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


# =========================
# 4) TOOLS (STUBS)
# =========================

@tool
def bank_kb_search(query: str) -> str:
    """Search in bank FAQ/KB and return best answer snippet."""
    q = query.lower().strip()
    for item in BANK_FAQ:
        if item["q"] in q or q in item["q"]:
            return item["a"]
    return "Подскажите, пожалуйста, по какому продукту вопрос (ипотека/авто/микро/услуга) и что именно нужно уточнить?"


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


# =========================
# 5) LLM
# =========================

def build_llm() -> ChatOpenAI:
    return ChatOpenAI(
        model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        temperature=0.2,
    )


# =========================
# 6) SYSTEM POLICY
# =========================

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


# =========================
# 7) NODES
# =========================

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

    intent = state.get("intent", "unknown")

    if intent == "greeting":
        return "greeting"

    if intent in ("mortgage", "auto_loan", "microloan", "service"):
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

    if "акци" in lower:
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


def node_start_flow(state: BotState) -> BotState:
    intent = state.get("intent", "unknown")
    state["active_flow"] = intent if intent in ("mortgage", "auto_loan", "microloan", "service") else None
    state["step"] = 0
    state["form"] = {}
    return state


def _flow_questions(flow: Intent) -> List[str]:
    if flow == "mortgage":
        return [
            "Понимаю. Для ипотеки уточните, пожалуйста: стоимость недвижимости (примерно) и ваш первоначальный взнос?",
            "На какой срок планируете ипотеку (в месяцах или годах)?",
            "Какой у вас ориентировочный ежемесячный доход (можно диапазон) и в каком регионе оформляете?",
            "Цель ипотеки (покупка на вторичке/новостройка/строительство/рефинансирование)?",
        ]
    if flow == "auto_loan":
        return [
            "Для автокредита уточните: стоимость автомобиля и первоначальный взнос?",
            "Авто новое или с пробегом?",
            "На какой срок хотите кредит (в месяцах)?",
            "Ваш ориентировочный ежемесячный доход?",
        ]
    if flow == "microloan":
        return [
            "Для микрозайма уточните: сумма и срок (в месяцах)?",
            "Цель займа (например: ремонт/покупка/медицина/другое)?",
            "Ваш ориентировочный ежемесячный доход?",
        ]
    if flow == "service":
        return [
            "Какую услугу вы хотите (например: карта, счёт, перевод, депозит, онлайн-банк)?",
            "Кратко опишите детали запроса (что именно нужно сделать/узнать).",
        ]
    return ["Уточните, пожалуйста, что именно вам нужно."]


def _extract_fields(flow: Intent, user_text: str, current_form: Dict[str, Any]) -> Dict[str, Any]:
    txt = user_text.replace(",", ".").lower()

    def find_numbers() -> List[float]:
        nums: List[float] = []
        buff = ""
        for ch in txt:
            if ch.isdigit() or ch == ".":
                buff += ch
            else:
                if buff:
                    try:
                        nums.append(float(buff))
                    except:
                        pass
                    buff = ""
        if buff:
            try:
                nums.append(float(buff))
            except:
                pass
        return nums

    nums = find_numbers()
    current_form.setdefault("_raw", []).append(user_text)

    if flow == "mortgage":
        if len(nums) >= 2 and (current_form.get("property_price") is None or current_form.get("down_payment") is None):
            current_form.setdefault("property_price", nums[0])
            current_form.setdefault("down_payment", nums[1])
        # crude term detect
        if any(k in txt for k in ["мес", "месяц"]) and current_form.get("term_months") is None and len(nums) >= 1:
            current_form["term_months"] = int(nums[0])
        if any(k in txt for k in ["год", "лет"]) and current_form.get("term_months") is None and len(nums) >= 1:
            current_form["term_months"] = int(nums[0] * 12)
        if current_form.get("income_monthly") is None and any(k in txt for k in ["доход", "зарплат"]):
            if nums:
                current_form["income_monthly"] = nums[0]
        if current_form.get("region") is None and "регион" in txt:
            current_form["region"] = user_text
        if current_form.get("purpose") is None and any(k in txt for k in ["вторич", "новост", "строит", "рефин"]):
            current_form["purpose"] = user_text
        return current_form

    if flow == "auto_loan":
        if "нов" in txt:
            current_form["new_or_used"] = "new"
        if "пробег" in txt or "б/у" in txt or "бу" in txt:
            current_form["new_or_used"] = "used"
        if len(nums) >= 2 and (current_form.get("car_price") is None or current_form.get("down_payment") is None):
            current_form.setdefault("car_price", nums[0])
            current_form.setdefault("down_payment", nums[1])
        if current_form.get("term_months") is None and any(k in txt for k in ["мес", "месяц"]) and nums:
            current_form["term_months"] = int(nums[0])
        if current_form.get("income_monthly") is None and any(k in txt for k in ["доход", "зарплат"]) and nums:
            current_form["income_monthly"] = nums[0]
        return current_form

    if flow == "microloan":
        if current_form.get("amount") is None and nums:
            current_form["amount"] = nums[0]
        if current_form.get("term_months") is None:
            if any(k in txt for k in ["мес", "месяц"]) and nums:
                current_form["term_months"] = int(nums[0])
            elif len(nums) >= 2:
                current_form["term_months"] = int(nums[1])
        if current_form.get("purpose") is None and len(user_text) > 3:
            current_form["purpose"] = user_text
        if current_form.get("income_monthly") is None and any(k in txt for k in ["доход", "зарплат"]) and nums:
            current_form["income_monthly"] = nums[0]
        return current_form

    if flow == "service":
        if current_form.get("service_name") is None:
            current_form["service_name"] = user_text
        else:
            current_form["details"] = user_text
        return current_form

    return current_form


def _build_recommendation(flow: Intent, form: Dict[str, Any]) -> str:
    if flow in ("mortgage", "auto_loan", "microloan"):
        product = PRODUCTS[flow]
        rate = product["rate_annual"]

        if flow == "mortgage":
            price = float(form.get("property_price") or 0)
            dp = float(form.get("down_payment") or 0)
            principal = max(price - dp, 0)
            term = int(form.get("term_months") or 0)

            calc = annuity_payment.invoke({"principal": principal, "annual_rate": rate, "term_months": term}) \
                if principal > 0 and term > 0 else "Для точного расчёта подскажите сумму кредита и срок."
            promo = get_active_promos.invoke({"product_type": "mortgage"})

            return (
                "Предварительная консультация по ипотеке:\n"
                f"- Оценочная сумма кредита: {principal:,.0f}\n"
                f"- Ставка (ориентир): {rate*100:.2f}% годовых\n"
                f"- {calc}\n"
                f"- {promo}\n"
                "Если удобно, я оформлю обращение, чтобы оператор связался с вами и уточнил детали."
            )

        if flow == "auto_loan":
            price = float(form.get("car_price") or 0)
            dp = float(form.get("down_payment") or 0)
            principal = max(price - dp, 0)
            term = int(form.get("term_months") or 0)

            calc = annuity_payment.invoke({"principal": principal, "annual_rate": rate, "term_months": term}) \
                if principal > 0 and term > 0 else "Для точного расчёта подскажите сумму кредита и срок."
            promo = get_active_promos.invoke({"product_type": "auto_loan"})

            return (
                "Предварительная консультация по автокредиту:\n"
                f"- Оценочная сумма кредита: {principal:,.0f}\n"
                f"- Ставка (ориентир): {rate*100:.2f}% годовых\n"
                f"- {calc}\n"
                f"- {promo}\n"
                "Если удобно, я оформлю обращение, чтобы оператор связался с вами и уточнил детали."
            )

        if flow == "microloan":
            amount = float(form.get("amount") or 0)
            term = int(form.get("term_months") or 0)

            calc = annuity_payment.invoke({"principal": amount, "annual_rate": rate, "term_months": term}) \
                if amount > 0 and term > 0 else "Для точного расчёта подскажите сумму и срок."
            promo = get_active_promos.invoke({"product_type": "microloan"})

            return (
                "Предварительная консультация по микрозайму:\n"
                f"- Сумма: {amount:,.0f}\n"
                f"- Ставка (ориентир): {rate*100:.2f}% годовых\n"
                f"- {calc}\n"
                f"- {promo}\n"
                "Если удобно, я оформлю обращение, чтобы оператор связался с вами и уточнил детали."
            )

    return (
        "Понял запрос по услуге. Я могу передать обращение оператору.\n"
        "Подтвердите, пожалуйста: оформить обращение для обратной связи?"
    )


def node_flow(state: BotState) -> BotState:
    flow: Intent = state.get("active_flow") or "unknown"
    questions = _flow_questions(flow)
    step = int(state.get("step") or 0)

    user_text = state["last_user_text"]
    if step > 0:
        state["form"] = _extract_fields(flow, user_text, state.get("form", {}))

    if step < len(questions):
        next_q = questions[step]
        msgs = state.get("messages", [])
        msgs = msgs + [HumanMessage(content=user_text)]
        msgs.append(AIMessage(content=next_q))
        state["messages"] = msgs
        state["answer"] = next_q
        state["step"] = step + 1
        return state

    recommendation = _build_recommendation(flow, state.get("form", {}))

    payload = {"flow": flow, "form": state.get("form", {})}
    summary = f"Запрос клиента: {flow}. Собрано: {json.dumps(payload, ensure_ascii=False)}"

    lead_json = create_lead.invoke({
        "product_type": flow,
        "payload_json": json.dumps(payload, ensure_ascii=False),
        "summary": summary,
    })

    msgs = state.get("messages", [])
    msgs = msgs + [HumanMessage(content=user_text)]
    msgs.append(AIMessage(content=recommendation + "\n\nНомер обращения (демо): " + json.loads(lead_json)["lead_id"]))
    state["messages"] = msgs
    state["answer"] = recommendation
    state["lead"] = json.loads(lead_json)

    state["active_flow"] = None
    state["step"] = 0
    return state


# =========================
# 8) BUILD GRAPH
# =========================

def build_graph():
    graph = StateGraph(BotState)

    graph.add_node("classify_intent", node_classify_intent)
    graph.add_node("greeting", node_greeting)
    graph.add_node("qa", node_qa)
    graph.add_node("start_flow", node_start_flow)
    graph.add_node("flow", node_flow)

    graph.set_entry_point("classify_intent")
    graph.add_conditional_edges("classify_intent", node_route, {
        "greeting": "greeting",
        "qa": "qa",
        "start_flow": "start_flow",
        "flow": "flow",
    })

    graph.add_edge("greeting", END)
    graph.add_edge("qa", END)
    graph.add_edge("start_flow", "flow")
    graph.add_edge("flow", END)

    checkpointer = MemorySaver()
    return graph.compile(checkpointer=checkpointer)


APP = build_graph()


# =========================
# 9) API-FRIENDLY TURN FUNCTION
# =========================

def run_turn(chat_id: str, user_text: str) -> str:
    state_in: BotState = {
        "chat_id": chat_id,
        "last_user_text": user_text,
        "messages": [SystemMessage(content=SYSTEM_POLICY)],
    }
    out = APP.invoke(state_in, config={"configurable": {"thread_id": chat_id}})
    return out.get("answer") or "Уточните, пожалуйста, ваш вопрос."


# =========================
# 10) CLI
# =========================

def main():
    chat_id = str(uuid.uuid4())[:8]
    print("Bank Call-Center Bot (CLI). Напишите 'exit' для выхода.")
    print(f"chat_id={chat_id}\n")

    while True:
        user_text = input("Вы: ").strip()
        if user_text.lower() in ("exit", "quit"):
            break
        if not user_text:
            print("Бот: Подскажите, пожалуйста, ваш вопрос.\n")
            continue
        answer = run_turn(chat_id, user_text)
        print(f"Бот: {answer}\n")


if __name__ == "__main__":
    main()
