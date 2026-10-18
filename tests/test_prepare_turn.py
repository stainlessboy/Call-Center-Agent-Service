"""node_prepare_turn — language resolution + profile load (roadmap П.3).

This logic used to live in `Agent._ainvoke_locked`, where it forced a
checkpoint read before every graph invocation. Moving it into the graph's
entry node removed that read; these tests pin the behaviour that moved (it
had no direct coverage before) and the round-trip saving that motivated it.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest

from app.agent.nodes import prepare as prepare_module
from app.agent.state import _default_dialog


def _state(**overrides) -> dict:
    base = {
        "last_user_text": "здравствуйте",
        "dialog": _default_dialog(),
        "user_id": None,
        "user_profile": None,
    }
    base.update(overrides)
    return base


class TestLanguageResolution:
    @pytest.mark.asyncio
    async def test_preset_lang_skips_the_detector(self, monkeypatch):
        """A language already on the state is an answer, not a question —
        paying an LLM call to re-derive it would be pure waste."""
        detector = AsyncMock(return_value="uz")
        monkeypatch.setattr(prepare_module, "detect_language", detector)

        cmd = await prepare_module.node_prepare_turn(_state(lang="ru"))

        assert cmd.update["lang"] == "ru"
        detector.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_requested_lang_wins_without_detector(self, monkeypatch):
        """`User.language` (picked in /start) is authoritative and free."""
        detector = AsyncMock(return_value="uz")
        monkeypatch.setattr(prepare_module, "detect_language", detector)

        cmd = await prepare_module.node_prepare_turn(_state(requested_lang="en"))

        assert cmd.update["lang"] == "en"
        detector.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_detector_runs_on_first_contact(self, monkeypatch):
        monkeypatch.setattr(prepare_module, "detect_language", AsyncMock(return_value="uz"))
        monkeypatch.setattr(prepare_module, "check_lang_mismatch", lambda *a: None)
        monkeypatch.setattr(prepare_module, "looks_worth_llm_recheck", lambda *a: False)

        cmd = await prepare_module.node_prepare_turn(_state())

        assert cmd.update["lang"] == "uz"
        assert cmd.update["dialog"]["last_lang"] == "uz"

    @pytest.mark.asyncio
    async def test_mismatch_is_suggested_never_applied(self, monkeypatch):
        """The turn is still answered in the current language; the switch is
        only offered."""
        monkeypatch.setattr(prepare_module, "check_lang_mismatch", lambda *a: "uz")

        cmd = await prepare_module.node_prepare_turn(_state(requested_lang="ru"))

        assert cmd.update["lang"] == "ru"
        assert cmd.update["suggested_lang"] == "uz"
        assert cmd.update["dialog"]["lang_switch_offered"] == "uz"

    @pytest.mark.asyncio
    async def test_same_offer_is_not_repeated(self, monkeypatch):
        monkeypatch.setattr(prepare_module, "check_lang_mismatch", lambda *a: "uz")
        dialog = {**_default_dialog(), "lang_switch_offered": "uz"}

        cmd = await prepare_module.node_prepare_turn(
            _state(requested_lang="ru", dialog=dialog)
        )

        assert cmd.update["suggested_lang"] is None

    @pytest.mark.asyncio
    async def test_detector_failure_falls_back_to_dialog_language(self, monkeypatch):
        """A detector outage must degrade to the previous language, not fail
        the customer's turn."""
        monkeypatch.setattr(
            prepare_module, "detect_language", AsyncMock(side_effect=RuntimeError("down")),
        )
        dialog = {**_default_dialog(), "last_lang": "uz"}

        cmd = await prepare_module.node_prepare_turn(_state(dialog=dialog))

        assert cmd.update["lang"] == "uz"
        assert cmd.update["suggested_lang"] is None


class TestProfileLoad:
    @pytest.mark.asyncio
    async def test_profile_is_loaded_for_known_user(self, monkeypatch):
        profile = {"facts": {"income_monthly": 5_000_000}, "notes": ""}
        monkeypatch.setattr(prepare_module, "load_user_profile", AsyncMock(return_value=profile))

        cmd = await prepare_module.node_prepare_turn(_state(lang="ru", user_id=42))

        assert cmd.update["user_profile"] == profile

    @pytest.mark.asyncio
    async def test_db_failure_degrades_to_no_profile(self, monkeypatch):
        monkeypatch.setattr(
            prepare_module, "load_user_profile", AsyncMock(side_effect=RuntimeError("db down")),
        )

        cmd = await prepare_module.node_prepare_turn(_state(lang="ru", user_id=42))

        assert cmd.update["user_profile"] is None

    @pytest.mark.asyncio
    async def test_anonymous_turn_skips_the_query(self, monkeypatch):
        loader = AsyncMock(return_value={"facts": {}})
        monkeypatch.setattr(prepare_module, "load_user_profile", loader)

        await prepare_module.node_prepare_turn(_state(lang="ru", user_id=None))

        loader.assert_not_awaited()


class TestRouting:
    @pytest.mark.asyncio
    async def test_hands_over_to_router(self):
        cmd = await prepare_module.node_prepare_turn(_state(lang="ru"))
        assert cmd.goto == "router"


class TestCheckpointRoundTrips:
    """The point of moving this logic into the graph: one read + one write
    per turn instead of a standalone `aget_state` first."""

    @pytest.mark.asyncio
    async def test_single_read_and_write_per_turn(self, monkeypatch):
        from unittest.mock import AsyncMock as _AsyncMock

        from app.agent.agent import Agent
        from app.agent.nodes import faq as faq_module

        monkeypatch.setattr(faq_module, "_get_chat_openai", lambda role=None: None)
        monkeypatch.setattr(faq_module, "faq_precheck_answer", _AsyncMock(return_value=None))
        monkeypatch.setattr(faq_module, "_faq_lookup", _AsyncMock(return_value="Ответ"))

        agent = Agent()
        checkpointer = agent._graph.checkpointer
        reads, writes = [], []
        real_get, real_put = checkpointer.aget_tuple, checkpointer.aput

        async def counted_get(config):
            reads.append(config)
            return await real_get(config)

        async def counted_put(config, checkpoint, metadata, new_versions):
            writes.append(config)
            return await real_put(config, checkpoint, metadata, new_versions)

        monkeypatch.setattr(checkpointer, "aget_tuple", counted_get)
        monkeypatch.setattr(checkpointer, "aput", counted_put)

        await agent.send_message(
            session_id=f"rt-{uuid.uuid4()}", user_id=1, text="привет", language="ru",
        )

        # One read to load the thread. Writes are per super-step (LangGraph
        # checkpoints after each node), so the meaningful assertion is that
        # the turn no longer performs a SECOND, separate state load before
        # the graph even starts.
        assert len(reads) == 1, f"expected a single checkpoint load, got {len(reads)}"
        assert writes, "the turn must persist its state"


class TestEndToEndLanguageSuggestion:
    @pytest.mark.asyncio
    async def test_suggestion_reaches_the_turn_result(self, monkeypatch):
        """The suggestion is produced inside the graph now and has to travel
        back out through the node's state update into `AgentTurnResult`,
        which is what the bot layer renders the "switch language?" prompt
        from."""
        from unittest.mock import AsyncMock as _AsyncMock

        from app.agent.agent import Agent
        from app.agent.nodes import faq as faq_module

        monkeypatch.setattr(faq_module, "_get_chat_openai", lambda role=None: None)
        monkeypatch.setattr(faq_module, "faq_precheck_answer", _AsyncMock(return_value=None))
        monkeypatch.setattr(faq_module, "_faq_lookup", _AsyncMock(return_value="Ответ"))
        monkeypatch.setattr(prepare_module, "check_lang_mismatch", lambda *a: "uz")

        agent = Agent()
        result = await agent.send_message(
            session_id=f"lang-{uuid.uuid4()}", user_id=1,
            text="Menga kredit kerak", language="ru",
        )

        assert result.suggested_language == "uz"

    @pytest.mark.asyncio
    async def test_no_suggestion_when_languages_agree(self, monkeypatch):
        from unittest.mock import AsyncMock as _AsyncMock

        from app.agent.agent import Agent
        from app.agent.nodes import faq as faq_module

        monkeypatch.setattr(faq_module, "_get_chat_openai", lambda role=None: None)
        monkeypatch.setattr(faq_module, "faq_precheck_answer", _AsyncMock(return_value=None))
        monkeypatch.setattr(faq_module, "_faq_lookup", _AsyncMock(return_value="Ответ"))
        monkeypatch.setattr(prepare_module, "check_lang_mismatch", lambda *a: None)

        agent = Agent()
        result = await agent.send_message(
            session_id=f"lang-{uuid.uuid4()}", user_id=1, text="привет", language="ru",
        )

        assert result.suggested_language is None
