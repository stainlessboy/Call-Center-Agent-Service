from __future__ import annotations

import re

from langgraph.types import Command

from app.agent.constants import FLOW_QUALIFY
from app.agent.i18n import _localized_name, at, category_label
from app.agent.nodes.helpers import _finalize_turn
from app.agent.state import BotState, _default_dialog, _reset_dialog


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").lower().strip())


_CONTINUE_TOKENS = ("продолж", "continue", "davom")
_RESTART_TOKENS = (
    "начать заново", "заново", "начни сначала", "с начала",
    "restart", "start over",
    "qaytadan", "boshidan", "boshqatdan boshla",
)


def _is_continue(text: str) -> bool:
    norm = _norm(text)
    return any(t in norm for t in _CONTINUE_TOKENS)


def _is_restart(text: str) -> bool:
    norm = _norm(text)
    return any(t in norm for t in _RESTART_TOKENS)


def _describe_progress(dialog: dict, lang: str) -> str:
    """Human-readable one-liner for the recap offer: what were we doing?"""
    if dialog.get("flow") == FLOW_QUALIFY:
        category = dialog.get("qualify_category") or ""
        return category_label(category, lang) if category else at("recap_generic_progress", lang)

    category = dialog.get("category") or ""
    label = category_label(category, lang) if category else ""
    product = dialog.get("selected_product") or {}
    product_name = _localized_name(product, lang) if product else ""
    if product_name and label:
        return f"{label} — {product_name}"
    return product_name or label or at("recap_generic_progress", lang)


def _recap_buttons(lang: str) -> list[str]:
    return [
        at("btn_recap_continue", lang),
        at("btn_recap_restart", lang),
        at("btn_recap_other", lang),
    ]


async def node_recap(state: BotState) -> dict | Command:
    """Offer to resume a stale in-progress calc_flow/qualify_flow, or handle
    the customer's reply to that offer.

    Reached from node_router in two situations (see router.py):
      1. `dialog.flow` is calc_flow/qualify_flow and the gap since
         `dialog.last_turn_at` exceeds RECAP_GAP_MINUTES — first entry here,
         `dialog.recap_pending` is not yet set.
      2. `dialog.recap_pending` is already True — the previous turn showed
         the offer, this turn is the customer's answer to it.

    First entry finalizes the turn itself (shows the offer + 3 buttons) and
    relies on the static recap -> END edge. The answer-handling branch
    returns a Command(goto=...) that hands off to another node WITHOUT
    finalizing a turn of its own — same dynamic-routing pattern node_router
    uses for calc/qualify/faq.
    """
    user_text = (state.get("last_user_text") or "").strip()
    dialog = dict(state.get("dialog") or _default_dialog())
    lang = state.get("lang") or dialog.get("last_lang") or "ru"

    if not dialog.get("recap_pending"):
        description = _describe_progress(dialog, lang)
        new_dialog = {**dialog, "recap_pending": True, "last_lang": lang}
        return _finalize_turn(
            state,
            at("recap_offer", lang, description=description),
            new_dialog,
            _recap_buttons(lang),
            is_fallback=False,
        )

    # Answering the offer. `recap_pending` is cleared in every branch below —
    # whatever happens next, this offer has been resolved.
    if _is_continue(user_text):
        # "Continue": hand off to whichever flow was in progress, dialog
        # otherwise untouched — that node re-processes `user_text` ("Продолжить"
        # / its localized equivalent) against its own current step. It won't
        # match a real answer, so calc_flow/qualify_flow's own "didn't
        # understand, here's the question again" path naturally re-displays
        # where the customer left off. KNOWN EDGE CASE: if the stale step was
        # specifically `lead_step == "offer"` ("want us to call?"), neither
        # "yes" nor "recalculate" matches this text either, so that specific
        # sub-flow reads it as a decline (dialog resets, polite "ok, no
        # problem" message) rather than re-showing the offer — a rare timing
        # window (the gap has to land exactly on that one step) accepted as
        # a known limitation rather than special-cased here.
        target = "qualify_flow" if dialog.get("flow") == FLOW_QUALIFY else "calc_flow"
        cleared = {**dialog, "recap_pending": False, "last_lang": lang}
        return Command(goto=target, update={"dialog": cleared})

    if _is_restart(user_text):
        return Command(goto="faq", update={"dialog": _reset_dialog(dialog, last_lang=lang)})

    # "Different question" (button) or any other free text — drop the
    # recap_pending flag and let node_faq treat this turn as an ordinary
    # question. dialog.flow/category/selected_product are deliberately left
    # as-is (not reset) so the LLM still has that context available via
    # `<state>` if it's relevant to the follow-up.
    cleared = {**dialog, "recap_pending": False, "last_lang": lang}
    return Command(goto="faq", update={"dialog": cleared})
