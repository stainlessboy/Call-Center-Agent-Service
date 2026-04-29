from __future__ import annotations

import asyncio
import json
import logging as _logging
import os
from typing import List, Optional

from langchain_core.messages import HumanMessage, SystemMessage, trim_messages
from langgraph.prebuilt import ToolNode
from openai import APIError

from app.agent.constants import (
    FLOW_CALC,
    FLOW_OFFICE_DETAIL,
    FLOW_PRODUCT_DETAIL,
    FLOW_SHOW_OFFICES,
    FLOW_SHOW_PRODUCTS,
)
from app.agent.i18n import (
    at,
    get_calc_questions,
    get_credit_menu_buttons,
    get_main_menu_buttons,
    get_system_policy,
)
from app.agent.llm import (
    _get_chat_openai,
    accumulate_usage,
    extract_text_content,
    extract_token_usage,
    finalize_usage,
)
from app.agent.nodes.helpers import _finalize_turn
from app.agent.pii_masker import mask_pii
from app.agent.products import _find_product_by_name, _get_products_by_category
from app.agent.state import BotState, _default_dialog
from app.agent.tools import FAQ_LOW_CONFIDENCE, NO_MATCH_IN_FAQ, _FAQ_TOOLS
from app.utils.faq_tools import _faq_lookup, get_faq_fallback

_agent_logger = _logging.getLogger(__name__)


def _normalize_user_text(text: str) -> str:
    """Light normalization of user input before handing it to the LLM.

    - collapses whitespace
    - strips surrounding punctuation noise ("!!!", "???")
    - limits length to 2000 chars (pathological pastes)

    We keep the original case / diacritics — they are linguistic signal.
    Returned string is what we send to the LLM; the raw original is still
    persisted in state for logging / history.
    """
    if not text:
        return ""
    import re
    s = text.strip()
    # Collapse internal whitespace (tabs, multiple spaces, newlines)
    s = re.sub(r"\s+", " ", s)
    # Trim repeated punctuation at both ends (!!!, ???, ... etc.)
    s = re.sub(r"^[\s!?.,;:\-–—]+", "", s)
    s = re.sub(r"[!?.,;:\-–—]{3,}\s*$", "", s)
    if len(s) > 2000:
        s = s[:2000]
    return s


def _xml_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _format_state_xml(dialog: dict) -> str:
    """Serialize dialog state as XML for the LLM system prompt.

    GPT-4o-mini parses XML tags more reliably than free-form 'Current state:' text.
    Returns empty string when there's nothing to report.
    """
    flow = dialog.get("flow")
    category = dialog.get("category", "")
    products = list(dialog.get("products") or [])
    selected = dialog.get("selected_product") or {}

    lines: list[str] = []
    if flow:
        lines.append(f"  <flow>{_xml_escape(str(flow))}</flow>")
    if category:
        lines.append(f"  <category>{_xml_escape(str(category))}</category>")
    if products:
        lines.append("  <products>")
        for i, p in enumerate(products[:10], start=1):
            name = _xml_escape(str(p.get("name", "")))
            lines.append(f'    <product index="{i}">{name}</product>')
        lines.append("  </products>")
        lines.append(
            "  <hint>If the user sends only a number (e.g. '2'), "
            "call select_product with the product at that index.</hint>"
        )
    if selected.get("name"):
        lines.append(f"  <selected_product>{_xml_escape(str(selected['name']))}</selected_product>")

    offices = list(dialog.get("offices") or [])
    selected_office = dialog.get("selected_office") or {}
    if offices:
        lines.append("  <offices>")
        for i, o in enumerate(offices[:10], start=1):
            name = _xml_escape(str(o.get("name", "")))
            lines.append(f'    <office index="{i}">{name}</office>')
        lines.append("  </offices>")
        lines.append(
            "  <hint>If the user sends only a number (e.g. '1') OR a word like "
            "'all'/'все'/'хаммаси'/'barchasi'/'hammasini' after offices were shown, call "
            "select_office. NEVER promise 'wait a few seconds' — fetch details NOW.</hint>"
        )
    if selected_office.get("name"):
        lines.append(
            f"  <selected_office>{_xml_escape(str(selected_office['name']))}</selected_office>"
        )

    if not lines:
        return ""
    return "<state>\n" + "\n".join(lines) + "\n</state>"


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

    if name == "find_office":
        from app.agent.branches import search_offices
        office_type = args.get("office_type", "")
        query = args.get("query", "")
        offices = (
            await search_offices(query=query, office_types=[office_type], limit=5)
            if office_type
            else []
        )

        def _office_name(obj, lng: str) -> str:
            if lng == "uz":
                val = getattr(obj, "name_uz", None)
                if val:
                    return val
            return getattr(obj, "name_ru", None) or ""

        new_dialog = {
            **_default_dialog(),
            "flow": FLOW_SHOW_OFFICES,
            "office_type": office_type,
            "offices": [
                {"name": _office_name(o, lang), "office_type": o.OFFICE_TYPE_CODE, "id": o.id}
                for o in offices
            ],
            "last_lang": lang,
        }
        keyboard = [item["name"] for item in new_dialog["offices"]] or None
        return new_dialog, keyboard

    if name == "select_office":
        office_name = args.get("office_name", "")
        offices_state = list(dialog.get("offices") or [])
        selected = None
        norm = (office_name or "").strip().lower()
        if norm.isdigit():
            idx = int(norm) - 1
            if 0 <= idx < len(offices_state):
                selected = offices_state[idx]
        else:
            for it in offices_state:
                if norm and norm in (it.get("name") or "").lower():
                    selected = it
                    break
        new_dialog = {
            **dialog,
            "flow": FLOW_OFFICE_DETAIL,
            "selected_office": selected,
        }
        return new_dialog, None

    if name == "get_office_types_info":
        return {**_default_dialog(), "last_lang": lang}, None

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

    if name == "clarify":
        options = args.get("options") or []
        keyboard: Optional[List[str]] = list(options) if options else None
        if not keyboard:
            # Preserve flow keyboard if any — clarify shouldn't strip context.
            _, keyboard = _reattach_keyboard(dialog, lang)
        return dict(dialog), keyboard

    if name == "request_operator":
        return {**dialog, "operator_requested": True}, None

    return _reattach_keyboard(dialog, lang)


# ---------------------------------------------------------------------------
# Fallback detection helpers
# ---------------------------------------------------------------------------

import re as _re

# Compiled once at module load — matches "giving up" phrases in all 3 languages.
_GIVING_UP_RE = _re.compile(
    r"переформулир|не понял|не могу помочь|не смог понять"
    r"|rephrase|i don.t understand|i cannot help|couldn.t understand"
    r"|tushunmadim|qaytadan yozing|tushuna olmadim",
    _re.IGNORECASE,
)

_PRODUCTIVE_TOOLS = frozenset({
    "greeting_response", "thanks_response", "find_office", "select_office",
    "get_office_types_info", "get_currency_info", "show_credit_menu",
    "get_products", "select_product", "start_calculator",
    "custom_loan_calculator", "request_operator", "clarify",
})


def _looks_like_giving_up(answer: str, lang: str) -> bool:  # noqa: ARG001
    """Return True only when the answer is an obvious 'I can't help' reply.

    Conservative by design — only catches clear giving-up phrases so that
    legitimate partial answers ("I can't do X, but here's Y") are not flagged.
    """
    if not answer or len(answer) < 10:
        return False
    return bool(_GIVING_UP_RE.search(answer))


def _last_useful_tool_output(loop_msgs: list) -> Optional[str]:
    """Walk loop_msgs backward and return the last ToolMessage content that
    is non-empty and not a sentinel value, or None if the last tool result
    was a sentinel (not useful to surface to the user).
    """
    from langchain_core.messages import ToolMessage
    for msg in reversed(loop_msgs):
        if isinstance(msg, ToolMessage):
            content = (str(msg.content or "")).strip()
            if content and content not in (NO_MATCH_IN_FAQ, FAQ_LOW_CONFIDENCE):
                return content
            # The last tool result was a sentinel — nothing useful to recover.
            return None
    return None


# ---------------------------------------------------------------------------
# NODE: faq — LLM with tools
# ---------------------------------------------------------------------------

async def node_faq(state: BotState) -> dict:
    """
    Main FAQ node. The LLM decides which tool to call based on user intent.
    """
    user_text = (state.get("last_user_text") or "").strip()
    normalized_text = _normalize_user_text(user_text)
    dialog = dict(state.get("dialog") or _default_dialog())
    lang = state.get("lang") or dialog.get("last_lang") or "ru"

    llm = _get_chat_openai()

    # Build message list for LLM.
    # Stable per-language policy and dynamic <state> XML go in SEPARATE
    # SystemMessages so OpenAI auto-prompt-caching (≥1024 tokens) can hit on the
    # policy block across turns. If we glued them into one string, every turn's
    # changing <state> would invalidate the cache prefix.
    #
    # `mode` selects an optional addendum on top of the cached base. When the
    # user is browsing offices we add OFFICE SELECTION rules; otherwise we
    # ship the lean base so the cache hits on more turns.
    policy_mode = "office_select" if dialog.get("offices") else "default"
    policy = get_system_policy(lang, policy_mode)
    existing_msgs = list(state.get("messages") or [SystemMessage(content=policy)])
    history_tail = existing_msgs[1:] if existing_msgs and isinstance(existing_msgs[0], SystemMessage) else list(existing_msgs)

    _max_tokens = int(os.getenv("MAX_DIALOG_TOKENS", "3000"))
    if history_tail:
        history_tail = trim_messages(
            history_tail,
            max_tokens=_max_tokens,
            token_counter="approximate",
            strategy="last",
            start_on="human",
            allow_partial=False,
        )

    state_xml = _format_state_xml(dialog)
    chat_msgs: list = [SystemMessage(content=policy)]
    if state_xml:
        chat_msgs.append(SystemMessage(content=state_xml))
    chat_msgs.extend(history_tail)
    chat_msgs.append(HumanMessage(content=mask_pii(normalized_text or user_text)))

    fallback_reply = get_faq_fallback(lang)
    answer = fallback_reply
    is_fallback = True
    tool_calls_made: list[dict] = []
    turn_usage: dict = {}

    llm_with_tools = llm.bind_tools(_FAQ_TOOLS)
    max_rounds = 3
    try:
        loop_msgs = list(chat_msgs)
        hit_limit_with_pending_tools = False
        for round_idx in range(max_rounds):
            ai_msg = await llm_with_tools.ainvoke(loop_msgs)
            loop_msgs.append(ai_msg)
            accumulate_usage(turn_usage, extract_token_usage(ai_msg))

            tool_calls = getattr(ai_msg, "tool_calls", None) or []
            if not tool_calls:
                # No more tool calls → final answer
                content = extract_text_content(ai_msg).strip()
                if content:
                    answer = content
                break

            tool_calls_made.extend(tool_calls)
            tool_node = ToolNode(_FAQ_TOOLS)
            tool_results = await tool_node.ainvoke({"messages": loop_msgs, "dialog": dialog})
            loop_msgs.extend(tool_results.get("messages", []))

            if round_idx == max_rounds - 1:
                hit_limit_with_pending_tools = True

        if hit_limit_with_pending_tools:
            _agent_logger.warning(
                "node_faq tool loop hit %d-round limit, session=%s last_tools=%s",
                max_rounds,
                state.get("session_id"),
                [tc.get("name") for tc in tool_calls_made[-max_rounds:]],
            )
            # Attempt to surface the last useful tool output directly so the
            # user gets their data even without an LLM wrapping turn.
            recovered = _last_useful_tool_output(loop_msgs)
            if recovered:
                answer = recovered

        # ---------------------------------------------------------------------------
        # Determine is_fallback based on what actually happened this turn.
        # ---------------------------------------------------------------------------
        called_names = {tc.get("name") for tc in tool_calls_made}

        if hit_limit_with_pending_tools:
            is_fallback = True
        elif called_names & _PRODUCTIVE_TOOLS:
            is_fallback = False
        elif "faq_lookup" in called_names:
            # Collect all ToolMessage contents for faq_lookup calls.
            from langchain_core.messages import ToolMessage
            faq_results = {
                str(msg.content or "").strip()
                for msg in loop_msgs
                if isinstance(msg, ToolMessage)
            }
            # All faq results were sentinels → no useful answer was found.
            is_fallback = faq_results <= {NO_MATCH_IN_FAQ, FAQ_LOW_CONFIDENCE}
        elif not tool_calls_made:
            # Free-text answer only — check if it reads as a give-up reply.
            is_fallback = _looks_like_giving_up(answer, lang)
        else:
            is_fallback = False

    except (asyncio.TimeoutError, APIError, json.JSONDecodeError) as exc:
        _agent_logger.warning("node_faq LLM failed: %s", exc)
        # Fall through to FAQ lookup fallback
        faq_ans = await _faq_lookup(user_text, lang)
        if faq_ans:
            answer = faq_ans
            is_fallback = False

    if turn_usage:
        finalize_usage(turn_usage)

    # The dedicated detector in agent._ainvoke already wrote state["lang"]
    # for this turn. Trust it over any `lang` arg the LLM put in tool_calls.
    new_dialog, keyboard = await _update_dialog_from_tools(
        dialog, tool_calls_made, user_text, lang,
    )
    new_dialog["last_lang"] = lang

    result = _finalize_turn(state, answer, new_dialog, keyboard, is_fallback=is_fallback)
    if turn_usage:
        result["token_usage"] = turn_usage
    return result
