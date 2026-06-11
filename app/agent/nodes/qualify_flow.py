from __future__ import annotations

import logging as _logging

from langchain_core.messages import HumanMessage, SystemMessage

from app.agent.constants import FLOW_QUALIFY, FLOW_SHOW_PRODUCTS
from app.agent.i18n import at
from app.agent.intent import _looks_like_question
from app.agent.llm import (
    _get_chat_openai,
    accumulate_usage,
    extract_text_content,
    extract_token_usage,
    finalize_usage,
)
from app.agent.nodes.helpers import _finalize_turn
from app.agent.pii_masker import mask_pii
from app.agent.qualify import (
    NODE_DEAD_END,
    NODE_FILTER,
    NODE_QUESTION,
    filter_qualified_products,
    get_node,
    match_answer,
    prefill,
    render_buttons,
)
from app.agent.state import BotState, _default_dialog
from app.utils.faq_tools import _faq_lookup

_agent_logger = _logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Shared rendering — used by node_qualify_flow AND by node_faq's entry point
# ---------------------------------------------------------------------------

async def render_filter_result(category: str, answers: dict, lang: str) -> tuple[str, dict, list[str] | None]:
    """Run the terminal DB filter and build the product-list reply.

    On success the dialog is set to a SHOW_PRODUCTS state (with the filtered
    products stored) so the existing select_product / product-detail / calculator
    chain keeps working unchanged. On empty match returns the "no products" text.
    Returns (answer, new_dialog, keyboard).
    """
    from app.agent.products import _format_product_list_text

    products = await filter_qualified_products(category, answers)
    if not products:
        return at("qualify_results_empty", lang), {**_default_dialog(), "last_lang": lang}, None

    answer = at("qualify_results_header", lang) + "\n\n" + _format_product_list_text(products, category, lang)
    if category == "deposit" and answers.get("deposit_currency") == "EUR":
        answer += "\n\n" + at("qualify_deposit_eur_note", lang)

    new_dialog = {
        **_default_dialog(),
        "flow": FLOW_SHOW_PRODUCTS,
        "category": category,
        "products": products,
        "selected_product": None,
        "last_lang": lang,
    }
    keyboard = [p["name"] for p in products] or None
    return answer, new_dialog, keyboard


async def _resolve_destination(
    category: str, node_key: str | None, answers: dict, lang: str
) -> tuple[str, dict, list[str] | None]:
    """Resolve a destination node key into (answer, new_dialog, keyboard).

    A question node is presented (and qualify state persisted); a filter terminal
    runs the DB filter; a dead-end shows its message and resets the dialog.
    """
    node = get_node(category, node_key) if node_key else None
    if node is None:
        return at("qualify_results_empty", lang), {**_default_dialog(), "last_lang": lang}, None

    ntype = node.get("type")
    if ntype == NODE_QUESTION:
        new_dialog = {
            **_default_dialog(),
            "flow": FLOW_QUALIFY,
            "qualify_category": category,
            "qualify_node": node_key,
            "qualify_answers": dict(answers),
            "last_lang": lang,
        }
        return at(node["q"], lang), new_dialog, render_buttons(node, lang)
    if ntype == NODE_FILTER:
        return await render_filter_result(category, answers, lang)
    if ntype == NODE_DEAD_END:
        return at(node["message"], lang), {**_default_dialog(), "last_lang": lang}, None
    return at("qualify_results_empty", lang), {**_default_dialog(), "last_lang": lang}, None


async def start_qualify(category: str, user_text: str, lang: str) -> tuple[str, dict, list[str] | None]:
    """Entry point: begin the questionnaire for *category*.

    Prefills any answers the user already gave in *user_text* and jumps straight
    to the first unanswered question (or to a terminal if everything is known).
    """
    node_key, answers = prefill(category, user_text, lang)
    if not node_key:
        # No tree for this category — fall back to showing all products.
        return await render_filter_result(category, dict(answers), lang)
    return await _resolve_destination(category, node_key, dict(answers), lang)


# ---------------------------------------------------------------------------
# Side-question handling (mirrors node_calc_flow)
# ---------------------------------------------------------------------------

async def _answer_side_question(user_text: str, lang: str, turn_usage: dict) -> str:
    faq_ans = await _faq_lookup(user_text, lang) or ""
    if faq_ans:
        return faq_ans
    llm = _get_chat_openai()
    if not llm:
        return ""
    try:
        ai_msg = await llm.ainvoke([
            SystemMessage(content=at("calc_side_system", lang)),
            HumanMessage(content=mask_pii(user_text)),
        ])
        accumulate_usage(turn_usage, extract_token_usage(ai_msg))
        return extract_text_content(ai_msg).strip()
    except Exception as exc:
        _agent_logger.debug("qualify side-question LLM failed: %s", exc)
        return ""


# ---------------------------------------------------------------------------
# NODE: qualify_flow — deterministic branching questionnaire
# ---------------------------------------------------------------------------

async def node_qualify_flow(state: BotState) -> dict:
    """Walk the qualification decision tree one answer at a time.

    Matches the user's reply (button / free text / index) to the current
    question's options, advances the branch, and on a terminal either shows the
    DB-filtered products or a dead-end message. Unrecognized replies that look
    like questions are answered, then the current question is re-asked.
    """
    user_text = (state.get("last_user_text") or "").strip()
    dialog = dict(state.get("dialog") or _default_dialog())
    lang = state.get("lang") or dialog.get("last_lang") or "ru"

    category = dialog.get("qualify_category")
    node_key = dialog.get("qualify_node")
    answers = dict(dialog.get("qualify_answers") or {})

    node = get_node(category, node_key) if (category and node_key) else None

    # Corrupted / stale qualify state — restart the flow if we still know the
    # category, otherwise bail out cleanly.
    if node is None or node.get("type") != NODE_QUESTION:
        if category:
            answer, new_dialog, keyboard = await start_qualify(category, user_text, lang)
            return _finalize_turn(state, answer, new_dialog, keyboard)
        return _finalize_turn(
            state, at("qualify_results_empty", lang), {**_default_dialog(), "last_lang": lang}
        )

    opt = match_answer(node, user_text, lang)
    if opt is not None:
        answers.update(opt.get("set") or {})
        answer, new_dialog, keyboard = await _resolve_destination(
            category, opt.get("goto"), answers, lang
        )
        return _finalize_turn(state, answer, new_dialog, keyboard)

    # No option matched — re-ask the current question, answering a side question
    # first if the user asked one (don't lose questionnaire progress).
    keyboard = render_buttons(node, lang)
    question_text = at(node["q"], lang)
    same_dialog = {
        **_default_dialog(),
        "flow": FLOW_QUALIFY,
        "qualify_category": category,
        "qualify_node": node_key,
        "qualify_answers": answers,
        "last_lang": lang,
    }

    turn_usage: dict = {}
    if _looks_like_question(user_text):
        side = await _answer_side_question(user_text, lang, turn_usage)
        prefix = f"{side}\n\n↩️ " if side else "↩️ "
        result = _finalize_turn(state, prefix + question_text, same_dialog, keyboard)
    else:
        result = _finalize_turn(state, question_text, same_dialog, keyboard)

    if turn_usage:
        finalize_usage(turn_usage)
        result["token_usage"] = turn_usage
    return result
