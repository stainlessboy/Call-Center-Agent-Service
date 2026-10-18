"""Human approval gate for write tools (roadmap П.6).

Reading a customer's application status is safe to do unattended. Creating an
application, blocking a card or moving money is not — those pause the graph
until a person approves, edits or rejects the call. `HumanInTheLoopMiddleware`
enforces that; these tests pin the two halves that are ours: which tools get
gated, and that a paused session can be recognised and resumed later.

The "later" matters: the approval may arrive from a different request, minutes
after the turn that triggered it, which is why the interrupt has to live in the
session's own checkpoint rather than in memory.
"""
from __future__ import annotations

import uuid

import pytest
from langchain_core.messages import AIMessage
from langchain_core.tools import tool as lc_tool
from unittest.mock import AsyncMock

from app.agent.state import _default_dialog


@lc_tool
async def create_application(product: str, amount: int) -> str:
    """Submit a credit application (a WRITE operation)."""
    return f"Заявка создана: {product} на {amount}"


@lc_tool
async def get_application_status(application_id: str) -> str:
    """Read the status of an application (safe, read-only)."""
    return f"Заявка {application_id}: на рассмотрении"


class TestGateSelection:
    def test_only_write_tools_are_gated(self):
        from app.agent.consultant import _approval_gated_tools

        gated = _approval_gated_tools([create_application, get_application_status])

        assert set(gated) == {"create_application"}

    def test_server_prefixed_names_are_recognised(self):
        """`MultiServerMCPClient(tool_name_prefix=True)` yields `crm_block_card`;
        the gate must not be defeated by the prefix."""
        from app.agent.consultant import _approval_gated_tools

        class _T:
            def __init__(self, name):
                self.name = name

        gated = _approval_gated_tools([_T("crm_block_card"), _T("crm_get_scoring")])

        assert set(gated) == {"crm_block_card"}

    def test_no_write_tools_means_no_middleware(self):
        """With nothing to gate the middleware is left out entirely rather
        than added as a no-op interceptor."""
        from app.agent.consultant import _approval_gated_tools

        assert _approval_gated_tools([get_application_status]) == {}


class TestPauseAndResume:
    """Drives the real agent: a gated tool must stop the run before it
    executes, and the decision must be applied when the graph resumes."""

    @pytest.mark.asyncio
    async def test_write_tool_pauses_before_executing(self, monkeypatch):
        graph, model = _agent_with_checkpointer(monkeypatch)
        config = {"configurable": {"thread_id": f"hitl-{uuid.uuid4()}"}}

        out = await graph.ainvoke(_agent_state("оформите ипотеку"), config=config)

        assert "__interrupt__" in out, "a write tool must not run unattended"
        # The tool itself has not run: no ToolMessage carrying its output.
        assert not any(
            "Заявка создана" in str(getattr(m, "content", "")) for m in out.get("messages", [])
        )

    @pytest.mark.asyncio
    async def test_approve_lets_the_tool_run(self, monkeypatch):
        from langgraph.types import Command

        graph, model = _agent_with_checkpointer(monkeypatch)
        config = {"configurable": {"thread_id": f"hitl-{uuid.uuid4()}"}}
        await graph.ainvoke(_agent_state("оформите ипотеку"), config=config)

        out = await graph.ainvoke(
            Command(resume={"decisions": [{"type": "approve"}]}), config=config,
        )

        assert any(
            "Заявка создана" in str(getattr(m, "content", "")) for m in out["messages"]
        )

    @pytest.mark.asyncio
    async def test_reject_keeps_the_tool_from_running(self, monkeypatch):
        from langgraph.types import Command

        graph, model = _agent_with_checkpointer(monkeypatch)
        config = {"configurable": {"thread_id": f"hitl-{uuid.uuid4()}"}}
        await graph.ainvoke(_agent_state("оформите ипотеку"), config=config)

        out = await graph.ainvoke(
            Command(resume={"decisions": [{
                "type": "reject", "message": "Нужна личная явка в отделение",
            }]}),
            config=config,
        )

        assert not any(
            "Заявка создана" in str(getattr(m, "content", "")) for m in out["messages"]
        )

    @pytest.mark.asyncio
    async def test_edited_arguments_are_what_runs(self, monkeypatch):
        """The whole point of an `edit` decision: a person corrects the
        model's arguments, and the corrected call is the one executed."""
        from langgraph.types import Command

        graph, model = _agent_with_checkpointer(monkeypatch)
        config = {"configurable": {"thread_id": f"hitl-{uuid.uuid4()}"}}
        await graph.ainvoke(_agent_state("оформите ипотеку"), config=config)

        out = await graph.ainvoke(
            Command(resume={"decisions": [{
                "type": "edit",
                "edited_action": {
                    "name": "create_application",
                    "args": {"product": "Ипотека", "amount": 999},
                },
            }]}),
            config=config,
        )

        blob = " ".join(str(getattr(m, "content", "")) for m in out["messages"])
        assert "999" in blob


class TestAgentInterruptApi:
    @pytest.mark.asyncio
    async def test_idle_session_reports_no_interrupt(self, monkeypatch):
        from app.agent.agent import Agent

        agent = Agent()
        assert await agent.get_pending_interrupt(f"idle-{uuid.uuid4()}") is None

    @pytest.mark.asyncio
    async def test_state_read_failure_is_not_an_exception(self, monkeypatch):
        """A checkpointer blip must read as "we don't know", so the caller can
        fall back to the flag it already had instead of failing the request."""
        from app.agent.agent import Agent

        agent = Agent()
        agent._graph = _RaisingGraph()

        assert await agent.get_pending_interrupt("any") is None

    @pytest.mark.asyncio
    async def test_operator_wait_is_reported_as_such(self, monkeypatch):
        """`node_human_mode_turn`'s interrupt and an approval gate's interrupt
        must be distinguishable — they are resumed with different payloads."""
        from app.agent.agent import Agent

        agent = Agent()
        agent._graph = _StubGraph({"user_message": "привет", "reason": "human_mode_active"})

        pending = await agent.get_pending_interrupt("s")

        assert pending["kind"] == "operator"

    @pytest.mark.asyncio
    async def test_approval_wait_is_reported_as_such(self):
        from app.agent.agent import Agent

        agent = Agent()
        agent._graph = _StubGraph({
            "action_requests": [{"name": "create_application", "args": {}}],
        })

        pending = await agent.get_pending_interrupt("s")

        assert pending["kind"] == "approval"
        assert pending["action_requests"][0]["name"] == "create_application"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

class _ScriptedModel:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def bind_tools(self, tools, **kwargs):
        return self

    async def ainvoke(self, msgs, **kwargs):
        resp = self._responses[min(self.calls, len(self._responses) - 1)]
        self.calls += 1
        return resp


def _agent_with_checkpointer(monkeypatch):
    """Compile the consultant with its own checkpointer.

    In production it inherits the parent graph's; here it is standalone so the
    test drives the interrupt directly, which is the behaviour under test.
    """
    from langgraph.checkpoint.memory import InMemorySaver

    from app.agent import consultant as consultant_module

    monkeypatch.setattr(
        consultant_module, "_display_tools",
        lambda: [create_application, get_application_status],
    )
    model = _ScriptedModel([
        AIMessage(
            content="",
            tool_calls=[{
                "name": "create_application",
                "args": {"product": "Ипотека", "amount": 100},
                "id": "c1", "type": "tool_call",
            }],
        ),
        AIMessage(content="Готово."),
    ])
    monkeypatch.setattr(
        consultant_module, "create_agent",
        _checkpointing_create_agent(consultant_module.create_agent, InMemorySaver()),
    )
    # `model_resolver_middleware` replaces whatever model the agent was
    # compiled with on every request (that is how a provider outage or an env
    # change is picked up without a restart), so the scripted model has to be
    # injected at THAT seam — passing it to build_consultant is not enough.
    from app.agent.nodes import faq as faq_module

    monkeypatch.setattr(faq_module, "_get_chat_openai", lambda role=None: model)
    return consultant_module.build_consultant(model), model


def _checkpointing_create_agent(original, checkpointer):
    def _wrapped(*args, **kwargs):
        kwargs["checkpointer"] = checkpointer
        return original(*args, **kwargs)

    return _wrapped


def _agent_state(text: str) -> dict:
    from langchain_core.messages import HumanMessage

    return {
        "messages": [HumanMessage(content=text)],
        "dialog": _default_dialog(),
        "lang": "ru",
        "user_profile": None,
        "session_id": "s-1",
        "user_id": 1,
    }


class _StubGraph:
    def __init__(self, interrupt_value):
        self._value = interrupt_value

    async def aget_state(self, config):
        class _Interrupt:
            id = "i-1"
            value = self._value

        class _Snapshot:
            interrupts = (_Interrupt(),)

        return _Snapshot()


class _RaisingGraph:
    async def aget_state(self, config):
        raise RuntimeError("checkpointer down")
