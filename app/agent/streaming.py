"""Mini App token-streaming plumbing (Phase 3 — "Mini App UX").

A per-turn `contextvars.ContextVar` carrying an optional async callback
`on_token(text: str) -> None`. The Mini App chat route
(`app/miniapp/routes/chat.py`) sets it right before calling
`ChatService.handle_user_message` and resets it in a `finally` right after;
`node_faq` (`app/agent/nodes/faq.py`) reads it via `get_on_token_callback()`
and, when set, streams its tool-loop rounds through the callback instead of
a plain `.ainvoke()` — see that module's `_run_llm_round()`.

The Telegram path never sets this contextvar, so `get_on_token_callback()`
always returns `None` there and `node_faq`'s behavior is byte-for-byte
unchanged. This module exists so that holds without `node_faq` importing
anything from `app/miniapp` — the Mini App is a presentation layer on top of
the agent (see CLAUDE.md's Mini App design principle), never the reverse.

Mechanically this is the same pattern as the existing per-turn FAQ-search
memoization in `app/utils/faq_tools.py` (`reset_faq_turn_cache` /
`_faq_turn_cache`): a `ContextVar` defaulting to `None`/unset, propagated to
every coroutine *awaited* (not spawned as a separate task) within the same
turn's asyncio Task, and reset by the caller that opted in. Any code path
that never sets it sees the default and behaves exactly as before.
"""
from __future__ import annotations

import contextvars
from typing import Awaitable, Callable, Optional

OnTokenCallback = Callable[[str], Awaitable[None]]

_on_token_var: "contextvars.ContextVar[Optional[OnTokenCallback]]" = contextvars.ContextVar(
    "on_token_callback", default=None,
)


def set_on_token_callback(callback: Optional[OnTokenCallback]) -> contextvars.Token:
    """Arm the current context's streaming callback. Returns a reset token —
    pass it to `reset_on_token_callback` in a `finally` block."""
    return _on_token_var.set(callback)


def reset_on_token_callback(token: contextvars.Token) -> None:
    _on_token_var.reset(token)


def get_on_token_callback() -> Optional[OnTokenCallback]:
    """None on every Telegram turn, and on any Mini App turn where streaming
    wasn't armed (disabled via MINIAPP_STREAMING_ENABLED, or no session_id
    yet to publish to) — callers must treat None as "stream nothing, take
    the plain path"."""
    return _on_token_var.get()
