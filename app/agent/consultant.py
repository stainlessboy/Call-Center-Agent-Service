"""The consultant agent — `create_agent` replacement for `node_faq`'s hand-rolled
tool loop (roadmap П.1).

What the legacy loop did by hand and what replaces it here:

| hand-rolled in `node_faq`                        | now                              |
|--------------------------------------------------|----------------------------------|
| `for round_idx in range(5)` + manual ToolNode     | the agent loop, `recursion_limit`|
| `llm.bind_tools(..., parallel_tool_calls=False)`  | `single_tool_call_middleware`    |
| system prompt + `<state>` XML assembly            | `state_inject_middleware`        |
| clarify anti-loop rebind mid-turn                 | `tool_scope_middleware`          |
| display-tool short-circuit (`break` on tool text) | `return_direct=True` on the tool |
| strict-`faq_lookup` rewording                     | `faq_rephrase_middleware`        |
| `except APIError` → single FAQ fallback           | `ModelFallbackMiddleware`        |
| — (no retries existed)                            | `ToolRetryMiddleware`            |
| `trim_messages(MAX_DIALOG_TOKENS)`                | `SummarizationMiddleware`        |

The model is resolved lazily, per turn, through `app.agent.llm._get_chat_openai`
rather than captured at import: the process must survive a provider outage at
startup, tests monkeypatch that seam, and the role→model mapping is env-driven.

Enabled by `CONSULTANT_AGENT_V2` (see `app/agent/graph.py`). While that flag is
off, nothing in this module runs.
"""
from __future__ import annotations

import logging as _logging
import os
from typing import Any, Optional

from langchain.agents import create_agent
from langchain.agents.middleware import (
    AgentState,
    HumanInTheLoopMiddleware,
    InterruptOnConfig,
    ModelCallLimitMiddleware,
    ModelFallbackMiddleware,
    SummarizationMiddleware,
    ToolRetryMiddleware,
)

from app.agent.middleware import (
    PlaceholderModel,
    faq_rephrase_middleware,
    model_resolver_middleware,
    single_tool_call_middleware,
    state_inject_middleware,
    tool_scope_middleware,
)
from app.agent.tools import _FAQ_TOOLS

_logger = _logging.getLogger(__name__)


class ConsultantState(AgentState):
    """Agent state = LangChain's `messages`/`jump_to`/`structured_response`
    plus the dialog keys our tools and middleware read.

    It is a SUBSET of `BotState`, which is what lets the compiled agent sit in
    the main graph as a node and share the parent's state directly.
    """

    dialog: dict
    lang: str
    user_profile: Optional[dict]
    session_id: Optional[str]
    user_id: Optional[int]


# Tools whose output is already customer-facing, pre-formatted text (product
# cards, office details, currency tables, calculator results). The legacy node
# surfaced these by breaking out of its loop, because gpt-4o-mini reliably
# summarised them away ("Показал программу" instead of the actual card).
# `return_direct` is the framework's own name for that behaviour.
#
# `faq_lookup` is deliberately NOT here: its low-confidence and no-match
# sentinels are instructions for the model, so the loop must continue. Its
# strict hits are handled by `faq_rephrase_middleware` instead.
_DISPLAY_TOOLS = frozenset({
    "find_office", "select_office", "get_office_types_info", "get_currency_info",
    "show_credit_menu", "get_products", "select_product", "compare_products",
    "custom_loan_calculator", "what_if_scenario", "affordability_check",
    "recommend_product", "clarify",
})

# Tools that reach the network or the database, and are therefore worth a
# retry. Pure formatters are not.
_RETRYABLE_TOOLS = ["find_office", "select_office", "get_products", "faq_lookup",
                    "get_currency_info", "recommend_product", "compare_products"]

# The legacy node allowed 5 model rounds per turn (`max_rounds = 5`) before
# giving up and surfacing the last useful tool output. `ModelCallLimitMiddleware`
# enforces the same ceiling, but ends the run cleanly instead of raising
# GraphRecursionError — so a model that keeps calling tools degrades into
# "answer with what we have" rather than into a failed turn.
CONSULTANT_MAX_MODEL_CALLS = int(os.getenv("CONSULTANT_MAX_MODEL_CALLS") or 5)


def consultant_enabled() -> bool:
    """`CONSULTANT_AGENT_V2` — off by default during the rollout window."""
    return (os.getenv("CONSULTANT_AGENT_V2") or "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def _summarization_enabled() -> bool:
    """History summarization replaces plain trimming. Off by default: it
    changes what the model sees on long dialogs and deserves its own
    measured rollout (see roadmap П.2)."""
    return (os.getenv("CONSULTANT_SUMMARIZATION") or "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def _display_tools() -> list:
    """`_FAQ_TOOLS` with `return_direct` set on the display tools, plus any
    MCP tools loaded at startup.

    Copies each tool rather than mutating the shared objects — the legacy node
    imports the very same list, and flipping a flag on it would change that
    path's behaviour too while the rollout flag says it should not.

    MCP tools (roadmap П.8) come from the process-wide registry rather than an
    import, because they are discovered asynchronously in the FastAPI lifespan;
    an empty registry (MCP off, or a server that did not answer) simply means
    the agent runs with its built-in tools.
    """
    out = []
    for tool in _FAQ_TOOLS:
        if getattr(tool, "name", None) in _DISPLAY_TOOLS and not tool.return_direct:
            tool = tool.model_copy(update={"return_direct": True})
        out.append(tool)

    from app.agent.mcp_tools import get_mcp_tools

    out.extend(get_mcp_tools())
    return out


def _approval_gated_tools(tools: list) -> dict:
    """`interrupt_on` config for `HumanInTheLoopMiddleware` (roadmap П.6).

    Only WRITE tools are gated. A read ("what is the status of application
    X?") is safe to run unattended; creating an application or blocking a card
    on a customer's say-so is not — a person confirms it first. The set is
    owned by `mcp_tools.is_sensitive_tool`, so adding a write tool to an MCP
    server does not also require editing this module.

    Returns `{}` when nothing needs gating, which is the signal to leave the
    middleware out entirely rather than add a no-op interceptor.
    """
    from app.agent.mcp_tools import is_sensitive_tool

    return {
        name: InterruptOnConfig(allowed_decisions=["approve", "edit", "reject"])
        for name in (getattr(t, "name", "") for t in tools)
        if name and is_sensitive_tool(name)
    }


def build_consultant(model: Any = None):
    """Compile the consultant agent around an already-resolved chat model.

    No checkpointer and no store are passed: as a node inside the main graph
    the agent inherits both from the parent compile, which is what lets a
    `HumanInTheLoopMiddleware` interrupt (roadmap П.6) resume on the parent's
    thread.
    """
    middleware: list = [
        # First: put the real, currently-configured model on the request (the
        # agent is compiled around a placeholder — see model_resolver).
        model_resolver_middleware,
        # Order matters: the system prompt is assembled first so later
        # middleware see the final message list, and tool scoping runs before
        # the request reaches the provider.
        state_inject_middleware,
        tool_scope_middleware,
        single_tool_call_middleware,
        faq_rephrase_middleware,
        ToolRetryMiddleware(max_retries=2, tools=_RETRYABLE_TOOLS, initial_delay=0.2),
        # Replaces the legacy `max_rounds = 5` loop guard. `exit_behavior="end"`
        # stops the agent and leaves whatever it produced in `messages`, which
        # faq_post then turns into the customer's answer.
        ModelCallLimitMiddleware(run_limit=CONSULTANT_MAX_MODEL_CALLS, exit_behavior="end"),
    ]

    fallback_model = os.getenv("CONSULTANT_FALLBACK_MODEL")
    if fallback_model:
        # A provider blip on the main model costs the customer a turn; a
        # cheaper second model answering is strictly better than the generic
        # "temporarily unavailable" reply.
        middleware.append(ModelFallbackMiddleware(fallback_model))

    if _summarization_enabled():
        summary_model = os.getenv("CONSULTANT_SUMMARY_MODEL") or "gpt-4o-mini"
        max_tokens = int(os.getenv("MAX_DIALOG_TOKENS") or 16000)
        middleware.append(
            SummarizationMiddleware(
                model=summary_model,
                # Summarize well before the budget so the summarizing call
                # itself always fits.
                trigger=("tokens", int(max_tokens * 0.75)),
                keep=("messages", 20),
            )
        )

    tools = _display_tools()

    gated = _approval_gated_tools(tools)
    if gated:
        # Pauses the graph BEFORE a write tool runs and waits for a person to
        # approve, edit the arguments, or reject (roadmap П.6). The interrupt
        # is checkpointed on the parent session's thread — which is why the
        # agent is a node in the main graph rather than a nested invoke — so
        # `Agent.resume_pending_interrupt` can resume it a minute or an hour
        # later, from a different request.
        middleware.append(HumanInTheLoopMiddleware(interrupt_on=gated))
        _logger.info("Approval gate armed for MCP write tools: %s", sorted(gated))

    return create_agent(
        model=model if model is not None else PlaceholderModel(),
        tools=tools,
        # The prompt is supplied per-request by state_inject_middleware, which
        # needs the turn's language and dialog — neither is known at build time.
        system_prompt=None,
        middleware=middleware,
        state_schema=ConsultantState,
        name="consultant",
    )
