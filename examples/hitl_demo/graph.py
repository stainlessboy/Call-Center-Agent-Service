"""Standalone LangGraph 1.x demo: every Human-in-the-Loop (HITL) pattern in one graph.

This module is intentionally self-contained — it does NOT import anything from
``app/`` and can be copy-pasted into any other LangGraph project. It exists purely
to demo HITL mechanics to colleagues; see ``README.md`` (in Russian) for the
concept walkthrough and how each pattern maps onto the production bot.

Scenario: "Кредитная заявка с участием человека" (a credit application that a
human reviews/approves at several points before it is submitted to a fake CRM).

Five distinct HITL patterns are demonstrated, each labeled in the node docstring:

    PATTERN 1 — ask the user for missing input      -> node `intake`
    PATTERN 2 — parallel interrupts (fan-out/fan-in) -> node `verify_document`
    PATTERN 3 — review & edit / reject with feedback -> node `review_offer`
    PATTERN 4 — tool-call approval gate              -> node `approval_gate`
    PATTERN 5 — handoff to a live operator           -> node `operator_handoff`

All patterns rely on the same two LangGraph primitives:

    * ``langgraph.types.interrupt(value)`` — raises ``GraphInterrupt`` the first
      time it is called in a node's execution, surfacing ``value`` to the caller
      and pausing the whole graph run. On resume, the ENTIRE node re-executes
      from the top and ``interrupt()`` returns the resume value instead of
      raising. This is why every node that calls ``interrupt()`` must be
      idempotent up to that point (see the "idempotency" comments below).
    * ``langgraph.types.Command(resume=...)`` — passed as the input to
      ``graph.invoke()``/``graph.stream()`` to continue a paused run.

A checkpointer is REQUIRED for interrupts to work at all (state must persist
across the pause). ``build_graph()`` defaults to an in-memory one.
"""

from __future__ import annotations

import operator
import os
import re
from typing import Annotated, Any, Literal, TypedDict

from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import Command, RetryPolicy, Send, interrupt

# --------------------------------------------------------------------------- #
# "Fake CRM" — a module-level list standing in for a real database/API call.
# PATTERN-adjacent note: the side effect of `submit_application` (appending
# here) intentionally lives in its OWN node, downstream of the `approval_gate`
# node that calls `interrupt()`. If the side effect lived inside a node that
# itself calls `interrupt()`, a resume would re-execute the WHOLE node body —
# including the side effect — a second time (interrupt() only skips its own
# raise on resume, everything else in the function body still reruns from
# line 1). Splitting "ask for approval" (approval_gate) from "do the thing"
# (submit_application) is the idiomatic way to keep HITL nodes idempotent.
# --------------------------------------------------------------------------- #
FAKE_CRM: list[dict[str, Any]] = []

DEFAULT_DOCUMENTS = ["паспорт", "справка о доходах", "ИНН"]
DEFAULT_INCOME_UZS = 8_000_000.0
ANNUAL_RATE = 0.24
MAX_REVIEW_ROUNDS = 3
DTI_LOW = 0.35
DTI_HIGH = 0.50


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #
def _merge_verified_docs(
    left: dict[str, bool] | None, right: dict[str, bool] | None
) -> dict[str, bool]:
    """Reducer for `verified_docs`: dict-merge instead of overwrite.

    Needed because `verify_document` runs N times in parallel (PATTERN 2, via
    `Send`) and each parallel branch only knows about ONE document — without a
    merging reducer the default TypedDict channel behavior (last write wins)
    would make every branch clobber the others' results.
    """
    merged = dict(left or {})
    merged.update(right or {})
    return merged


class BankState(TypedDict, total=False):
    """Graph-wide state. `total=False` — every node reads via `.get()` with a
    default, since fields are populated incrementally as the flow progresses."""

    messages: Annotated[list[Any], add_messages]
    log: Annotated[list[str], operator.add]  # audit trail, every node appends

    customer_request: str
    amount: int | None
    term_months: int | None
    purpose: str | None

    documents: list[str]
    verified_docs: Annotated[dict[str, bool], _merge_verified_docs]

    risk_level: Literal["low", "medium", "high"]
    dti: float | None

    offer_text: str
    review_feedback: str | None
    review_round: int

    approved_action: dict[str, Any] | None
    application_id: str | None

    operator_reply: str | None
    final_message: str


class VerifyDocInput(TypedDict):
    """Local input schema for the `verify_document` node. `Send(node, arg)`
    passes `arg` as the ENTIRE input for that parallel task — it does not get
    merged with the rest of `BankState` — so this node only ever sees the one
    document name it must verify, not the whole application."""

    document: str


# --------------------------------------------------------------------------- #
# Small deterministic parsers (regex only — no LLM, no I/O => safe to redo on
# every resume, which is exactly what `intake`'s idempotency requires).
# --------------------------------------------------------------------------- #
_AMOUNT_RE = re.compile(
    r"(\d[\d\s]{2,})\s*(млн|миллион\w*|млн\.)?", re.IGNORECASE
)
_TERM_MONTHS_RE = re.compile(r"(\d{1,3})\s*(месяц\w*|мес\.?)", re.IGNORECASE)
_TERM_YEARS_RE = re.compile(r"(\d{1,2})\s*(лет|год\w*)", re.IGNORECASE)
_PURPOSE_KEYWORDS = {
    "ипотека": "ипотека",
    "ипотеку": "ипотека",
    "квартир": "ипотека",
    "жилье": "ипотека",
    "жильё": "ипотека",
    "авто": "автокредит",
    "машин": "автокредит",
    "образован": "образовательный кредит",
    "учеб": "образовательный кредит",
    "ремонт": "потребительский кредит",
    "потребит": "потребительский кредит",
    "бизнес": "бизнес-кредит",
}
_INCOME_RE = re.compile(
    r"(?:доход|зарплата|зп)\D{0,10}(\d[\d\s]{2,})\s*(млн|миллион\w*)?",
    re.IGNORECASE,
)


def _parse_amount(text: str) -> int | None:
    """Extract a loan amount in UZS from free text. Supports plain digits
    ("50000000") and a "млн" (million) shorthand ("50 млн")."""
    if not text:
        return None
    for match in _AMOUNT_RE.finditer(text):
        digits = match.group(1).replace(" ", "")
        if not digits.isdigit():
            continue
        value = int(digits)
        if match.group(2):  # "млн" suffix
            value *= 1_000_000
        # Guard against accidentally matching a term/year number picked up by
        # the same generic digit pattern (e.g. "24 месяца" alone).
        if value >= 100_000 or match.group(2):
            return value
    return None


def _parse_term(text: str) -> int | None:
    """Extract a loan term in months from free text ("36 месяцев" / "3 года")."""
    if not text:
        return None
    m = _TERM_MONTHS_RE.search(text)
    if m:
        return int(m.group(1))
    y = _TERM_YEARS_RE.search(text)
    if y:
        return int(y.group(1)) * 12
    return None


def _parse_purpose(text: str) -> str | None:
    """Map free text to one of a fixed set of loan purposes via keyword match."""
    if not text:
        return None
    lowered = text.lower()
    for keyword, purpose in _PURPOSE_KEYWORDS.items():
        if keyword in lowered:
            return purpose
    return None


def _parse_income(text: str) -> float:
    """Extract a monthly income from free text, defaulting to the demo's fixed
    income figure when none is mentioned (per the scenario spec)."""
    if text:
        m = _INCOME_RE.search(text)
        if m:
            digits = m.group(1).replace(" ", "")
            if digits.isdigit():
                value = float(digits)
                if m.group(2):
                    value *= 1_000_000
                return value
    return DEFAULT_INCOME_UZS


def _monthly_payment(amount: float, term_months: int, annual_rate: float = ANNUAL_RATE) -> float:
    """Simple annuity monthly payment formula, same shape as
    `app/utils/amortization.py` in the main project (kept dependency-free here)."""
    if term_months <= 0:
        return amount
    monthly_rate = annual_rate / 12
    if monthly_rate == 0:
        return amount / term_months
    return amount * monthly_rate / (1 - (1 + monthly_rate) ** (-term_months))


_QUESTIONS = {
    "amount": "На какую сумму (в сумах) вы хотите оформить кредит?",
    "term_months": "На какой срок (в месяцах) вы хотите оформить кредит?",
    "purpose": "Какова цель кредита? (ипотека, автокредит, образование, потребительский, бизнес)",
}


# --------------------------------------------------------------------------- #
# Node 1: intake — PATTERN 1 "ask the user for missing input"
# --------------------------------------------------------------------------- #
def intake(state: BankState) -> dict[str, Any]:
    """PATTERN 1: ask the user for missing input.

    Idempotency note: this node does ONLY parsing (regex over strings already
    in state) before it might call `interrupt()` — no network/DB/file writes.
    That means re-running it from the top on resume is perfectly safe: parsing
    the same `customer_request` twice yields the same result, so nothing is
    duplicated or corrupted.

    Only ONE missing field is asked about per interrupt. Routing back to this
    same node (via a conditional edge, not a Python while-loop) is what lets
    the graph ask multiple follow-up questions one at a time — the LangGraph
    docs recommend conditional edges over an in-node loop specifically so each
    question becomes its own resumable checkpoint.
    """
    request = state.get("customer_request", "")
    amount = state.get("amount")
    term_months = state.get("term_months")
    purpose = state.get("purpose")

    if amount is None:
        amount = _parse_amount(request)
    if term_months is None:
        term_months = _parse_term(request)
    if purpose is None:
        purpose = _parse_purpose(request)

    missing = [
        name
        for name, value in (
            ("amount", amount),
            ("term_months", term_months),
            ("purpose", purpose),
        )
        if value is None
    ]

    if missing:
        field = missing[0]
        answer = interrupt(
            {
                "type": "ask_user",
                "question": _QUESTIONS[field],
                "missing": missing,
            }
        )
        answer_text = str(answer or "")
        if field == "amount":
            amount = _parse_amount(answer_text) or _parse_amount(f"{answer_text} ")
        elif field == "term_months":
            term_months = _parse_term(answer_text)
            if term_months is None and answer_text.strip().isdigit():
                # Human just typed a bare number ("36") answering "in months?"
                term_months = int(answer_text.strip())
        elif field == "purpose":
            purpose = _parse_purpose(answer_text) or answer_text.strip()

        return {
            "amount": amount,
            "term_months": term_months,
            "purpose": purpose,
            "log": [f"intake: получен ответ на вопрос о поле '{field}' -> {answer_text!r}"],
        }

    return {
        "amount": amount,
        "term_months": term_months,
        "purpose": purpose,
        "log": ["intake: все обязательные поля заполнены"],
    }


def route_after_intake(state: BankState) -> str:
    """Conditional edge (not `Command`) — deliberately shown alongside the
    `Command(goto=...)` style used elsewhere, per the task's requirement to
    demo both routing styles."""
    if state.get("amount") is None or state.get("term_months") is None or state.get("purpose") is None:
        return "intake"
    return "risk_check"


# --------------------------------------------------------------------------- #
# Node 2: risk_check — deterministic risk scoring + fan-out via Send
# --------------------------------------------------------------------------- #
def risk_check(state: BankState) -> Command[Literal["verify_document"]]:
    """Deterministic DTI/risk scoring, then fans out one `Send` per document
    to `verify_document` — this is the "map" half of a map-reduce style
    parallel branch (the "reduce" half is `collect_verification`)."""
    amount = float(state.get("amount") or 0)
    term_months = int(state.get("term_months") or 1)
    income = _parse_income(state.get("customer_request", ""))

    payment = _monthly_payment(amount, term_months)
    dti = round(payment / income, 3) if income else None

    if dti is None:
        risk: Literal["low", "medium", "high"] = "medium"
    elif dti < DTI_LOW:
        risk = "low"
    elif dti < DTI_HIGH:
        risk = "medium"
    else:
        risk = "high"

    documents = state.get("documents") or list(DEFAULT_DOCUMENTS)

    # PATTERN 2 setup: one Send per document -> parallel `verify_document`
    # tasks. Each Send's `arg` becomes that task's ENTIRE input (see
    # `VerifyDocInput`), not a merge with the rest of BankState.
    sends = [Send("verify_document", {"document": doc}) for doc in documents]

    return Command(
        goto=sends,
        update={
            "risk_level": risk,
            "dti": dti,
            "documents": documents,
            "log": [
                f"risk_check: платёж/мес={payment:.0f}, доход={income:.0f}, "
                f"dti={dti}, риск={risk}, документы={documents}"
            ],
        },
    )


# --------------------------------------------------------------------------- #
# Node 3: verify_document — PATTERN 2 "parallel interrupts"
# --------------------------------------------------------------------------- #
def verify_document(state: VerifyDocInput) -> dict[str, Any]:
    """PATTERN 2: parallel interrupts.

    Every branch spawned by `risk_check`'s `Send` calls `interrupt()`
    independently and *in the same graph step* — LangGraph pauses the whole
    run once ALL parallel tasks at this step have either completed or
    interrupted, and `invoke()` returns every pending `Interrupt` at once in
    `__interrupt__`. The caller must resume ALL of them together with a
    SINGLE `Command(resume={interrupt.id: value, ...})` — a dict keyed by
    each `Interrupt.id` (see `run_demo.py`), not one `Command` per branch.
    """
    document = state["document"]
    verdict = interrupt({"type": "verify_document", "document": document})
    verified = bool(verdict)
    return {
        "verified_docs": {document: verified},
        "log": [f"verify_document: '{document}' -> {'подтверждён' if verified else 'отклонён'}"],
    }


def collect_verification(state: BankState) -> dict[str, Any]:
    """Fan-in node: the static edge `verify_document -> collect_verification`
    (defined once, regardless of how many parallel `verify_document` tasks
    ran) is where LangGraph waits for every parallel branch to finish before
    proceeding — the standard map-reduce "reduce" step."""
    verified = state.get("verified_docs", {})
    return {"log": [f"collect_verification: итог проверки документов = {verified}"]}


def route_after_verification(state: BankState) -> str:
    verified = state.get("verified_docs", {})
    documents = state.get("documents", DEFAULT_DOCUMENTS)
    any_unverified = any(not verified.get(doc, False) for doc in documents)
    if any_unverified or state.get("risk_level") == "high":
        return "operator_handoff"
    return "draft_offer"


# --------------------------------------------------------------------------- #
# Node 4: draft_offer — template or optional LLM, with a RetryPolicy attached
# --------------------------------------------------------------------------- #
def _template_offer(state: BankState) -> str:
    amount = state.get("amount") or 0
    term_months = state.get("term_months") or 0
    purpose = state.get("purpose") or "кредит"
    feedback = state.get("review_feedback")
    lines = [
        f"Предложение по кредиту «{purpose}»:",
        f"Сумма: {amount:,.0f} сум".replace(",", " "),
        f"Срок: {term_months} мес.",
        f"Ставка: {ANNUAL_RATE * 100:.0f}% годовых",
        f"Ежемесячный платёж: {_monthly_payment(float(amount), int(term_months or 1)):,.0f} сум".replace(",", " "),
    ]
    if feedback:
        lines.append(f"(учтены замечания рецензента: {feedback})")
    return "\n".join(lines)


def make_draft_offer(use_llm: bool):
    """Factory so `build_graph(use_llm=...)` can control whether this node is
    allowed to call OpenAI, without any global mutable state."""

    def draft_offer(state: BankState) -> dict[str, Any]:
        """Builds `offer_text`. Template by default; with `use_llm=True` AND
        `OPENAI_API_KEY` set, tries `ChatOpenAI` first and falls back to the
        template on ANY failure (network, auth, quota, import) — the demo
        must keep working with zero external dependencies."""
        text: str | None = None
        used_llm = False
        if use_llm and os.getenv("OPENAI_API_KEY"):
            try:
                from langchain_openai import ChatOpenAI

                llm = ChatOpenAI(model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"))
                feedback = state.get("review_feedback")
                prompt = (
                    "Составь короткое официальное кредитное предложение на русском "
                    f"языке. Сумма: {state.get('amount')} сум, срок: {state.get('term_months')} мес., "
                    f"цель: {state.get('purpose')}, ставка {ANNUAL_RATE * 100:.0f}% годовых."
                )
                if feedback:
                    prompt += f" Учти замечания предыдущего ревью: {feedback}."
                response = llm.invoke(prompt)
                text = str(response.content).strip() or None
                used_llm = text is not None
            except Exception as exc:  # noqa: BLE001 - demo must degrade gracefully
                text = None

        if text is None:
            text = _template_offer(state)

        round_no = state.get("review_round", 0)
        source = "LLM" if used_llm else "шаблон"
        return {
            "offer_text": text,
            "log": [f"draft_offer: черновик предложения сформирован ({source}, раунд ревью {round_no})"],
        }

    return draft_offer


# --------------------------------------------------------------------------- #
# Node 5: review_offer — PATTERN 3 "review & edit / reject with feedback"
# --------------------------------------------------------------------------- #
def review_offer(
    state: BankState,
) -> Command[Literal["draft_offer", "approval_gate", "operator_handoff"]]:
    """PATTERN 3: review & edit / reject with feedback.

    Resume value contract:
        {"decision": "approve"}
        {"decision": "edit", "edited_text": "..."}
        {"decision": "reject", "feedback": "..."}

    Routing depends on the resume value, so this node returns `Command(goto=...)`
    directly rather than relying on a separate conditional-edge function —
    the other style shown in this graph (see `route_after_intake` /
    `route_after_verification` for the `add_conditional_edges` style).
    """
    decision_payload = interrupt(
        {
            "type": "review",
            "draft": state.get("offer_text", ""),
            "allowed_decisions": ["approve", "edit", "reject"],
        }
    )
    decision_payload = decision_payload or {}
    decision = decision_payload.get("decision")
    round_no = state.get("review_round", 0)

    if decision == "approve":
        return Command(
            goto="approval_gate",
            update={"log": ["review_offer: черновик одобрен без изменений"]},
        )

    if decision == "edit":
        edited_text = decision_payload.get("edited_text") or state.get("offer_text", "")
        return Command(
            goto="approval_gate",
            update={
                "offer_text": edited_text,
                "log": ["review_offer: черновик отредактирован человеком"],
            },
        )

    if decision == "reject":
        feedback = decision_payload.get("feedback", "")
        new_round = round_no + 1
        if new_round >= MAX_REVIEW_ROUNDS:
            return Command(
                goto="operator_handoff",
                update={
                    "review_feedback": feedback,
                    "review_round": new_round,
                    "log": [
                        f"review_offer: отклонено {new_round} раз(а) подряд, "
                        "эскалация живому оператору"
                    ],
                },
            )
        return Command(
            goto="draft_offer",
            update={
                "review_feedback": feedback,
                "review_round": new_round,
                "log": [f"review_offer: отклонено, раунд {new_round}, обратная связь: {feedback!r}"],
            },
        )

    # Unknown/malformed decision — fail safe to a human rather than guessing.
    return Command(
        goto="operator_handoff",
        update={"log": [f"review_offer: нераспознанное решение {decision_payload!r}, эскалация"]},
    )


# --------------------------------------------------------------------------- #
# Node 6: approval_gate — PATTERN 4 "tool-call approval gate"
# --------------------------------------------------------------------------- #
def approval_gate(state: BankState) -> Command[Literal["submit_application", "notify"]]:
    """PATTERN 4: tool-call approval gate, payload-shaped like LangChain's
    `HumanInTheLoopMiddleware` (`action_requests` / `review_configs`).

    Resume value contract (mirrors the middleware's decision shapes):
        {"decisions": [{"type": "approve"}]}
        {"type": "edit", "edited_action": {"name": "...", "args": {...}}}
        {"type": "reject", "message": "..."}
    """
    action_request = {
        "name": "submit_application",
        "args": {
            "amount": state.get("amount"),
            "term_months": state.get("term_months"),
            "purpose": state.get("purpose"),
            "offer_text": state.get("offer_text"),
        },
        "description": "Отправить кредитную заявку в CRM банка",
    }
    review_configs = [
        {"action_name": "submit_application", "allowed_decisions": ["approve", "edit", "reject"]}
    ]

    resume_value = interrupt(
        {
            "type": "action_request",
            "action_requests": [action_request],
            "review_configs": review_configs,
        }
    )
    resume_value = resume_value or {}

    if "decisions" in resume_value:
        decisions = resume_value.get("decisions") or [{}]
        decision = decisions[0]
        if decision.get("type") == "approve":
            return Command(
                goto="submit_application",
                update={
                    "approved_action": action_request,
                    "log": ["approval_gate: действие 'submit_application' одобрено"],
                },
            )

    if resume_value.get("type") == "edit":
        edited = resume_value.get("edited_action") or {}
        merged_args = {**action_request["args"], **(edited.get("args") or {})}
        approved = {"name": edited.get("name", action_request["name"]), "args": merged_args}
        # The human edited the ACTION — the graph state must follow, otherwise
        # the final message / CRM record would still show the pre-edit figures.
        new_amount = merged_args.get("amount") or state.get("amount")
        new_term = merged_args.get("term_months") or state.get("term_months")
        new_purpose = merged_args.get("purpose") or state.get("purpose")
        offer_text = str(merged_args.get("offer_text") or state.get("offer_text") or "")
        terms_changed = (new_amount, new_term) != (state.get("amount"), state.get("term_months"))
        if terms_changed:
            payment = _monthly_payment(float(new_amount or 0), int(new_term or 1))
            amount_str = f"{new_amount:,.0f}".replace(",", " ")
            payment_str = f"{payment:,.0f}".replace(",", " ")
            offer_text += (
                "\nУсловия скорректированы при согласовании: "
                f"сумма {amount_str} сум, срок {new_term} мес., "
                f"платёж {payment_str} сум/мес."
            )
        return Command(
            goto="submit_application",
            update={
                "approved_action": approved,
                "amount": new_amount,
                "term_months": new_term,
                "purpose": new_purpose,
                "offer_text": offer_text,
                "log": [f"approval_gate: действие изменено человеком и одобрено: {merged_args}"],
            },
        )

    if resume_value.get("type") == "reject":
        message = resume_value.get("message", "Заявка отклонена на этапе согласования")
        return Command(
            goto="notify",
            update={
                "final_message": message,
                "log": [f"approval_gate: действие отклонено человеком: {message!r}"],
            },
        )

    return Command(
        goto="notify",
        update={
            "final_message": "Решение не распознано, заявка отклонена по умолчанию",
            "log": [f"approval_gate: нераспознанный формат ответа {resume_value!r}"],
        },
    )


# --------------------------------------------------------------------------- #
# Node 7: submit_application — the ONLY node with a real side effect
# --------------------------------------------------------------------------- #
def submit_application(state: BankState) -> dict[str, Any]:
    """Side effect happens strictly AFTER the approval interrupt (see the
    module docstring above `FAKE_CRM` for why this must be a separate node).
    This node itself never calls `interrupt()`, so it is not subject to the
    "whole node reruns on resume" replay concern — it runs exactly once per
    graph step, same as a normal node in a non-HITL graph."""
    action = state.get("approved_action") or {}
    args = action.get("args") or {}
    # The CRM record is built from the APPROVED action (possibly edited by a
    # human at `approval_gate`), never from whatever the graph computed before
    # the human looked at it.
    entry = {
        "action": action,
        "amount": args.get("amount", state.get("amount")),
        "term_months": args.get("term_months", state.get("term_months")),
        "purpose": args.get("purpose", state.get("purpose")),
    }
    FAKE_CRM.append(entry)
    application_id = f"APP-{len(FAKE_CRM):04d}"
    return {
        "application_id": application_id,
        "log": [f"submit_application: заявка сохранена в CRM, id={application_id}"],
    }


# --------------------------------------------------------------------------- #
# Node 8: operator_handoff — PATTERN 5 "handoff to a live operator"
# --------------------------------------------------------------------------- #
def _build_operator_summary(state: BankState) -> str:
    parts = [
        f"Клиент запросил: {state.get('purpose', '—')}, "
        f"сумма {state.get('amount', '—')}, срок {state.get('term_months', '—')} мес.",
        f"Риск: {state.get('risk_level', '—')} (DTI={state.get('dti', '—')})",
    ]
    verified = state.get("verified_docs")
    if verified:
        parts.append(f"Документы: {verified}")
    if state.get("review_feedback"):
        parts.append(f"Причина эскалации: {state['review_feedback']!r}, раунд {state.get('review_round')}")
    return " | ".join(parts)


def operator_handoff(state: BankState) -> dict[str, Any]:
    """PATTERN 5: handoff to a live operator, waiting indefinitely.

    Unlike the other patterns, there is no timeout and no expected shape for
    the resume value beyond "operator's free text reply" — the graph simply
    stays paused (a checkpoint on disk/DB, no process needs to stay alive)
    until someone calls `Command(resume=<text>)`. This mirrors the production
    bot's `node_human_mode_turn`, see README.md."""
    summary = _build_operator_summary(state)
    reply = interrupt({"type": "operator", "summary": summary})
    return {
        "operator_reply": str(reply),
        "log": ["operator_handoff: получен ответ живого оператора"],
    }


# --------------------------------------------------------------------------- #
# Node 9: notify — terminal node, composes the final message
# --------------------------------------------------------------------------- #
def notify(state: BankState) -> dict[str, Any]:
    if state.get("application_id"):
        text = (
            f"Заявка {state['application_id']} успешно отправлена в банк.\n"
            f"{state.get('offer_text', '')}"
        )
    elif state.get("operator_reply"):
        text = f"Ответ оператора: {state['operator_reply']}"
    elif state.get("final_message"):
        text = state["final_message"]
    else:
        text = "Заявка обработана."

    return {
        "final_message": text,
        "messages": [AIMessage(content=text)],
        "log": ["notify: сформировано финальное сообщение клиенту"],
    }


# --------------------------------------------------------------------------- #
# Graph assembly
# --------------------------------------------------------------------------- #
def build_graph(checkpointer: Any = None, use_llm: bool = False):
    """Compile the demo graph.

    Args:
        checkpointer: any LangGraph `BaseCheckpointSaver`. Defaults to an
            in-memory one (`InMemorySaver`) — fine for a single-process demo;
            production uses a Postgres-backed saver (see `app/agent/checkpointer.py`).
        use_llm: if True, `draft_offer` will try `ChatOpenAI` when
            `OPENAI_API_KEY` is set, falling back to the deterministic
            template on any failure. Defaults to False (fully offline demo).
    """
    if checkpointer is None:
        checkpointer = InMemorySaver()

    graph = StateGraph(BankState)

    graph.add_node("intake", intake)
    graph.add_node("risk_check", risk_check)
    graph.add_node("verify_document", verify_document)
    graph.add_node("collect_verification", collect_verification)
    # RetryPolicy demo: draft_offer is the node most likely to hit a
    # transient failure in real life (an LLM call) — max_attempts=2 means
    # LangGraph will retry the node body once before giving up.
    graph.add_node("draft_offer", make_draft_offer(use_llm), retry_policy=RetryPolicy(max_attempts=2))
    graph.add_node("review_offer", review_offer)
    graph.add_node("approval_gate", approval_gate)
    graph.add_node("submit_application", submit_application)
    graph.add_node("operator_handoff", operator_handoff)
    graph.add_node("notify", notify)

    graph.add_edge(START, "intake")

    # `add_conditional_edges` style (routing decided by a plain function,
    # separate from the node itself):
    graph.add_conditional_edges(
        "intake", route_after_intake, {"intake": "intake", "risk_check": "risk_check"}
    )

    # risk_check routes dynamically via the Command(goto=[Send(...), ...]) it
    # returns — no static edge from risk_check is declared here.
    graph.add_edge("verify_document", "collect_verification")
    graph.add_conditional_edges(
        "collect_verification",
        route_after_verification,
        {"operator_handoff": "operator_handoff", "draft_offer": "draft_offer"},
    )

    graph.add_edge("draft_offer", "review_offer")
    # review_offer and approval_gate route via Command(goto=...) themselves
    # (the other style, demoed deliberately alongside add_conditional_edges).

    graph.add_edge("submit_application", "notify")
    graph.add_edge("operator_handoff", "notify")
    graph.add_edge("notify", END)

    return graph.compile(checkpointer=checkpointer)
