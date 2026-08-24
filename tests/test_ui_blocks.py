"""Phase 3 ("Mini App UX") — tests for structured ui_blocks and node_faq
token streaming.

Covers (per the Phase 3 spec's test-plan requirement):
  * artifact emission from the converted tools (content_and_artifact)
  * calc_result numbers matching app.utils.amortization.amortize
  * ui_blocks propagation: _finalize_turn -> BotState -> Agent -> AgentTurnResult
  * node_faq's tool-loop artifact accumulation, incl. the FLOW_QUALIFY
    interception NOT leaking a premature product_list
  * qualify_flow's product_list / comparison_table generation
  * _run_llm_round's streaming buffering (tool_call round -> dropped, text
    round -> forwarded)
"""
from __future__ import annotations

import asyncio

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk
from unittest.mock import AsyncMock, patch

from app.agent.i18n import at
from app.agent.state import _default_dialog
from app.agent.ui_blocks import (
    MAX_SCHEDULE_ROWS,
    credit_calc_result_block,
    deposit_calc_result_block,
    schedule_rows_public,
)
from app.utils.amortization import amortize


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# app.agent.ui_blocks — pure helpers (no I/O)
# ---------------------------------------------------------------------------

class TestScheduleRowsPublic:
    def test_no_truncation_under_cap(self):
        amort = amortize(10_000_000, 20.0, 12)
        rows, truncated = schedule_rows_public(amort.rows)
        assert len(rows) == 12
        assert truncated is False
        assert rows[0]["month"] == 1
        assert set(rows[0].keys()) == {"month", "payment", "principal_part", "interest_part", "balance"}

    def test_truncates_above_cap(self):
        amort = amortize(10_000_000, 20.0, MAX_SCHEDULE_ROWS + 50)
        rows, truncated = schedule_rows_public(amort.rows)
        assert len(rows) == MAX_SCHEDULE_ROWS
        assert truncated is True


class TestCreditCalcResultBlock:
    def test_matches_amortize_output(self):
        principal = 40_000_000
        amort = amortize(principal, 22.0, 36)
        block = credit_calc_result_block(
            amort, product_name="Ипотека Стандарт", amount=50_000_000, downpayment=10_000_000,
        )
        assert block["type"] == "calc_result"
        data = block["data"]
        assert data["kind"] == "credit"
        assert data["product_name"] == "Ипотека Стандарт"
        assert data["amount"] == 50_000_000
        assert data["downpayment"] == 10_000_000
        assert data["downpayment_pct"] == 20.0
        assert data["principal"] == principal
        assert data["rate_pct"] == 22.0
        assert data["term_months"] == 36
        assert data["monthly_payment"] == round(amort.monthly_payment, 2)
        assert data["total_payment"] == round(amort.total_payment, 2)
        assert data["overpayment"] == round(amort.overpayment, 2)
        assert len(data["schedule"]) == 36
        assert data["schedule_truncated"] is False
        # internal consistency: monthly * term ~= total (annuity payment is
        # constant per row in this implementation)
        assert abs(data["monthly_payment"] * 36 - data["total_payment"]) < 1.0

    def test_product_name_none_for_custom_calculator(self):
        amort = amortize(10_000_000, 18.0, 12)
        block = credit_calc_result_block(amort, product_name=None, amount=10_000_000, downpayment=0)
        assert block["data"]["product_name"] is None
        assert block["data"]["downpayment_pct"] == 0.0


class TestDepositCalcResultBlock:
    def test_shape_and_math(self):
        amount = 20_000_000
        rate_pct = 19.0
        term_months = 12
        interest_total = amount * rate_pct / 100 * term_months / 12
        block = deposit_calc_result_block(
            product_name="Депозит Стандарт", amount=amount, term_months=term_months,
            rate_pct=rate_pct, interest_total=interest_total, total=amount + interest_total,
        )
        assert block["type"] == "calc_result"
        data = block["data"]
        assert data["kind"] == "deposit"
        assert "schedule" not in data
        assert data["interest_total"] == round(interest_total, 2)
        assert data["total"] == round(amount + interest_total, 2)
        assert data["monthly_income"] == round(interest_total / term_months, 2)


# ---------------------------------------------------------------------------
# app.agent.products / app.agent.branches — public serializers
# ---------------------------------------------------------------------------

class TestProductPublicDict:
    def test_drops_internal_fields_keeps_display_fields(self):
        from app.agent.products import _product_public_dict

        product = {
            "name": "Ипотека Стандарт",
            "rate": "17.0%",
            "rate_rules": [{"rate_min_pct": 17.0}],
            "_recommend_reasons": ["best_rate", "age_fit"],
            "_recommend_score": 12.3,
        }
        out = _product_public_dict(product)
        assert "rate_rules" not in out
        assert "_recommend_reasons" not in out
        assert "_recommend_score" not in out
        assert out["reason"] == ["best_rate", "age_fit"]
        assert out["name"] == "Ипотека Стандарт"
        assert out["rate"] == "17.0%"

    def test_no_reason_key_when_not_ranked(self):
        from app.agent.products import _product_public_dict

        out = _product_public_dict({"name": "X", "rate_rules": []})
        assert "reason" not in out


class TestComparisonColumns:
    def test_credit_columns(self):
        from app.agent.products import _comparison_columns
        assert _comparison_columns("mortgage") == ["rate", "term", "amount", "downpayment"]

    def test_deposit_columns(self):
        from app.agent.products import _comparison_columns
        assert _comparison_columns("deposit") == ["rate", "term", "min_amount", "currency"]

    def test_card_columns(self):
        from app.agent.products import _comparison_columns
        assert _comparison_columns("debit_card") == ["network", "annual_fee", "cashback"]


class TestOfficePublicDict:
    def test_extracts_common_and_optional_fields(self):
        from app.agent.branches import office_public_dict

        class FakeSalesOffice:
            OFFICE_TYPE_CODE = "sales_office"
            id = 7
            name_ru = "Офис продаж Чиланзар"
            name_uz = None
            address_ru = "ул. Чиланзарская 1"
            address_uz = None
            region_ru = "Ташкент"
            region_uz = None
            latitude = 41.2
            longitude = 69.2
            phone = "+998712000000"
            hours = "09:00-18:00"

        out = office_public_dict(FakeSalesOffice())
        assert out["id"] == 7
        assert out["office_type"] == "sales_office"
        assert out["region_ru"] == "Ташкент"
        # fields that don't exist on this type default to None via getattr
        assert out["landmark_ru"] is None
        assert out["location_url"] is None


# ---------------------------------------------------------------------------
# tools.py — content_and_artifact tools not already covered in test_agent.py
# ---------------------------------------------------------------------------

class TestGetProductsArtifact:
    def test_products_found_returns_product_list_artifact(self):
        from app.agent import tools as tools_module

        products = [
            {"name": "Ипотека Стандарт", "rate": "17.0%", "rate_min_pct": 17.0, "rate_rules": [{"rate_min_pct": 17.0}]},
        ]
        with patch.object(tools_module, "_get_products_by_category", new=AsyncMock(return_value=products)):
            text, artifact = _run(tools_module.get_products.coroutine(category="mortgage", state={"lang": "ru"}))

        assert "Ипотека Стандарт" in text
        assert artifact["type"] == "product_list"
        assert artifact["data"]["category"] == "mortgage"
        assert "kind" not in artifact["data"]  # plain catalog browse, not recommend/qualify
        assert artifact["data"]["products"][0]["name"] == "Ипотека Стандарт"
        assert "rate_rules" not in artifact["data"]["products"][0]

    def test_no_products_returns_no_artifact(self):
        from app.agent import tools as tools_module

        with patch.object(tools_module, "_get_products_by_category", new=AsyncMock(return_value=[])):
            text, artifact = _run(tools_module.get_products.coroutine(category="mortgage", state={"lang": "ru"}))

        assert artifact is None
        assert text


class TestRecommendProductArtifact:
    def test_ranked_products_get_reason_and_recommend_kind(self):
        from app.agent import tools as tools_module

        products = [
            {"name": "Автокредит Базовый", "rate_min_pct": 20.0, "rate_rules": []},
            {"name": "Автокредит Плюс", "rate_min_pct": 18.0, "rate_rules": []},
        ]
        with patch.object(tools_module, "_get_products_by_category", new=AsyncMock(return_value=products)):
            text, artifact = _run(
                tools_module.recommend_product.coroutine(goal="куплю машину", state={"lang": "ru", "dialog": {}})
            )

        assert artifact is not None
        assert artifact["type"] == "product_list"
        assert artifact["data"]["kind"] == "recommend"
        assert artifact["data"]["category"] == "autoloan"
        top = artifact["data"]["products"][0]
        assert top["name"] == "Автокредит Плюс"  # lowest rate ranks first
        assert "best_rate" in top["reason"]
        assert text

    def test_no_goal_no_category_returns_no_artifact(self):
        from app.agent import tools as tools_module

        text, artifact = _run(
            tools_module.recommend_product.coroutine(goal="", state={"lang": "ru", "dialog": {}})
        )
        assert artifact is None
        assert text == at("recommend_no_goal", "ru")


class TestGetCurrencyInfoArtifact:
    def test_rates_returns_rate_table_artifact(self):
        from app.agent import tools as tools_module

        fake_rates = [
            {"code": "USD", "name_ru": "Доллар США", "name_en": "US Dollar", "name_uz": "AQSH dollari",
             "nominal": "1", "rate": "12 750.50", "diff": "5.20", "date": "06.08.2026", "icon": "🇺🇸"},
        ]
        with patch("app.utils.cbu_rates.fetch_cbu_rates", new=AsyncMock(return_value=fake_rates)):
            text, artifact = _run(tools_module.get_currency_info.coroutine(state={"lang": "ru"}))

        assert "USD" in text
        assert artifact["type"] == "rate_table"
        assert artifact["data"]["date"] == "06.08.2026"
        rate_row = artifact["data"]["rates"][0]
        assert rate_row["code"] == "USD"
        assert rate_row["rate"] == 12750.50
        assert rate_row["diff"] == 5.20
        assert rate_row["nominal"] == 1.0

    def test_no_rates_returns_no_artifact(self):
        from app.agent import tools as tools_module

        with patch("app.utils.cbu_rates.fetch_cbu_rates", new=AsyncMock(return_value=[])):
            text, artifact = _run(tools_module.get_currency_info.coroutine(state={"lang": "ru"}))
        assert artifact is None
        assert text


class TestSelectOfficeArtifact:
    """select_office fetches full ORM rows for the office(s) picked from the
    dialog's saved `offices` list — patch app.db.session.get_session (local
    import inside the tool, same pattern as tests/test_profile.py's
    _patch_get_session) rather than mocking the ORM query itself."""

    class _FakeResult:
        def __init__(self, obj):
            self._obj = obj

        def scalar_one_or_none(self):
            return self._obj

    class _FakeSession:
        def __init__(self, objs):
            self._objs = list(objs)
            self.calls = 0

        async def execute(self, stmt):
            obj = self._objs[self.calls] if self.calls < len(self._objs) else None
            self.calls += 1
            return TestSelectOfficeArtifact._FakeResult(obj)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class _FakeFilial:
        OFFICE_TYPE_CODE = "filial"
        id = 1
        name_ru = "ЦБУ Юнусабад"
        name_uz = None
        address_ru = "ул. Тестовая 5"
        address_uz = None
        landmark_ru = None
        landmark_uz = None
        location_url = None
        latitude = None
        longitude = None
        phone = None
        hours = None

    def _dialog(self):
        return {"offices": [{"name": "ЦБУ Юнусабад", "office_type": "filial", "id": 1}]}

    def test_single_pick_returns_office_detail(self, monkeypatch):
        from app.agent import tools as tools_module

        def fake_get_session():
            return self._FakeSession([self._FakeFilial()])

        monkeypatch.setattr("app.db.session.get_session", fake_get_session)
        text, artifact = _run(
            tools_module.select_office.coroutine(office_name="1", state={"lang": "ru", "dialog": self._dialog()})
        )
        assert "Юнусабад" in text
        assert artifact["type"] == "office_detail"
        assert artifact["data"]["office"]["name_ru"] == "ЦБУ Юнусабад"

    def test_all_returns_office_list(self, monkeypatch):
        from app.agent import tools as tools_module

        def fake_get_session():
            return self._FakeSession([self._FakeFilial()])

        monkeypatch.setattr("app.db.session.get_session", fake_get_session)
        text, artifact = _run(
            tools_module.select_office.coroutine(office_name="all", state={"lang": "ru", "dialog": self._dialog()})
        )
        assert artifact["type"] == "office_list"
        assert artifact["data"]["offices"][0]["name_ru"] == "ЦБУ Юнусабад"

    def test_no_offices_in_dialog_returns_no_artifact(self):
        from app.agent import tools as tools_module

        text, artifact = _run(
            tools_module.select_office.coroutine(office_name="1", state={"lang": "ru", "dialog": {}})
        )
        assert artifact is None


# ---------------------------------------------------------------------------
# helpers._finalize_turn -> BotState.ui_blocks
# ---------------------------------------------------------------------------

class TestFinalizeTurnUiBlocks:
    def test_ui_blocks_passed_through(self):
        from app.agent.nodes.helpers import _finalize_turn

        state = {"last_user_text": "hi", "messages": [], "dialog": _default_dialog()}
        block = {"type": "product_list", "data": {"category": "mortgage", "products": []}}
        result = _finalize_turn(state, "answer", _default_dialog(), ui_blocks=[block])
        assert result["ui_blocks"] == [block]

    def test_empty_ui_blocks_becomes_none(self):
        from app.agent.nodes.helpers import _finalize_turn

        state = {"last_user_text": "hi", "messages": [], "dialog": _default_dialog()}
        result = _finalize_turn(state, "answer", _default_dialog())
        assert result["ui_blocks"] is None
        result2 = _finalize_turn(state, "answer", _default_dialog(), ui_blocks=[])
        assert result2["ui_blocks"] is None


# ---------------------------------------------------------------------------
# Agent._ainvoke -> AgentTurnResult.ui_blocks
# ---------------------------------------------------------------------------

class TestAgentPropagatesUiBlocks:
    @pytest.mark.asyncio
    async def test_ui_blocks_reach_agent_turn_result(self, monkeypatch):
        from app.agent.agent import Agent

        monkeypatch.setenv("MEMORY_EXTRACT_ENABLED", "false")
        agent = Agent()
        expected_blocks = [{"type": "rate_table", "data": {"date": "", "rates": []}}]

        async def fake_graph_ainvoke(state_in, config=None):
            return {
                "answer": "курс валют",
                "keyboard_options": None,
                "show_operator_button": False,
                "token_usage": None,
                "lang": "ru",
                "ui_blocks": expected_blocks,
            }

        monkeypatch.setattr(agent._graph, "ainvoke", fake_graph_ainvoke)
        monkeypatch.setattr("app.agent.agent.load_user_profile", AsyncMock(return_value=None))

        result = await agent._ainvoke("s-ui-blocks", "курс доллара", language="ru", user_id=42)
        assert result.ui_blocks == expected_blocks

    @pytest.mark.asyncio
    async def test_no_ui_blocks_is_none_not_empty_list(self, monkeypatch):
        from app.agent.agent import Agent

        monkeypatch.setenv("MEMORY_EXTRACT_ENABLED", "false")
        agent = Agent()

        async def fake_graph_ainvoke(state_in, config=None):
            return {"answer": "ok", "lang": "ru"}

        monkeypatch.setattr(agent._graph, "ainvoke", fake_graph_ainvoke)
        monkeypatch.setattr("app.agent.agent.load_user_profile", AsyncMock(return_value=None))

        result = await agent._ainvoke("s-ui-blocks-2", "привет", language="ru", user_id=42)
        assert result.ui_blocks is None


# ---------------------------------------------------------------------------
# node_faq: artifact accumulation across the tool-call loop, and the
# FLOW_QUALIFY interception must NOT leak the unfiltered product_list.
# ---------------------------------------------------------------------------

class _StubBoundLLM:
    """Returns pre-scripted AIMessages, one per round.coroutine call."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    async def ainvoke(self, msgs):
        resp = self._responses[min(self.calls, len(self._responses) - 1)]
        self.calls += 1
        return resp


class _StubLLM:
    def __init__(self, responses):
        self._responses = responses

    def bind_tools(self, tools):
        # No `parallel_tool_calls` kwarg — faq.py's TypeError-fallback path
        # (see llm_factory_pattern memory note) is what makes this the right
        # test double shape.
        return _StubBoundLLM(self._responses)


async def _run_node_faq_via_graph(state_in: dict) -> dict:
    """Invoke node_faq through a real compiled graph rather than calling it
    directly. `ToolNode.ainvoke(...)` (used inside node_faq's tool-call loop)
    needs the LangGraph runtime context that only `CompiledStateGraph.ainvoke`
    establishes (contextvars set up per-run) — calling `node_faq(state)` as a
    bare coroutine bypasses that and raises "Missing required config key" the
    moment a tool call actually reaches the ToolNode. A fresh MemorySaver
    graph per call keeps this hermetic (no real checkpoint backend needed).
    """
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


class TestNodeFaqArtifactAccumulation:
    @pytest.mark.asyncio
    async def test_display_tool_short_circuit_carries_artifact(self, monkeypatch):
        """find_office is a display-tool short-circuit (round 1 tool result
        is surfaced directly) — its artifact must still reach state.ui_blocks
        even though no second LLM round ever runs."""
        from app.agent.nodes import faq as faq_module

        tool_call_msg = AIMessage(
            content="",
            tool_calls=[{"name": "find_office", "args": {"office_type": "filial", "query": ""}, "id": "1", "type": "tool_call"}],
        )
        monkeypatch.setattr(faq_module, "_get_chat_openai", lambda role=None: _StubLLM([tool_call_msg]))
        monkeypatch.setattr(faq_module, "faq_precheck_answer", AsyncMock(return_value=None))

        class FakeFilial:
            OFFICE_TYPE_CODE = "filial"
            id = 9
            name_ru = "ЦБУ Тест"
            name_uz = None
            address_ru = "ул. Тест 1"
            address_uz = None
            landmark_ru = None
            landmark_uz = None
            location_url = None
            latitude = None
            longitude = None
            phone = None
            hours = None

        with patch("app.agent.branches.search_offices", new=AsyncMock(return_value=[FakeFilial()])):
            state = {
                "last_user_text": "где ближайший филиал",
                "messages": [],
                "dialog": _default_dialog(),
                "lang": "ru",
                "session_id": "test-1",
                "user_id": 1,
            }
            result = await _run_node_faq_via_graph(state)

        assert result["ui_blocks"]
        assert result["ui_blocks"][0]["type"] == "office_list"
        assert result["ui_blocks"][0]["data"]["offices"][0]["name_ru"] == "ЦБУ Тест"

    @pytest.mark.asyncio
    async def test_qualify_interception_does_not_leak_unfiltered_product_list(self, monkeypatch):
        """get_products(category="mortgage") is intercepted by the
        FLOW_QUALIFY entry point (mortgage has a qualify tree) — the
        unfiltered product_list artifact that the raw get_products call
        would have produced must NOT reach the final turn; only the
        questionnaire's own (here: None, since it stops at a question) blocks
        may."""
        from app.agent.nodes import faq as faq_module
        from app.agent import tools as tools_module

        tool_call_msg = AIMessage(
            content="",
            tool_calls=[{"name": "get_products", "args": {"category": "mortgage"}, "id": "1", "type": "tool_call"}],
        )
        monkeypatch.setattr(faq_module, "_get_chat_openai", lambda role=None: _StubLLM([tool_call_msg]))
        monkeypatch.setattr(faq_module, "faq_precheck_answer", AsyncMock(return_value=None))

        products = [{"name": "Ипотека Стандарт", "rate_min_pct": 17.0, "rate_rules": []}]
        with patch.object(tools_module, "_get_products_by_category", new=AsyncMock(return_value=products)):
            state = {
                # Doesn't match any option of the mortgage tree's entry
                # (salary) question, so start_qualify stops there and returns
                # a plain question — ui_blocks=None (see qualify_flow.py).
                "last_user_text": "ипотека",
                "messages": [],
                "dialog": _default_dialog(),
                "lang": "ru",
                "session_id": "test-2",
                "user_id": 1,
            }
            result = await _run_node_faq_via_graph(state)

        assert result["answer"] == at("q_salary_mortgage", "ru")
        assert not result.get("ui_blocks")
        assert result["dialog"]["flow"] == "qualify"


# ---------------------------------------------------------------------------
# qualify_flow.py: product_list + comparison_table generation
# ---------------------------------------------------------------------------

class TestQualifyFlowUiBlocks:
    @pytest.mark.asyncio
    async def test_single_product_gets_only_product_list(self):
        from app.agent.nodes.qualify_flow import render_filter_result

        products = [{"name": "Авто Базовый", "rate_min_pct": 20.0, "rate_rules": []}]
        with patch(
            "app.agent.nodes.qualify_flow.filter_qualified_products",
            new=AsyncMock(return_value=products),
        ):
            answer, dialog, keyboard, ui_blocks = await render_filter_result("autoloan", {}, "ru")

        assert len(ui_blocks) == 1
        assert ui_blocks[0]["type"] == "product_list"
        assert ui_blocks[0]["data"]["kind"] == "qualify_result"

    @pytest.mark.asyncio
    async def test_multiple_products_add_comparison_table(self):
        from app.agent.nodes.qualify_flow import render_filter_result

        products = [
            {"name": "Авто Базовый", "rate_min_pct": 20.0, "rate_rules": []},
            {"name": "Авто Плюс", "rate_min_pct": 18.0, "rate_rules": []},
        ]
        with patch(
            "app.agent.nodes.qualify_flow.filter_qualified_products",
            new=AsyncMock(return_value=products),
        ):
            answer, dialog, keyboard, ui_blocks = await render_filter_result("autoloan", {}, "ru")

        types = [b["type"] for b in ui_blocks]
        assert types == ["product_list", "comparison_table"]
        comparison = ui_blocks[1]
        assert comparison["data"]["columns"] == ["rate", "term", "amount", "downpayment"]
        assert len(comparison["data"]["products"]) == 2

    @pytest.mark.asyncio
    async def test_empty_result_has_no_ui_blocks(self):
        from app.agent.nodes.qualify_flow import render_filter_result

        with patch(
            "app.agent.nodes.qualify_flow.filter_qualified_products",
            new=AsyncMock(return_value=[]),
        ):
            answer, dialog, keyboard, ui_blocks = await render_filter_result("autoloan", {}, "ru")

        assert ui_blocks is None


# ---------------------------------------------------------------------------
# calc_flow.py: deterministic calc_result blocks (credit + deposit)
# ---------------------------------------------------------------------------

class TestCalcFlowUiBlocks:
    @staticmethod
    def _credit_state(amount, term_months, dp_pct):
        from app.agent.state import BotState  # noqa: F401 — documents the shape below

        product = {
            "name": "Test Mortgage",
            # Single unconditional rule (no age/amount/term/income_type axis)
            # so select_rate matches it regardless of inputs — deterministic
            # 18.0% rate to compare the artifact against.
            "rate_rules": [{"rate_min_pct": 18.0}],
            "rate_matrix": [{
                "rate_min_pct": 18.0, "rate_max_pct": 18.0,
                "term_min_months": 1, "term_max_months": 240,
                "downpayment_min_pct": 0, "downpayment_max_pct": 100,
            }],
        }
        return {
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

    @pytest.mark.asyncio
    async def test_credit_result_matches_amortize(self):
        from app.agent.nodes.calc_flow import node_calc_flow

        amount, term_months, dp_pct = 500_000_000, 120, 20.0
        principal = amount - int(amount * dp_pct / 100)
        expected = amortize(principal, 18.0, term_months)

        with patch("app.agent.nodes.calc_flow.generate_amortization_pdf", return_value="/tmp/x.pdf"):
            result = await node_calc_flow(self._credit_state(amount, term_months, dp_pct))

        assert result["ui_blocks"]
        block = result["ui_blocks"][0]
        assert block["type"] == "calc_result"
        data = block["data"]
        assert data["kind"] == "credit"
        assert data["rate_pct"] == 18.0
        assert data["principal"] == principal
        assert data["monthly_payment"] == round(expected.monthly_payment, 2)
        assert data["total_payment"] == round(expected.total_payment, 2)
        assert len(data["schedule"]) == term_months

    @pytest.mark.asyncio
    async def test_credit_result_ui_block_present_even_on_pdf_failure(self):
        """The calc_result block is computed independently of the PDF
        generator — a PDF failure (falls back to credit_result_fallback text)
        must not also drop the ui_block."""
        from app.agent.nodes.calc_flow import node_calc_flow

        with patch("app.agent.nodes.calc_flow.generate_amortization_pdf", side_effect=RuntimeError("boom")):
            result = await node_calc_flow(self._credit_state(100_000_000, 24, 0.0))

        assert result["ui_blocks"]
        assert result["ui_blocks"][0]["data"]["kind"] == "credit"

    @pytest.mark.asyncio
    async def test_deposit_result_matches_simple_interest_math(self):
        from app.agent.nodes.calc_flow import node_calc_flow

        product = {"name": "Test Deposit", "rate_pct": 19.0, "rate_schedule": []}
        amount, term_months = 20_000_000, 12
        state = {
            "last_user_text": "",
            "messages": [],
            "dialog": {
                **_default_dialog(),
                "flow": "calc_flow",
                "category": "deposit",
                "selected_product": product,
                "calc_slots": {"amount": amount, "term_months": term_months},
                "calc_step": None,
            },
            "lang": "ru",
            "session_id": "test",
            "user_id": 1,
        }
        result = await node_calc_flow(state)

        assert result["ui_blocks"]
        data = result["ui_blocks"][0]["data"]
        assert data["kind"] == "deposit"
        expected_interest = amount * 19.0 / 100 * term_months / 12
        assert data["interest_total"] == round(expected_interest, 2)
        assert data["total"] == round(amount + expected_interest, 2)


# ---------------------------------------------------------------------------
# node_faq._run_llm_round — Mini App streaming buffering
# ---------------------------------------------------------------------------

class _FakeStreamingLLM:
    def __init__(self, chunks):
        self._chunks = chunks

    async def astream(self, msgs, **kwargs):
        for c in self._chunks:
            yield c


class TestRunLlmRoundStreaming:
    @pytest.mark.asyncio
    async def test_on_token_none_takes_plain_ainvoke_path(self):
        from app.agent.nodes.faq import _run_llm_round

        expected = AIMessage(content="plain answer")

        class _Plain:
            async def ainvoke(self, msgs):
                return expected

        result = await _run_llm_round(_Plain(), [], None)
        assert result is expected

    @pytest.mark.asyncio
    async def test_text_round_forwards_every_chunk(self):
        from app.agent.nodes.faq import _run_llm_round

        chunks = [AIMessageChunk(content="Здрав"), AIMessageChunk(content="ствуйте"), AIMessageChunk(content="!")]
        llm = _FakeStreamingLLM(chunks)
        forwarded: list[str] = []

        async def on_token(text: str) -> None:
            forwarded.append(text)

        result = await _run_llm_round(llm, [], on_token)
        assert forwarded == ["Здрав", "ствуйте", "!"]
        assert result.content == "Здравствуйте!"
        assert not (getattr(result, "tool_calls", None) or [])

    @pytest.mark.asyncio
    async def test_tool_call_round_forwards_nothing(self):
        from app.agent.nodes.faq import _run_llm_round

        chunks = [
            AIMessageChunk(
                content="",
                tool_call_chunks=[{"name": "get_products", "args": '{"category":', "id": "call_1", "index": 0}],
            ),
            AIMessageChunk(
                content="",
                tool_call_chunks=[{"name": None, "args": '"mortgage"}', "id": None, "index": 0}],
            ),
        ]
        llm = _FakeStreamingLLM(chunks)
        forwarded: list[str] = []

        async def on_token(text: str) -> None:
            forwarded.append(text)

        result = await _run_llm_round(llm, [], on_token)
        assert forwarded == []
        assert result.tool_calls
        assert result.tool_calls[0]["name"] == "get_products"

    @pytest.mark.asyncio
    async def test_on_token_exception_does_not_break_the_round(self):
        """A publish failure inside the callback (dead socket, etc.) must not
        crash the turn — see app/miniapp/routes/chat.py's own try/except
        around hub.publish, mirrored defensively here too."""
        from app.agent.nodes.faq import _run_llm_round

        chunks = [AIMessageChunk(content="ok")]
        llm = _FakeStreamingLLM(chunks)

        async def failing_on_token(text: str) -> None:
            raise RuntimeError("socket gone")

        result = await _run_llm_round(llm, [], failing_on_token)
        assert result.content == "ok"


# ---------------------------------------------------------------------------
# app.agent.streaming — contextvar plumbing
# ---------------------------------------------------------------------------

class TestStreamingContextvar:
    def test_default_is_none(self):
        from app.agent.streaming import get_on_token_callback
        assert get_on_token_callback() is None

    def test_set_and_reset(self):
        from app.agent.streaming import get_on_token_callback, reset_on_token_callback, set_on_token_callback

        async def cb(text: str) -> None:
            pass

        token = set_on_token_callback(cb)
        try:
            assert get_on_token_callback() is cb
        finally:
            reset_on_token_callback(token)
        assert get_on_token_callback() is None
