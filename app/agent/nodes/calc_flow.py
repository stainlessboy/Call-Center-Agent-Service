from __future__ import annotations

import asyncio
import html as _html
import logging as _logging

from langchain_core.messages import HumanMessage, SystemMessage

from app.agent.constants import _REQUEST_LANGUAGE
from app.agent.i18n import _localized_name, at, get_calc_questions
from app.agent.intent import _is_yes, _looks_like_question
from app.agent.llm import _get_chat_openai
from app.agent.nodes.helpers import _finalize_turn, _save_lead_async
from app.agent.parsers import _parse_amount, _parse_downpayment, _parse_term_months
from app.agent.state import BotState, _default_dialog
from app.utils.faq_tools import _faq_lookup
from app.utils.pdf_generator import generate_amortization_pdf

_agent_logger = _logging.getLogger(__name__)


def _lookup_credit_rate(product: dict, calc_slots: dict) -> float:
    """Find the best matching rate from rate_matrix for user's inputs."""
    rate_matrix = product.get("rate_matrix") or []
    if not rate_matrix:
        return float(product.get("rate_min_pct") or product.get("rate_pct") or 20.0)

    term_months = calc_slots.get("term_months")
    downpayment = calc_slots.get("downpayment")

    best_rate = None
    best_score = -1

    for entry in rate_matrix:
        score = 0
        t_min = entry.get("term_min_months")
        t_max = entry.get("term_max_months")
        if term_months is not None and t_min is not None and t_max is not None:
            if t_min <= term_months <= t_max:
                score += 2
            else:
                continue
        d_min = entry.get("downpayment_min_pct")
        d_max = entry.get("downpayment_max_pct")
        if downpayment is not None and d_min is not None:
            if d_min <= downpayment <= (d_max or 100):
                score += 2
            else:
                continue
        rate = entry.get("rate_min_pct")
        if rate is not None and score > best_score:
            best_score = score
            best_rate = rate

    if best_rate is not None:
        return float(best_rate)

    all_rates = [e["rate_min_pct"] for e in rate_matrix if e.get("rate_min_pct") is not None]
    if all_rates:
        return float(min(all_rates))
    return float(product.get("rate_min_pct") or 20.0)


def _lookup_deposit_rate(product: dict, calc_slots: dict) -> float:
    """Find matching deposit rate for user's entered term_months."""
    rate_schedule = product.get("rate_schedule") or []
    term_months = calc_slots.get("term_months")

    if not rate_schedule or term_months is None:
        return float(product.get("rate_pct") or 15.0)

    for entry in rate_schedule:
        if entry.get("term_months") == term_months and entry.get("currency", "UZS") == "UZS":
            if entry.get("rate_pct") is not None:
                return float(entry["rate_pct"])

    for entry in rate_schedule:
        if entry.get("term_months") == term_months and entry.get("rate_pct") is not None:
            return float(entry["rate_pct"])

    closest = None
    closest_diff = float("inf")
    for entry in rate_schedule:
        if entry.get("rate_pct") is None:
            continue
        et = entry.get("term_months")
        if et is not None:
            diff = abs(et - term_months)
            if diff < closest_diff:
                closest_diff = diff
                closest = entry["rate_pct"]

    if closest is not None:
        return float(closest)

    return float(product.get("rate_pct") or 15.0)


async def node_calc_flow(state: BotState) -> dict:
    """Handles both calc_step (collecting calculator inputs) and lead_step (name/phone capture)."""
    user_text = (state.get("last_user_text") or "").strip()
    dialog = dict(state.get("dialog") or _default_dialog())

    if dialog.get("lead_step"):
        return await _handle_lead_step(state, user_text, dialog)
    return await _handle_calc_step(state, user_text, dialog)


async def _handle_lead_step(state: BotState, user_text: str, dialog: dict) -> dict:
    """Lead capture mini-flow: offer → name → phone → save."""
    lead_step = dialog.get("lead_step")
    category = dialog.get("category") or ""
    calc_slots = dict(dialog.get("calc_slots") or {})
    selected_product = dialog.get("selected_product") or {}
    lang = _REQUEST_LANGUAGE.get()

    if lead_step == "offer":
        if _is_yes(user_text):
            new_dialog = {**dialog, "lead_step": "name"}
            return _finalize_turn(state, at("lead_ask_name", lang), new_dialog)
        return _finalize_turn(state, at("lead_decline", lang), _default_dialog())

    if lead_step == "name":
        lead_slots = dict(dialog.get("lead_slots") or {})
        lead_slots["name"] = user_text
        new_dialog = {**dialog, "lead_step": "phone", "lead_slots": lead_slots}
        return _finalize_turn(state, at("lead_ask_phone", lang), new_dialog)

    if lead_step == "phone":
        lead_slots = dict(dialog.get("lead_slots") or {})
        lead_slots["phone"] = user_text
        try:
            await _save_lead_async({
                "session_id": state.get("session_id"),
                "user_id": state.get("user_id"),
                "category": category,
                "product_name": selected_product.get("name"),
                "amount": calc_slots.get("amount"),
                "term_months": calc_slots.get("term_months"),
                "rate_pct": _lookup_credit_rate(selected_product, calc_slots)
                if category != "deposit"
                else _lookup_deposit_rate(selected_product, calc_slots),
                "name": lead_slots.get("name", ""),
                "phone": lead_slots.get("phone", user_text),
            })
        except Exception as exc:
            _agent_logger.warning("lead save failed: %s", exc)
        return _finalize_turn(state, at("lead_saved", lang), _default_dialog())

    # Unexpected lead_step value — reset
    return _finalize_turn(state, at("lead_fallback", lang), _default_dialog())


async def _handle_calc_step(state: BotState, user_text: str, dialog: dict) -> dict:
    """Calculator step: collect amount/term/downpayment, then generate result."""
    category = dialog.get("category") or ""
    calc_step = dialog.get("calc_step")
    calc_slots = dict(dialog.get("calc_slots") or {})
    selected_product = dialog.get("selected_product") or {}
    lang = _REQUEST_LANGUAGE.get()

    # Parse answer for current step
    parsed_value = False
    if calc_step == "amount":
        val = _parse_amount(user_text)
        if val is not None:
            calc_slots["amount"] = val
            parsed_value = True
    elif calc_step == "term":
        val = _parse_term_months(user_text)
        if val is not None:
            calc_slots["term_months"] = val
            parsed_value = True
    elif calc_step == "downpayment":
        val = _parse_downpayment(user_text)
        if val is not None:
            calc_slots["downpayment"] = val
            parsed_value = True

    # Off-topic or unrecognised input during calc
    calc_qs = get_calc_questions(category, lang)
    if calc_step and not parsed_value:
        if _looks_like_question(user_text):
            # Answer the side question, then re-ask current step
            faq_ans = await _faq_lookup(user_text, lang) or ""
            if not faq_ans:
                llm = _get_chat_openai()
                if llm:
                    try:
                        ai_msg = await llm.ainvoke([
                            SystemMessage(content=at("calc_side_system", lang)),
                            HumanMessage(content=user_text),
                        ])
                        faq_ans = str(ai_msg.content or "").strip()
                    except Exception:
                        pass
            current_q = next((q for k, q in calc_qs if k == calc_step), "")
            prefix = f"{faq_ans}\n\n↩️ " if faq_ans else "↩️ "
            return _finalize_turn(state, prefix + current_q, {**dialog, "calc_slots": calc_slots})
        else:
            _hints = {
                "amount": at("hint_amount", lang),
                "term": at("hint_term", lang),
                "downpayment": at("hint_downpayment", lang),
            }
            return _finalize_turn(
                state,
                _hints.get(calc_step, at("hint_generic", lang)),
                {**dialog, "calc_slots": calc_slots},
            )

    # Find next unanswered question
    for step_key, step_q in calc_qs:
        slot_key = "term_months" if step_key == "term" else step_key
        if slot_key not in calc_slots:
            new_dialog = {**dialog, "calc_step": step_key, "calc_slots": calc_slots}
            return _finalize_turn(state, step_q, new_dialog)

    # All slots collected → generate result
    product_name = _localized_name(selected_product, lang) or selected_product.get("name") or "—"
    amount = int(calc_slots.get("amount") or 10_000_000)
    term_months = int(calc_slots.get("term_months") or 12)
    amount_fmt = f"{amount:,}".replace(",", " ")
    lead_keyboard = [at("btn_yes_call", lang), at("btn_no_thanks", lang)]

    if category == "deposit":
        rate_pct = _lookup_deposit_rate(selected_product, calc_slots)
        total_interest = amount * rate_pct / 100 * term_months / 12
        interest_fmt = f"{total_interest:,.0f}".replace(",", " ")
        total_fmt = f"{(amount + total_interest):,.0f}".replace(",", " ")
        answer = at(
            "deposit_result", lang,
            product=_html.escape(product_name),
            amount=amount_fmt,
            term=str(term_months),
            rate=f"{rate_pct:.1f}",
            interest=interest_fmt,
            total=total_fmt,
        )
        lead_dialog = {
            **_default_dialog(),
            "flow": "calc_flow",
            "category": category,
            "selected_product": selected_product,
            "calc_slots": calc_slots,
            "lead_step": "offer",
        }
        return _finalize_turn(state, answer, lead_dialog, lead_keyboard)

    # Credit → PDF amortization schedule
    rate_pct = _lookup_credit_rate(selected_product, calc_slots)
    try:
        pdf_path = await asyncio.to_thread(
            generate_amortization_pdf,
            product_name=product_name,
            principal=amount,
            annual_rate_pct=rate_pct,
            term_months=term_months,
            output_dir="/tmp",
        )
        answer = at(
            "credit_result_pdf", lang,
            product=_html.escape(product_name),
            amount=amount_fmt,
            rate=f"{rate_pct:.1f}",
            term=str(term_months),
            pdf_link=f"[[PDF:{pdf_path}]]",
        )
    except Exception:
        answer = at(
            "credit_result_fallback", lang,
            product=_html.escape(product_name),
            amount=amount_fmt,
            rate=f"{rate_pct:.1f}",
            term=str(term_months),
        )

    lead_dialog = {
        **_default_dialog(),
        "flow": "calc_flow",
        "category": category,
        "selected_product": selected_product,
        "calc_slots": calc_slots,
        "lead_step": "offer",
    }
    return _finalize_turn(state, answer, lead_dialog, lead_keyboard)
