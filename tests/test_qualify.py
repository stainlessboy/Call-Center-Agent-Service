"""Unit tests for FLOW_QUALIFY: tree config, matching, prefill, filtering, node."""
import asyncio

from unittest.mock import AsyncMock, patch

from app.agent import qualify as q
from app.agent.constants import FLOW_QUALIFY, FLOW_SHOW_PRODUCTS
from app.agent.i18n import at, get_system_policy
from app.agent.nodes.qualify_flow import node_qualify_flow, start_qualify
from app.agent.nodes.router import node_router
from app.agent.state import _default_dialog
from langchain_core.messages import SystemMessage


def _run(coro):
    return asyncio.run(coro)


def _qstate(user_text, category, node_key, answers=None):
    dialog = {
        **_default_dialog(),
        "flow": FLOW_QUALIFY,
        "qualify_category": category,
        "qualify_node": node_key,
        "qualify_answers": answers or {},
    }
    return {
        "last_user_text": user_text,
        "messages": [SystemMessage(content=get_system_policy("ru"))],
        "dialog": dialog,
        "human_mode": False,
        "keyboard_options": None,
        "lang": "ru",
        "session_id": "t",
        "user_id": 1,
        "answer": "",
    }


# ---- match_answer ----------------------------------------------------------

class TestMatchAnswer:
    def test_button_label(self):
        node = q.get_node("autoloan", "salary")
        assert q.match_answer(node, "Да", "ru")["goto"] == "salary_card"
        assert q.match_answer(node, "Нет", "ru")["goto"] == "self_employed"

    def test_numeric_index(self):
        node = q.get_node("autoloan", "salary")
        assert q.match_answer(node, "2", "ru")["goto"] == "self_employed"

    def test_free_text_token(self):
        node = q.get_node("autoloan", "salary")
        assert q.match_answer(node, "официальная зарплата есть", "ru")["goto"] == "salary_card"

    def test_brand_tokens(self):
        node = q.get_node("autoloan", "auto_brand")
        assert q.match_answer(node, "хочу chevrolet", "ru")["set"]["tag"] == "for_brand_gm"
        assert q.match_answer(node, "kia", "ru")["set"]["tag"] == "for_brand_other"

    def test_deposit_currency_label(self):
        node = q.get_node("deposit", "currency")
        assert q.match_answer(node, "Доллар США", "ru")["set"]["deposit_currency"] == "USD"
        assert q.match_answer(node, "евро", "ru")["set"]["deposit_currency"] == "EUR"

    def test_no_match(self):
        node = q.get_node("autoloan", "salary")
        assert q.match_answer(node, "абракадабра", "ru") is None


# ---- prefill ---------------------------------------------------------------

class TestPrefill:
    def test_full_prefill_reaches_terminal(self):
        nk, ans = q.prefill("autoloan", "автокредит на GM, официальная зарплата на карту Асака", "ru")
        assert nk == "result"
        assert ans["income_types"] == ["payroll"]
        assert ans["tag"] == "for_brand_gm"

    def test_deposit_prefill(self):
        nk, ans = q.prefill("deposit", "хочу копить в долларах", "ru")
        assert nk == "result"
        assert ans == {"deposit_goal": "topup", "deposit_currency": "USD"}

    def test_bare_request_asks_first_question(self):
        nk, ans = q.prefill("mortgage", "ипотека", "ru")
        assert nk == "salary"
        assert ans == {}

    def test_partial_prefill(self):
        # salary answered (official), card unknown → stop at salary_card
        nk, ans = q.prefill("autoloan", "у меня официальная зарплата", "ru")
        assert nk == "salary_card"
        assert ans == {}


# ---- prefill_from_profile (Phase 2: "personal consultant" memory) ---------

class TestPrefillFromProfile:
    def test_income_type_payroll_skips_to_auto_brand(self):
        node_key, answers, applied = q.prefill_from_profile(
            "autoloan", {"income_type": "payroll"}, "ru"
        )
        assert node_key == "auto_brand"
        assert answers["income_types"] == ["payroll"]
        assert applied == ["income_type", "income_type"]

    def test_income_type_official_skips_via_salary_card(self):
        node_key, answers, applied = q.prefill_from_profile(
            "mortgage", {"income_type": "official"}, "ru"
        )
        assert node_key == "market"
        assert answers["income_types"] == ["official"]

    def test_income_type_no_official_routes_through_self_employed(self):
        node_key, answers, applied = q.prefill_from_profile(
            "autoloan", {"income_type": "no_official"}, "ru"
        )
        assert node_key == "auto_brand"
        assert answers["income_types"] == ["no_official"]

    def test_microloan_no_official_fully_resolves_to_terminal(self):
        # microloan's self_employed=yes branch goes straight to "result",
        # skipping the channel question entirely — same as a real user typing
        # the equivalent answers would.
        node_key, answers, applied = q.prefill_from_profile(
            "microloan", {"income_type": "no_official"}, "ru"
        )
        assert node_key == "result"
        assert answers["income_types"] == ["no_official"]

    def test_empty_facts_stops_at_entry(self):
        node_key, answers, applied = q.prefill_from_profile("autoloan", {}, "ru")
        assert node_key == "salary"
        assert answers == {}
        assert applied == []

    def test_unknown_income_type_value_stops_at_entry(self):
        node_key, answers, applied = q.prefill_from_profile(
            "autoloan", {"income_type": "something_else"}, "ru"
        )
        assert node_key == "salary"
        assert applied == []

    def test_unmarked_entry_node_blocks_deeper_currency_match(self):
        """deposit's entry ("goal") has no profile_key — a currency fact alone
        cannot skip anything because the walk never gets past goal, even
        though the "currency" node itself is marked (see qualify.py's
        comment on that node)."""
        node_key, answers, applied = q.prefill_from_profile(
            "deposit", {"currency": "USD"}, "ru"
        )
        assert node_key == "goal"
        assert applied == []

    def test_no_tree_for_category_returns_none(self):
        node_key, answers, applied = q.prefill_from_profile("nonexistent", {"income_type": "payroll"}, "ru")
        assert node_key is None
        assert answers == {}
        assert applied == []


# ---- start_qualify: profile prefill composed with text prefill ------------

class TestStartQualifyWithProfile:
    def test_profile_prefill_skips_questions_and_acknowledges(self):
        profile = {"facts": {"income_type": "payroll"}, "notes": ""}
        answer, dialog, keyboard, ui_blocks = _run(
            start_qualify("autoloan", "", "ru", profile)
        )
        assert at("q_auto_brand", "ru") in answer
        assert at("qualify_prefill_income_payroll", "ru") in answer
        assert dialog["qualify_node"] == "auto_brand"
        assert dialog["qualify_answers"]["income_types"] == ["payroll"]

    def test_profile_and_text_prefill_compose(self):
        """Profile answers the salary questions, the user's own opening
        message answers the brand question that's left — full prefill to a
        terminal, from two different sources in the same call."""
        profile = {"facts": {"income_type": "payroll"}, "notes": ""}
        with patch(
            "app.agent.nodes.qualify_flow.filter_qualified_products",
            new=AsyncMock(return_value=[{"name": "Авто GM"}]),
        ):
            answer, dialog, keyboard, ui_blocks = _run(
                start_qualify("autoloan", "хочу chevrolet", "ru", profile)
            )
        assert at("qualify_results_header", "ru") in answer
        assert at("qualify_prefill_income_payroll", "ru") in answer
        assert dialog["flow"] == FLOW_SHOW_PRODUCTS

    def test_no_profile_behaves_like_before(self):
        answer, dialog, keyboard, ui_blocks = _run(start_qualify("mortgage", "ипотека", "ru", None))
        assert answer == at("q_salary_mortgage", "ru")
        assert dialog["qualify_node"] == "salary"

    def test_empty_profile_facts_behaves_like_before(self):
        answer, dialog, keyboard, ui_blocks = _run(
            start_qualify("mortgage", "ипотека", "ru", {"facts": {}, "notes": ""})
        )
        assert answer == at("q_salary_mortgage", "ru")
        assert dialog["qualify_node"] == "salary"


# ---- node_qualify_flow: navigation ----------------------------------------

class TestNodeQualifyFlow:
    def test_answer_advances_to_next_question(self):
        state = _qstate("Да", "autoloan", "salary")
        result = _run(node_qualify_flow(state))
        assert result["answer"] == at("q_salary_card", "ru")
        assert result["dialog"]["qualify_node"] == "salary_card"
        assert result["dialog"]["flow"] == FLOW_QUALIFY

    def test_set_accumulates_answers(self):
        state = _qstate("Асакабанк", "autoloan", "salary_card")
        result = _run(node_qualify_flow(state))
        assert result["dialog"]["qualify_answers"]["income_types"] == ["payroll"]
        assert result["dialog"]["qualify_node"] == "auto_brand"

    def test_terminal_filter_shows_products(self):
        state = _qstate("GM", "autoloan", "auto_brand", {"income_types": ["payroll"]})
        products = [{"name": "Автокредит 2.6", "rate": "21%"}]
        with patch(
            "app.agent.nodes.qualify_flow.filter_qualified_products",
            new=AsyncMock(return_value=products),
        ):
            result = _run(node_qualify_flow(state))
        assert at("qualify_results_header", "ru") in result["answer"]
        assert "Автокредит 2.6" in result["answer"]
        assert result["dialog"]["flow"] == FLOW_SHOW_PRODUCTS
        assert result["dialog"]["products"] == products
        assert result["keyboard_options"] == ["Автокредит 2.6"]

    def test_terminal_filter_empty(self):
        state = _qstate("GM", "autoloan", "auto_brand", {"income_types": ["payroll"]})
        with patch(
            "app.agent.nodes.qualify_flow.filter_qualified_products",
            new=AsyncMock(return_value=[]),
        ):
            result = _run(node_qualify_flow(state))
        assert result["answer"] == at("qualify_results_empty", "ru")
        assert result["dialog"]["flow"] is None

    def test_dead_end_no_offers(self):
        """autoloan's dead end has a rescue category (microloan, Phase 4) —
        mock it to empty so this stays a deterministic "no rescue available"
        test; see tests/test_phase4_tools.py for the rescue-fires case."""
        state = _qstate("Нет", "autoloan", "self_employed")
        with patch("app.agent.products._get_products_by_category", new=AsyncMock(return_value=[])):
            result = _run(node_qualify_flow(state))
        assert result["answer"] == at("qualify_no_offers", "ru")
        assert result["dialog"]["flow"] is None

    def test_microloan_self_employed_no_consider_others(self):
        state = _qstate("Нет", "microloan", "self_employed")
        result = _run(node_qualify_flow(state))
        assert result["answer"] == at("qualify_consider_others", "ru")

    def test_mortgage_salary_asks_which_bank(self):
        """Mortgage rates differ by card issuer, so the branch must not stop at
        an ambiguous [payroll, official]."""
        state = _qstate("Да", "mortgage", "salary")
        result = _run(node_qualify_flow(state))
        assert result["answer"] == at("q_salary_card", "ru")
        assert result["dialog"]["qualify_node"] == "salary_card"
        assert "income_types" not in result["dialog"]["qualify_answers"]

    def test_mortgage_salary_card_yields_single_income_type(self):
        asaka = _run(node_qualify_flow(_qstate("Асакабанк", "mortgage", "salary_card")))
        assert asaka["dialog"]["qualify_answers"]["income_types"] == ["payroll"]
        assert asaka["dialog"]["qualify_node"] == "market"

        other = _run(node_qualify_flow(_qstate("Другого банка", "mortgage", "salary_card")))
        assert other["dialog"]["qualify_answers"]["income_types"] == ["official"]
        assert other["dialog"]["qualify_node"] == "market"

    def test_deposit_eur_note_appended(self):
        state = _qstate("Евро", "deposit", "currency", {"deposit_goal": "topup"})
        with patch(
            "app.agent.nodes.qualify_flow.filter_qualified_products",
            new=AsyncMock(return_value=[{"name": "On-line"}]),
        ):
            result = _run(node_qualify_flow(state))
        assert at("qualify_deposit_eur_note", "ru") in result["answer"]

    def test_side_question_reasks_current(self):
        state = _qstate("а какая процентная ставка?", "autoloan", "salary")
        with patch(
            "app.agent.nodes.qualify_flow._faq_lookup",
            new=AsyncMock(return_value="Ставка от 21%."),
        ):
            result = _run(node_qualify_flow(state))
        assert "Ставка от 21%." in result["answer"]
        assert at("q_salary_autoloan", "ru") in result["answer"]
        assert result["dialog"]["qualify_node"] == "salary"  # progress kept

    def test_unrecognized_reasks_current(self):
        state = _qstate("блаблабла", "autoloan", "salary")
        result = _run(node_qualify_flow(state))
        assert result["answer"] == at("q_salary_autoloan", "ru")
        assert result["dialog"]["qualify_node"] == "salary"


# ---- start_qualify (entry helper) -----------------------------------------

class TestStartQualify:
    def test_bare_request_asks_first(self):
        answer, dialog, keyboard, ui_blocks = _run(start_qualify("mortgage", "ипотека", "ru"))
        assert answer == at("q_salary_mortgage", "ru")
        assert dialog["flow"] == FLOW_QUALIFY
        assert dialog["qualify_node"] == "salary"
        assert keyboard == [at("btn_q_yes", "ru"), at("btn_q_no", "ru")]

    def test_full_prefill_jumps_to_results(self):
        with patch(
            "app.agent.nodes.qualify_flow.filter_qualified_products",
            new=AsyncMock(return_value=[{"name": "Авто X"}]),
        ):
            answer, dialog, keyboard, ui_blocks = _run(
                start_qualify("autoloan", "автокредит GM официальная зарплата Асака", "ru")
            )
        assert at("qualify_results_header", "ru") in answer
        assert dialog["flow"] == FLOW_SHOW_PRODUCTS


# ---- router ----------------------------------------------------------------

class TestRouterQualify:
    def test_flow_qualify_routes_to_node(self):
        state = _qstate("Да", "autoloan", "salary")
        result = _run(node_router(state))
        assert result.goto == "qualify_flow"


# ---- DB filter (mocked loaders) -------------------------------------------

class TestFilterCredits:
    def test_income_and_tag(self):
        rows = [
            {"section_name": "Автокредит", "service_name": "Авто 2.6", "income_type": "payroll", "for_brand_gm": True},
            {"section_name": "Автокредит", "service_name": "Авто 2.6", "income_type": "official", "for_brand_gm": True},
            {"section_name": "Автокредит", "service_name": "Онлайн авто", "income_type": "payroll", "for_brand_gm": None, "for_brand_other": True},
            {"section_name": "Ипотека", "service_name": "Ипотека X", "income_type": "payroll", "for_brand_gm": True},
        ]
        products = [{"name": "Авто 2.6"}, {"name": "Онлайн авто"}]
        with patch("app.utils.data_loaders._load_credit_product_offers", new=AsyncMock(return_value=rows)), \
             patch("app.agent.products._get_products_by_category", new=AsyncMock(return_value=products)):
            out = _run(q.filter_qualified_products("autoloan", {"income_types": ["payroll"], "tag": "for_brand_gm"}))
        assert [p["name"] for p in out] == ["Авто 2.6"]

    def test_null_income_matches_any(self):
        rows = [
            {"section_name": "Ипотека", "service_name": "Ипотека U", "income_type": None, "for_market_primary": True},
        ]
        products = [{"name": "Ипотека U"}]
        with patch("app.utils.data_loaders._load_credit_product_offers", new=AsyncMock(return_value=rows)), \
             patch("app.agent.products._get_products_by_category", new=AsyncMock(return_value=products)):
            out = _run(q.filter_qualified_products("mortgage", {"income_types": ["no_official"], "tag": "for_market_primary"}))
        assert [p["name"] for p in out] == ["Ипотека U"]


class TestFilterDeposits:
    def test_withdrawal_currency(self):
        rows = [
            {"service_name": "Макс выгода", "currency_code": "USD", "topup_allowed": True, "partial_withdrawal_allowed": False},
            {"service_name": "On-line", "currency_code": "USD", "topup_allowed": True, "partial_withdrawal_allowed": True},
            {"service_name": "On-line", "currency_code": "UZS", "topup_allowed": True, "partial_withdrawal_allowed": True},
        ]
        products = [{"name": "Макс выгода"}, {"name": "On-line"}]
        with patch("app.utils.data_loaders._load_deposit_product_offers", new=AsyncMock(return_value=rows)), \
             patch("app.agent.products._get_products_by_category", new=AsyncMock(return_value=products)):
            out = _run(q.filter_qualified_products("deposit", {"deposit_goal": "withdrawal", "deposit_currency": "USD"}))
        assert [p["name"] for p in out] == ["On-line"]

    def test_topup_currency(self):
        rows = [
            {"service_name": "Макс выгода", "currency_code": "USD", "topup_allowed": True, "partial_withdrawal_allowed": False},
            {"service_name": "On-line", "currency_code": "USD", "topup_allowed": True, "partial_withdrawal_allowed": True},
        ]
        products = [{"name": "Макс выгода"}, {"name": "On-line"}]
        with patch("app.utils.data_loaders._load_deposit_product_offers", new=AsyncMock(return_value=rows)), \
             patch("app.agent.products._get_products_by_category", new=AsyncMock(return_value=products)):
            out = _run(q.filter_qualified_products("deposit", {"deposit_goal": "topup", "deposit_currency": "USD"}))
        assert {p["name"] for p in out} == {"Макс выгода", "On-line"}
