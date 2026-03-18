from __future__ import annotations

from langgraph.types import Command

from app.agent.state import BotState, _default_dialog


async def node_router(state: BotState) -> Command:
    """
    Minimal router:
    - human_mode active → human_mode node
    - lead_step or calc_flow active → calc_flow node
    - everything else → faq node (LLM picks the right tool)
    """
    if state.get("human_mode"):
        return Command(goto="human_mode")
    dialog = state.get("dialog") or _default_dialog()
    if dialog.get("lead_step"):
        return Command(goto="calc_flow")
    if dialog.get("flow") == "calc_flow":
        return Command(goto="calc_flow")
    return Command(goto="faq")
