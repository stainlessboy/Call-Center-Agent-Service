from __future__ import annotations

from app.agent.nodes.helpers import _finalize_turn
from app.agent.state import BotState, _default_dialog


async def node_human_mode_turn(state: BotState) -> dict:
    """Pause graph and wait for operator reply via interrupt()."""
    from langgraph.types import interrupt as langgraph_interrupt
    user_text = (state.get("last_user_text") or "").strip()
    operator_reply = langgraph_interrupt({"user_message": user_text, "reason": "human_mode_active"})
    answer = str(operator_reply) if operator_reply else ""
    return _finalize_turn(state, answer, dict(state.get("dialog") or _default_dialog()))
