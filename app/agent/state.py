from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List, Optional, TypedDict


class BotState(TypedDict):
    messages: List[Any]
    last_user_text: str
    answer: str
    human_mode: bool
    keyboard_options: Optional[List[str]]
    dialog: dict          # see _default_dialog()
    lang: str             # "ru" | "en" | "uz" — set by the language detector in agent._ainvoke
    _route: str
    session_id: Optional[str]
    user_id: Optional[int]
    show_operator_button: bool
    token_usage: Optional[dict]  # {"model": str, "prompt_tokens": int, "completion_tokens": int, "total_tokens": int, "cost": float}


@dataclass
class AgentTurnResult:
    """Structured result returned by Agent.send_message."""
    text: str
    keyboard_options: Optional[List[str]] = None
    show_operator_button: bool = False
    token_usage: Optional[dict] = None
    # Set when the user's message looks like it's in a different language than
    # User.language. The bot layer surfaces an inline "switch?" prompt; we
    # never switch silently. None when no mismatch was detected.
    suggested_language: Optional[str] = None


def _default_dialog() -> dict:
    return {
        "flow": None,
        "category": None,
        "products": [],
        "selected_product": None,
        "calc_step": None,
        "calc_slots": {},
        "lead_step": None,
        "lead_slots": {},
        "qualify_category": None,
        "qualify_node": None,
        "qualify_answers": {},
        "fallback_streak": 0,
        "last_lang": "ru",
        "offices": [],
        "selected_office": None,
        "office_type": None,
    }


# Keys that survive a dialog reset — they are session-scoped flags that must
# not be wiped when the LLM transitions between product/office browsing flows.
_STICKY_DIALOG_KEYS = ("lang_switch_offered",)


def _reset_dialog(current: dict, **overrides) -> dict:
    """Return a fresh default dialog, preserving sticky per-session keys from `current`.

    Use this instead of bare ``{**_default_dialog(), ...}`` whenever the dialog
    is being reset mid-session (e.g. after find_office / get_products) so that
    flags like ``lang_switch_offered`` are not accidentally cleared.

    Keyword arguments are merged last, identical to ``{**_default_dialog(), **overrides}``.
    """
    base = _default_dialog()
    for key in _STICKY_DIALOG_KEYS:
        if key in current:
            base[key] = current[key]
    base.update(overrides)
    return base
