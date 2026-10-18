"""`BotState.messages` + the `add_messages` reducer (roadmap П.3).

Before the reducer, every node returned the FULL new history and
`Agent._ainvoke_locked` fed the prior history back in as graph input. That
worked but cost a checkpoint read per turn and made concurrent turns race.
With the reducer, nodes return only what they add — which is exactly the
thing that breaks loudly (duplicated or lost history) if a node or the agent
regresses to passing the full list. Hence these tests.
"""
from __future__ import annotations

import uuid

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from app.agent.graph import build_graph
from app.agent.state import _default_dialog


def _config():
    return {"configurable": {"thread_id": f"reducer-{uuid.uuid4()}"}}


def _turn_state(text: str) -> dict:
    """A turn's input — deliberately WITHOUT `messages`, mirroring
    `Agent._ainvoke_locked`."""
    return {
        "last_user_text": text,
        "answer": "",
        "human_mode": False,
        "keyboard_options": None,
        "dialog": _default_dialog(),
        "lang": "ru",
        "_route": "",
        "session_id": "s-1",
        "user_id": 1,
        "show_operator_button": False,
        "token_usage": None,
        "user_profile": None,
        "ui_blocks": None,
    }


@pytest.fixture(autouse=True)
def _no_llm_no_faq(monkeypatch):
    """Force node_faq down its deterministic no-LLM path so these tests
    exercise state plumbing only, with no network."""
    from unittest.mock import AsyncMock

    from app.agent.nodes import faq as faq_module

    monkeypatch.setattr(faq_module, "_get_chat_openai", lambda role=None: None)
    monkeypatch.setattr(faq_module, "faq_precheck_answer", AsyncMock(return_value=None))
    monkeypatch.setattr(faq_module, "_faq_lookup", AsyncMock(return_value="Ответ из базы"))


class TestMessageAccumulation:
    @pytest.mark.asyncio
    async def test_two_turns_append_without_duplicates(self):
        graph = build_graph()
        config = _config()

        out1 = await graph.ainvoke(_turn_state("первый вопрос"), config=config)
        assert [m.content for m in out1["messages"]] == ["первый вопрос", "Ответ из базы"]

        out2 = await graph.ainvoke(_turn_state("второй вопрос"), config=config)
        contents = [m.content for m in out2["messages"]]
        assert contents == [
            "первый вопрос", "Ответ из базы",
            "второй вопрос", "Ответ из базы",
        ], "history must grow by exactly one exchange per turn"

    @pytest.mark.asyncio
    async def test_reducer_assigns_ids(self):
        """Ids are what make targeted removal/replacement possible later."""
        graph = build_graph()
        out = await graph.ainvoke(_turn_state("вопрос"), config=_config())
        assert all(m.id for m in out["messages"])

    @pytest.mark.asyncio
    async def test_alternating_roles_preserved(self):
        graph = build_graph()
        config = _config()
        for i in range(3):
            out = await graph.ainvoke(_turn_state(f"вопрос {i}"), config=config)
        roles = [type(m).__name__ for m in out["messages"]]
        assert roles == ["HumanMessage", "AIMessage"] * 3


class TestLegacyCheckpointMigration:
    @pytest.mark.asyncio
    async def test_history_without_ids_is_adopted_not_duplicated(self):
        """A checkpoint written before the reducer holds messages with
        `id=None`. The first merge must back-fill ids and keep the history
        intact rather than duplicating or dropping it."""
        graph = build_graph()
        config = _config()

        legacy = [HumanMessage(content="старый вопрос"), AIMessage(content="старый ответ")]
        assert all(m.id is None for m in legacy)
        await graph.aupdate_state(config, {"messages": legacy})

        out = await graph.ainvoke(_turn_state("новый вопрос"), config=config)
        contents = [m.content for m in out["messages"]]
        assert contents == [
            "старый вопрос", "старый ответ", "новый вопрос", "Ответ из базы",
        ]
        assert all(m.id for m in out["messages"])


class TestTrimming:
    @pytest.mark.asyncio
    async def test_budget_overflow_replaces_history_instead_of_growing(self, monkeypatch):
        """When the token budget bites, `_finalize_turn` switches to the
        full-replacement form. The history must SHRINK, not silently keep
        both the old and the trimmed copy."""
        monkeypatch.setenv("MAX_DIALOG_TOKENS", "40")
        graph = build_graph()
        config = _config()

        long_text = "очень длинный вопрос про ипотеку " * 20
        for _ in range(4):
            out = await graph.ainvoke(_turn_state(long_text), config=config)

        assert len(out["messages"]) < 8, "trimming must drop old exchanges"
        assert out["messages"][-1].content == "Ответ из базы"
        assert all(m.id for m in out["messages"])


class TestSyncHistory:
    @pytest.mark.asyncio
    async def test_sync_history_appends_only_new_events(self, monkeypatch):
        """`sync_history` writes a delta now. Two calls must accumulate, not
        replay everything they were given the first time."""
        from app.agent.agent import Agent

        agent = Agent()
        session_id = f"sync-{uuid.uuid4()}"

        await agent.sync_history(session_id, [{"role": "user", "text": "привет"}])
        await agent.sync_history(session_id, [{"role": "operator", "text": "здравствуйте"}])

        state = await agent._graph.aget_state(agent._build_config(session_id))
        contents = [m.content for m in state.values["messages"]]
        assert contents == ["привет", "здравствуйте"]

    @pytest.mark.asyncio
    async def test_sync_history_masks_pii_from_customer(self):
        from app.agent.agent import Agent

        agent = Agent()
        session_id = f"sync-{uuid.uuid4()}"
        await agent.sync_history(session_id, [{"role": "user", "text": "мой номер +998901234567"}])

        state = await agent._graph.aget_state(agent._build_config(session_id))
        assert "998901234567" not in state.values["messages"][0].content
