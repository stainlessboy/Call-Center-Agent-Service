from __future__ import annotations

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph
from langgraph.types import RetryPolicy

from app.agent.nodes import (
    node_calc_flow,
    node_faq,
    node_human_mode_turn,
    node_prepare_turn,
    node_qualify_flow,
    node_recap,
    node_router,
)
from app.agent.consultant import consultant_enabled
from app.agent.state import BotState


# Retry policy for nodes that reach the network or the database (roadmap
# П.10). A transient blip — a dropped connection to Postgres, one 5xx from the
# provider — used to cost the customer their whole turn; a single retry inside
# the graph is cheaper than the "temporarily unavailable" reply, and cheaper
# than the customer retyping. Deterministic, purely local nodes (router,
# prepare_turn, recap) are not retried: they cannot fail transiently, so a
# retry would only hide a real bug.
#
# Note this is retry-per-NODE. `node_faq`/`consultant` re-runs the whole turn
# body, which is safe because nothing in it writes outside the graph state —
# the one node with a real side effect (calc_flow's lead insert) is fired as a
# background task and is not part of the retried body.
_NETWORK_RETRY = RetryPolicy(max_attempts=2, initial_interval=0.2, backoff_factor=2.0)


def build_graph(checkpointer=None, store=None):
    graph = StateGraph(BotState)

    graph.add_node("prepare_turn", node_prepare_turn)
    graph.add_node("router", node_router)

    # The consultant (roadmap П.1/П.2) replaces node_faq's hand-rolled tool
    # loop with a `create_agent` subgraph wrapped by two deterministic nodes.
    # Both paths answer to the node name "faq" so the router is identical
    # either way; CONSULTANT_AGENT_V2 picks which one is wired.
    if consultant_enabled():
        from app.agent.consultant import build_consultant
        from app.agent.nodes.consultant_node import node_faq_post, node_faq_pre

        graph.add_node("faq", node_faq_pre, retry_policy=_NETWORK_RETRY)
        # Compiled without its own checkpointer/store: as a node it inherits
        # the parent's, which is what lets a human-in-the-loop interrupt
        # inside a tool resume on this session's thread.
        graph.add_node("consultant", build_consultant())
        graph.add_node("faq_post", node_faq_post, retry_policy=_NETWORK_RETRY)
        graph.add_edge("consultant", "faq_post")
        graph.add_edge("faq_post", END)
    else:
        graph.add_node("faq", node_faq, retry_policy=_NETWORK_RETRY)
    graph.add_node("calc_flow", node_calc_flow, retry_policy=_NETWORK_RETRY)
    graph.add_node("qualify_flow", node_qualify_flow, retry_policy=_NETWORK_RETRY)
    graph.add_node("human_mode", node_human_mode_turn)
    graph.add_node("recap", node_recap)

    # prepare_turn resolves language + loads the client profile, then
    # Command(goto="router"). It is the entry point so that work can read
    # `dialog` straight from the checkpoint instead of the agent reading
    # the whole checkpoint itself before invoking (roadmap П.3).
    graph.set_entry_point("prepare_turn")

    # router/recap use Command(goto=...) — no explicit conditional edges
    # needed for their own dynamic routing. The static edges below only
    # cover the "this node finalized the turn itself" case (recap's first
    # entry, or any node returning a plain dict instead of a Command).
    _terminal = ["calc_flow", "qualify_flow", "human_mode", "recap"]
    if not consultant_enabled():
        # In the V2 path `faq` is faq_pre, which routes with Command(goto=...)
        # to either the consultant or END, so it must not carry a static edge.
        _terminal.append("faq")
    for name in _terminal:
        graph.add_edge(name, END)

    return graph.compile(checkpointer=checkpointer or MemorySaver(), store=store)
