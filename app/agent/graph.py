from __future__ import annotations

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph

from app.agent.nodes import (
    node_calc_flow,
    node_faq,
    node_human_mode_turn,
    node_qualify_flow,
    node_recap,
    node_router,
)
from app.agent.state import BotState


def build_graph(checkpointer=None, store=None):
    graph = StateGraph(BotState)

    graph.add_node("router", node_router)
    graph.add_node("faq", node_faq)
    graph.add_node("calc_flow", node_calc_flow)
    graph.add_node("qualify_flow", node_qualify_flow)
    graph.add_node("human_mode", node_human_mode_turn)
    graph.add_node("recap", node_recap)

    graph.set_entry_point("router")

    # router/recap use Command(goto=...) — no explicit conditional edges
    # needed for their own dynamic routing. The static edges below only
    # cover the "this node finalized the turn itself" case (recap's first
    # entry, or any node returning a plain dict instead of a Command).
    for name in ("faq", "calc_flow", "qualify_flow", "human_mode", "recap"):
        graph.add_edge(name, END)

    return graph.compile(checkpointer=checkpointer or MemorySaver(), store=store)
