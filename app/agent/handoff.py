"""Operator handoff briefing (Phase 4 "Экспертиза").

When a session transfers to a live operator (the "Живой оператор" button in
`app/bot/handlers/commands.py::enable_human_mode`, or the Mini App's
`POST /chat/operator` in `app/miniapp/routes/chat.py::toggle_operator`),
`send_operator_handoff_summary` builds a short RUSSIAN briefing (operators
are Russian-speaking — confirmed against docs/CHAT_MIDDLEWARE_INTEGRATION.md,
which has no client-context field on `start-chat`'s fixed payload
`{userPhone, userName, lang, requestId, telegramId, isTestRequest}`) and
delivers it as the FIRST `send-message` to the middleware, right after
`start_chat` succeeds.

Persistence decision: the summary is saved to `Message` with `role="system"`
via `ChatService.save_system_note` — it is deliberately NOT a `role="user"`
or `role="agent"` row, so it never appears in `GET /chat/history` or
`GET /sessions/{id}` (both filter to `roles=("user", "agent", "operator")`)
and is never shown to the client, while still being visible in the DB /
admin export for audit purposes.

Every failure here (LLM unavailable, timeout, malformed context, middleware
send failure) is caught and logged — a summary is a nice-to-have, never a
requirement for the handoff itself to succeed.
"""
from __future__ import annotations

import asyncio
import logging as _logging
from typing import Any, Optional

from langchain_core.messages import HumanMessage, SystemMessage

from app.agent.i18n import category_label
from app.agent.llm import _get_chat_openai, extract_text_content

_logger = _logging.getLogger(__name__)

_HANDOFF_TIMEOUT_SECONDS = 10.0
_MAX_SUMMARY_CHARS = 700

_REASON_LABELS = {
    "user_request": "клиент явно попросил оператора",
    "identity_required": "нужна операция с верификацией личности",
    "unclear_message": "бот не смог разобраться в запросе",
}

# Only these profile facts are relevant to an operator picking up a banking
# conversation — never dump the whole `facts` blob (which may include
# free-text goals/preferences not worth an operator's attention, though
# never PII: the memory extractor is itself forbidden from storing PII —
# see app/agent/memory_extract.py's prompt).
_RELEVANT_PROFILE_KEYS = ("income_monthly", "income_type", "age", "family_status", "currency")


def _dialog_context_text(dialog: Optional[dict]) -> str:
    dialog = dialog or {}
    parts: list[str] = []

    category = dialog.get("category")
    if category:
        parts.append(f"Категория интереса: {category_label(category, 'ru')}")

    selected_name = (dialog.get("selected_product") or {}).get("name")
    if selected_name:
        parts.append(f"Выбранный продукт: {selected_name}")

    calc_slots = dialog.get("calc_slots") or {}
    bits: list[str] = []
    if calc_slots.get("amount"):
        bits.append(f"сумма {calc_slots['amount']:,}".replace(",", " "))
    if calc_slots.get("term_months"):
        bits.append(f"срок {calc_slots['term_months']} мес.")
    if calc_slots.get("downpayment"):
        bits.append(f"взнос {calc_slots['downpayment']}%")
    if bits:
        parts.append("Параметры расчёта: " + ", ".join(bits))

    qualify_answers = dialog.get("qualify_answers") or {}
    if qualify_answers:
        readable = ", ".join(f"{k}={v}" for k, v in qualify_answers.items())
        parts.append(f"Ответы анкеты: {readable}")

    return "\n".join(parts) if parts else "нет данных"


def _profile_context_text(profile: Optional[dict]) -> str:
    facts = (profile or {}).get("facts") or {}
    bits = [f"{key}: {facts[key]}" for key in _RELEVANT_PROFILE_KEYS if facts.get(key)]
    return "; ".join(bits) if bits else "нет данных"


def _recent_turns_text(recent_messages: Optional[list]) -> str:
    if not recent_messages:
        return "нет истории"
    lines: list[str] = []
    for msg in list(recent_messages)[-6:]:
        msg_type = getattr(msg, "type", "") or ""
        role_label = "Клиент" if msg_type == "human" else "Консультант"
        content = str(getattr(msg, "content", "") or "").strip()
        if content:
            lines.append(f"{role_label}: {content}")
    return "\n".join(lines) if lines else "нет истории"


_SUMMARY_PROMPT = """Ты готовишь краткую сводку для оператора банка, который сейчас примет диалог у бота. \
Сводка адресована оператору, а не клиенту — пиши по-деловому, на русском, без приветствий и вступлений.

Контекст диалога:
{dialog_context}

Известные факты о клиенте:
{profile_context}

Последние реплики диалога:
{recent_turns}

Причина перевода на оператора: {reason_text}

Составь сводку из 3-5 коротких пунктов: с чем пришёл клиент, что уже обсудили или посчитали, какие факты \
о клиенте стоит знать, в чём суть последнего вопроса/проблемы. НЕ включай ничего похожего на ФИО, номер \
карты/счёта/телефона/паспорта/ПИНФЛ — если такие данные встретятся в тексте выше (или в виде токенов \
[NAME]/[PHONE]/[CARD]/[PASSPORT]/[PINFL]/[IBAN]), просто пропусти их. Верни только текст сводки, без \
markdown-заголовков и вступлений, максимум {max_chars} символов."""


async def build_handoff_summary(
    *,
    dialog: Optional[dict] = None,
    user_profile: Optional[dict] = None,
    recent_messages: Optional[list] = None,
    reason: str = "",
) -> Optional[str]:
    """Build a short RU briefing for the human operator taking over this
    session. Returns None on ANY failure (LLM unavailable, timeout, empty
    response) — callers must treat that as "skip the summary this time",
    never let it block or fail the handoff itself.
    """
    try:
        llm = _get_chat_openai(role="extractor")
        if llm is None:
            return None
        reason_text = _REASON_LABELS.get(reason, reason or "не указана")
        prompt = _SUMMARY_PROMPT.format(
            dialog_context=_dialog_context_text(dialog),
            profile_context=_profile_context_text(user_profile),
            recent_turns=_recent_turns_text(recent_messages),
            reason_text=reason_text,
            max_chars=_MAX_SUMMARY_CHARS,
        )
        ai_msg = await asyncio.wait_for(
            llm.ainvoke([
                SystemMessage(content=prompt),
                HumanMessage(content="Составь сводку по инструкции выше."),
            ]),
            timeout=_HANDOFF_TIMEOUT_SECONDS,
        )
        text = extract_text_content(ai_msg).strip()
        return text[:_MAX_SUMMARY_CHARS] if text else None
    except Exception as exc:
        _logger.warning("Handoff summary build failed: %s", exc)
        return None


async def send_operator_handoff_summary(chat_service: Any, middleware_client: Any, session_id: str) -> None:
    """Build and deliver the operator handoff summary for *session_id* — the
    single call site both integration points (bot `enable_human_mode`, Mini
    App `toggle_operator`) use right after a successful
    `ChatMiddlewareClient.start_chat()`.

    Never raises: any failure here must not break the handoff the user just
    triggered — worst case the operator just doesn't get a summary this time.
    """
    try:
        context = await chat_service.agent_client.get_handoff_context(session_id)
        dialog = context.get("dialog") or {}
        summary = await build_handoff_summary(
            dialog=dialog,
            user_profile=context.get("user_profile"),
            recent_messages=context.get("messages"),
            reason=dialog.get("operator_reason", ""),
        )
        if not summary:
            return
        summary_text = f"[Сводка для оператора]\n{summary}"
        await middleware_client.send_message(session_id, summary_text)
        await chat_service.save_system_note(session_id, summary_text)
    except Exception:
        _logger.warning(
            "Failed to send operator handoff summary for session=%s", session_id, exc_info=True,
        )
