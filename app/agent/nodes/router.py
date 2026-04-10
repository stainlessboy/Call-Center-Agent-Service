from __future__ import annotations

from langgraph.types import Command

from app.agent.constants import FLOW_CALC, FLOW_PRODUCT_DETAIL
from app.agent.i18n import get_calc_questions
from app.agent.intent import _is_calc_trigger
from app.agent.state import BotState, _default_dialog


async def node_router(state: BotState) -> Command:
    """
    Minimal router:
    - human_mode active → human_mode node
    - lead_step or calc_flow active → calc_flow node
    - calc trigger button + selected product → calc_flow node (deterministic, no LLM)
    - everything else → faq node (LLM picks the right tool)
    """
    if state.get("human_mode"):
        return Command(goto="human_mode")
    dialog = state.get("dialog") or _default_dialog()
    if dialog.get("lead_step"):
        return Command(goto="calc_flow")
    if dialog.get("flow") == FLOW_CALC:
        return Command(goto="calc_flow")

    # Deterministic calc trigger: only via button press, not free text from LLM
    user_text = (state.get("last_user_text") or "").strip()
    if _is_calc_trigger(user_text) and dialog.get("flow") == FLOW_PRODUCT_DETAIL:
        category = dialog.get("category", "")
        calc_qs = get_calc_questions(category, state.get("_lang", "ru"))
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
