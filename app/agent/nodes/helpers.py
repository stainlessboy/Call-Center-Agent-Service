from __future__ import annotations

import logging as _logging
import os
from typing import List, Optional

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, trim_messages

from app.agent.constants import FALLBACK_STREAK_THRESHOLD
from app.agent.i18n import at, get_system_policy
from app.agent.intent import _is_operator_request
from app.agent.pii_masker import mask_pii
from app.agent.state import BotState

_logger = _logging.getLogger(__name__)


def _finalize_turn(
    state: BotState,
    answer: str,
    dialog: dict,
    keyboard_options: Optional[List[str]] = None,
    *,
    is_fallback: bool = False,
    mask_user_text: Optional[str] = None,
) -> dict:
    user_text = (state.get("last_user_text") or "").strip()
    lang = (dialog or {}).get("last_lang") or "ru"
    msgs = list(state.get("messages") or [SystemMessage(content=get_system_policy(lang))])
    # mask_user_text — explicit token override used by lead-name/phone steps
    # where we know exactly what the field is. Otherwise apply the generic
    # regex masker so volunteered PII (card, passport, IBAN, etc.) never
    # lands in the conversation history that gets sent to OpenAI.
    if mask_user_text is not None:
        history_text = mask_user_text
    else:
        history_text = mask_pii(user_text)
    msgs.append(HumanMessage(content=history_text))
    msgs.append(AIMessage(content=answer))
    _max = int(os.getenv("MAX_DIALOG_MESSAGES", "12"))
    if len(msgs) > _max + 1:
        # Always preserve the leading SystemMessage; trim the rest with
        # tool-call/tool-result pair safety via trim_messages.
        head = [msgs[0]] if isinstance(msgs[0], SystemMessage) else []
        tail_source = msgs[1:] if head else msgs
        tail = trim_messages(
            tail_source,
            max_tokens=_max,
            token_counter=len,
            strategy="last",
            start_on="human",
            allow_partial=False,
        )
        msgs = head + tail

    # Track consecutive fallback answers
    streak = dialog.get("fallback_streak", 0)
    streak = streak + 1 if is_fallback else 0

    show_operator = (
        streak >= FALLBACK_STREAK_THRESHOLD
        or _is_operator_request(user_text)
        or dialog.get("operator_requested", False)
    )
    dialog = {**dialog, "fallback_streak": streak, "operator_requested": False}

    return {
        "messages": msgs,
        "answer": answer,
        "dialog": dialog,
        "keyboard_options": keyboard_options,
        "show_operator_button": show_operator,
    }


async def _save_lead_async(data: dict) -> None:
    from app.db.session import get_session
    from app.db.models import Lead
    async with get_session() as session:
        lead = Lead(
            session_id=data.get("session_id"),
            telegram_user_id=data.get("user_id"),
            product_category=data.get("category"),
            product_name=data.get("product_name"),
            amount=data.get("amount"),
            term_months=data.get("term_months"),
            rate_pct=data.get("rate_pct") or None,
            contact_name=data.get("name") or None,
            contact_phone=data.get("phone") or None,
        )
        session.add(lead)
        await session.commit()
