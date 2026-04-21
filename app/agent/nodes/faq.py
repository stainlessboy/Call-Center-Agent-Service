from __future__ import annotations

import asyncio
import json
import logging as _logging
import os
from typing import List, Optional

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.prebuilt import ToolNode
from openai import APIError

from app.agent.constants import (
    _LANG_INSTRUCTION,
    _REQUEST_LANGUAGE,
    FLOW_CALC,
    FLOW_PRODUCT_DETAIL,
    FLOW_SHOW_PRODUCTS,
    resolve_language,
)
from app.agent.i18n import (
    SYSTEM_POLICY,
    at,
    get_calc_questions,
    get_credit_menu_buttons,
    get_main_menu_buttons,
)
from app.agent.llm import _get_chat_openai, accumulate_usage, extract_token_usage, finalize_usage
from app.agent.nodes.helpers import _finalize_turn
from app.agent.products import _find_product_by_name, _get_products_by_category
from app.agent.state import BotState, _default_dialog
from app.agent.tools import _FAQ_TOOLS
from app.utils.faq_tools import _faq_lookup, get_faq_fallback

_agent_logger = _logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Dialog state update from tool calls
# ---------------------------------------------------------------------------

def _reattach_keyboard(dialog: dict, lang: str) -> tuple[dict, Optional[List[str]]]:
    """Re-attach flow-appropriate keyboard."""
    flow = dialog.get("flow")
    products = list(dialog.get("products") or [])
    category = dialog.get("category", "")
    if flow == FLOW_PRODUCT_DETAIL:
        if category in ("debit_card", "fx_card"):
            return dict(dialog), [at("btn_submit_app", lang), at("btn_all_products", lang)]
        return dict(dialog), [at("btn_calc_payment", lang), at("btn_all_products", lang)]
    if flow == FLOW_SHOW_PRODUCTS and products:
        return dict(dialog), [p["name"] for p in products]
    return dict(dialog), None


async def _update_dialog_from_tools(
    dialog: dict, tool_calls: list, user_text: str, lang: str,
) -> tuple[dict, Optional[List[str]]]:
    """Inspect which tools the LLM called and update dialog/keyboard accordingly.

    `lang` must be the already-resolved language for this turn (see resolve_language).
    """
    if not tool_calls:
        return _reattach_keyboard(dialog, lang)

    last_tc = tool_calls[-1]
    name = last_tc["name"]
    args = last_tc.get("args", {})

    if name == "greeting_response":
        return _default_dialog(), get_main_menu_buttons(lang)

    if name == "thanks_response":
        return dict(dialog), None

    if name == "get_branch_info":
        return dict(dialog), None

    if name == "get_currency_info":
        return dict(dialog), None

    if name == "show_credit_menu":
        return dict(dialog), get_credit_menu_buttons(lang)

    if name == "get_products":
        category = args.get("category", "")
        products = await _get_products_by_category(category)
        new_dialog = {
            **_default_dialog(),
            "flow": FLOW_SHOW_PRODUCTS,
            "category": category,
            "products": products,
        }
        return new_dialog, [p["name"] for p in products] if products else None

    if name == "select_product":
        product_name = args.get("product_name", "")
        products = list(dialog.get("products") or [])
        category = dialog.get("category", "")
        matched = _find_product_by_name(product_name, products)
        if not matched and products:
            matched = products[0]
        new_dialog = {**dialog, "flow": FLOW_PRODUCT_DETAIL, "selected_product": matched}
        if category in ("debit_card", "fx_card"):
            return new_dialog, [at("btn_submit_app", lang), at("btn_all_products", lang)]
        return new_dialog, [at("btn_calc_payment", lang), at("btn_all_products", lang)]

    if name == "compare_products":
        products = list(dialog.get("products") or [])
        return dict(dialog), [p["name"] for p in products] if products else None

    if name == "back_to_product_list":
        products = list(dialog.get("products") or [])
        new_dialog = {**dialog, "flow": FLOW_SHOW_PRODUCTS, "selected_product": None}
        return new_dialog, [p["name"] for p in products] if products else None

    if name == "start_calculator":
        category = dialog.get("category", "")
        calc_qs = get_calc_questions(category, lang)
        if not calc_qs:
            return _default_dialog(), None
        first_step, _ = calc_qs[0]
        # Defensive: if selected_product was lost (e.g. LLM gave a text reply before
        # calling start_calculator), pick the first product from the dialog products list.
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
        return new_dialog, None

    if name == "faq_lookup":
        return _reattach_keyboard(dialog, lang)

    if name == "request_operator":
        return {**dialog, "operator_requested": True}, None

    return _reattach_keyboard(dialog, lang)


# ---------------------------------------------------------------------------
# NODE: faq — LLM with tools
# ---------------------------------------------------------------------------

async def node_faq(state: BotState) -> dict:
    """
    Main FAQ node. The LLM decides which tool to call based on user intent.
    """
    user_text = (state.get("last_user_text") or "").strip()
    dialog = dict(state.get("dialog") or _default_dialog())
    lang = _REQUEST_LANGUAGE.get()

    llm = _get_chat_openai()

    # Build message list for LLM
    existing_msgs = list(state.get("messages") or [SystemMessage(content=SYSTEM_POLICY)])
    lang_instruction = _LANG_INSTRUCTION.get(lang, "")
    system_content = SYSTEM_POLICY + lang_instruction

    # Add current state context for better tool selection
    context_parts: list[str] = []
    flow = dialog.get("flow")
    products = list(dialog.get("products") or [])
    if flow:
        context_parts.append(f"Current flow: {flow}")
    if products:
        numbered = ", ".join(f"{i+1}. {p['name']}" for i, p in enumerate(products[:10]))
        context_parts.append(f"Products displayed: {numbered}")
        context_parts.append(
            "If the user sends a number (e.g. '2'), call select_product with the corresponding product name."
        )
    if dialog.get("selected_product"):
        context_parts.append(f"Selected: {dialog['selected_product'].get('name')}")
    if dialog.get("category"):
        context_parts.append(f"Category: {dialog['category']}")
    if context_parts:
        system_content += "\n\nCurrent state:\n" + "\n".join(context_parts)

    if existing_msgs and isinstance(existing_msgs[0], SystemMessage):
        chat_msgs = [SystemMessage(content=system_content)] + existing_msgs[1:]
    else:
        chat_msgs = [SystemMessage(content=system_content)] + existing_msgs
    chat_msgs.append(HumanMessage(content=user_text))

    _max = int(os.getenv("MAX_DIALOG_MESSAGES", "50"))
    if len(chat_msgs) > _max + 1:
        chat_msgs = [chat_msgs[0]] + chat_msgs[-_max:]

    fallback_reply = get_faq_fallback(lang)
    answer = fallback_reply
    is_fallback = True
    tool_calls_made: list[dict] = []
    turn_usage: dict = {}

    llm_with_tools = llm.bind_tools(_FAQ_TOOLS)
    try:
        loop_msgs = list(chat_msgs)
        for _ in range(3):  # max 3 tool call rounds
            ai_msg = await llm_with_tools.ainvoke(loop_msgs)
            loop_msgs.append(ai_msg)
            accumulate_usage(turn_usage, extract_token_usage(ai_msg))

            tool_calls = getattr(ai_msg, "tool_calls", None) or []
            if not tool_calls:
                # No more tool calls → final answer
                content = str(getattr(ai_msg, "content", "") or "").strip()
                if content:
                    answer = content
                    is_fallback = False
                break

            tool_calls_made.extend(tool_calls)
            is_fallback = False
            tool_node = ToolNode(_FAQ_TOOLS)
            tool_results = await tool_node.ainvoke({"messages": loop_msgs, "dialog": dialog})
            loop_msgs.extend(tool_results.get("messages", []))
    except (asyncio.TimeoutError, APIError, json.JSONDecodeError) as exc:
        _agent_logger.warning("node_faq LLM failed: %s", exc)
        # Fall through to FAQ lookup fallback
        faq_ans = await _faq_lookup(user_text, lang)
        if faq_ans:
            answer = faq_ans
            is_fallback = False

    if turn_usage:
        finalize_usage(turn_usage)

    detected_lang = resolve_language(dialog, tool_calls_made, default=lang)
    new_dialog, keyboard = await _update_dialog_from_tools(
        dialog, tool_calls_made, user_text, detected_lang,
    )
    # Persist detected language for next turn (used by calc_flow via contextvar)
    new_dialog["last_lang"] = detected_lang

    result = _finalize_turn(state, answer, new_dialog, keyboard, is_fallback=is_fallback)
    if turn_usage:
        result["token_usage"] = turn_usage
    return result
