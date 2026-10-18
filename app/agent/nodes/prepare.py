"""node_prepare_turn — the graph's entry node (roadmap П.3).

Everything here used to run inside ``Agent._ainvoke_locked`` BEFORE the graph
was invoked, which forced the agent to read the whole checkpoint itself
(``aget_state``) just to see ``dialog`` — two Postgres round-trips per turn,
and a read-modify-write window that only an in-process lock kept safe.

As a node it gets ``dialog`` from the checkpoint for free, so the agent now
does a single graph invocation per turn and passes only genuinely new input.

Two jobs, in order:

1. **Language resolution** (unchanged hybrid logic, just relocated):
   an explicit ``User.language`` wins; otherwise the LLM detector runs; a
   cheap regex heuristic on top may *suggest* (never silently apply) a switch.
2. **Profile load** — the "personal consultant" memory, read once per turn so
   ``node_faq`` can put it in the system prompt.

Both are best-effort: a detector or DB failure degrades to "default language"
/ "no profile" rather than failing the customer's turn.
"""
from __future__ import annotations

import logging as _logging
from typing import Optional

from langgraph.types import Command

from app.agent.constants import VALID_LANGS, resolve_language
from app.agent.lang_detect import detect_language
from app.agent.lang_heuristic import check_lang_mismatch, looks_worth_llm_recheck
from app.agent.profile import load_user_profile
from app.agent.state import BotState, _default_dialog

_logger = _logging.getLogger(__name__)


async def _resolve_language(
    user_text: str, requested_lang: Optional[str], dialog: dict,
) -> tuple[str, Optional[str]]:
    """Return ``(language_for_this_turn, suggested_switch_or_None)``.

    1. ``requested_lang`` (``User.language``, chosen in /start) is
       authoritative and costs zero tokens.
    2. If it is unset (first contact), the dedicated LLM detector decides.
    3. A cheap regex heuristic runs on top: a message that clearly looks like
       another language produces a *suggestion* the bot layer offers to the
       user. We never switch silently.
    4. If the regex stayed silent on a "suspicious" message (Uzbek typos,
       short transliterated text), fall back to the LLM detector — its marker
       list is narrower than real usage. ``mask_pii`` is applied inside
       ``detect_language``, so raw PII never reaches the LLM.
    """
    primary_lang = requested_lang if requested_lang in VALID_LANGS else None
    llm_detected: Optional[str] = None
    if primary_lang is None:
        llm_detected = await detect_language(user_text, fallback=resolve_language(dialog))
        primary_lang = llm_detected

    suggested_lang: Optional[str] = check_lang_mismatch(user_text, primary_lang)
    if (
        suggested_lang is None
        and primary_lang in VALID_LANGS
        and looks_worth_llm_recheck(user_text)
    ):
        # Reuse the step-2 result if we already paid for it this turn.
        recheck = llm_detected if llm_detected is not None else await detect_language(
            user_text, fallback=primary_lang
        )
        if recheck in VALID_LANGS and recheck != primary_lang:
            suggested_lang = recheck

    # Don't re-prompt every turn: once a switch toward a given language has
    # been offered and answered, `dialog["lang_switch_offered"]` suppresses it.
    if suggested_lang and dialog.get("lang_switch_offered") == suggested_lang:
        suggested_lang = None
    return primary_lang, suggested_lang


async def node_prepare_turn(state: BotState) -> Command:
    """Resolve this turn's language and load the client profile, then hand
    over to the router."""
    user_text = (state.get("last_user_text") or "").strip()
    dialog = dict(state.get("dialog") or _default_dialog())

    # `lang` already set on the incoming state means the caller resolved the
    # language itself (tests drive the graph this way, and so does any caller
    # that already knows). Skip detection rather than paying for an LLM call
    # to re-derive a known answer.
    preset_lang = state.get("lang")
    if preset_lang in VALID_LANGS:
        primary_lang, suggested_lang = preset_lang, None
    else:
        try:
            primary_lang, suggested_lang = await _resolve_language(
                user_text, state.get("requested_lang"), dialog,
            )
        except Exception as exc:
            # The detector is a nicety; a turn answered in the previous
            # language beats a turn that fails.
            _logger.warning("Language resolution failed: %s", exc)
            primary_lang, suggested_lang = resolve_language(dialog), None

    dialog["last_lang"] = primary_lang
    if suggested_lang:
        # Remember the offer so the next turn doesn't re-prompt. Cleared
        # whenever a tool resets the dialog (greeting, get_products, ...).
        dialog["lang_switch_offered"] = suggested_lang

    user_profile = state.get("user_profile")
    user_id = state.get("user_id")
    if user_profile is None and user_id is not None:
        try:
            user_profile = await load_user_profile(user_id)
        except Exception as exc:
            _logger.warning("Failed to load user profile for user %s: %s", user_id, exc)
            user_profile = None

    return Command(
        goto="router",
        update={
            "lang": primary_lang,
            "dialog": dialog,
            "user_profile": user_profile,
            "suggested_lang": suggested_lang,
        },
    )
