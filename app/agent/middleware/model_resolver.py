"""Resolve the chat model per request, not at build time (roadmap П.1).

`create_agent` takes the model when the agent is COMPILED, but this project
needs it chosen when the request is MADE:

  * the process must start even while the provider is unreachable — the
    legacy node called `_get_chat_openai()` inside the turn and fell back to
    a FAQ-only reply when it returned None;
  * the model is env-driven per role (`CONSULTANT_LLM_MODEL`, `USE_GPT`, …),
    and a compiled-in model would freeze whatever was configured at import;
  * every existing test monkeypatches that same accessor to inject a scripted
    model, and keeping the seam is what lets those tests describe the new
    agent's behavior unchanged.

So the agent is compiled around a placeholder and this middleware swaps in the
real model on each call.
"""
from __future__ import annotations

import logging as _logging
from typing import Any

_logger = _logging.getLogger(__name__)


class ModelUnavailable(RuntimeError):
    """Raised when no chat model can be constructed for this request.

    The calling node catches it and degrades to the FAQ-only answer, exactly
    as the legacy `node_faq` did when `_get_chat_openai()` returned None.
    """


class PlaceholderModel:
    """Stands in until `resolve_model` replaces it. Never actually called."""

    def bind_tools(self, tools: Any, **kwargs: Any) -> "PlaceholderModel":  # pragma: no cover
        return self

    async def ainvoke(self, *args: Any, **kwargs: Any):  # pragma: no cover
        raise ModelUnavailable("consultant model was not resolved for this request")


def resolve_model(request) -> Any:
    """`wrap_model_call` body: put the currently-configured model on the request.

    Imported from `nodes.faq` rather than `llm` on purpose: that module is the
    seam the whole test suite already patches, and routing both paths through
    it keeps one definition of "which model is the consultant using".
    """
    from app.agent.nodes.faq import _get_chat_openai

    model = _get_chat_openai(role="consultant")
    if model is None:
        raise ModelUnavailable("no chat model available")
    return request.override(model=model)
