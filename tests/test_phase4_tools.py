"""Phase 4 ("Экспертиза") — compare_products / what_if_scenario /
affordability_check tools, the clarify anti-loop guard, the deterministic
DTI warning in calc_flow, the qualify dead-end rescue, and the operator
handoff summary.
"""
from __future__ import annotations

import asyncio

import pytest
from langchain_core.messages import AIMessage
from unittest.mock import AsyncMock, patch

from app.agent.i18n import at
from app.agent.state import _default_dialog
from app.utils.amortization import amortize
from app.utils.faq_tools import FaqSearch


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# compare_products
# ---------------------------------------------------------------------------

class TestCompareProducts:
    def test_resolves_from_dialog_products_by_index(self):
        from app.agent import tools as tools_module

        products = [
            {"name": "Ипотека Стандарт", "rate": "17.0%", "term": "до 180 мес.",
             "amount": "от 50 000 000 сум", "downpayment": "от 25%", "rate_rules": []},
            {"name": "Ипотека Лайт", "rate": "20.0%", "term": "до 120 мес.",
             "amount": "от 30 000 000 сум", "downpayment": "от 15%", "rate_rules": []},
        ]
        state = {"lang": "ru", "dialog": {**_default_dialog(), "category": "mortgage", "products": products}}
        text, artifact = _run(tools_module.compare_products.coroutine(product_names=["1", "2"], state=state))

        assert "Ипотека Стандарт" in text
        assert "Ипотека Лайт" in text
        assert artifact["type"] == "comparison_table"
        assert artifact["data"]["category"] == "mortgage"
        assert artifact["data"]["columns"] == ["rate", "term", "amount", "downpayment"]
        assert len(artifact["data"]["products"]) == 2

    def test_falls_back_to_db_lookup_when_dialog_has_no_products(self):
        from app.agent import tools as tools_module

        db_products = [
            {"name": "Автокредит Базовый", "rate_min_pct": 20.0, "rate_rules": []},
            {"name": "Автокредит Плюс", "rate_min_pct": 18.0, "rate_rules": []},
        ]
        state = {"lang": "ru", "dialog": {**_default_dialog(), "category": "autoloan", "products": []}}
        with patch.object(tools_module, "_get_products_by_category", new=AsyncMock(return_value=db_products)):
            text, artifact = _run(
                tools_module.compare_products.coroutine(
                    product_names=["Автокредит Базовый", "Автокредит Плюс"], state=state,
                )
            )
        assert artifact is not None
        assert len(artifact["data"]["products"]) == 2

    def test_no_candidates_at_all(self):
        from app.agent import tools as tools_module

        state = {"lang": "ru", "dialog": {**_default_dialog()}}
        text, artifact = _run(tools_module.compare_products.coroutine(product_names=["1", "2"], state=state))
        assert artifact is None
        assert text == at("compare_no_products", "ru")

    def test_fewer_than_two_matches_returns_no_artifact(self):
        from app.agent import tools as tools_module

        products = [{"name": "Ипотека Стандарт", "rate_rules": []}]
        state = {"lang": "ru", "dialog": {**_default_dialog(), "category": "mortgage", "products": products}}
        text, artifact = _run(
            tools_module.compare_products.coroutine(product_names=["Ипотека Стандарт", "Несуществующий"], state=state)
        )
        assert artifact is None
        assert "Ипотека Стандарт" in text  # listed as an available option

    def test_deduplicates_same_product_matched_twice(self):
        from app.agent import tools as tools_module

        products = [
            {"name": "Ипотека Стандарт", "rate_rules": []},
            {"name": "Ипотека Лайт", "rate_rules": []},
        ]
        state = {"lang": "ru", "dialog": {**_default_dialog(), "category": "mortgage", "products": products}}
        # "1" and "стандарт" both resolve to the same product — should not count twice.
        text, artifact = _run(
            tools_module.compare_products.coroutine(product_names=["1", "стандарт", "2"], state=state)
        )
        assert artifact is not None
        assert len(artifact["data"]["products"]) == 2


# ---------------------------------------------------------------------------
# what_if_scenario
# ---------------------------------------------------------------------------

class TestWhatIfScenario:
    _PRODUCT = {"name": "Ипотека Стандарт", "rate_rules": [{"rate_min_pct": 18.0}]}

    def test_no_product_resolvable(self):
        from app.agent import tools as tools_module

        state = {"lang": "ru", "dialog": {**_default_dialog()}}
        text, artifact = _run(tools_module.what_if_scenario.coroutine(amount=10_000_000, term_months=12, state=state))
        assert artifact is None
        assert text == at("what_if_no_product", "ru")

    def test_missing_amount_and_term(self):
        from app.agent import tools as tools_module

        state = {"lang": "ru", "dialog": {**_default_dialog(), "selected_product": self._PRODUCT}}
        text, artifact = _run(tools_module.what_if_scenario.coroutine(state=state))
        assert artifact is None
        assert text == at("what_if_missing_params", "ru")

    def test_not_available_for_deposit(self):
        from app.agent import tools as tools_module

        state = {
            "lang": "ru",
            "dialog": {**_default_dialog(), "category": "deposit", "selected_product": {"name": "Депозит"}},
        }
        text, artifact = _run(
            tools_module.what_if_scenario.coroutine(amount=10_000_000, term_months=12, state=state)
        )
        assert artifact is None
        assert text == at("what_if_not_for_deposit", "ru")

    def test_matches_amortize_and_marks_hypothetical(self):
        from app.agent import tools as tools_module

        state = {
            "lang": "ru",
            "dialog": {**_default_dialog(), "category": "mortgage", "selected_product": self._PRODUCT},
        }
        text, artifact = _run(
            tools_module.what_if_scenario.coroutine(amount=40_000_000, term_months=36, downpayment_pct=0, state=state)
        )
        expected = amortize(40_000_000, 18.0, 36)
        assert artifact["type"] == "calc_result"
        data = artifact["data"]
        assert data["kind"] == "credit"
        assert data["is_hypothetical"] is True
        assert data["rate_pct"] == 18.0
        assert data["monthly_payment"] == round(expected.monthly_payment, 2)
        assert data["product_name"] == "Ипотека Стандарт"
        assert "Ипотека Стандарт" in text

    def test_falls_back_to_calc_slots_for_unset_params(self):
        from app.agent import tools as tools_module

        state = {
            "lang": "ru",
            "dialog": {
                **_default_dialog(), "category": "mortgage", "selected_product": self._PRODUCT,
                "calc_slots": {"amount": 40_000_000, "term_months": 36, "downpayment": 10},
            },
        }
        # Only override term_months — amount/downpayment should come from calc_slots.
        text, artifact = _run(tools_module.what_if_scenario.coroutine(term_months=60, state=state))
        assert artifact["data"]["term_months"] == 60
        assert artifact["data"]["amount"] == 40_000_000
        assert artifact["data"]["downpayment_pct"] == 10.0

    def test_does_not_mutate_dialog_calc_slots(self):
        """The core Phase 4 requirement: a what_if call is a side-hypothesis,
        never a mutation of the customer's real in-progress calculator slots."""
        from app.agent import tools as tools_module

        original_slots = {"amount": 40_000_000, "term_months": 36, "downpayment": 10}
        dialog = {
            **_default_dialog(), "category": "mortgage", "selected_product": self._PRODUCT,
            "calc_slots": dict(original_slots),
        }
        state = {"lang": "ru", "dialog": dialog}
        _run(tools_module.what_if_scenario.coroutine(amount=90_000_000, term_months=84, downpayment_pct=40, state=state))
        assert dialog["calc_slots"] == original_slots

        # And the node_faq dialog-update handler for this tool name doesn't
        # touch calc_slots either — it's not special-cased, so it falls
        # through to the generic keyboard reattachment with an unchanged copy.
        from app.agent.nodes.faq import _update_dialog_from_tools

        new_dialog, _keyboard = _run(
            _update_dialog_from_tools(
                dialog,
                [{"name": "what_if_scenario", "args": {"amount": 90_000_000, "term_months": 84}}],
                "а если на 7 лет", "ru",
            )
        )
        assert new_dialog["calc_slots"] == original_slots


# ---------------------------------------------------------------------------
# affordability_check
# ---------------------------------------------------------------------------

class TestAffordabilityCheck:
    def test_not_enough_context(self):
        from app.agent import tools as tools_module

        state = {"lang": "ru", "dialog": {**_default_dialog()}}
        text, artifact = _run(tools_module.affordability_check.coroutine(state=state))
        assert artifact is None
        assert text == at("affordability_need_more_info", "ru")

    def test_direct_payment_income_unknown_gives_general_rule(self):
        from app.agent import tools as tools_module

        state = {"lang": "ru", "dialog": {**_default_dialog()}}
        text, artifact = _run(tools_module.affordability_check.coroutine(monthly_payment=3_000_000, state=state))
        assert text == at("affordability_general_rule", "ru", payment="3 000 000")
        assert artifact["data"]["dti_ratio"] is None
        assert artifact["data"]["monthly_payment"] == 3_000_000.0

    def test_direct_payment_income_known_high_ratio_warns(self):
        from app.agent import tools as tools_module

        state = {
            "lang": "ru",
            "dialog": {**_default_dialog()},
            "user_profile": {"facts": {"income_monthly": 5_000_000}},
        }
        text, artifact = _run(tools_module.affordability_check.coroutine(monthly_payment=3_000_000, state=state))
        assert artifact["data"]["dti_ratio"] == 0.6
        assert at("affordability_verdict_high", "ru") in text

    def test_direct_payment_income_known_low_ratio_ok(self):
        from app.agent import tools as tools_module

        state = {
            "lang": "ru",
            "dialog": {**_default_dialog()},
            "user_profile": {"facts": {"income_monthly": 10_000_000}},
        }
        text, artifact = _run(tools_module.affordability_check.coroutine(monthly_payment=2_000_000, state=state))
        assert artifact["data"]["dti_ratio"] == 0.2
        assert at("affordability_verdict_ok", "ru") in text

    def test_derives_payment_from_amount_and_term(self):
        from app.agent import tools as tools_module

        product = {"rate_rules": [{"rate_min_pct": 20.0}]}
        state = {
            "lang": "ru",
            "dialog": {**_default_dialog(), "selected_product": product},
        }
        text, artifact = _run(
            tools_module.affordability_check.coroutine(loan_amount=40_000_000, term_months=36, state=state)
        )
        expected_payment = amortize(40_000_000, 20.0, 36).monthly_payment
        assert artifact["data"]["monthly_payment"] == round(expected_payment, 2)
        assert artifact["data"]["loan_amount"] == 40_000_000
        assert artifact["data"]["term_months"] == 36


# ---------------------------------------------------------------------------
# clarify anti-loop guard (needs a real graph run — ToolNode requires
# LangGraph runtime context, see tests/test_ui_blocks.py's
# _run_node_faq_via_graph for the same pattern/rationale).
# ---------------------------------------------------------------------------

class _ScriptedBoundLLM:
    """Unlike test_ui_blocks.py's _StubBoundLLM, this stays the SAME object
    across repeated `bind_tools(...)` calls (returned by _ScriptedLLM below)
    so its internal round counter survives node_faq's clarify-guard rebind
    (`llm.bind_tools([...without clarify...])` mid-turn) instead of being
    reset back to round 0."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    async def ainvoke(self, msgs):
        resp = self._responses[min(self.calls, len(self._responses) - 1)]
        self.calls += 1
        return resp


class _ScriptedLLM:
    def __init__(self, responses):
        self._bound = _ScriptedBoundLLM(responses)

    def bind_tools(self, tools, **kwargs):
        return self._bound


async def _run_node_faq_via_graph(state_in: dict) -> dict:
    from app.agent.graph import build_graph

    graph = build_graph()
    config = {"configurable": {"thread_id": f"test-{id(state_in)}"}}
    full_state = {
        "answer": "", "human_mode": False, "keyboard_options": None,
        "_route": "", "show_operator_button": False, "token_usage": None,
        "user_profile": None, "ui_blocks": None,
        **state_in,
    }
    return await graph.ainvoke(full_state, config=config)


def _clarify_tool_call(options):
    return AIMessage(
        content="",
        tool_calls=[{
            "name": "clarify",
            "args": {"question": "Какая у вас карта — Uzcard или Humo?", "options": options},
            "id": "1", "type": "tool_call",
        }],
    )


def _faq_lookup_tool_call(query="как заблокировать карту"):
    return AIMessage(
        content="",
        tool_calls=[{"name": "faq_lookup", "args": {"query": query}, "id": "2", "type": "tool_call"}],
    )


class TestClarifyAntiLoopGuard:
    @pytest.mark.asyncio
    async def test_first_clarify_call_is_displayed_and_marks_dialog(self, monkeypatch):
        from app.agent.nodes import faq as faq_module

        monkeypatch.setattr(
            faq_module, "_get_chat_openai",
            lambda role=None: _ScriptedLLM([_clarify_tool_call(["Uzcard", "Humo"])]),
        )
        monkeypatch.setattr(faq_module, "faq_precheck_answer", AsyncMock(return_value=None))

        state = {
            "last_user_text": "как заблокировать карту",
            "messages": [],
            "dialog": _default_dialog(),
            "lang": "ru",
            "session_id": "test-clarify-1",
            "user_id": 1,
        }
        result = await _run_node_faq_via_graph(state)

        assert result["answer"] == "Какая у вас карта — Uzcard или Humo?"
        assert result["keyboard_options"] == ["Uzcard", "Humo"]
        assert result["dialog"]["clarify_last_turn"] is True

    @pytest.mark.asyncio
    async def test_second_consecutive_clarify_is_blocked_and_falls_through(self, monkeypatch):
        from app.agent.nodes import faq as faq_module

        monkeypatch.setattr(
            faq_module, "_get_chat_openai",
            lambda role=None: _ScriptedLLM([
                _clarify_tool_call(["Uzcard", "Humo"]),
                _faq_lookup_tool_call(),
            ]),
        )
        monkeypatch.setattr(faq_module, "faq_precheck_answer", AsyncMock(return_value=None))
        monkeypatch.setattr(
            "app.agent.tools.faq_search",
            AsyncMock(return_value=FaqSearch(answer="Ответ из базы знаний", tier="strict")),
        )

        # Previous turn already ended in a clarify prompt, and the customer
        # replied with free text instead of tapping a button.
        state = {
            "last_user_text": "не знаю, помогите",
            "messages": [],
            "dialog": {**_default_dialog(), "clarify_last_turn": True},
            "lang": "ru",
            "session_id": "test-clarify-2",
            "user_id": 1,
        }
        result = await _run_node_faq_via_graph(state)

        assert result["answer"] == "Ответ из базы знаний"
        assert result["answer"] != "Какая у вас карта — Uzcard или Humo?"
        assert result["dialog"]["clarify_last_turn"] is False


# ---------------------------------------------------------------------------
# faq_lookup display-tool short-circuit → rephrase_faq_answer (2026-08-11)
#
# A confident (strict-tier) faq_lookup result is real, non-sentinel text, so
# it lands in the SAME short-circuit branch as product cards / office details
# (see the comment in nodes/faq.py) — the LLM never gets a wrapping round, so
# it must be rephrased right there instead of shipped as the raw DB row. A
# non-FAQ display tool must be completely unaffected: passed through as-is,
# rephrase_faq_answer never even called.
# ---------------------------------------------------------------------------

def _show_credit_menu_tool_call():
    return AIMessage(
        content="",
        tool_calls=[{"name": "show_credit_menu", "args": {}, "id": "3", "type": "tool_call"}],
    )


class TestFaqLookupShortCircuitRephrase:
    @pytest.mark.asyncio
    async def test_confident_faq_hit_is_rephrased(self, monkeypatch):
        from app.agent.nodes import faq as faq_module

        monkeypatch.setattr(
            faq_module, "_get_chat_openai",
            lambda role=None: _ScriptedLLM([_faq_lookup_tool_call(query="эскроу счет")]),
        )
        monkeypatch.setattr(faq_module, "faq_precheck_answer", AsyncMock(return_value=None))
        monkeypatch.setattr(
            "app.agent.tools.faq_search",
            AsyncMock(return_value=FaqSearch(answer="Эскроу-счёт — это ...", tier="strict")),
        )
        rephrase_mock = AsyncMock(return_value="Переформулированный естественный ответ")
        monkeypatch.setattr(faq_module, "rephrase_faq_answer", rephrase_mock)

        state = {
            "last_user_text": "давайте мне интересно эскроу счет",
            "messages": [],
            "dialog": _default_dialog(),
            "lang": "ru",
            "session_id": "test-faq-rephrase-1",
            "user_id": 1,
        }
        result = await _run_node_faq_via_graph(state)

        # _finalize_turn received the REPHRASED text, not the raw DB answer.
        assert result["answer"] == "Переформулированный естественный ответ"
        assert result["answer"] != "Эскроу-счёт — это ..."
        # rephrase_faq_answer was called with the verbatim DB text as source.
        rephrase_mock.assert_awaited_once()
        call_args = rephrase_mock.await_args
        assert call_args.args[0] == "Эскроу-счёт — это ..."

    @pytest.mark.asyncio
    async def test_non_faq_display_tool_passes_through_untouched(self, monkeypatch):
        """A display tool other than faq_lookup (product cards, office
        details, show_credit_menu, ...) must reach the user AS-IS — the
        rephrase helper must not even be invoked."""
        from app.agent.nodes import faq as faq_module

        monkeypatch.setattr(
            faq_module, "_get_chat_openai",
            lambda role=None: _ScriptedLLM([_show_credit_menu_tool_call()]),
        )
        monkeypatch.setattr(faq_module, "faq_precheck_answer", AsyncMock(return_value=None))
        rephrase_mock = AsyncMock(return_value="should never be used")
        monkeypatch.setattr(faq_module, "rephrase_faq_answer", rephrase_mock)

        state = {
            "last_user_text": "хочу кредит",
            "messages": [],
            "dialog": _default_dialog(),
            "lang": "ru",
            "session_id": "test-faq-rephrase-2",
            "user_id": 1,
        }
        result = await _run_node_faq_via_graph(state)

        assert result["answer"] == at("credit_menu_prompt", "ru")
        rephrase_mock.assert_not_awaited()


# ---------------------------------------------------------------------------
# Deterministic DTI warning — nodes/calc_flow.py
# ---------------------------------------------------------------------------

class TestCalcFlowDtiWarning:
    @staticmethod
    def _credit_state(amount, term_months, dp_pct, user_profile=None):
        product = {
            "name": "Test Mortgage",
            "rate_rules": [{"rate_min_pct": 18.0}],
            "rate_matrix": [{
                "rate_min_pct": 18.0, "rate_max_pct": 18.0,
                "term_min_months": 1, "term_max_months": 240,
                "downpayment_min_pct": 0, "downpayment_max_pct": 100,
            }],
        }
        state = {
            "last_user_text": "",
            "messages": [],
            "dialog": {
                **_default_dialog(),
                "flow": "calc_flow",
                "category": "mortgage",
                "selected_product": product,
                "calc_slots": {"amount": amount, "term_months": term_months, "downpayment": dp_pct},
                "calc_step": None,
            },
            "lang": "ru",
            "session_id": "test",
            "user_id": 1,
        }
        if user_profile is not None:
            state["user_profile"] = user_profile
        return state

    @pytest.mark.asyncio
    async def test_no_profile_no_warning_and_null_dti(self):
        from app.agent.nodes.calc_flow import node_calc_flow

        with patch("app.agent.nodes.calc_flow.generate_amortization_pdf", return_value="/tmp/x.pdf"):
            result = await node_calc_flow(self._credit_state(500_000_000, 120, 20.0))

        assert "⚠️" not in result["answer"]
        assert result["ui_blocks"][0]["data"]["dti_ratio"] is None

    @pytest.mark.asyncio
    async def test_high_payment_relative_to_income_warns(self):
        from app.agent.nodes.calc_flow import node_calc_flow
        from app.utils.amortization import amortize, dti_ratio

        amount, term_months, dp_pct = 500_000_000, 60, 0.0
        principal = amount - int(amount * dp_pct / 100)
        expected_monthly = amortize(principal, 18.0, term_months).monthly_payment
        income = 5_000_000  # payment will be far above 45% of this
        expected_ratio = dti_ratio(expected_monthly, income)
        assert expected_ratio > 0.45  # sanity: this scenario really is over threshold

        profile = {"facts": {"income_monthly": income}}
        with patch("app.agent.nodes.calc_flow.generate_amortization_pdf", return_value="/tmp/x.pdf"):
            result = await node_calc_flow(self._credit_state(amount, term_months, dp_pct, user_profile=profile))

        assert "⚠️" in result["answer"]
        assert result["ui_blocks"][0]["data"]["dti_ratio"] == round(expected_ratio, 4)

    @pytest.mark.asyncio
    async def test_comfortable_payment_relative_to_income_no_warning(self):
        from app.agent.nodes.calc_flow import node_calc_flow

        profile = {"facts": {"income_monthly": 50_000_000}}  # very high income, tiny loan
        with patch("app.agent.nodes.calc_flow.generate_amortization_pdf", return_value="/tmp/x.pdf"):
            result = await node_calc_flow(self._credit_state(10_000_000, 24, 50.0, user_profile=profile))

        assert "⚠️" not in result["answer"]
        assert result["ui_blocks"][0]["data"]["dti_ratio"] is not None
        assert result["ui_blocks"][0]["data"]["dti_ratio"] < 0.45

    @pytest.mark.asyncio
    async def test_dti_warning_survives_pdf_failure(self):
        """The DTI check runs off the same independent amortize() call as the
        calc_result block (Phase 3 precedent) — a PDF failure must not also
        drop the warning."""
        from app.agent.nodes.calc_flow import node_calc_flow

        profile = {"facts": {"income_monthly": 5_000_000}}
        with patch("app.agent.nodes.calc_flow.generate_amortization_pdf", side_effect=RuntimeError("boom")):
            result = await node_calc_flow(self._credit_state(500_000_000, 60, 0.0, user_profile=profile))

        assert "⚠️" in result["answer"]


# ---------------------------------------------------------------------------
# Qualify dead-end rescue — nodes/qualify_flow.py
# ---------------------------------------------------------------------------

class TestQualifyDeadEndRescue:
    @pytest.mark.asyncio
    async def test_autoloan_dead_end_rescues_with_microloan_products(self):
        from app.agent.nodes.qualify_flow import _resolve_destination

        microloan_products = [
            {"name": "Микрозайм Быстрый", "rate_min_pct": 24.0, "rate_rules": []},
            {"name": "Микрозайм Онлайн", "rate_min_pct": 22.0, "rate_rules": []},
        ]
        with patch("app.agent.products._get_products_by_category", new=AsyncMock(return_value=microloan_products)):
            answer, dialog, keyboard, ui_blocks = await _resolve_destination("autoloan", "dead_no_offers", {}, "ru")

        assert at("qualify_no_offers", "ru") in answer
        assert "Микрозайм" in answer
        assert dialog["flow"] == "show_products"
        assert dialog["category"] == "microloan"
        assert len(dialog["products"]) == 2
        # Lower rate ranks first.
        assert dialog["products"][0]["name"] == "Микрозайм Онлайн"
        assert keyboard == ["Микрозайм Онлайн", "Микрозайм Быстрый"]
        assert ui_blocks[0]["type"] == "product_list"
        assert ui_blocks[0]["data"]["kind"] == "recommend"
        assert ui_blocks[0]["data"]["category"] == "microloan"

    @pytest.mark.asyncio
    async def test_microloan_dead_end_has_no_rescue_target(self):
        """microloan is itself the rescue target for the other three trees —
        its own dead end is unchanged (no DB call, no rescue)."""
        from app.agent.nodes.qualify_flow import _resolve_destination

        answer, dialog, keyboard, ui_blocks = await _resolve_destination(
            "microloan", "dead_consider_others", {}, "ru",
        )
        assert answer == at("qualify_consider_others", "ru")
        assert keyboard is None
        assert ui_blocks is None

    @pytest.mark.asyncio
    async def test_rescue_falls_back_when_no_alternative_products(self):
        from app.agent.nodes.qualify_flow import _resolve_destination

        with patch("app.agent.products._get_products_by_category", new=AsyncMock(return_value=[])):
            answer, dialog, keyboard, ui_blocks = await _resolve_destination("mortgage", "dead_no_offers", {}, "ru")

        assert answer == at("qualify_no_offers", "ru")
        assert keyboard is None
        assert ui_blocks is None


# ---------------------------------------------------------------------------
# Operator handoff summary — app/agent/handoff.py
# ---------------------------------------------------------------------------

class _FakeHandoffLLM:
    def __init__(self, content):
        self._content = content

    async def ainvoke(self, msgs):
        return AIMessage(content=self._content)


class TestBuildHandoffSummary:
    @pytest.mark.asyncio
    async def test_returns_llm_text_on_success(self, monkeypatch):
        from app.agent import handoff as handoff_module

        monkeypatch.setattr(
            handoff_module, "_get_chat_openai",
            lambda role=None: _FakeHandoffLLM("Клиент интересовался ипотекой, сумма 50 млн."),
        )
        summary = await handoff_module.build_handoff_summary(
            dialog={"category": "mortgage", "calc_slots": {"amount": 50_000_000}},
            user_profile={"facts": {"income_monthly": 8_000_000}},
            recent_messages=[],
            reason="user_request",
        )
        assert summary == "Клиент интересовался ипотекой, сумма 50 млн."

    @pytest.mark.asyncio
    async def test_returns_none_when_llm_unavailable(self, monkeypatch):
        from app.agent import handoff as handoff_module

        monkeypatch.setattr(handoff_module, "_get_chat_openai", lambda role=None: None)
        summary = await handoff_module.build_handoff_summary(dialog={}, user_profile=None)
        assert summary is None

    @pytest.mark.asyncio
    async def test_returns_none_on_llm_exception(self, monkeypatch):
        from app.agent import handoff as handoff_module

        class _BoomLLM:
            async def ainvoke(self, msgs):
                raise RuntimeError("provider down")

        monkeypatch.setattr(handoff_module, "_get_chat_openai", lambda role=None: _BoomLLM())
        summary = await handoff_module.build_handoff_summary(dialog={}, user_profile=None)
        assert summary is None

    @pytest.mark.asyncio
    async def test_empty_llm_response_returns_none(self, monkeypatch):
        from app.agent import handoff as handoff_module

        monkeypatch.setattr(handoff_module, "_get_chat_openai", lambda role=None: _FakeHandoffLLM("   "))
        summary = await handoff_module.build_handoff_summary(dialog={}, user_profile=None)
        assert summary is None


class TestSendOperatorHandoffSummary:
    @pytest.mark.asyncio
    async def test_sends_and_persists_on_success(self, monkeypatch):
        from app.agent import handoff as handoff_module

        monkeypatch.setattr(
            handoff_module, "build_handoff_summary",
            AsyncMock(return_value="Клиент интересовался автокредитом."),
        )

        class _FakeAgentClient:
            async def get_handoff_context(self, session_id):
                return {"dialog": {"category": "autoloan"}, "user_profile": None, "messages": []}

        class _FakeChatService:
            def __init__(self):
                self.agent_client = _FakeAgentClient()
                self.saved = []

            async def save_system_note(self, session_id, text):
                self.saved.append((session_id, text))

        class _FakeMiddlewareClient:
            def __init__(self):
                self.sent = []

            async def send_message(self, session_id, text):
                self.sent.append((session_id, text))
                return True

        chat_service = _FakeChatService()
        middleware_client = _FakeMiddlewareClient()
        await handoff_module.send_operator_handoff_summary(chat_service, middleware_client, "session-1")

        assert len(middleware_client.sent) == 1
        assert middleware_client.sent[0][0] == "session-1"
        assert "Клиент интересовался автокредитом." in middleware_client.sent[0][1]
        assert "[Сводка для оператора]" in middleware_client.sent[0][1]
        assert len(chat_service.saved) == 1
        assert chat_service.saved[0][1] == middleware_client.sent[0][1]

    @pytest.mark.asyncio
    async def test_no_summary_means_nothing_sent_or_saved(self, monkeypatch):
        from app.agent import handoff as handoff_module

        monkeypatch.setattr(handoff_module, "build_handoff_summary", AsyncMock(return_value=None))

        class _FakeAgentClient:
            async def get_handoff_context(self, session_id):
                return {"dialog": {}, "user_profile": None, "messages": []}

        class _FakeChatService:
            def __init__(self):
                self.agent_client = _FakeAgentClient()
                self.saved = []

            async def save_system_note(self, session_id, text):
                self.saved.append((session_id, text))

        class _FakeMiddlewareClient:
            def __init__(self):
                self.sent = []

            async def send_message(self, session_id, text):
                self.sent.append((session_id, text))
                return True

        chat_service = _FakeChatService()
        middleware_client = _FakeMiddlewareClient()
        await handoff_module.send_operator_handoff_summary(chat_service, middleware_client, "session-2")

        assert middleware_client.sent == []
        assert chat_service.saved == []

    @pytest.mark.asyncio
    async def test_failure_never_raises(self, monkeypatch):
        from app.agent import handoff as handoff_module

        class _BoomAgentClient:
            async def get_handoff_context(self, session_id):
                raise RuntimeError("checkpoint read failed")

        class _FakeChatService:
            def __init__(self):
                self.agent_client = _BoomAgentClient()

        # Should not raise, even though get_handoff_context blows up.
        await handoff_module.send_operator_handoff_summary(_FakeChatService(), object(), "session-3")


# ---------------------------------------------------------------------------
# Agent.get_handoff_context
# ---------------------------------------------------------------------------

class TestAgentGetHandoffContext:
    @pytest.mark.asyncio
    async def test_returns_dialog_messages_and_profile(self, monkeypatch):
        from app.agent.agent import Agent

        agent = Agent()

        async def fake_aload_existing_state(config):
            return {"dialog": {"category": "mortgage"}, "messages": ["m1"], "user_id": 42}

        monkeypatch.setattr(agent, "_aload_existing_state", fake_aload_existing_state)
        monkeypatch.setattr(
            "app.agent.agent.load_user_profile",
            AsyncMock(return_value={"facts": {"income_monthly": 8_000_000}, "notes": ""}),
        )

        context = await agent.get_handoff_context("session-x")
        assert context["dialog"] == {"category": "mortgage"}
        assert context["messages"] == ["m1"]
        assert context["user_profile"]["facts"]["income_monthly"] == 8_000_000

    @pytest.mark.asyncio
    async def test_state_load_failure_returns_empty_dict(self, monkeypatch):
        from app.agent.agent import Agent

        agent = Agent()

        async def fake_aload_existing_state(config):
            raise RuntimeError("db down")

        monkeypatch.setattr(agent, "_aload_existing_state", fake_aload_existing_state)
        context = await agent.get_handoff_context("session-y")
        assert context == {}


# ---------------------------------------------------------------------------
# ui_blocks in the Mini App session archive endpoint
# ---------------------------------------------------------------------------

class TestSessionArchiveUiBlocks:
    @pytest.mark.asyncio
    async def test_session_detail_includes_ui_blocks(self):
        from datetime import datetime, timezone

        from app.db.models import ChatSession, SessionStatus, User
        from app.miniapp.routes.sessions import session_detail

        class _FakeMessage:
            def __init__(self, id, role, text, ui_blocks):
                self.id = id
                self.role = role
                self.text = text
                self.created_at = datetime.now(timezone.utc)
                self.ui_blocks = ui_blocks

        block = {"type": "product_list", "data": {"category": "mortgage", "products": []}}
        chat_session = ChatSession(
            id="s-1", user_id=1, status=SessionStatus.ENDED,
            started_at=datetime.now(timezone.utc), last_activity_at=datetime.now(timezone.utc),
            closed_reason="manual_end",
        )
        messages = [
            _FakeMessage(1, "user", "покажи ипотеки", None),
            _FakeMessage(2, "agent", "Вот список", [block]),
        ]

        class _FakeChatService:
            async def get_user_session(self, user_id, session_id):
                return chat_session

            async def get_active_session(self, user_id):
                return None

            async def get_recent_messages(self, session_id, limit=200, roles=()):
                return messages

        user = User(id=1, telegram_user_id=1)
        payload = await session_detail(
            "s-1", limit=200, user=user, chat_service=_FakeChatService(),
        )
        assert payload["messages"][0]["ui_blocks"] == []
        assert payload["messages"][1]["ui_blocks"] == [block]
