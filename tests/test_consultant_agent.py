"""The `create_agent` consultant path (roadmap П.1/П.2).

Everything here runs with `CONSULTANT_AGENT_V2=true`. The legacy `node_faq`
suite stays untouched and keeps guarding the default path until this one is
verified in production — that dual-path window is the rollout plan.

The tests are written against BEHAVIOUR the legacy node guaranteed, because
that is what a refactor has to preserve: display tools ship verbatim, the
clarify guard cannot loop, a strict FAQ hit skips the model entirely, a
product browse enters the questionnaire, history stays a clean Human→AI pair.
"""
from __future__ import annotations

import uuid

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from unittest.mock import AsyncMock

from app.agent.state import _default_dialog


@pytest.fixture(autouse=True)
def _enable_v2(monkeypatch):
    monkeypatch.setenv("CONSULTANT_AGENT_V2", "true")
    # node_prepare_turn would otherwise reach for a real database; the profile
    # load itself is covered in tests/test_prepare_turn.py.
    from app.agent.nodes import prepare as prepare_module

    monkeypatch.setattr(prepare_module, "load_user_profile", AsyncMock(return_value=None))


class _ScriptedBound:
    """Returns the next scripted AIMessage per model call."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0
        self.seen_requests: list = []

    async def ainvoke(self, msgs, **kwargs):
        self.seen_requests.append(msgs)
        resp = self._responses[min(self.calls, len(self._responses) - 1)]
        self.calls += 1
        return resp


class _ScriptedLLM:
    """Same shape as the stubs the legacy suite uses: `bind_tools` returns a
    stable object so state survives a mid-turn rebind."""

    def __init__(self, responses):
        self.bound = _ScriptedBound(responses)
        self.bind_calls: list = []

    def bind_tools(self, tools, **kwargs):
        self.bind_calls.append(list(tools))
        return self.bound


def _tool_call(name: str, args: dict, call_id: str = "1") -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": args, "id": call_id, "type": "tool_call"}],
    )


def _state(text: str, dialog: dict | None = None, **overrides) -> dict:
    base = {
        "last_user_text": text,
        "answer": "",
        "human_mode": False,
        "keyboard_options": None,
        "dialog": dialog or _default_dialog(),
        "lang": "ru",
        "_route": "",
        "session_id": "s-1",
        "user_id": 1,
        "show_operator_button": False,
        "token_usage": None,
        "user_profile": None,
        "ui_blocks": None,
    }
    base.update(overrides)
    return base


async def _run(state: dict, llm, monkeypatch, **patches):
    """Drive a full turn through the V2 graph."""
    from app.agent.graph import build_graph
    from app.agent.nodes import faq as faq_module

    monkeypatch.setattr(faq_module, "_get_chat_openai", lambda role=None: llm)
    monkeypatch.setattr(
        faq_module, "faq_precheck_answer",
        patches.get("precheck", AsyncMock(return_value=None)),
    )
    import app.agent.nodes.consultant_node as cn
    monkeypatch.setattr(
        cn, "faq_precheck_answer", patches.get("precheck", AsyncMock(return_value=None)),
    )

    graph = build_graph()
    config = {"configurable": {"thread_id": f"v2-{uuid.uuid4()}"}}
    return await graph.ainvoke(state, config=config)


class TestGraphShape:
    def test_v2_wires_pre_agent_post(self):
        from app.agent.graph import build_graph

        nodes = set(build_graph().get_graph().nodes)
        assert {"faq", "consultant", "faq_post"} <= nodes

    def test_v1_has_no_consultant(self, monkeypatch):
        from app.agent.graph import build_graph

        monkeypatch.setenv("CONSULTANT_AGENT_V2", "false")
        nodes = set(build_graph().get_graph().nodes)
        assert "consultant" not in nodes and "faq" in nodes


class TestDeterministicShortcuts:
    @pytest.mark.asyncio
    async def test_strict_faq_hit_never_reaches_the_model(self, monkeypatch):
        """A verified database answer leaves the model nothing to decide —
        the legacy node skipped it too, and that is most of the saving."""
        import app.agent.nodes.consultant_node as cn

        monkeypatch.setattr(cn, "rephrase_faq_answer", AsyncMock(return_value="Ответ из базы"))
        llm = _ScriptedLLM([AIMessage(content="модель не должна отвечать")])

        out = await _run(
            _state("как заблокировать карту"), llm, monkeypatch,
            precheck=AsyncMock(return_value="Позвоните в колл-центр"),
        )

        assert out["answer"] == "Ответ из базы"
        assert llm.bound.calls == 0

    @pytest.mark.asyncio
    async def test_back_button_reshows_stored_list_without_the_model(self, monkeypatch):
        dialog = {
            **_default_dialog(),
            "flow": "show_products",
            "category": "mortgage",
            "products": [{"name": "Ипотека A"}, {"name": "Ипотека Б"}],
        }
        llm = _ScriptedLLM([AIMessage(content="не должно вызываться")])

        out = await _run(_state("◀ Все продукты", dialog), llm, monkeypatch)

        assert llm.bound.calls == 0
        assert out["keyboard_options"] == ["Ипотека A", "Ипотека Б"]
        assert out["dialog"]["selected_product"] is None


class TestAgentLoop:
    @pytest.mark.asyncio
    async def test_plain_answer_is_wrapped_as_assistant_content(self, monkeypatch):
        """A free-text answer with no tool behind it is model-generated, so it
        ships under the "assistant, not verified bank info" wrapper — the same
        rule the legacy node applied."""
        llm = _ScriptedLLM([AIMessage(content="Здравствуйте! Чем помогу?")])

        out = await _run(_state("привет"), llm, monkeypatch)

        assert "Здравствуйте! Чем помогу?" in out["answer"]
        assert "Ассистент" in out["answer"]
        # History stores the RAW answer, never the wrapper.
        assert out["messages"][-1].content == "Здравствуйте! Чем помогу?"

    @pytest.mark.asyncio
    async def test_display_tool_output_ships_verbatim(self, monkeypatch):
        """`return_direct` replaces the legacy short-circuit: the card the tool
        formatted must reach the customer, not the model's summary of it."""
        llm = _ScriptedLLM([
            _tool_call("get_currency_info", {}),
            AIMessage(content="Я показал курсы"),  # must never be used
        ])

        out = await _run(_state("какой курс доллара"), llm, monkeypatch)

        assert out["answer"] != "Я показал курсы"
        assert llm.bound.calls == 1, "agent must stop after a return_direct tool"

    @pytest.mark.asyncio
    async def test_history_keeps_only_the_clean_exchange(self, monkeypatch):
        """The agent's working trace (tool-call turns, tool results) must not
        accumulate in the session history — every later turn would pay for it."""
        llm = _ScriptedLLM([
            _tool_call("get_currency_info", {}),
        ])

        out = await _run(_state("курс"), llm, monkeypatch)

        kinds = [type(m).__name__ for m in out["messages"]]
        assert kinds == ["HumanMessage", "AIMessage"], kinds
        assert not any(isinstance(m, ToolMessage) for m in out["messages"])

    @pytest.mark.asyncio
    async def test_user_message_is_masked_in_history(self, monkeypatch):
        llm = _ScriptedLLM([AIMessage(content="принято")])

        out = await _run(_state("мой номер +998901234567"), llm, monkeypatch)

        assert "998901234567" not in out["messages"][0].content


class TestToolScoping:
    @pytest.mark.asyncio
    async def test_unusable_tools_are_not_offered(self, monkeypatch):
        """With no products or offices in the dialog, picking from a list is
        impossible — those schemas are pure token cost (roadmap П.9)."""
        llm = _ScriptedLLM([AIMessage(content="ок")])

        await _run(_state("привет"), llm, monkeypatch)

        offered = {getattr(t, "name", "") for t in llm.bind_calls[-1]}
        assert "select_product" not in offered
        assert "select_office" not in offered
        assert "faq_lookup" in offered, "always-usable tools must stay"

    @pytest.mark.asyncio
    async def test_selection_tools_appear_once_a_list_exists(self, monkeypatch):
        dialog = {**_default_dialog(), "products": [{"name": "A"}, {"name": "Б"}]}
        llm = _ScriptedLLM([AIMessage(content="ок")])

        await _run(_state("расскажи про первую", dialog), llm, monkeypatch)

        offered = {getattr(t, "name", "") for t in llm.bind_calls[-1]}
        assert "select_product" in offered
        assert "compare_products" in offered

    @pytest.mark.asyncio
    async def test_clarify_is_withheld_after_a_clarify_turn(self, monkeypatch):
        """The anti-loop guarantee: the model cannot ask two clarifying
        questions in a row, because the tool is not on the request."""
        dialog = {**_default_dialog(), "clarify_last_turn": True}
        llm = _ScriptedLLM([AIMessage(content="Отвечаю по существу")])

        await _run(_state("uzcard", dialog), llm, monkeypatch)

        offered = {getattr(t, "name", "") for t in llm.bind_calls[-1]}
        assert "clarify" not in offered


class TestSystemPrompt:
    @pytest.mark.asyncio
    async def test_policy_and_state_are_separate_messages(self, monkeypatch):
        """Two blocks, not one: a stable policy prefix is what OpenAI's prompt
        cache can hit across turns."""
        from langchain_core.messages import SystemMessage

        dialog = {**_default_dialog(), "products": [{"name": "Ипотека A"}]}
        llm = _ScriptedLLM([AIMessage(content="ок")])

        await _run(_state("покажи первую", dialog), llm, monkeypatch)

        sent = llm.bound.seen_requests[-1]
        systems = [m for m in sent if isinstance(m, SystemMessage)]
        assert len(systems) >= 2
        assert "<state>" in systems[-1].content
        assert "Ипотека A" in systems[-1].content

    @pytest.mark.asyncio
    async def test_client_profile_reaches_the_prompt(self, monkeypatch):
        from langchain_core.messages import SystemMessage

        llm = _ScriptedLLM([AIMessage(content="ок")])
        state = _state("привет", user_profile={"facts": {"age": 30}, "notes": "постоянный клиент"})

        await _run(state, llm, monkeypatch)

        systems = [m for m in llm.bound.seen_requests[-1] if isinstance(m, SystemMessage)]
        blob = " ".join(m.content for m in systems)
        assert "client_profile" in blob and "постоянный клиент" in blob


class TestQualifyEntry:
    @pytest.mark.asyncio
    async def test_product_browse_enters_the_questionnaire(self, monkeypatch):
        """A category browse asks the filtering questions first — the same
        interception the legacy node did after its loop."""
        import app.agent.nodes.consultant_node as cn

        async def fake_start_qualify(category, text, lang, profile):
            return "Вопрос анкеты?", {**_default_dialog(), "flow": "qualify"}, ["Да", "Нет"], None

        monkeypatch.setattr(
            "app.agent.nodes.qualify_flow.start_qualify", fake_start_qualify,
        )
        llm = _ScriptedLLM([_tool_call("get_products", {"category": "mortgage"})])

        out = await _run(_state("хочу ипотеку"), llm, monkeypatch)

        assert out["answer"] == "Вопрос анкеты?"
        assert out["keyboard_options"] == ["Да", "Нет"]


class TestFallbackClassification:
    @pytest.mark.asyncio
    async def test_giving_up_answer_counts_as_fallback(self, monkeypatch):
        llm = _ScriptedLLM([AIMessage(content="Извините, я не могу помочь с этим вопросом")])

        out = await _run(_state("что-то непонятное"), llm, monkeypatch)

        assert out["dialog"]["fallback_streak"] == 1

    @pytest.mark.asyncio
    async def test_productive_tool_is_not_a_fallback(self, monkeypatch):
        llm = _ScriptedLLM([_tool_call("get_currency_info", {})])

        out = await _run(_state("курс"), llm, monkeypatch)

        assert out["dialog"]["fallback_streak"] == 0


class TestDegradedPaths:
    @pytest.mark.asyncio
    async def test_no_model_falls_back_to_faq_only(self, monkeypatch):
        """During a provider outage the bot must still answer documented
        questions instead of failing the turn — the legacy node's guard."""
        import app.agent.nodes.consultant_node as cn
        from app.agent.nodes import faq as faq_module

        monkeypatch.setattr(faq_module, "_get_chat_openai", lambda role=None: None)
        monkeypatch.setattr(faq_module, "faq_precheck_answer", AsyncMock(return_value=None))
        monkeypatch.setattr(cn, "faq_precheck_answer", AsyncMock(return_value=None))
        monkeypatch.setattr(cn, "_faq_only_answer", AsyncMock(return_value="Ответ из FAQ"))

        from app.agent.graph import build_graph

        out = await build_graph().ainvoke(
            _state("как открыть вклад"),
            config={"configurable": {"thread_id": f"v2-{uuid.uuid4()}"}},
        )

        assert out["answer"] == "Ответ из FAQ"

    @pytest.mark.asyncio
    async def test_runaway_tool_loop_is_capped(self, monkeypatch):
        """A model that keeps calling tools must end the turn with what it
        has, not raise. `faq_lookup` is not return_direct, so this loops until
        the model-call limit stops it."""
        from app.agent.consultant import CONSULTANT_MAX_MODEL_CALLS

        llm = _ScriptedLLM([_tool_call("faq_lookup", {"query": "вклад"}, "loop")])

        out = await _run(_state("вклад"), llm, monkeypatch)

        assert llm.bound.calls <= CONSULTANT_MAX_MODEL_CALLS
        assert out["answer"], "a capped turn still has to answer something"
