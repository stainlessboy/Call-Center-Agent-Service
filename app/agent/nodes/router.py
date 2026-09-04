from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Optional

from langgraph.types import Command

from app.agent.constants import FLOW_CALC, FLOW_PRODUCT_DETAIL, FLOW_QUALIFY
from app.agent.i18n import get_calc_questions
from app.agent.intent import _is_calc_trigger
from app.agent.state import BotState, _default_dialog

# Default minutes of inactivity within an in-progress calc_flow/qualify_flow
# before node_recap offers to resume ("Продолжим с того места...?") instead
# of silently continuing to interpret the next message as an answer to a
# question the customer may no longer remember being asked. Override via
# RECAP_GAP_MINUTES.
_RECAP_GAP_MINUTES_DEFAULT = 30


def _recap_gap_exceeded(last_turn_at: Optional[str]) -> bool:
    """True if the ISO-8601 `last_turn_at` timestamp is older than RECAP_GAP_MINUTES.

    `last_turn_at` is None for a flow that just started this same turn (set
    by helpers._finalize_turn on every finalized turn) — treated as "not
    stale" rather than an error.
    """
    if not last_turn_at:
        return False
    try:
        dt = datetime.fromisoformat(str(last_turn_at))
    except ValueError:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    gap_minutes = int(os.getenv("RECAP_GAP_MINUTES") or _RECAP_GAP_MINUTES_DEFAULT)
    return (datetime.now(timezone.utc) - dt).total_seconds() >= gap_minutes * 60


async def node_router(state: BotState) -> Command:
    """
    Minimal router:
    - human_mode active → human_mode node
    - a recap offer is already pending (dialog.recap_pending) → recap node,
      to interpret the customer's answer to it
    - an in-progress calc_flow/qualify_flow (incl. lead_step, which always
      carries flow=FLOW_CALC) has gone stale (RECAP_GAP_MINUTES since the
      last turn) → recap node, to offer resuming it
    - lead_step or calc_flow active → calc_flow node
    - qualify flow active → qualify_flow node (deterministic questionnaire)
    - calc trigger button + selected product → calc_flow node (deterministic, no LLM)
    - everything else → faq node (LLM picks the right tool)
    """
    if state.get("human_mode"):
        return Command(goto="human_mode")
    dialog = state.get("dialog") or _default_dialog()

    if dialog.get("recap_pending"):
        return Command(goto="recap")
    if dialog.get("flow") in (FLOW_CALC, FLOW_QUALIFY) and _recap_gap_exceeded(
        dialog.get("last_turn_at")
    ):
        return Command(goto="recap")

    if dialog.get("lead_step"):
        return Command(goto="calc_flow")
    if dialog.get("flow") == FLOW_CALC:
        return Command(goto="calc_flow")
    if dialog.get("flow") == FLOW_QUALIFY:
        return Command(goto="qualify_flow")

    # Deterministic calc trigger: only via button press, not free text from LLM
    user_text = (state.get("last_user_text") or "").strip()
    if _is_calc_trigger(user_text) and dialog.get("flow") == FLOW_PRODUCT_DETAIL:
        category = dialog.get("category", "")
        calc_qs = get_calc_questions(category, state.get("lang", "ru"))
        if calc_qs:
            first_step, _ = calc_qs[0]
            selected_product = dialog.get("selected_product")
            if selected_product is None:
                products = list(dialog.get("products") or [])
                if products:
                    selected_product = products[0]
            new_dialog = {
                **dialog,
                "flow": FLOW_CALC,
                "calc_step": first_step,
                "calc_slots": {},
                "selected_product": selected_product,
            }
            return Command(goto="calc_flow", update={"dialog": new_dialog})

    return Command(goto="faq")
