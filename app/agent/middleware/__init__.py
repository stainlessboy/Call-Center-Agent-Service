"""Middleware for the `create_agent` consultant (roadmap П.2).

Each module here replaces a hand-rolled block of the legacy `node_faq`:

| legacy code in nodes/faq.py                  | module here            |
|----------------------------------------------|------------------------|
| system prompt + `<state>` XML assembly        | `state_inject`         |
| clarify anti-loop rebind + (new) П.9 scoping  | `tool_scope`           |
| strict-`faq_lookup` rewording short-circuit   | `faq_rephrase_mw`      |
| `parallel_tool_calls=False` on bind_tools     | `single_tool_call`     |

Retries, model fallback and history summarization are LangChain's own
middleware and are assembled in `app/agent/consultant.py`.
"""
from __future__ import annotations

from langchain.agents.middleware import wrap_model_call, wrap_tool_call

from app.agent.middleware.faq_rephrase_mw import rephrase_faq_tool_result
from app.agent.middleware.model_resolver import ModelUnavailable, PlaceholderModel, resolve_model
from app.agent.middleware.single_tool_call import force_single_tool_call
from app.agent.middleware.state_inject import apply_system_prompt
from app.agent.middleware.tool_scope import scope_tools


# All model-call hooks are async: the graph is always driven with `ainvoke`,
# and a sync-only hook raises NotImplementedError there.
@wrap_model_call
async def model_resolver_middleware(request, handler):
    """Swap the placeholder model for the currently-configured one."""
    return await handler(resolve_model(request))


@wrap_model_call
async def state_inject_middleware(request, handler):
    """Attach the cached policy prompt and this turn's `<state>` XML."""
    return await handler(apply_system_prompt(request))


@wrap_model_call
async def tool_scope_middleware(request, handler):
    """Drop tools that cannot apply in the current dialog state, and enforce
    the clarify anti-loop guard."""
    return await handler(scope_tools(request))


@wrap_model_call
async def single_tool_call_middleware(request, handler):
    """Ask the provider for at most one tool call per round."""
    return await handler(force_single_tool_call(request))


@wrap_tool_call
async def faq_rephrase_middleware(request, handler):
    """Reword a strict `faq_lookup` hit into natural language, fact-guarded."""
    return await rephrase_faq_tool_result(request, handler)


__all__ = [
    "ModelUnavailable",
    "PlaceholderModel",
    "model_resolver_middleware",
    "state_inject_middleware",
    "tool_scope_middleware",
    "single_tool_call_middleware",
    "faq_rephrase_middleware",
]
