from __future__ import annotations

import logging as _logging
from typing import Optional

from langchain_core.messages import HumanMessage, SystemMessage

from app.agent.constants import FLOW_QUALIFY, FLOW_SHOW_PRODUCTS
from app.agent.i18n import at, category_label, get_main_menu_buttons
from app.agent.intent import _is_back_trigger, _looks_like_question
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
    prefill_from_profile,
    render_buttons,
)
from app.agent.state import BotState, _default_dialog, _reset_dialog
from app.utils.faq_tools import _faq_lookup

_agent_logger = _logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Dead-end rescue (Phase 4 "Экспертиза") — deterministic, no LLM. When a
# questionnaire branch ends in a dead_end (no matching offers for THIS
# category), deterministically pitch an ADJACENT, easier-to-qualify-for
# category instead of just leaving the customer at a "no offers" message.
#
# Adjacency is intentionally one-directional and narrow: only the three
# credit trees that actually HAVE a dead_end node today (autoloan, mortgage,
# microloan — see _TREES in app/agent/qualify.py) are covered; deposit/card
# trees have no dead_end node to rescue. microloan's OWN dead end
# (`dead_consider_others`, reached when the client has neither an official
# salary nor self-employment income) deliberately has NO rescue target —
# microloan is already the "easier" fallback for the other three, so there
# is no natural next-easier product within this set to suggest instead;
# behavior there is unchanged (plain message, dialog reset).
# ---------------------------------------------------------------------------
_DEAD_END_RESCUE_CATEGORY: dict[str, str] = {
    "mortgage": "microloan",
    "autoloan": "microloan",
    "education_credit": "microloan",
}


async def _dead_end_with_rescue(
    category: str, node: dict, lang: str,
) -> tuple[str, dict, list[str] | None, Optional[list[dict]]]:
    """Render a dead_end node's fixed message, then deterministically try to
    pitch an adjacent category's products (see _DEAD_END_RESCUE_CATEGORY)
    before resetting the dialog. No LLM involved — a plain rate-based
    ranking (recommend.rank_products with no client profile threaded
    through this deterministic path) picks up to 2 products, same shape as
    recommend_product's pitch.
    """
    base_message = at(node["message"], lang)
    reset_dialog = {**_default_dialog(), "last_lang": lang}

    rescue_category = _DEAD_END_RESCUE_CATEGORY.get(category)
    if not rescue_category:
        return base_message, reset_dialog, None, None

    from app.agent.products import _format_product_list_text, _get_products_by_category, _product_public_dict
    from app.agent.recommend import rank_products

    products = await _get_products_by_category(rescue_category)
    ranked = rank_products(products, None, rescue_category, top_n=2)
    if not ranked:
        return base_message, reset_dialog, None, None

    label = category_label(rescue_category, lang)
    answer = (
        f"{base_message}\n\n"
        f"{at('qualify_dead_end_rescue', lang, category=label)}\n\n"
        f"{_format_product_list_text(ranked, rescue_category, lang)}"
    )
    new_dialog = {
        **_default_dialog(),
        "flow": FLOW_SHOW_PRODUCTS,
        "category": rescue_category,
        "products": ranked,
        "last_lang": lang,
    }
    keyboard = [p["name"] for p in ranked] or None
    ui_blocks = [{
        "type": "product_list",
        "data": {
            "category": rescue_category,
            "kind": "recommend",
            "products": [_product_public_dict(p) for p in ranked],
        },
    }]
    return answer, new_dialog, keyboard, ui_blocks


# ---------------------------------------------------------------------------
# Shared rendering — used by node_qualify_flow AND by node_faq's entry point
# ---------------------------------------------------------------------------

async def render_filter_result(
    category: str, answers: dict, lang: str
) -> tuple[str, dict, list[str] | None, Optional[list[dict]]]:
    """Run the terminal DB filter and build the product-list reply.

    On success the dialog is set to a SHOW_PRODUCTS state (with the filtered
    products stored) so the existing select_product / product-detail / calculator
    chain keeps working unchanged. On empty match returns the "no products" text.
    Returns (answer, new_dialog, keyboard, ui_blocks).

    ui_blocks (Mini App, Phase 3): a `product_list` block for the filtered
    set, plus a `comparison_table` block when the questionnaire narrowed
    things down to MORE than one product — a single result doesn't need a
    comparison. `None` when there is nothing to show (empty match).
    """
    from app.agent.products import _comparison_columns, _format_product_list_text, _product_public_dict

    products = await filter_qualified_products(category, answers)
    if not products:
        return at("qualify_results_empty", lang), {**_default_dialog(), "last_lang": lang}, None, None

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

    public_products = [_product_public_dict(p) for p in products]
    ui_blocks: list[dict] = [{
        "type": "product_list",
        "data": {"category": category, "kind": "qualify_result", "products": public_products},
    }]
    if len(products) > 1:
        ui_blocks.append({
            "type": "comparison_table",
            "data": {
                "category": category,
                "columns": _comparison_columns(category),
                "products": public_products,
            },
        })
    return answer, new_dialog, keyboard, ui_blocks


async def _resolve_destination(
    category: str, node_key: str | None, answers: dict, lang: str
) -> tuple[str, dict, list[str] | None, Optional[list[dict]]]:
    """Resolve a destination node key into (answer, new_dialog, keyboard, ui_blocks).

    A question node is presented (and qualify state persisted, no ui_blocks —
    it's just a question); a filter terminal runs the DB filter (ui_blocks per
    render_filter_result); a dead-end shows its message and resets the dialog.
    """
    node = get_node(category, node_key) if node_key else None
    if node is None:
        return at("qualify_results_empty", lang), {**_default_dialog(), "last_lang": lang}, None, None

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
        return at(node["q"], lang), new_dialog, render_buttons(node, lang), None
    if ntype == NODE_FILTER:
        return await render_filter_result(category, answers, lang)
    if ntype == NODE_DEAD_END:
        return await _dead_end_with_rescue(category, node, lang)
    return at("qualify_results_empty", lang), {**_default_dialog(), "last_lang": lang}, None, None


_INCOME_TYPE_ACK_KEYS = {
    "payroll": "qualify_prefill_income_payroll",
    "official": "qualify_prefill_income_official",
    "no_official": "qualify_prefill_income_no_official",
}


def _profile_prefill_ack(applied_keys: list[str], facts: dict, lang: str) -> str:
    """"Исходя из того, что..." acknowledgement prefix, or "" if nothing from
    the profile was applied. Only the first applied key drives the wording —
    in practice that's always "income_type" (the only entry-adjacent
    profile_key marked in qualify.py's trees; "currency" is marked but
    currently unreachable via the auto-walk — see qualify.py's comment on
    the deposit tree's "currency" node) — the generic fallback keeps this
    robust if that ever changes.
    """
    if not applied_keys:
        return ""
    key = applied_keys[0]
    if key == "income_type":
        ack_key = _INCOME_TYPE_ACK_KEYS.get(facts.get("income_type"))
        if ack_key:
            return at("qualify_prefill_prefix", lang, fact=at(ack_key, lang))
    if key == "currency" and facts.get("currency"):
        fact_text = at("qualify_prefill_currency", lang, currency=facts["currency"])
        return at("qualify_prefill_prefix", lang, fact=fact_text)
    return at("qualify_prefill_prefix", lang, fact=at("qualify_prefill_generic", lang))


async def start_qualify(
    category: str, user_text: str, lang: str, profile: dict | None = None,
) -> tuple[str, dict, list[str] | None, Optional[list[dict]]]:
    """Entry point: begin the questionnaire for *category*.

    Two prefill passes, composed in order:
      1. Profile-based (`qualify.prefill_from_profile`) — silently answers
         any entry-adjacent question whose `profile_key` is known from
         `state.user_profile`, e.g. skipping "do you have an official
         salary?" when the client already told us their income type.
      2. Free-text, from *user_text* (`qualify.prefill`) — continues from
         wherever pass 1 stopped, so "хочу автокредит для BYD" can still
         prefill the auto-brand question even after the salary questions
         were already skipped by the profile.

    If pass 1 answered anything, the reply is prefixed with a short
    acknowledgement ("Исходя из того, что...", i18n `qualify_prefill_prefix`)
    so the skip doesn't feel like the bot forgot to ask.

    Returns (answer, new_dialog, keyboard, ui_blocks) — ui_blocks is only
    non-None when this call resolves straight to a filtered product list
    (both prefill passes together answered every question), see
    `render_filter_result`.
    """
    facts = (profile or {}).get("facts") or {}
    profile_node_key, profile_answers, applied_keys = prefill_from_profile(category, facts, lang)
    if profile_node_key is None:
        # No tree for this category at all — fall back to showing all products.
        return await render_filter_result(category, dict(profile_answers), lang)

    text_node_key, text_answers = prefill(category, user_text, lang, start_node=profile_node_key)
    merged_answers = {**profile_answers, **text_answers}

    answer, new_dialog, keyboard, ui_blocks = await _resolve_destination(
        category, text_node_key, merged_answers, lang
    )
    if applied_keys:
        answer = _profile_prefill_ack(applied_keys, facts, lang) + answer
    return answer, new_dialog, keyboard, ui_blocks


# ---------------------------------------------------------------------------
# Side-question handling (mirrors node_calc_flow)
# ---------------------------------------------------------------------------

async def _answer_side_question(user_text: str, lang: str, turn_usage: dict) -> str:
    faq_ans = await _faq_lookup(user_text, lang) or ""
    if faq_ans:
        return faq_ans
    llm = _get_chat_openai(role="consultant")
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

    # Escape hatch: "назад"/"отмена"/"cancel" etc. abandon the questionnaire
    # at any question — there was previously no way out short of finishing
    # it or explicitly asking for an operator. Unlike calc_flow there is no
    # product list to return to yet (qualify runs *before* products are
    # shown), so this always goes back to the main menu.
    if _is_back_trigger(user_text):
        new_dialog = _reset_dialog(dialog, last_lang=lang)
        return _finalize_turn(
            state, at("qualify_cancelled_to_menu", lang), new_dialog, get_main_menu_buttons(lang),
            is_fallback=False,
        )

    category = dialog.get("qualify_category")
    node_key = dialog.get("qualify_node")
    answers = dict(dialog.get("qualify_answers") or {})

    node = get_node(category, node_key) if (category and node_key) else None

    # Corrupted / stale qualify state — restart the flow if we still know the
    # category, otherwise bail out cleanly.
    if node is None or node.get("type") != NODE_QUESTION:
        if category:
            answer, new_dialog, keyboard, ui_blocks = await start_qualify(
                category, user_text, lang, state.get("user_profile")
            )
            return _finalize_turn(state, answer, new_dialog, keyboard, ui_blocks=ui_blocks)
        return _finalize_turn(
            state, at("qualify_results_empty", lang), {**_default_dialog(), "last_lang": lang}
        )

    opt = match_answer(node, user_text, lang)
    if opt is not None:
        answers.update(opt.get("set") or {})
        answer, new_dialog, keyboard, ui_blocks = await _resolve_destination(
            category, opt.get("goto"), answers, lang
        )
        return _finalize_turn(state, answer, new_dialog, keyboard, ui_blocks=ui_blocks)

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
        # Carry the incoming streak forward — _default_dialog() would reset
        # it to 0 every re-ask, which (combined with is_fallback=True below)
        # would make fallback_streak stick at 1 forever instead of
        # accumulating across consecutive unmatched answers.
        "fallback_streak": dialog.get("fallback_streak", 0),
    }

    turn_usage: dict = {}
    # is_fallback=True in both branches: the user's answer did not match any
    # option for the current question, so no questionnaire progress was made
    # this turn — this must count toward fallback_streak so the operator
    # button eventually surfaces (see helpers._finalize_turn).
    if _looks_like_question(user_text):
        side = await _answer_side_question(user_text, lang, turn_usage)
        prefix = f"{side}\n\n↩️ " if side else "↩️ "
        result = _finalize_turn(state, prefix + question_text, same_dialog, keyboard, is_fallback=True)
    else:
        result = _finalize_turn(state, question_text, same_dialog, keyboard, is_fallback=True)

    if turn_usage:
        finalize_usage(turn_usage)
        result["token_usage"] = turn_usage
    return result
