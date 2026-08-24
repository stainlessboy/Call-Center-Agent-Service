"""Tests for the LLM-factory/None-guard and per-turn FAQ-search memoization
robustness fixes:

  - app/agent/llm.py: _get_chat_openai() must not permanently cache a
    construction failure (lru_cache lives on _build_chat_openai, which raises
    instead of returning None).
  - app/agent/nodes/faq.py: node_faq must not crash on llm.bind_tools(None)
    when _get_chat_openai() returns None — it degrades to the strict FAQ
    lookup fallback instead.
  - app/utils/faq_tools.py: faq_search() is memoized within a single agent
    turn (via reset_faq_turn_cache) so the node_faq strict pre-check and the
    faq_lookup tool don't pay for the embedding call + lexical scan twice.

Also covers the 2026-07 audit fixes (C-2 through C-6, C-9):

  - app/agent/lang_detect.py (C-4): _get_detector_llm must not permanently
    cache a construction failure — same _build_*/_get_* split as llm.py.
  - app/agent/agent.py (C-2): Agent._aload_existing_state retries a failed
    checkpoint read a couple of times, then RAISES instead of silently
    returning {} (which would otherwise reset the whole session).
  - app/agent/agent.py (C-3): Agent._ainvoke/resume_human_mode serialize
    concurrent turns for the same session_id via a per-session asyncio.Lock.
  - app/agent/nodes/calc_flow.py + qualify_flow.py (C-5): reprompt/re-ask
    branches now pass is_fallback=True so fallback_streak actually
    accumulates and the operator button surfaces after repeated confusion.
  - app/agent/nodes/calc_flow.py + qualify_flow.py (C-6): a "назад"/"отмена"
    style message now backs the user out of the calculator / lead-capture /
    qualify questionnaire instead of being a dead end.
  - app/agent/tools.py (C-9): custom_loan_calculator clamps term_months/amount
    to sane ceilings instead of risking an OverflowError on pathological input.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# node_faq: None-LLM guard
# ---------------------------------------------------------------------------

class TestNodeFaqNoneLLMGuard:
    @pytest.mark.asyncio
    async def test_does_not_raise_and_returns_fallback(self, monkeypatch):
        """When _get_chat_openai() returns None, node_faq must not reach
        llm.bind_tools(None) — it should return a fallback answer instead of
        raising."""
        from app.agent.nodes import faq as faq_module
        from app.agent.state import _default_dialog

        monkeypatch.setattr(faq_module, "_get_chat_openai", lambda role=None: None)
        # The deterministic pre-check now goes through faq_precheck_answer
        # (stricter: both legs must be strict) — stub it to miss so the guard
        # path (_faq_lookup) below is what's actually under test.
        monkeypatch.setattr(faq_module, "faq_precheck_answer", AsyncMock(return_value=None))
        monkeypatch.setattr(faq_module, "_faq_lookup", AsyncMock(return_value=None))

        state = {
            "last_user_text": "как заблокировать карту",
            "messages": [],
            "dialog": _default_dialog(),
            "lang": "ru",
            "session_id": "test-none-llm-fallback",
            "user_id": 1,
        }

        result = await faq_module.node_faq(state)

        assert isinstance(result, dict)
        assert result.get("answer")
        assert result["dialog"]["last_lang"] == "ru"

    @pytest.mark.asyncio
    async def test_uses_strict_faq_lookup_when_available(self, monkeypatch):
        """When _get_chat_openai() returns None but the strict FAQ lookup
        finds an answer, the guard must surface it (is_fallback=False)
        instead of the generic fallback reply."""
        from app.agent.nodes import faq as faq_module
        from app.agent.state import _default_dialog

        monkeypatch.setattr(faq_module, "_get_chat_openai", lambda role=None: None)
        # node_faq's deterministic pre-check now goes through the stricter
        # faq_precheck_answer (both legs must be strict) — stub it to miss so
        # we actually reach the None-LLM guard, whose own (single-leg)
        # _faq_lookup call then returns the answer.
        monkeypatch.setattr(faq_module, "faq_precheck_answer", AsyncMock(return_value=None))
        faq_lookup_mock = AsyncMock(return_value="Зайдите в приложение")
        monkeypatch.setattr(faq_module, "_faq_lookup", faq_lookup_mock)

        state = {
            "last_user_text": "как заблокировать карту",
            "messages": [],
            "dialog": _default_dialog(),
            "lang": "ru",
            "session_id": "test-none-llm-found",
            "user_id": 1,
        }

        result = await faq_module.node_faq(state)

        assert result["answer"] == "Зайдите в приложение"
        assert faq_lookup_mock.await_count == 1


# ---------------------------------------------------------------------------
# faq_search: per-turn memoization
# ---------------------------------------------------------------------------

class TestFaqSearchTurnCache:
    @pytest.mark.asyncio
    async def test_second_identical_call_is_cached_after_reset(self, monkeypatch):
        """Within a turn (reset_faq_turn_cache called once), a second
        faq_search call with the same (query, language) must not re-run the
        embedding call."""
        monkeypatch.setenv("FAQ_EMBEDDING_ENABLED", "true")
        monkeypatch.setenv("OPENAI_API_KEY", "fake-key")
        from app.config import get_settings
        get_settings.cache_clear()

        from app.utils import faq_tools
        from app.utils.faq_tools import faq_search, reset_faq_turn_cache

        embed_calls = 0

        async def fake_embed_texts(texts):
            nonlocal embed_calls
            embed_calls += 1
            # None vector short-circuits _semantic_lookup before it touches
            # the DB — we only care about the embed_texts call count here.
            return [None for _ in texts]

        try:
            with patch(
                "app.utils.embeddings.embed_texts", new=AsyncMock(side_effect=fake_embed_texts)
            ), patch.object(
                faq_tools, "_lexical_lookup", new=AsyncMock(return_value=(None, 0.0))
            ):
                reset_faq_turn_cache()
                await faq_search("как заблокировать карту", "ru")
                await faq_search("как заблокировать карту", "ru")

            assert embed_calls == 1
        finally:
            get_settings.cache_clear()

    @pytest.mark.asyncio
    async def test_not_cached_without_reset(self, monkeypatch):
        """Without an active turn cache (contextvar left at its default
        None), faq_search must behave exactly as before — every call hits
        the embedding leg fresh. This proves non-agent callers (tests,
        scripts) are unaffected by the memoization."""
        monkeypatch.setenv("FAQ_EMBEDDING_ENABLED", "true")
        monkeypatch.setenv("OPENAI_API_KEY", "fake-key")
        from app.config import get_settings
        get_settings.cache_clear()

        from app.utils import faq_tools
        from app.utils.faq_tools import faq_search

        # Make sure no turn-cache is active regardless of test execution order.
        faq_tools._faq_turn_cache.set(None)

        embed_calls = 0

        async def fake_embed_texts(texts):
            nonlocal embed_calls
            embed_calls += 1
            return [None for _ in texts]

        try:
            with patch(
                "app.utils.embeddings.embed_texts", new=AsyncMock(side_effect=fake_embed_texts)
            ), patch.object(
                faq_tools, "_lexical_lookup", new=AsyncMock(return_value=(None, 0.0))
            ):
                await faq_search("как заблокировать карту", "ru")
                await faq_search("как заблокировать карту", "ru")

            assert embed_calls == 2
        finally:
            get_settings.cache_clear()


# ---------------------------------------------------------------------------
# C-4: lang_detect detector-LLM factory must not cache a None permanently
# ---------------------------------------------------------------------------

class TestLangDetectorNotCachedOnFailure:
    def test_transient_construction_failure_is_retried(self, monkeypatch):
        """_get_detector_llm() must not get permanently stuck on None after a
        transient construction failure — lru_cache lives on
        _build_detector_llm, which raises instead of returning None (same
        split already applied to app/agent/llm.py's _build_chat_openai)."""
        from app.agent import lang_detect as ld

        ld._get_detector_llm.cache_clear()

        calls = {"n": 0}

        class _FakeChatOpenAI:
            def __init__(self, **kwargs):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise RuntimeError("transient provider outage")
                self.kwargs = kwargs

        monkeypatch.setattr(ld, "ChatOpenAI", _FakeChatOpenAI)
        monkeypatch.setattr(ld, "use_gpt", lambda: True)
        monkeypatch.setenv("OPENAI_API_KEY", "fake-key")

        try:
            first = ld._get_detector_llm()
            assert first is None, "first (failing) call must be swallowed, not raise"
            assert calls["n"] == 1

            second = ld._get_detector_llm()
            assert isinstance(second, _FakeChatOpenAI), (
                "second call must retry construction, not stay stuck at None "
                "from a cached failure"
            )
            assert calls["n"] == 2
        finally:
            ld._get_detector_llm.cache_clear()


# ---------------------------------------------------------------------------
# C-2: Agent._aload_existing_state — retry transient failures, raise on
# persistent failure instead of silently resetting the session
# ---------------------------------------------------------------------------

class TestAgentLoadExistingStateRetryOrRaise:
    @pytest.mark.asyncio
    async def test_persistent_failure_raises_instead_of_silent_reset(self, monkeypatch):
        """A checkpointer that never recovers must propagate the exception —
        NOT return {}, which would look like a brand new session and let the
        following graph.ainvoke silently overwrite the real dialog/messages."""
        from app.agent.agent import Agent

        agent = Agent()

        async def always_fails(config):
            raise RuntimeError("db down")

        monkeypatch.setattr(agent._graph, "aget_state", always_fails)

        with pytest.raises(RuntimeError, match="db down"):
            await agent._aload_existing_state(agent._build_config("s-always-fails"))

    @pytest.mark.asyncio
    async def test_transient_failure_recovers_within_retry_budget(self, monkeypatch):
        """A checkpointer that fails once then recovers must return the real
        state, not raise and not silently degrade to {}."""
        from app.agent.agent import Agent

        agent = Agent()
        calls = {"n": 0}

        class _FakeSnapshot:
            values = {"dialog": {"flow": "calc_flow"}, "messages": ["hist"]}

        async def fails_once_then_ok(config):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("transient blip")
            return _FakeSnapshot()

        monkeypatch.setattr(agent._graph, "aget_state", fails_once_then_ok)

        result = await agent._aload_existing_state(agent._build_config("s-recovers"))
        assert result == {"dialog": {"flow": "calc_flow"}, "messages": ["hist"]}
        assert calls["n"] == 2

    @pytest.mark.asyncio
    async def test_new_session_empty_state_is_not_an_error(self, monkeypatch):
        """A brand new session (no checkpoint yet) must return {} immediately
        — this is NOT the same as a read failure and must not retry/raise."""
        from app.agent.agent import Agent

        agent = Agent()
        calls = {"n": 0}

        class _EmptySnapshot:
            values = None

        async def returns_empty(config):
            calls["n"] += 1
            return _EmptySnapshot()

        monkeypatch.setattr(agent._graph, "aget_state", returns_empty)

        result = await agent._aload_existing_state(agent._build_config("s-new"))
        assert result == {}
        assert calls["n"] == 1


# ---------------------------------------------------------------------------
# C-3: Agent per-session lock serializes concurrent turns for the same
# session_id (but NOT across different sessions)
# ---------------------------------------------------------------------------

class TestAgentSessionLockSerialization:
    @pytest.mark.asyncio
    async def test_concurrent_calls_same_session_are_serialized(self, monkeypatch):
        from app.agent.agent import Agent
        from app.agent.state import AgentTurnResult

        agent = Agent()
        first_entered = asyncio.Event()
        release_first = asyncio.Event()
        concurrent_entries = 0
        max_concurrent = 0
        call_count = 0

        async def fake_locked(session_id, user_text, language, human_mode, user_id):
            nonlocal concurrent_entries, max_concurrent, call_count
            call_count += 1
            concurrent_entries += 1
            max_concurrent = max(max_concurrent, concurrent_entries)
            if call_count == 1:
                first_entered.set()
                await release_first.wait()
            concurrent_entries -= 1
            return AgentTurnResult(text=f"reply-{user_text}")

        monkeypatch.setattr(agent, "_ainvoke_locked", fake_locked)

        async def second_call():
            await first_entered.wait()
            # Give a (hypothetically unlocked) implementation a chance to
            # start the second turn concurrently before we release the first.
            await asyncio.sleep(0.01)
            release_first.set()
            return await agent._ainvoke("same-session", "msg2")

        first_task = asyncio.create_task(agent._ainvoke("same-session", "msg1"))
        second_task = asyncio.create_task(second_call())

        r1, r2 = await asyncio.gather(first_task, second_task)

        assert max_concurrent == 1, "the two turns for the same session must never overlap"
        assert r1.text == "reply-msg1"
        assert r2.text == "reply-msg2"

    @pytest.mark.asyncio
    async def test_different_sessions_are_not_serialized_against_each_other(self, monkeypatch):
        """Sanity check: the lock is per-session, not global."""
        from app.agent.agent import Agent
        from app.agent.state import AgentTurnResult

        agent = Agent()
        concurrent_entries = 0
        max_concurrent = 0

        async def fake_locked(session_id, user_text, language, human_mode, user_id):
            nonlocal concurrent_entries, max_concurrent
            concurrent_entries += 1
            max_concurrent = max(max_concurrent, concurrent_entries)
            await asyncio.sleep(0.02)
            concurrent_entries -= 1
            return AgentTurnResult(text="ok")

        monkeypatch.setattr(agent, "_ainvoke_locked", fake_locked)

        await asyncio.gather(
            agent._ainvoke("session-a", "hi"),
            agent._ainvoke("session-b", "hi"),
        )
        assert max_concurrent == 2


# ---------------------------------------------------------------------------
# C-5: fallback_streak must actually accumulate through calc_flow /
# qualify_flow reprompt turns, surfacing the operator button after
# FALLBACK_STREAK_THRESHOLD consecutive unrecognized answers.
# ---------------------------------------------------------------------------

class TestCalcFlowFallbackStreakE2E:
    @pytest.mark.asyncio
    async def test_three_unparsed_answers_show_operator_button(self, monkeypatch):
        from app.agent import calc_extractor
        from app.agent.constants import FLOW_CALC, STEP_AMOUNT
        from app.agent.nodes.calc_flow import node_calc_flow
        from app.agent.state import _default_dialog

        # No LLM available → extract_calc_value degrades to {"type": "unparsed"}
        # deterministically, without hitting the network.
        monkeypatch.setattr(calc_extractor, "_get_chat_openai", lambda role=None: None)

        dialog = {
            **_default_dialog(),
            "flow": FLOW_CALC,
            "category": "mortgage",
            "selected_product": {"name": "Test Mortgage", "rate_matrix": []},
            "calc_step": STEP_AMOUNT,
            "calc_slots": {},
        }

        show_operator = False
        for _ in range(3):
            state = {
                "last_user_text": "не знаю",
                "messages": [],
                "dialog": dialog,
                "lang": "ru",
                "session_id": "test-calc-streak",
                "user_id": 1,
            }
            result = await node_calc_flow(state)
            dialog = result["dialog"]
            show_operator = result["show_operator_button"]

        assert dialog["fallback_streak"] == 3
        assert show_operator is True


class TestQualifyFlowFallbackStreakE2E:
    @pytest.mark.asyncio
    async def test_three_unmatched_answers_show_operator_button(self):
        from app.agent.constants import FLOW_QUALIFY
        from app.agent.nodes.qualify_flow import node_qualify_flow
        from app.agent.state import _default_dialog

        dialog = {
            **_default_dialog(),
            "flow": FLOW_QUALIFY,
            "qualify_category": "autoloan",
            "qualify_node": "salary",
            "qualify_answers": {},
        }

        show_operator = False
        for _ in range(3):
            state = {
                "last_user_text": "мяу",
                "messages": [],
                "dialog": dialog,
                "lang": "ru",
                "session_id": "test-qualify-streak",
                "user_id": 1,
            }
            result = await node_qualify_flow(state)
            dialog = result["dialog"]
            show_operator = result["show_operator_button"]

        assert dialog["fallback_streak"] == 3
        assert show_operator is True


# ---------------------------------------------------------------------------
# C-6: "назад"/"отмена" backs the user out of calc_flow / qualify_flow
# instead of being a dead end.
# ---------------------------------------------------------------------------

class TestCalcFlowBackTriggerCancel:
    @pytest.mark.asyncio
    async def test_back_trigger_with_products_shows_product_list(self):
        from app.agent.constants import FLOW_CALC, FLOW_SHOW_PRODUCTS, STEP_AMOUNT
        from app.agent.nodes.calc_flow import node_calc_flow
        from app.agent.state import _default_dialog

        products = [{"name": "Ипотека Стандарт"}, {"name": "Ипотека Лайт"}]
        dialog = {
            **_default_dialog(),
            "flow": FLOW_CALC,
            "category": "mortgage",
            "products": products,
            "selected_product": products[0],
            "calc_step": STEP_AMOUNT,
            "calc_slots": {},
        }
        state = {
            "last_user_text": "отмена",
            "messages": [],
            "dialog": dialog,
            "lang": "ru",
            "session_id": "test-calc-cancel-list",
            "user_id": 1,
        }

        result = await node_calc_flow(state)

        assert result["dialog"]["flow"] == FLOW_SHOW_PRODUCTS
        assert result["dialog"]["calc_step"] is None
        assert result["keyboard_options"] == ["Ипотека Стандарт", "Ипотека Лайт"]
        assert "Ипотека Стандарт" in result["answer"]
        assert result["show_operator_button"] is False

    @pytest.mark.asyncio
    async def test_back_trigger_without_products_resets_to_menu(self):
        from app.agent.constants import FLOW_CALC, STEP_AMOUNT
        from app.agent.nodes.calc_flow import node_calc_flow
        from app.agent.state import _default_dialog

        dialog = {
            **_default_dialog(),
            "flow": FLOW_CALC,
            "category": "mortgage",
            "selected_product": {"name": "Ипотека Стандарт"},
            "calc_step": STEP_AMOUNT,
            "calc_slots": {"amount": 100_000_000},
        }
        state = {
            "last_user_text": "cancel",
            "messages": [],
            "dialog": dialog,
            "lang": "ru",
            "session_id": "test-calc-cancel-menu",
            "user_id": 1,
        }

        result = await node_calc_flow(state)

        assert result["dialog"]["flow"] is None
        assert result["dialog"]["calc_step"] is None
        assert result["keyboard_options"]  # main menu buttons, non-empty

    @pytest.mark.asyncio
    async def test_back_trigger_cancels_lead_capture(self):
        """"назад" during the post-calculation lead-capture mini-flow (name/
        phone collection) must also bail out, not be taken literally as the
        customer's name/phone."""
        from app.agent.constants import FLOW_CALC
        from app.agent.nodes.calc_flow import node_calc_flow
        from app.agent.state import _default_dialog

        dialog = {
            **_default_dialog(),
            "flow": FLOW_CALC,
            "category": "mortgage",
            "selected_product": {"name": "Ипотека Стандарт"},
            "calc_slots": {"amount": 100_000_000, "term_months": 60},
            "lead_step": "name",
        }
        state = {
            "last_user_text": "отмена",
            "messages": [],
            "dialog": dialog,
            "lang": "ru",
            "session_id": "test-lead-cancel",
            "user_id": 1,
        }

        result = await node_calc_flow(state)

        assert result["dialog"]["lead_step"] is None
        assert result["dialog"]["flow"] is None


class TestQualifyFlowBackTriggerCancel:
    @pytest.mark.asyncio
    async def test_back_trigger_resets_to_menu(self):
        from app.agent.constants import FLOW_QUALIFY
        from app.agent.nodes.qualify_flow import node_qualify_flow
        from app.agent.state import _default_dialog

        dialog = {
            **_default_dialog(),
            "flow": FLOW_QUALIFY,
            "qualify_category": "autoloan",
            "qualify_node": "salary",
            "qualify_answers": {},
        }
        state = {
            "last_user_text": "назад",
            "messages": [],
            "dialog": dialog,
            "lang": "ru",
            "session_id": "test-qualify-cancel",
            "user_id": 1,
        }

        result = await node_qualify_flow(state)

        assert result["dialog"]["flow"] is None
        assert result["dialog"]["qualify_category"] is None
        assert result["keyboard_options"]  # main menu buttons, non-empty
        assert result["show_operator_button"] is False


# ---------------------------------------------------------------------------
# C-9: custom_loan_calculator must not risk an OverflowError on pathological
# term_months/amount — it should return a clear range message instead.
# ---------------------------------------------------------------------------

class TestCustomLoanCalculatorSanityCaps:
    @pytest.mark.asyncio
    async def test_excessive_term_returns_range_message_not_crash(self):
        from app.agent.tools import custom_loan_calculator

        result, artifact = await custom_loan_calculator.coroutine(
            amount=50_000_000, term_months=3600, downpayment=0, state={"lang": "ru"},
        )

        assert result
        assert "600" in result  # states the allowed max term in months
        assert artifact is None

    @pytest.mark.asyncio
    async def test_excessive_amount_returns_range_message_not_crash(self):
        from app.agent.tools import custom_loan_calculator

        result, artifact = await custom_loan_calculator.coroutine(
            amount=50_000_000_000, term_months=60, downpayment=0, state={"lang": "ru"},
        )

        assert result
        assert "10 000 000 000" in result
        assert artifact is None

    @pytest.mark.asyncio
    async def test_within_bounds_still_computes_normally(self):
        """Regression guard: the new caps must not affect ordinary inputs —
        checked by asserting the error path's "слишком" marker is absent
        (not by guessing at computed monthly-payment digits, which would be
        a coincidental/fragile assertion)."""
        from app.agent.tools import custom_loan_calculator

        result, artifact = await custom_loan_calculator.coroutine(
            amount=50_000_000, term_months=60, downpayment=0, state={"lang": "ru"},
        )

        assert "слишком" not in result
        assert "50 000 000" in result
        assert artifact["type"] == "calc_result"
        assert artifact["data"]["schedule"]
        assert len(artifact["data"]["schedule"]) == 60
