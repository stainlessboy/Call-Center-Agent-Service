"""Keep tool calls to one per round (roadmap П.2).

The legacy node bound tools with ``parallel_tool_calls=False`` and the whole
post-processing chain still assumes it: ``_update_dialog_from_tools`` inspects
``tool_calls[-1]`` alone, and the display-tool handling assumes a single tool
result decides the turn. Two products selected in one round would silently
lose one of them.

Some OpenAI-compatible backends (Gemma via vLLM, older Together models) reject
or ignore the flag, which is why this degrades to "ask anyway, accept what
comes" rather than failing the turn.
"""
from __future__ import annotations

from typing import Any


def force_single_tool_call(request) -> Any:
    """`wrap_model_call` body: request at most one tool call per round."""
    if not request.tools:
        return request
    settings = dict(request.model_settings or {})
    settings.setdefault("parallel_tool_calls", False)
    try:
        return request.override(model_settings=settings)
    except Exception:
        # A backend that refuses the setting is not a reason to drop the turn.
        return request
