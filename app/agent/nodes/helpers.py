from __future__ import annotations

import logging as _logging
import os
from typing import List, Optional

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, trim_messages

from app.agent.constants import FALLBACK_STREAK_THRESHOLD
from app.agent.i18n import at
from app.agent.intent import _is_operator_request
from app.agent.pii_masker import mask_pii
from app.agent.state import BotState, _STICKY_DIALOG_KEYS

_logger = _logging.getLogger(__name__)


def _finalize_turn(
    state: BotState,
    answer: str,
    dialog: dict,
    keyboard_options: Optional[List[str]] = None,
    *,
    is_fallback: bool = False,
    mask_user_text: Optional[str] = None,
    wrap_ai_generated: bool = False,
) -> dict:
    user_text = (state.get("last_user_text") or "").strip()
    # Existing checkpoints from older code may have a SystemMessage at index 0.
    # We don't write new ones (policy is rebuilt fresh each turn in node_faq),
    # but we preserve any legacy head as opaque so old sessions stay valid.
    prior = list(state.get("messages") or [])
    legacy_system_head = (
        [prior[0]] if prior and isinstance(prior[0], SystemMessage) else []
    )
    msgs = list(prior)
    # mask_user_text — explicit token override used by lead-name/phone steps
    # where we know exactly what the field is. Otherwise apply the generic
    # regex masker so volunteered PII (card, passport, IBAN, etc.) never
    # lands in the conversation history that gets sent to OpenAI.
    if mask_user_text is not None:
        history_text = mask_user_text
    else:
        history_text = mask_pii(user_text)
    msgs.append(HumanMessage(content=history_text))
    # Store the RAW answer in history so subsequent turns never see the
    # "💡 Ассистент / Yordamchi" wrapper. The wrapper is applied to the
    # display answer only (returned in the "answer" field below).
    msgs.append(AIMessage(content=answer))
    _max_tokens = int(os.getenv("MAX_DIALOG_TOKENS", "3000"))
    tail_source = msgs[len(legacy_system_head):]
    tail = trim_messages(
        tail_source,
        max_tokens=_max_tokens,
        token_counter="approximate",
        strategy="last",
        start_on="human",
        allow_partial=False,
    )
    msgs = legacy_system_head + tail

    # Track consecutive fallback answers
    streak = dialog.get("fallback_streak", 0)
    streak = streak + 1 if is_fallback else 0

    show_operator = (
        streak >= FALLBACK_STREAK_THRESHOLD
        or _is_operator_request(user_text)
        or dialog.get("operator_requested", False)
    )
    dialog = {**dialog, "fallback_streak": streak, "operator_requested": False}
    # Sticky session flags must survive dialog resets regardless of which node
    # rebuilt the dialog (qualify/calc helpers construct it from scratch).
    prior_dialog = state.get("dialog") or {}
    for _key in _STICKY_DIALOG_KEYS:
        if _key not in dialog and _key in prior_dialog:
            dialog[_key] = prior_dialog[_key]

    # Apply the "AI assistant" wrapper to the DISPLAY answer only, after
    # history has been built from the raw answer. This prevents the wrapper
    # text from appearing in subsequent turns and being mimicked by the model.
    display_answer = answer
    if wrap_ai_generated:
        lang = dialog.get("last_lang") or "ru"
        display_answer = at("ai_answer_wrapped", lang, body=answer)

    return {
        "messages": msgs,
        "answer": display_answer,
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
