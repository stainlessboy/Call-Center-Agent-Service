from __future__ import annotations

import asyncio
import html as _html
import logging as _logging

from langchain_core.messages import HumanMessage, SystemMessage

from app.agent.calc_extractor import (
    extract_calc_value,
    extract_prefill_from_history,
    extract_updated_value,
    regex_fallback,
)
from app.agent.constants import FLOW_CALC, STEP_AMOUNT, STEP_DOWNPAYMENT, STEP_TERM, _REQUEST_LANGUAGE
from app.agent.i18n import _localized_name, at, get_calc_questions
from app.agent.intent import _is_yes, _looks_like_question
from app.agent.llm import _get_chat_openai, accumulate_usage, extract_token_usage, finalize_usage
from app.agent.nodes.helpers import _finalize_turn, _save_lead_async
from app.agent.parsers import _parse_amount, _parse_downpayment, _parse_term_months
from app.agent.state import BotState, _default_dialog
from app.utils.faq_tools import _faq_lookup
from app.utils.pdf_generator import generate_amortization_pdf

_agent_logger = _logging.getLogger(__name__)


def _get_product_term_range(product: dict, category: str) -> tuple[int | None, int | None]:
    """Extract (min_months, max_months) from product constraints."""
    if category == "deposit":
        schedule = product.get("rate_schedule") or []
        terms = [e["term_months"] for e in schedule if e.get("term_months") is not None]
        if terms:
            return min(terms), max(terms)
        return None, None

    matrix = product.get("rate_matrix") or []
    all_min = [e["term_min_months"] for e in matrix if e.get("term_min_months") is not None]
    all_max = [e["term_max_months"] for e in matrix if e.get("term_max_months") is not None]
    t_min = min(all_min) if all_min else None
    t_max = max(all_max) if all_max else None
    return t_min, t_max


def _get_product_downpayment_range(product: dict) -> tuple[float | None, float | None]:
    """Extract (min_pct, max_pct) for downpayment from rate_matrix."""
    matrix = product.get("rate_matrix") or []
    all_min = [e["downpayment_min_pct"] for e in matrix if e.get("downpayment_min_pct") is not None]
    all_max = [e["downpayment_max_pct"] for e in matrix if e.get("downpayment_max_pct") is not None]
    d_min = min(all_min) if all_min else None
    d_max = max(all_max) if all_max else None
    return d_min, d_max


def _clamp_term(term_months: int, product: dict, category: str) -> tuple[int, bool]:
    """Clamp term to product constraints. Returns (clamped_value, was_adjusted)."""
    t_min, t_max = _get_product_term_range(product, category)

    if category == "deposit":
        schedule = product.get("rate_schedule") or []
        available = sorted({e["term_months"] for e in schedule if e.get("term_months") is not None})
        if available and term_months not in available:
            closest = min(available, key=lambda t: abs(t - term_months))
            return closest, True
        return term_months, False

    if t_min is not None and term_months < t_min:
        return t_min, True
    if t_max is not None and term_months > t_max:
        return t_max, True
    return term_months, False


def _clamp_downpayment(dp: float, product: dict) -> tuple[float, bool]:
    """Clamp downpayment to product constraints. Returns (clamped_value, was_adjusted)."""
    d_min, d_max = _get_product_downpayment_range(product)
    if d_min is not None and dp < d_min:
        return d_min, True
    if d_max is not None and dp > d_max:
        return d_max, True
    return dp, False


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
            return _finalize_turn(state, at("lead_saved", lang), _default_dialog())
        except Exception as exc:
            _agent_logger.exception("lead save failed: %s", exc)
            return _finalize_turn(state, at("lead_save_error", lang), _default_dialog())

    # Unexpected lead_step value — reset
    return _finalize_turn(state, at("lead_fallback", lang), _default_dialog())


async def _handle_calc_step(state: BotState, user_text: str, dialog: dict) -> dict:
    """Calculator step: collect amount/term/downpayment, then generate result."""
    category = dialog.get("category") or ""
    calc_step = dialog.get("calc_step")
    calc_slots = dict(dialog.get("calc_slots") or {})
    selected_product = dialog.get("selected_product") or {}
    lang = _REQUEST_LANGUAGE.get()

    # Parse answer for current step via LLM extractor, regex as fallback
    parsed_value = False
    adjustment_note = ""
    is_question = False
    calc_qs = get_calc_questions(category, lang)
    turn_usage: dict = {}

    # --- Improvement 2: Pre-fill slots from conversation history on first entry ---
    # First entry: calc_step is None and slots are empty — try to seed from history
    if not calc_step and not calc_slots:
        messages = state.get("messages") or []
        prefill = await extract_prefill_from_history(messages, category, lang)
        if prefill:
            confirmation_parts: list[str] = []
            if "amount" in prefill:
                amount_fmt = f"{prefill['amount']:,}".replace(",", " ")
                calc_slots["amount"] = prefill["amount"]
                confirmation_parts.append(at("calc_prefill_amount", lang, amount=amount_fmt))
            if "term_months" in prefill:
                calc_slots["term_months"] = prefill["term_months"]
            if confirmation_parts:
                adjustment_note = "\n".join(confirmation_parts)
            _agent_logger.debug(
                "calc prefill from history: category=%s prefill=%r", category, prefill
            )

    if calc_step:
        product_name = _localized_name(selected_product, lang) or selected_product.get("name") or ""
        recent_msgs = list(state.get("messages") or [])[-4:]
        extraction = await extract_calc_value(user_text, calc_step, product_name, lang, recent_messages=recent_msgs)
        accumulate_usage(turn_usage, extraction.get("_usage") or {})

        if extraction["type"] == "question":
            is_question = True
        elif extraction["type"] == "value":
            val = extraction["value"]
            try:
                if calc_step == STEP_AMOUNT:
                    calc_slots["amount"] = int(float(val))
                    parsed_value = True
                elif calc_step == STEP_TERM:
                    clamped, adjusted = _clamp_term(int(float(val)), selected_product, category)
                    if adjusted:
                        t_min, t_max = _get_product_term_range(selected_product, category)
                        if category == "deposit":
                            schedule = selected_product.get("rate_schedule") or []
                            available = sorted({e["term_months"] for e in schedule if e.get("term_months") is not None})
                            avail_str = ", ".join(str(t) for t in available)
                            adjustment_note = at("term_adjusted_deposit", lang, user_val=int(float(val)), new_val=clamped, available=avail_str)
                        else:
                            adjustment_note = at("term_adjusted", lang, user_val=int(float(val)), new_val=clamped, t_min=t_min or "?", t_max=t_max or "?")
                    calc_slots["term_months"] = clamped
                    parsed_value = True
                elif calc_step == STEP_DOWNPAYMENT:
                    clamped, adjusted = _clamp_downpayment(float(val), selected_product)
                    if adjusted:
                        d_min, d_max = _get_product_downpayment_range(selected_product)
                        adjustment_note = at("dp_adjusted", lang, user_val=f"{float(val):.0f}", new_val=f"{clamped:.0f}", d_min=f"{d_min:.0f}" if d_min else "?")
                    calc_slots["downpayment"] = clamped
                    parsed_value = True
            except (ValueError, TypeError):
                _agent_logger.warning("Invalid value from extractor: step=%s val=%r", calc_step, val)
                parsed_value = False
        else:
            # LLM unavailable — regex fallback
            val = regex_fallback(user_text, calc_step)
            if val is not None:
                if calc_step == STEP_AMOUNT:
                    calc_slots["amount"] = val
                    parsed_value = True
                elif calc_step == STEP_TERM:
                    clamped, adjusted = _clamp_term(val, selected_product, category)
                    if adjusted:
                        t_min, t_max = _get_product_term_range(selected_product, category)
                        if category == "deposit":
                            schedule = selected_product.get("rate_schedule") or []
                            available = sorted({e["term_months"] for e in schedule if e.get("term_months") is not None})
                            avail_str = ", ".join(str(t) for t in available)
                            adjustment_note = at("term_adjusted_deposit", lang, user_val=val, new_val=clamped, available=avail_str)
                        else:
                            adjustment_note = at("term_adjusted", lang, user_val=val, new_val=clamped, t_min=t_min or "?", t_max=t_max or "?")
                    calc_slots["term_months"] = clamped
                    parsed_value = True
                elif calc_step == STEP_DOWNPAYMENT:
                    clamped, adjusted = _clamp_downpayment(val, selected_product)
                    if adjusted:
                        d_min, d_max = _get_product_downpayment_range(selected_product)
                        adjustment_note = at("dp_adjusted", lang, user_val=f"{val:.0f}", new_val=f"{clamped:.0f}", d_min=f"{d_min:.0f}" if d_min else "?")
                    calc_slots["downpayment"] = clamped
                    parsed_value = True

    # User asked a question or gave ambiguous input — answer and re-ask
    if calc_step and (is_question or not parsed_value):
        if is_question or _looks_like_question(user_text):
            # --- Improvement 3: Check if the "question" is actually a context update ---
            product_name_ctx = _localized_name(selected_product, lang) or selected_product.get("name") or ""
            ctx_extraction = await extract_updated_value(
                user_text, calc_step, calc_slots, product_name_ctx, lang
            )
            accumulate_usage(turn_usage, ctx_extraction.get("_usage") or {})

            if ctx_extraction["type"] == "context_update":
                updates = ctx_extraction.get("updates") or {}
                for slot_key, slot_val in updates.items():
                    calc_slots[slot_key] = slot_val
                    if slot_key == "amount":
                        amount_fmt = f"{slot_val:,}".replace(",", " ")
                        adjustment_note = at("calc_context_update_amount", lang, amount=amount_fmt)
                _agent_logger.debug(
                    "calc context update applied: step=%s updates=%r", calc_step, updates
                )
                # Fall through to find the next unanswered question below
                parsed_value = True
                is_question = False
            else:
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
                            accumulate_usage(turn_usage, extract_token_usage(ai_msg))
                        except Exception as exc:
                            _agent_logger.debug("Side-question LLM failed: %s", exc)
                current_q = next((q for k, q in calc_qs if k == calc_step), "")
                prefix = f"{faq_ans}\n\n↩️ " if faq_ans else "↩️ "
                if turn_usage:
                    finalize_usage(turn_usage)
                result = _finalize_turn(state, prefix + current_q, {**dialog, "calc_slots": calc_slots})
                if turn_usage:
                    result["token_usage"] = turn_usage
                return result
        else:
            _hints = {
                STEP_AMOUNT: at("hint_amount", lang),
                STEP_TERM: at("hint_term", lang),
                STEP_DOWNPAYMENT: at("hint_downpayment", lang),
            }
            if turn_usage:
                finalize_usage(turn_usage)
            result = _finalize_turn(
                state,
                _hints.get(calc_step, at("hint_generic", lang)),
                {**dialog, "calc_slots": calc_slots},
            )
            if turn_usage:
                result["token_usage"] = turn_usage
            return result

    # Finalize cost before returning
    if turn_usage:
        finalize_usage(turn_usage)

    # Find next unanswered question
    for step_key, step_q in calc_qs:
        slot_key = "term_months" if step_key == STEP_TERM else step_key
        if slot_key not in calc_slots:
            # Auto-fill fixed-value steps (e.g. term=12..12 → skip question)
            if step_key == STEP_TERM:
                t_min, t_max = _get_product_term_range(selected_product, category)
                if t_min is not None and t_max is not None and t_min == t_max:
                    calc_slots["term_months"] = t_min
                    continue
            elif step_key == STEP_DOWNPAYMENT:
                d_min, d_max = _get_product_downpayment_range(selected_product)
                if d_min is not None and d_max is not None and d_min == d_max:
                    calc_slots["downpayment"] = d_min
                    continue

            new_dialog = {**dialog, "calc_step": step_key, "calc_slots": calc_slots}
            msg = f"{adjustment_note}\n\n{step_q}" if adjustment_note else step_q
            result = _finalize_turn(state, msg, new_dialog)
            if turn_usage:
                result["token_usage"] = turn_usage
            return result

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
        if adjustment_note:
            answer = f"{adjustment_note}\n\n{answer}"
        lead_dialog = {
            **_default_dialog(),
            "flow": FLOW_CALC,
            "category": category,
            "selected_product": selected_product,
            "calc_slots": calc_slots,
            "lead_step": "offer",
        }
        result = _finalize_turn(state, answer, lead_dialog, lead_keyboard)
        if turn_usage:
            result["token_usage"] = turn_usage
        return result

    # Credit → PDF amortization schedule
    rate_pct = _lookup_credit_rate(selected_product, calc_slots)
    try:
        pdf_path = await asyncio.to_thread(
            generate_amortization_pdf,
            product_name=product_name,
            principal=amount,
            annual_rate_pct=rate_pct,
            term_months=term_months,
            output_dir=None,
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
    if adjustment_note:
        answer = f"{adjustment_note}\n\n{answer}"

    lead_dialog = {
        **_default_dialog(),
        "flow": FLOW_CALC,
        "category": category,
        "selected_product": selected_product,
        "calc_slots": calc_slots,
        "lead_step": "offer",
    }
    result = _finalize_turn(state, answer, lead_dialog, lead_keyboard)
    if turn_usage:
        result["token_usage"] = turn_usage
    return result
