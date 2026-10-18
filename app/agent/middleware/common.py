"""Shared accessors for consultant middleware (roadmap П.2).

Middleware hooks receive the agent state as a plain dict. These helpers give
one place for the "where does the language / dialog / profile live" answer,
including the fallbacks the legacy node relied on, so five middleware modules
don't each re-derive them slightly differently.
"""
from __future__ import annotations

from typing import Any, Optional

from app.agent.state import _default_dialog


def dialog_of(state: Any) -> dict:
    """The dialog dict, never None — callers always get something indexable."""
    if not state:
        return _default_dialog()
    return dict(state.get("dialog") or _default_dialog())


def lang_of(state: Any) -> str:
    """Turn language. `state["lang"]` is authoritative (node_prepare_turn set
    it); `dialog.last_lang` is the fallback for a state assembled by hand."""
    if not state:
        return "ru"
    return state.get("lang") or (state.get("dialog") or {}).get("last_lang") or "ru"


def profile_of(state: Any) -> Optional[dict]:
    if not state:
        return None
    return state.get("user_profile") or None


def tool_name(tool: Any) -> str:
    """Tool objects and raw callables both show up in `request.tools`."""
    return getattr(tool, "name", None) or getattr(tool, "__name__", "") or ""
