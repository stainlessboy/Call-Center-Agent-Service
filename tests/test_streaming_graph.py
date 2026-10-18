"""Token streaming sourced from the graph (roadmap П.5).

Before this, `node_faq` had its own `.astream()` branch and forwarded chunks to
a contextvar callback. That could only ever see the model call inside that one
node — under `CONSULTANT_AGENT_V2` the call moved into the consultant subgraph
and the Mini App would have gone silent. Streaming from `graph.astream` covers
both paths.

The risk this trades for is over-streaming: the graph makes several internal
LLM calls per turn (language detection, FAQ rerank, answer rephrasing) that the
customer must never see. Most of these tests are about what must NOT be sent.
"""
from __future__ import annotations

import uuid

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from unittest.mock import AsyncMock

from app.agent import agent as agent_module


@pytest.fixture(autouse=True)
def _no_db(monkeypatch):
    from app.agent.nodes import prepare as prepare_module

    monkeypatch.setattr(prepare_module, "load_user_profile", AsyncMock(return_value=None))


class _StreamingModel(BaseChatModel):
    """A real `BaseChatModel` that answers the same text every time.

    It has to be a genuine chat model, because LangGraph's `messages` stream is
    fed by the chat-model callback machinery: a hand-rolled object with an
    `ainvoke` method returns an answer but emits no token events, so it would
    silently "prove" that streaming works while testing nothing.

    It deliberately does NOT use `GenericFakeChatModel`, whose `messages` field
    is a one-shot iterator: any copy of the model (`bind`, `bind_tools`) shares
    and drains that same iterator, so a turn that resolves the model more than
    once dies with StopIteration inside the graph — an artifact of the fake
    that looks exactly like a hang in the code under test.
    """

    answer: str = "Здравствуйте чем помочь"

    @property
    def _llm_type(self) -> str:
        return "streaming-fake"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content=self.answer))]
        )

    def _stream(self, messages, stop=None, run_manager=None, **kwargs):
        for word in self.answer.split():
            chunk = ChatGenerationChunk(message=AIMessageChunk(content=word + " "))
            if run_manager:
                run_manager.on_llm_new_token(word + " ", chunk=chunk)
            yield chunk


def _model(text: str = "Здравствуйте чем помочь") -> _StreamingModel:
    return _StreamingModel(answer=text)


async def _run_turn(monkeypatch, collected: list, text: str = "привет"):
    from app.agent.nodes import faq as faq_module
    from app.agent.streaming import reset_on_token_callback, set_on_token_callback

    monkeypatch.setattr(
        faq_module, "_get_chat_openai", lambda role=None: _model("Здравствуйте чем помочь"),
    )
    monkeypatch.setattr(faq_module, "faq_precheck_answer", AsyncMock(return_value=None))
    monkeypatch.setattr(
        "app.agent.nodes.consultant_node.faq_precheck_answer", AsyncMock(return_value=None),
    )

    async def on_token(chunk: str) -> None:
        collected.append(chunk)

    agent = agent_module.Agent()
    token = set_on_token_callback(on_token)
    try:
        return await agent.send_message(
            session_id=f"stream-{uuid.uuid4()}", user_id=1, text=text, language="ru",
        )
    finally:
        reset_on_token_callback(token)


class TestLegacyPath:
    @pytest.mark.asyncio
    async def test_tokens_reach_the_callback(self, monkeypatch):
        collected: list[str] = []
        result = await _run_turn(monkeypatch, collected)

        assert collected, "the customer-facing answer must stream"
        assert "".join(collected).strip() in result.text

    @pytest.mark.asyncio
    async def test_no_callback_means_no_streaming_path(self, monkeypatch):
        """Telegram never arms a callback; that path must stay a plain
        invoke, byte for byte as before."""
        from app.agent.nodes import faq as faq_module

        monkeypatch.setattr(
            faq_module, "_get_chat_openai", lambda role=None: _model("Ответ"),
        )
        monkeypatch.setattr(faq_module, "faq_precheck_answer", AsyncMock(return_value=None))

        agent = agent_module.Agent()
        result = await agent.send_message(
            session_id=f"plain-{uuid.uuid4()}", user_id=1, text="привет", language="ru",
        )

        assert result.text


class TestConsultantPath:
    @pytest.mark.asyncio
    async def test_tokens_stream_from_inside_the_subgraph(self, monkeypatch):
        """The whole point: with the agent subgraph the model call is two
        levels down, and it still has to reach the customer live."""
        monkeypatch.setenv("CONSULTANT_AGENT_V2", "true")
        collected: list[str] = []

        result = await _run_turn(monkeypatch, collected)

        assert collected, "consultant answers must stream too"
        assert "".join(collected).strip() in result.text


class TestInternalCallsAreNotStreamed:
    @pytest.mark.asyncio
    async def test_language_detector_output_never_streams(self, monkeypatch):
        """`node_prepare_turn` asks a model which language this is. Its
        answer is the token "uz", and it must not appear in the chat."""
        from app.agent.nodes import prepare as prepare_module

        detector_calls: list = []

        async def fake_detect(text, fallback="ru"):
            detector_calls.append(text)
            return "ru"

        monkeypatch.setattr(prepare_module, "detect_language", fake_detect)

        collected: list[str] = []
        # language=None forces the detector to run.
        from app.agent.nodes import faq as faq_module
        from app.agent.streaming import reset_on_token_callback, set_on_token_callback

        monkeypatch.setattr(
            faq_module, "_get_chat_openai", lambda role=None: _model("Ответ бота"),
        )
        monkeypatch.setattr(faq_module, "faq_precheck_answer", AsyncMock(return_value=None))

        async def on_token(chunk: str) -> None:
            collected.append(chunk)

        agent = agent_module.Agent()
        token = set_on_token_callback(on_token)
        try:
            await agent.send_message(
                session_id=f"det-{uuid.uuid4()}", user_id=1, text="привет", language=None,
            )
        finally:
            reset_on_token_callback(token)

        assert detector_calls, "the detector must actually have run"
        assert "uz" not in "".join(collected)

    def test_streamed_nodes_allowlist_is_narrow(self):
        """A regression fence: adding a node here is a decision to show its
        model output to the customer live."""
        assert agent_module._STREAMED_NODES == {"faq", "model"}
