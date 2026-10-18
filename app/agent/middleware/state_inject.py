"""System-prompt assembly for the consultant agent (roadmap П.2).

Replaces the hand-rolled prompt building at the top of the legacy
``node_faq``. The shape is deliberately preserved, because it is what makes
OpenAI's automatic prompt caching (≥1024 tokens) hit:

  1. a STABLE ``SystemMessage`` per (language, mode) — the persona/policy;
  2. a SEPARATE ``SystemMessage`` carrying this turn's ``<state>`` XML.

Gluing them into one string would put the changing ``<state>`` block inside
the cached prefix and invalidate the cache on every single turn.
"""
from __future__ import annotations

from langchain_core.messages import SystemMessage

from app.agent.i18n import get_system_policy
from app.agent.middleware.common import dialog_of, lang_of, profile_of
from app.agent.state_xml import format_state_xml


def _policy_mode(dialog: dict) -> str:
    """Office-selection rules are an addendum on top of the cached base — they
    are only worth their tokens while a list of offices is on screen."""
    return "office_select" if dialog.get("offices") else "default"


def apply_system_prompt(request):
    """`wrap_model_call` body: attach policy + `<state>` to the request.

    Returns a NEW request (``ModelRequest`` is immutable).
    """
    state = request.state or {}
    dialog = dialog_of(state)
    lang = lang_of(state)

    policy = get_system_policy(lang, _policy_mode(dialog))
    messages = list(request.messages)

    state_xml = format_state_xml(dialog, profile_of(state))
    if state_xml:
        # Prepended (not appended to the policy string) so the policy stays a
        # byte-identical cacheable prefix across turns.
        messages = [SystemMessage(content=state_xml), *messages]

    return request.override(
        system_message=SystemMessage(content=policy),
        messages=messages,
    )
