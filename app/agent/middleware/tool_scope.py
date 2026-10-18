"""Dynamic tool scoping (roadmap П.2 + П.9).

Two jobs, both implemented by removing tools from a single model request:

1. **Anti-loop guard for `clarify`.** The legacy `node_faq` rebuilt
   `llm.bind_tools(...)` mid-turn to make a second consecutive `clarify`
   physically impossible; the same guarantee here is one filter.
   Why it exists: `clarify` used to loop — the model asked "Uzcard or Humo?",
   the customer answered in free text, and the model asked again. Buttons fixed
   the happy path; this guard closes the rest.

2. **Scope by dialog state (П.9).** All 16 tool schemas cost roughly 9k tokens
   on every request, and several can only ever apply in a state the dialog is
   not in — `select_office` with no offices on screen, `select_product` with no
   product list, the calculator tools with nothing selected. Dropping those
   shrinks the request without changing what the model can achieve, because the
   preconditions are exactly what the tools themselves would fail on.

Anything not listed in `_REQUIRES` is always offered.
"""
from __future__ import annotations

import logging as _logging
import os
from typing import Any, Callable

from app.agent.middleware.common import dialog_of, tool_name

_logger = _logging.getLogger(__name__)

# tool name -> predicate over `dialog`; the tool is offered only when it holds.
_REQUIRES: dict[str, Callable[[dict], bool]] = {
    # Picking from a list needs a list.
    "select_office": lambda d: bool(d.get("offices")),
    "select_product": lambda d: bool(d.get("products")),
    "compare_products": lambda d: len(d.get("products") or []) >= 2,
    # The calculator tools all reason about a product the customer is looking
    # at (or, for what_if, at least a shown list to name one from).
    "start_calculator": lambda d: bool(d.get("selected_product") or d.get("products")),
    "what_if_scenario": lambda d: bool(d.get("selected_product") or d.get("products")),
    "affordability_check": lambda d: bool(
        d.get("selected_product") or d.get("products") or d.get("calc_slots")
    ),
}


def _scoping_enabled() -> bool:
    """`TOOL_SCOPING_ENABLED=false` restores the "always offer all 16" behavior
    — the escape hatch if a scoped-out tool is ever genuinely needed."""
    return (os.getenv("TOOL_SCOPING_ENABLED") or "true").strip().lower() in {
        "1", "true", "yes", "on",
    }


def scope_tools(request) -> Any:
    """`wrap_model_call` body: return a request carrying only usable tools."""
    tools = list(request.tools or [])
    if not tools:
        return request

    dialog = dialog_of(request.state)
    dropped: list[str] = []

    def keep(tool: Any) -> bool:
        name = tool_name(tool)
        if name == "clarify" and dialog.get("clarify_last_turn"):
            dropped.append(name)
            return False
        if not _scoping_enabled():
            return True
        predicate = _REQUIRES.get(name)
        if predicate is not None and not predicate(dialog):
            dropped.append(name)
            return False
        return True

    kept = [t for t in tools if keep(t)]
    if not kept or len(kept) == len(tools):
        # Never hand the model an empty toolset — that turns a scoping rule
        # into "the agent can no longer do anything".
        return request
    _logger.debug("tool scoping dropped: %s", ", ".join(dropped))
    return request.override(tools=kept)
