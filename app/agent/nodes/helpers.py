from __future__ import annotations

import logging as _logging
import os
from datetime import datetime, timezone
from typing import Any, List, Optional

from langchain_core.messages import (
    AIMessage, HumanMessage, RemoveMessage, SystemMessage, trim_messages,
)
from langgraph.graph.message import REMOVE_ALL_MESSAGES

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
    ui_blocks: Optional[list] = None,
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
    user_msg = HumanMessage(content=history_text)
    # Store the RAW answer in history so subsequent turns never see the
    # "💡 Ассистент / Yordamchi" wrapper. The wrapper is applied to the
    # display answer only (returned in the "answer" field below).
    answer_msg = AIMessage(content=answer)
    msgs.append(user_msg)
    msgs.append(answer_msg)
    _max_tokens = int(os.getenv("MAX_DIALOG_TOKENS", "16000"))
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
    # `trim_messages` drops whole messages from the FRONT until the budget
    # fits, and with a small enough budget (or a single huge message) that
    # includes the exchange we just added — it can even return nothing at all.
    # A bot that answers and then has no record of having answered is worse
    # than one slightly over budget, so the current turn is always kept.
    if not msgs or msgs[-1] is not answer_msg:
        msgs = legacy_system_head + [user_msg, answer_msg]

    # `BotState.messages` carries the `add_messages` reducer, so what we return
    # is a DELTA, not the new full history.
    #
    # Common case — nothing was trimmed: return just this turn's two messages
    # and let the reducer append them. Returning the full list here instead
    # would duplicate the entire history on every single turn.
    #
    # Trimmed case: the reducer only ever adds, so dropping old messages needs
    # the documented full-replacement form — a `RemoveMessage(REMOVE_ALL_MESSAGES)`
    # sentinel followed by exactly the messages we want to keep. It is used
    # only when the budget actually bit, which is rare, and it is also what
    # normalises any legacy checkpoint whose messages predate this reducer.
    if len(msgs) == len(prior) + 2:
        messages_update: List[Any] = [user_msg, answer_msg]
    else:
        messages_update = [RemoveMessage(id=REMOVE_ALL_MESSAGES), *msgs]

    # Track consecutive fallback answers
    streak = dialog.get("fallback_streak", 0)
    streak = streak + 1 if is_fallback else 0

    show_operator = (
        streak >= FALLBACK_STREAK_THRESHOLD
        or _is_operator_request(user_text)
        or dialog.get("operator_requested", False)
    )
    dialog = {
        **dialog,
        "fallback_streak": streak,
        "operator_requested": False,
        # Recap-gap detector (node_router / nodes/recap.py) needs a
        # per-turn timestamp regardless of which flow finalized the turn.
        "last_turn_at": datetime.now(timezone.utc).isoformat(),
    }
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
        "messages": messages_update,
        "answer": display_answer,
        "dialog": dialog,
        "keyboard_options": keyboard_options,
        "show_operator_button": show_operator,
        # Mini App structured blocks (see BotState.ui_blocks docstring). The
        # Telegram path never reads this key — always safe to populate.
        "ui_blocks": list(ui_blocks) if ui_blocks else None,
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
