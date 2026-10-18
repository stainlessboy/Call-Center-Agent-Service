"""`faq_pre` / `faq_post` — the deterministic halves around the consultant
agent (roadmap П.1).

The legacy `node_faq` was one function doing three jobs. Under `create_agent`
the middle job (the tool loop) belongs to the framework, so what remains is
split into two nodes around it:

    router → faq_pre → consultant → faq_post → END
                   ↘ (shortcut answered the turn) → END

`faq_pre` — everything that can answer without the model at all: the
"◀ Все продукты" back button, and the strict FAQ pre-check. Both existed
before and both skip the LLM entirely, which is most of this node's value.
It also appends the (PII-masked) customer message to the history, because
under `create_agent` the model reads `state["messages"]` directly — the
legacy node built a separate list and masked it there.

`faq_post` — everything the framework does not know about: which tools ran
and what that means for `dialog`, the qualification-flow entry, the
"is this a fallback / model-generated answer" classification that drives the
operator button and the disclaimer, and history hygiene.

History hygiene matters: the agent leaves its full working trace (assistant
tool-call turns and tool results) in `messages`. Product lists and office
cards are large, and every following turn would pay for them again, so the
turn is collapsed back to the one clean `Human → AI` exchange the rest of the
system expects (`trim_messages(start_on="human")`, the handoff summary, the
Mini App history screen).
"""
from __future__ import annotations

import logging as _logging
from typing import Any, Optional

from langchain_core.messages import AIMessage, RemoveMessage, ToolMessage
from langgraph.graph import END
from langgraph.types import Command

from app.agent.constants import FLOW_PRODUCT_DETAIL, FLOW_SHOW_PRODUCTS
from app.agent.faq_rephrase import rephrase_faq_answer
from app.agent.i18n import at
from app.agent.intent import _is_back_trigger
from app.agent.llm import finalize_usage
from app.agent.nodes.faq import (
    _PRODUCTIVE_TOOLS,
    _is_tool_error,
    _looks_like_giving_up,
    _normalize_user_text,
    _update_dialog_from_tools,
)
from app.agent.nodes.helpers import _finalize_turn
from app.agent.pii_masker import mask_pii
from app.agent.products import _format_product_list_text
from app.agent.qualify import QUALIFY_CATEGORIES
from app.agent.state import BotState, _default_dialog
from app.agent.tools import is_faq_sentinel
from app.utils.faq_tools import faq_precheck_answer, get_faq_fallback

_logger = _logging.getLogger(__name__)

# Marks how many messages the history held before this turn's agent run, so
# `faq_post` knows which ones to collapse. Lives in `dialog` because that is
# the one mutable per-session dict both nodes already share.
_BASELINE_KEY = "_turn_msg_baseline"


# ---------------------------------------------------------------------------
# faq_pre
# ---------------------------------------------------------------------------

async def node_faq_pre(state: BotState) -> Command:
    """Deterministic answers first; otherwise hand over to the agent."""
    user_text = (state.get("last_user_text") or "").strip()
    normalized_text = _normalize_user_text(user_text)
    dialog = dict(state.get("dialog") or _default_dialog())
    lang = state.get("lang") or dialog.get("last_lang") or "ru"

    # "◀ Все продукты" after a qualify-filtered list must re-show the STORED
    # filtered list. A fresh get_products would restart the questionnaire.
    if (
        _is_back_trigger(user_text)
        and dialog.get("products")
        and dialog.get("flow") in (FLOW_SHOW_PRODUCTS, FLOW_PRODUCT_DETAIL)
    ):
        products = list(dialog.get("products") or [])
        body = _format_product_list_text(products, dialog.get("category", ""), lang)
        new_dialog = {
            **dialog, "flow": FLOW_SHOW_PRODUCTS, "selected_product": None, "last_lang": lang,
        }
        keyboard = [p["name"] for p in products] or None
        return Command(
            goto=END,
            update=_finalize_turn(state, body, new_dialog, keyboard, is_fallback=False),
        )

    # Strict FAQ pre-check: the database already holds a verified answer, so
    # there is nothing for the model to decide. Reworded by one guarded call
    # (this path has no LLM turn that would otherwise do it).
    if normalized_text and not dialog.get("products") and not dialog.get("offices"):
        try:
            precheck = await faq_precheck_answer(normalized_text, lang)
        except Exception:
            precheck = None
        if precheck:
            answer_text = await rephrase_faq_answer(precheck, normalized_text, lang)
            return Command(
                goto=END,
                update=_finalize_turn(
                    state, answer_text, {**dialog, "last_lang": lang}, None, is_fallback=False,
                ),
            )

    # No model available (bad env, provider outage): answer from the FAQ
    # alone rather than letting the agent raise mid-run. The legacy node had
    # the same guard, and a bot that can still answer documented questions
    # during an OpenAI outage is worth the branch.
    from app.agent.nodes.faq import _get_chat_openai

    if _get_chat_openai(role="consultant") is None:
        _logger.warning("consultant: no LLM available, session=%s", state.get("session_id"))
        answer = await _faq_only_answer(user_text, lang)
        return Command(
            goto=END,
            update=_finalize_turn(
                state, answer or get_faq_fallback(lang), {**dialog, "last_lang": lang}, None,
                is_fallback=not answer,
            ),
        )

    # Hand over to the agent. The customer message enters the history here,
    # already masked, because the agent reads `messages` directly.
    history = list(state.get("messages") or [])
    dialog[_BASELINE_KEY] = len(history)
    dialog["last_lang"] = lang
    return Command(
        goto="consultant",
        update={
            "messages": [
                _human_message(mask_pii(normalized_text or user_text)),
            ],
            "dialog": dialog,
        },
    )


async def _faq_only_answer(user_text: str, lang: str) -> str:
    """Best-effort FAQ lookup used when no model can be reached."""
    from app.utils.faq_tools import _faq_lookup

    try:
        return await _faq_lookup(user_text, lang) or ""
    except Exception as exc:  # pragma: no cover - defensive
        _logger.debug("FAQ-only fallback failed: %s", exc)
        return ""


def _human_message(text: str):
    from langchain_core.messages import HumanMessage

    return HumanMessage(content=text)


# ---------------------------------------------------------------------------
# faq_post
# ---------------------------------------------------------------------------

def _turn_slice(state: BotState) -> list:
    """Messages the agent produced this turn (everything after the customer
    message `faq_pre` appended)."""
    messages = list(state.get("messages") or [])
    dialog = state.get("dialog") or {}
    baseline = dialog.get(_BASELINE_KEY)
    if not isinstance(baseline, int):
        return messages
    # +1 skips the customer message itself.
    return messages[baseline + 1:]


def _collect_tool_calls(turn_messages: list) -> list[dict]:
    calls: list[dict] = []
    for msg in turn_messages:
        for call in getattr(msg, "tool_calls", None) or []:
            calls.append(call)
    return calls


def _collect_ui_blocks(turn_messages: list) -> list[dict]:
    blocks: list[dict] = []
    for msg in turn_messages:
        artifact = getattr(msg, "artifact", None)
        if artifact:
            blocks.append(artifact)
    return blocks


def _collect_usage(turn_messages: list) -> dict:
    totals: dict = {}
    for msg in turn_messages:
        usage = getattr(msg, "usage_metadata", None) or {}
        if not usage:
            continue
        totals["prompt_tokens"] = totals.get("prompt_tokens", 0) + int(usage.get("input_tokens") or 0)
        totals["completion_tokens"] = totals.get("completion_tokens", 0) + int(usage.get("output_tokens") or 0)
        totals["total_tokens"] = totals.get("total_tokens", 0) + int(usage.get("total_tokens") or 0)
    return totals


def _final_answer(turn_messages: list) -> tuple[str, bool]:
    """The customer-facing text, and whether it came from a tool verbatim.

    A `return_direct` tool ends the agent with its own ToolMessage last —
    that text is already formatted for the customer and ships as-is. Otherwise
    the last AIMessage is the answer.
    """
    for msg in reversed(turn_messages):
        if isinstance(msg, ToolMessage):
            if _is_tool_error(msg):
                continue
            content = str(msg.content or "").strip()
            if content and not is_faq_sentinel(content):
                return content, True
            break
        if isinstance(msg, AIMessage):
            content = str(msg.content or "").strip()
            if content:
                return content, False
    return "", False


def _classify(answer: str, tool_calls: list[dict], turn_messages: list, fallback_reply: str) -> tuple[bool, bool]:
    """Return `(is_fallback, is_ai_generated)`, preserving the legacy rules.

    `is_fallback` drives the operator button after a streak of useless turns;
    `is_ai_generated` adds the "assistant, not verified bank info" wrapper.
    """
    called = {tc.get("name") for tc in tool_calls}

    if called & _PRODUCTIVE_TOOLS:
        return False, False

    if "faq_lookup" in called:
        results = {
            str(m.content or "").strip()
            for m in turn_messages
            if isinstance(m, ToolMessage) and not _is_tool_error(m)
        }
        all_sentinels = not results or all(is_faq_sentinel(r) for r in results)
        if not all_sentinels:
            return False, False
        is_fallback = (
            not answer or answer == fallback_reply or _looks_like_giving_up(answer, "")
        )
        return is_fallback, not is_fallback

    if not tool_calls:
        is_fallback = _looks_like_giving_up(answer, "")
        is_ai_generated = not is_fallback and bool(answer) and answer != fallback_reply
        return is_fallback, is_ai_generated

    return False, False


async def node_faq_post(state: BotState) -> dict:
    """Turn the agent's working trace into one finalized customer turn."""
    dialog = dict(state.get("dialog") or _default_dialog())
    lang = state.get("lang") or dialog.get("last_lang") or "ru"
    fallback_reply = get_faq_fallback(lang)

    turn_messages = _turn_slice(state)
    tool_calls = _collect_tool_calls(turn_messages)
    ui_blocks = _collect_ui_blocks(turn_messages)
    usage = _collect_usage(turn_messages)

    answer, from_tool = _final_answer(turn_messages)
    if not answer:
        answer = fallback_reply

    # Entry into the qualification questionnaire: a fresh product-browse
    # request asks a few filtering questions before listing anything.
    qualify_category = next(
        (
            (tc.get("args") or {}).get("category", "")
            for tc in tool_calls
            if tc.get("name") == "get_products"
            and (tc.get("args") or {}).get("category", "") in QUALIFY_CATEGORIES
        ),
        None,
    )
    if qualify_category:
        from app.agent.nodes.qualify_flow import start_qualify

        q_answer, q_dialog, q_keyboard, q_ui_blocks = await start_qualify(
            qualify_category, state.get("last_user_text") or "", lang, state.get("user_profile"),
        )
        q_dialog["last_lang"] = lang
        # The unfiltered product_list artifact `get_products` produced is
        # deliberately dropped: the questionnaire is about to contradict it.
        result = _finalize_turn(
            state, q_answer, q_dialog, q_keyboard, is_fallback=False, ui_blocks=q_ui_blocks,
        )
        return _with_clean_history(state, result, usage)

    is_fallback, is_ai_generated = _classify(answer, tool_calls, turn_messages, fallback_reply)
    if from_tool:
        # Pre-formatted tool output is never "the model made this up".
        is_ai_generated = False

    new_dialog, keyboard = await _update_dialog_from_tools(
        dialog, tool_calls, state.get("last_user_text") or "", lang, state.get("user_profile"),
    )
    new_dialog["last_lang"] = lang
    new_dialog["clarify_last_turn"] = bool(
        tool_calls and tool_calls[-1].get("name") == "clarify"
    )
    new_dialog.pop(_BASELINE_KEY, None)

    result = _finalize_turn(
        state, answer, new_dialog, keyboard,
        is_fallback=is_fallback,
        wrap_ai_generated=is_ai_generated,
        ui_blocks=ui_blocks,
    )
    return _with_clean_history(state, result, usage)


def _with_clean_history(state: BotState, result: dict, usage: dict) -> dict:
    """Collapse the agent's working trace and attach token usage.

    `_finalize_turn` already appended the answer; what is left is to delete
    the intermediate assistant/tool messages this turn produced, plus the
    duplicate customer message `_finalize_turn` adds (this path put its own,
    masked, copy into the history back in `faq_pre`).
    """
    removals = [
        RemoveMessage(id=msg.id)
        for msg in _turn_slice(state)
        if getattr(msg, "id", None)
    ]
    messages = list(result.get("messages") or [])
    # `_finalize_turn` returns either [Human, AI] or a full replacement; only
    # the plain-delta case needs the duplicate customer message dropped.
    if len(messages) == 2 and not isinstance(messages[0], AIMessage):
        messages = messages[1:]
    result["messages"] = [*removals, *messages]

    if usage:
        result["token_usage"] = finalize_usage(usage)
    return result
