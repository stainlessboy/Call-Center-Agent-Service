from __future__ import annotations

import json
import logging as _logging
import os
from typing import List, Optional

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, trim_messages
from langgraph.prebuilt import ToolNode
from openai import APIError

from app.agent.constants import (
    FLOW_CALC,
    FLOW_OFFICE_DETAIL,
    FLOW_PRODUCT_DETAIL,
    FLOW_SHOW_OFFICES,
    FLOW_SHOW_PRODUCTS,
)
from app.agent.faq_rephrase import rephrase_faq_answer
from app.agent.i18n import (
    at,
    get_calc_questions,
    get_credit_menu_buttons,
    get_main_menu_buttons,
    get_system_policy,
)
from app.agent.intent import _detect_product_category, _is_back_trigger
from app.agent.qualify import QUALIFY_CATEGORIES
from app.agent.llm import (
    _get_chat_openai,
    accumulate_usage,
    extract_text_content,
    extract_token_usage,
    finalize_usage,
)
from app.agent.nodes.helpers import _finalize_turn
from app.agent.pii_masker import mask_pii
from app.agent.products import (
    _find_product_by_name,
    _format_product_list_text,
    _get_products_by_category,
)
from app.agent.recommend import rank_products
from app.agent.state import BotState, _default_dialog, _reset_dialog
from app.agent.streaming import get_on_token_callback
from app.agent.tools import _FAQ_TOOLS, is_faq_sentinel
from app.utils.faq_tools import _faq_lookup, faq_precheck_answer, get_faq_fallback

_agent_logger = _logging.getLogger(__name__)

# Instantiated once at module load — ToolNode is stateless and safe to reuse
# across turns, avoiding repeated construction inside the per-turn tool loop.
_FAQ_TOOL_NODE = ToolNode(_FAQ_TOOLS)


async def _run_llm_round(llm_with_tools, loop_msgs: list, on_token) -> AIMessage:
    """Run one round of the tool-calling loop.

    Mini App streaming (Phase 3, see app/agent/streaming.py): when `on_token`
    is set, stream the round via `.astream()` and forward text chunks to the
    callback live, as they arrive. The moment a `tool_call_chunks` delta
    appears we know this round turned out to be a tool call, not prose for
    the user — stop forwarding further chunks for the rest of the round
    (nothing already sent can be un-sent, but OpenAI tool-calling rounds do
    not interleave prose with a tool call in practice, so this only ever
    silently drops an all-empty round). Telegram — and any Mini App turn
    where streaming wasn't armed — never sets `on_token`, so this always
    takes the plain `.ainvoke()` branch: behavior there is byte-for-byte
    unchanged from before Phase 3.

    Either branch returns a fully-accumulated AIMessage(-chunk) exposing the
    same `.content` / `.tool_calls` / usage shape the caller's round-handling
    logic already expects from `.ainvoke()`.
    """
    if on_token is None:
        return await llm_with_tools.ainvoke(loop_msgs)

    acc: Optional[AIMessage] = None
    is_tool_round = False
    async for chunk in llm_with_tools.astream(loop_msgs):
        acc = chunk if acc is None else acc + chunk
        if chunk.tool_call_chunks:
            is_tool_round = True
            continue
        if is_tool_round:
            continue
        text = extract_text_content(chunk)
        if text:
            try:
                await on_token(text)
            except Exception:
                _agent_logger.debug("on_token callback failed", exc_info=True)
    return acc if acc is not None else AIMessage(content="")


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


def _format_state_xml(dialog: dict, profile: Optional[dict] = None) -> str:
    """Serialize dialog state (+ optional client profile) as XML for the LLM
    system prompt.

    GPT-4o-mini parses XML tags more reliably than free-form 'Current state:' text.
    Returns empty string when there's nothing to report.

    `profile` is the "personal consultant" memory loaded once per turn by
    Agent._ainvoke_locked (app/agent/profile.py) — `{"facts": dict, "notes":
    str, "updated_at": ...}` or None. Only rendered when it actually carries
    something (non-empty facts or a non-empty notes string); a `UserProfile`
    row that exists but is still all-empty (freshly created, nothing learned
    yet) must not add a hollow `<client_profile>` block that wastes tokens
    and gives the LLM nothing to act on.
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

    if profile:
        facts = profile.get("facts") or {}
        notes = str(profile.get("notes") or "").strip()
        if facts or notes:
            lines.append("  <client_profile>")
            for key in sorted(facts.keys()):
                val = facts[key]
                val_str = ", ".join(str(v) for v in val) if isinstance(val, (list, tuple)) else str(val)
                val_str = val_str.strip()
                if val_str:
                    lines.append(f'    <fact key="{_xml_escape(str(key))}">{_xml_escape(val_str)}</fact>')
            if notes:
                lines.append(f"    <notes>{_xml_escape(notes)}</notes>")
            lines.append("  </client_profile>")
            lines.append(
                "  <hint>client_profile is background context about this returning "
                "customer — use it to personalize tone and avoid re-asking things "
                "they already told us, but never read it out loud or mention that "
                "you 'have a file' on them.</hint>"
            )

    if not lines:
        return ""
    return "<state>\n" + "\n".join(lines) + "\n</state>"


# ---------------------------------------------------------------------------
# Dialog state update from tool calls
# ---------------------------------------------------------------------------

def _reattach_keyboard(dialog: dict, lang: str) -> tuple[dict, Optional[List[str]]]:
    """Re-attach flow-appropriate keyboard.

    PRODUCT_DETAIL keeps its CTA buttons across side-questions (sticky context —
    user is "in" a product, deciding whether to apply). SHOW_PRODUCTS does NOT
    re-push the product list keyboard: the list is already visible upstream in
    chat, and pushing it on every unrelated reply makes the bot feel pushy.
    `dialog.products` is preserved either way, so user can still pick by name
    or index — `select_product` will match against the saved list.
    """
    flow = dialog.get("flow")
    category = dialog.get("category", "")
    if flow == FLOW_PRODUCT_DETAIL:
        if category in ("debit_card", "fx_card"):
            return dict(dialog), [at("btn_submit_app", lang), at("btn_all_products", lang)]
        return dict(dialog), [at("btn_calc_payment", lang), at("btn_all_products", lang)]
    return dict(dialog), None


async def _update_dialog_from_tools(
    dialog: dict, tool_calls: list, user_text: str, lang: str,
    profile: Optional[dict] = None,
) -> tuple[dict, Optional[List[str]]]:
    """Inspect which tools the LLM called and update dialog/keyboard accordingly.

    `lang` must be the already-resolved language for this turn (see resolve_language).
    `profile` is `state.user_profile` (see app/agent/profile.py) — only needed
    to re-derive the same ranking `recommend_product` already computed, so the
    exact products it pitched land in `dialog.products` for `select_product`.
    """
    if not tool_calls:
        return _reattach_keyboard(dialog, lang)

    last_tc = tool_calls[-1]
    name = last_tc["name"]
    args = last_tc.get("args", {})

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

        new_dialog = _reset_dialog(
            dialog,
            flow=FLOW_SHOW_OFFICES,
            office_type=office_type,
            offices=[
                {"name": _office_name(o, lang), "office_type": o.OFFICE_TYPE_CODE, "id": o.id}
                for o in offices
            ],
            last_lang=lang,
        )
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
        return _reset_dialog(dialog, last_lang=lang), None

    if name == "get_currency_info":
        return dict(dialog), None

    if name == "show_credit_menu":
        return dict(dialog), get_credit_menu_buttons(lang)

    if name == "get_products":
        category = args.get("category", "")
        products = await _get_products_by_category(category)
        new_dialog = _reset_dialog(
            dialog,
            flow=FLOW_SHOW_PRODUCTS,
            category=category,
            products=products,
        )
        return new_dialog, [p["name"] for p in products] if products else None

    if name == "recommend_product":
        goal = args.get("goal", "")
        category = _detect_product_category(goal or "") or dialog.get("category") or ""
        if not category or category == "credit_menu":
            return _reattach_keyboard(dialog, lang)
        products = await _get_products_by_category(category)
        if not products:
            return _reattach_keyboard(dialog, lang)
        ranked = rank_products(products, profile, category, top_n=2)
        if not ranked:
            return _reattach_keyboard(dialog, lang)
        new_dialog = _reset_dialog(
            dialog,
            flow=FLOW_SHOW_PRODUCTS,
            category=category,
            products=ranked,
            last_recommendation={
                "category": category,
                "goal": goal,
                "products": [p.get("name") for p in ranked],
            },
            last_lang=lang,
        )
        keyboard = [p["name"] for p in ranked] or None
        return new_dialog, keyboard

    if name == "select_product":
        product_name = args.get("product_name", "")
        products = list(dialog.get("products") or [])
        category = dialog.get("category", "")
        matched = _find_product_by_name(product_name, products)
        if not matched:
            # Tool already returned a "not found" user-facing message.
            # Do not switch flow or clobber selected_product with a wrong product.
            return _reattach_keyboard(dialog, lang)
        new_dialog = {**dialog, "flow": FLOW_PRODUCT_DETAIL, "selected_product": matched}
        if category in ("debit_card", "fx_card"):
            return new_dialog, [at("btn_submit_app", lang), at("btn_all_products", lang)]
        return new_dialog, [at("btn_calc_payment", lang), at("btn_all_products", lang)]

    if name == "start_calculator":
        category = dialog.get("category", "")
        calc_qs = get_calc_questions(category, lang)
        if not calc_qs:
            return _reset_dialog(dialog), None
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

    # NOTE: clarify is temporarily disabled (removed from _FAQ_TOOLS), so the
    # LLM can no longer emit a "clarify" tool call. The handler is gone; if the
    # tool is re-enabled, restore the keyboard-from-options branch here.

    if name == "request_operator":
        return {
            **dialog,
            "operator_requested": True,
            # Surfaced to a human operator in the Phase 4 handoff summary
            # (app/agent/handoff.py) when this turn escalates via the
            # "Живой оператор" button — see nodes/human_mode.py / commands.py.
            "operator_reason": args.get("reason", ""),
        }, None

    if name == "clarify":
        options = [str(o).strip() for o in (args.get("options") or []) if str(o).strip()]
        return dict(dialog), (options[:4] or None)

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
    "find_office", "select_office",
    "get_office_types_info", "get_currency_info", "show_credit_menu",
    "get_products", "select_product", "start_calculator",
    "custom_loan_calculator", "request_operator", "recommend_product",
    "compare_products", "what_if_scenario", "affordability_check", "clarify",
})


def _looks_like_giving_up(answer: str, lang: str) -> bool:  # noqa: ARG001
    """Return True only when the answer is an obvious 'I can't help' reply.

    Conservative by design — only catches clear giving-up phrases so that
    legitimate partial answers ("I can't do X, but here's Y") are not flagged.
    """
    if not answer or len(answer) < 10:
        return False
    return bool(_GIVING_UP_RE.search(answer))


def _is_tool_error(msg) -> bool:
    """True if a ToolMessage represents a failed tool execution.

    ToolNode(handle_tool_errors=True) returns a ToolMessage with status="error"
    and an "Error invoking tool ... Please fix ..." body when tool args fail
    validation (e.g. a weak LLM passing wrong kwargs). Such messages are
    internal — they must never be surfaced to the user.
    """
    if getattr(msg, "status", None) == "error":
        return True
    content = str(getattr(msg, "content", "") or "")
    # Belt-and-suspenders: ToolNode's error templates.
    return (
        content.startswith("Error invoking tool")
        or "Please fix the error and try again." in content
        or "Please fix your mistakes." in content
    )


def _last_useful_tool_output(loop_msgs: list) -> Optional[str]:
    """Walk loop_msgs backward and return the last ToolMessage content that
    is non-empty, not a sentinel value, and not a tool error — or None if the
    last tool result was a sentinel or error (not useful to surface to the user).
    """
    from langchain_core.messages import ToolMessage
    for msg in reversed(loop_msgs):
        if isinstance(msg, ToolMessage):
            if _is_tool_error(msg):
                # Tool error messages are internal — keep walking past them.
                continue
            content = (str(msg.content or "")).strip()
            if content and not is_faq_sentinel(content):
                return content
            # The last non-error tool result was a sentinel — nothing useful.
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

    # Deterministic "◀ All products" shortcut: after a qualify-filtered list the
    # back button must re-show the STORED filtered list, not restart the
    # questionnaire (a fresh get_products would otherwise re-enter FLOW_QUALIFY).
    if (
        _is_back_trigger(user_text)
        and dialog.get("products")
        and dialog.get("flow") in (FLOW_SHOW_PRODUCTS, FLOW_PRODUCT_DETAIL)
    ):
        products = list(dialog.get("products") or [])
        body = _format_product_list_text(products, dialog.get("category", ""), lang)
        new_dialog = {**dialog, "flow": FLOW_SHOW_PRODUCTS, "selected_product": None, "last_lang": lang}
        keyboard = [p["name"] for p in products] or None
        return _finalize_turn(state, body, new_dialog, keyboard, is_fallback=False)

    # Deterministic strict FAQ pre-check: skip the LLM entirely when the DB
    # already has a verified strict-tier answer for this query. This fires
    # only outside of product/office selection turns (where the user is picking
    # an item by index/name and the LLM must interpret the context).
    if (
        normalized_text
        and not dialog.get("products")
        and not dialog.get("offices")
    ):
        try:
            faq_precheck = await faq_precheck_answer(normalized_text, lang)
        except Exception:
            faq_precheck = None
        if faq_precheck:
            # Reword the verbatim DB answer into a natural reply before it
            # ships — this path skips the LLM entirely, so nothing else
            # would ever rephrase it. Guarded internally: falls back to
            # faq_precheck itself (verbatim) on any failure or if the
            # rewrite drops/changes a fact — see app/agent/faq_rephrase.py.
            answer_text = await rephrase_faq_answer(faq_precheck, normalized_text, lang)
            new_dialog = {**dialog, "last_lang": lang}
            return _finalize_turn(
                state, answer_text, new_dialog, None, is_fallback=False
            )

    llm = _get_chat_openai(role="consultant")

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

    _max_tokens = int(os.getenv("MAX_DIALOG_TOKENS", "16000"))
    if history_tail:
        history_tail = trim_messages(
            history_tail,
            max_tokens=_max_tokens,
            token_counter="approximate",
            strategy="last",
            start_on="human",
            allow_partial=False,
        )

    state_xml = _format_state_xml(dialog, state.get("user_profile"))
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

    if llm is None:
        # _get_chat_openai() failed to construct a client (bad env, transient
        # provider outage). Mirror the APIError fallback below instead of
        # crashing on llm.bind_tools(None): try the strict FAQ lookup, else
        # surface the generic fallback reply.
        _agent_logger.warning(
            "node_faq: no LLM available, session=%s", state.get("session_id"),
        )
        faq_ans = await _faq_lookup(user_text, lang)
        if faq_ans:
            answer = faq_ans
            is_fallback = False
        new_dialog, keyboard = await _update_dialog_from_tools(dialog, [], user_text, lang)
        new_dialog["last_lang"] = lang
        new_dialog["clarify_last_turn"] = False
        return _finalize_turn(state, answer, new_dialog, keyboard, is_fallback=is_fallback)

    # parallel_tool_calls=False keeps the per-round contract simple: the loop
    # below inspects a single tool call per round (_update_dialog_from_tools
    # only looks at tool_calls[-1], and the display-tool short-circuit assumes
    # one tool result). Some OpenAI-compatible backends (e.g. Gemma via vLLM)
    # emit multiple parallel tool calls even when the LLM object doesn't
    # advertise the kwarg — degrade gracefully in that case.
    try:
        llm_with_tools = llm.bind_tools(_FAQ_TOOLS, parallel_tool_calls=False)
    except TypeError:
        llm_with_tools = llm.bind_tools(_FAQ_TOOLS)
    on_token = get_on_token_callback()
    ui_blocks_acc: list[dict] = []
    max_rounds = 5
    # clarify anti-loop guard (Phase 4 — see the re-enable comment above
    # `clarify` in app/agent/tools.py): if the turn that produced the
    # CURRENT dialog was itself a clarify prompt, the model must not call
    # clarify again this turn. `clarify_displayed` tracks whether THIS
    # turn's own clarify call (if any) is the one actually shown to the
    # user, so `dialog["clarify_last_turn"]` can be set correctly below for
    # the NEXT turn's guard check.
    clarify_guard_active = bool(dialog.get("clarify_last_turn"))
    clarify_displayed = False
    try:
        loop_msgs = list(chat_msgs)
        hit_limit_with_pending_tools = False
        for round_idx in range(max_rounds):
            ai_msg = await _run_llm_round(llm_with_tools, loop_msgs, on_token)
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
            tool_results = await _FAQ_TOOL_NODE.ainvoke({"messages": loop_msgs, "dialog": dialog})
            new_tool_msgs = tool_results.get("messages", [])
            loop_msgs.extend(new_tool_msgs)

            # Mini App structured blocks (Phase 3): tools converted to
            # response_format="content_and_artifact" (app/agent/tools.py) put
            # their ui_block dict on ToolMessage.artifact — never in .content,
            # so this never reaches the LLM's context. Accumulated across
            # every round of the turn (not just the one that becomes `answer`)
            # per the Phase 3 spec; the one case this would be misleading —
            # a get_products() call intercepted by the FLOW_QUALIFY entry
            # below — is handled by NOT passing ui_blocks_acc into that
            # branch's _finalize_turn call further down.
            for _tm in new_tool_msgs:
                _artifact = getattr(_tm, "artifact", None)
                if _artifact:
                    ui_blocks_acc.append(_artifact)

            last_tc_name = tool_calls[-1].get("name") if tool_calls else None

            if last_tc_name == "clarify" and clarify_guard_active:
                # Second consecutive clarify attempt on the same ambiguity —
                # drop this tool result (don't display it, don't count it as
                # progress) and make it physically impossible to call again
                # this turn by rebinding without it, so the next round falls
                # through to faq_lookup / a direct answer instead.
                _agent_logger.info(
                    "node_faq: clarify anti-loop guard triggered, session=%s",
                    state.get("session_id"),
                )
                if new_tool_msgs:
                    # Neutralize the blocked ToolMessage's content so a later
                    # round-limit fallback (`_last_useful_tool_output`, which
                    # walks loop_msgs backward for the last non-sentinel
                    # ToolMessage) can never resurrect the very clarify
                    # prompt this guard just suppressed.
                    new_tool_msgs[-1].content = ""
                clarify_guard_active = False  # only strip once
                _remaining_tools = [t for t in _FAQ_TOOLS if getattr(t, "name", None) != "clarify"]
                try:
                    llm_with_tools = llm.bind_tools(_remaining_tools, parallel_tool_calls=False)
                except TypeError:
                    llm_with_tools = llm.bind_tools(_remaining_tools)
            # Display-tool short-circuit: tools return pre-formatted user-facing
            # text (product cards, office details, currency tables, calc results,
            # etc.). gpt-4o-mini consistently summarizes these in the wrapping
            # round — "Показал программу" instead of actually showing it. Surface
            # the tool output directly. Exception: faq_lookup sentinels mean the
            # LLM should chain to clarify/request_operator, so keep looping.
            # Also skip error ToolMessages — they are internal feedback for the
            # model (handle_tool_errors=True pattern) and must never reach users.
            #
            # Confident-FAQ rephrase (2026-08-11): a STRICT-tier faq_lookup
            # result is real, non-sentinel text — it lands in this branch just
            # like a product card or office detail, and would otherwise ship
            # the raw DB row verbatim (the i18n "rephrase, don't recite"
            # policy has no effect here because there is no further LLM turn
            # to follow it — the short-circuit's whole point is to skip one).
            # A LOW-confidence/no-match faq_lookup result is a sentinel, so it
            # never enters this branch at all — the loop keeps going and the
            # LLM composes the reply itself, where the i18n policy DOES
            # govern. Net architecture:
            #   confident FAQ hit (pre-check above, OR faq_lookup here)
            #       → deterministic rephrase_faq_answer() + fact guard
            #   low-confidence FAQ candidates → sentinel skips this branch,
            #       LLM composes the answer, i18n policy governs it
            #   every other display tool (product card, office detail, calc
            #       result, ...) → passed through AS-IS, never rephrased
            elif new_tool_msgs:
                last_msg = new_tool_msgs[-1]
                last_content = str(getattr(last_msg, "content", "") or "").strip()
                if (
                    last_content
                    and not is_faq_sentinel(last_content)
                    and not _is_tool_error(last_msg)
                ):
                    if last_tc_name == "faq_lookup":
                        answer = await rephrase_faq_answer(
                            last_content, normalized_text or user_text, lang
                        )
                    else:
                        answer = last_content
                    clarify_displayed = last_tc_name == "clarify"
                    break

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
            # Deliberately NOT run through rephrase_faq_answer() even when the
            # recovered content came from faq_lookup: this path only fires
            # after hitting the round-limit degraded case, an already-rare
            # failure mode — adding another LLM call here would spend an
            # extra round-trip (and another timeout risk) on the least
            # reliable path in the node instead of just shipping the verified
            # DB text, which is always a safe answer on its own.
            recovered = _last_useful_tool_output(loop_msgs)
            if recovered:
                answer = recovered

        # ---------------------------------------------------------------------------
        # ENTRY into FLOW_QUALIFY: a fresh get_products(category) browse request
        # starts the qualification questionnaire instead of listing immediately.
        # Informational tools (select_product, faq_lookup, find_office, …) are
        # untouched. The back-button case is handled by the shortcut at the top.
        # ---------------------------------------------------------------------------
        qualify_category = next(
            (
                (tc.get("args") or {}).get("category", "")
                for tc in tool_calls_made
                if tc.get("name") == "get_products"
                and (tc.get("args") or {}).get("category", "") in QUALIFY_CATEGORIES
            ),
            None,
        )
        if qualify_category:
            from app.agent.nodes.qualify_flow import start_qualify
            # NOTE: deliberately does NOT pass ui_blocks_acc here. The
            # get_products() call that triggered this branch already
            # produced a product_list artifact for the UNFILTERED category —
            # but the questionnaire is about to override `answer` with its
            # own question (or, if profile-prefill resolves it immediately,
            # its own FILTERED product_list/comparison_table). Showing the
            # premature unfiltered block would contradict the qualify text.
            q_answer, q_dialog, q_keyboard, q_ui_blocks = await start_qualify(
                qualify_category, user_text, lang, state.get("user_profile")
            )
            q_dialog["last_lang"] = lang
            result = _finalize_turn(
                state, q_answer, q_dialog, q_keyboard, is_fallback=False, ui_blocks=q_ui_blocks,
            )
            if turn_usage:
                finalize_usage(turn_usage)
                result["token_usage"] = turn_usage
            return result

        # ---------------------------------------------------------------------------
        # Determine is_fallback and is_ai_generated based on what happened.
        #
        # `is_ai_generated` flags answers produced by the LLM from general
        # knowledge (not a FAQ hit, not a tool's pre-formatted output). We wrap
        # those with a visible "Assistant" header + operator disclaimer so the
        # user can tell verified FAQ content from generated content.
        # ---------------------------------------------------------------------------
        called_names = {tc.get("name") for tc in tool_calls_made}
        is_ai_generated = False

        if hit_limit_with_pending_tools:
            is_fallback = True
        elif called_names & _PRODUCTIVE_TOOLS:
            is_fallback = False
        elif "faq_lookup" in called_names:
            # Collect all ToolMessage contents for faq_lookup calls.
            # Exclude error ToolMessages — they are internal model feedback and
            # must not influence the fallback determination (an error-only turn
            # must produce is_fallback=True, not flip to is_fallback=False).
            from langchain_core.messages import ToolMessage
            faq_results = {
                str(msg.content or "").strip()
                for msg in loop_msgs
                if isinstance(msg, ToolMessage) and not _is_tool_error(msg)
            }
            all_sentinels = not faq_results or all(is_faq_sentinel(r) for r in faq_results)
            if not all_sentinels:
                # At least one faq_lookup returned a real answer.
                is_fallback = False
            else:
                # All faq results were sentinels. The LLM is now allowed (per
                # the softened policy) to give a general banking answer. Treat
                # as fallback ONLY if the answer reads as a give-up reply —
                # otherwise the model produced useful content and we should NOT
                # show the generic "I didn't understand" UI.
                is_fallback = (
                    not answer
                    or answer == fallback_reply
                    or _looks_like_giving_up(answer, lang)
                )
                if not is_fallback:
                    is_ai_generated = True
        elif not tool_calls_made:
            # Free-text answer only — check if it reads as a give-up reply.
            is_fallback = _looks_like_giving_up(answer, lang)
            if not is_fallback and answer and answer != fallback_reply:
                is_ai_generated = True
        else:
            is_fallback = False

    except (APIError, json.JSONDecodeError) as exc:
        # APIError covers all openai network/timeout/status errors:
        #   APIConnectionError (incl. APITimeoutError) — transport-level failures
        #   APIStatusError (incl. RateLimitError, InternalServerError) — 4xx/5xx
        # json.JSONDecodeError covers malformed LLM response parsing.
        # asyncio.TimeoutError is NOT raised here because no asyncio.wait_for
        # wraps the loop; openai's own timeout raises APITimeoutError instead.
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
        dialog, tool_calls_made, user_text, lang, state.get("user_profile"),
    )
    new_dialog["last_lang"] = lang
    # Anti-loop guard bookkeeping for the NEXT turn — see the guard set-up
    # near the top of the tool-call loop above.
    new_dialog["clarify_last_turn"] = clarify_displayed

    result = _finalize_turn(
        state, answer, new_dialog, keyboard,
        is_fallback=is_fallback,
        wrap_ai_generated=is_ai_generated,
        ui_blocks=ui_blocks_acc,
    )
    if turn_usage:
        result["token_usage"] = turn_usage
    return result
