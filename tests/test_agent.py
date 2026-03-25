"""Unit tests for app/agent/ helper functions, tools, and nodes."""
import asyncio
import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.types import Command

from app.agent.state import BotState, _default_dialog
from app.agent.constants import (
    FALLBACK_STREAK_THRESHOLD,
    _CURRENT_DIALOG,
    _greeting_with_menu,
)
from app.agent.i18n import at
from app.agent.intent import (
    _detect_product_category,
    _is_back_trigger,
    _is_branch_question,
    _is_calc_trigger,
    _is_comparison_request,
    _is_currency_question,
    _is_greeting,
    _is_operator_request,
    _is_thanks,
    _is_yes,
)
from app.agent.parsers import _parse_amount, _parse_downpayment, _parse_term_months
from app.agent.products import _find_product_by_name, _fmt_rate
from app.agent.nodes.helpers import _finalize_turn
from app.agent.nodes.faq import _reattach_keyboard, _update_dialog_from_tools
from app.agent.nodes.router import node_router
from app.agent.nodes.calc_flow import node_calc_flow
from app.agent.tools import (
    greeting_response,
    thanks_response,
    get_branch_info,
    get_currency_info,
    show_credit_menu,
    back_to_product_list,
    start_calculator,
    select_product,
    request_operator,
)


# ---- helpers ---------------------------------------------------------------

def _make_state(user_text: str = "привет", messages=None) -> dict:
    return {
        "last_user_text": user_text,
        "messages": messages or [SystemMessage(content=at("system_policy", "ru"))],
        "dialog": _default_dialog(),
        "human_mode": False,
        "keyboard_options": None,
        "session_id": "test-session",
        "user_id": 1,
        "answer": "",
    }


def _run(coro):
    """Run async coroutine in sync test."""
    return asyncio.run(coro)


# ---- Intent helpers --------------------------------------------------------

class TestIsGreeting:
    def test_russian(self):
        assert _is_greeting("Привет!")
        assert _is_greeting("Здравствуйте")

    def test_english(self):
        assert _is_greeting("Hello there")
        assert _is_greeting("hi friend")

    def test_uzbek(self):
        assert _is_greeting("Ассалому алейкум")
        assert _is_greeting("Салом")

    def test_negative(self):
        assert not _is_greeting("Какой кредит")


class TestIsThanks:
    def test_positive(self):
        assert _is_thanks("Спасибо!")
        assert _is_thanks("Рахмат")
        assert _is_thanks("Thank you")

    def test_negative(self):
        assert not _is_thanks("Привет")


class TestIsBranchQuestion:
    def test_positive(self):
        assert _is_branch_question("Где ближайший филиал?")
        assert _is_branch_question("Адрес отделения")

    def test_negative(self):
        assert not _is_branch_question("Какой кредит?")


class TestIsCurrencyQuestion:
    def test_positive(self):
        assert _is_currency_question("Какой курс доллара?")
        assert _is_currency_question("Курс EUR")

    def test_negative(self):
        assert not _is_currency_question("Ипотека")


class TestIsCalcTrigger:
    def test_positive(self):
        assert _is_calc_trigger("Рассчитать платёж")
        assert _is_calc_trigger("✅ Рассчитать")

    def test_negative(self):
        assert not _is_calc_trigger("Привет")


class TestIsBackTrigger:
    def test_positive(self):
        assert _is_back_trigger("◀ Все продукты")
        assert _is_back_trigger("Назад")

    def test_negative(self):
        assert not _is_back_trigger("Дальше")


class TestIsYes:
    def test_positive(self):
        assert _is_yes("Да")
        assert _is_yes("Позвоните мне")
        assert _is_yes("ok")

    def test_negative(self):
        assert not _is_yes("Нет")


class TestIsComparisonRequest:
    def test_positive(self):
        assert _is_comparison_request("Сравни ипотечные программы")
        assert _is_comparison_request("В чем разница между вкладами?")

    def test_negative(self):
        assert not _is_comparison_request("Покажи ипотеку")


# ---- Product category detection --------------------------------------------

class TestDetectProductCategory:
    def test_mortgage(self):
        assert _detect_product_category("Хочу ипотеку") == "mortgage"
        assert _detect_product_category("Квартира в новостройке") == "mortgage"

    def test_autoloan(self):
        assert _detect_product_category("Автокредит на машину") == "autoloan"

    def test_microloan(self):
        assert _detect_product_category("Нужен микрозайм") == "microloan"

    def test_education(self):
        assert _detect_product_category("Образовательный кредит") == "education_credit"

    def test_deposit(self):
        assert _detect_product_category("Открыть вклад") == "deposit"

    def test_debit_card(self):
        assert _detect_product_category("Хочу карту Uzcard") == "debit_card"

    def test_credit_menu(self):
        assert _detect_product_category("Мне нужен кредит") == "credit_menu"

    def test_unknown(self):
        assert _detect_product_category("Какая погода?") is None


# ---- Number parsers --------------------------------------------------------

class TestParseAmount:
    def test_plain(self):
        assert _parse_amount("500000000") == 500_000_000

    def test_millions(self):
        assert _parse_amount("500 млн") == 500_000_000

    def test_billions(self):
        assert _parse_amount("1.5 млрд") == 1_500_000_000

    def test_thousands(self):
        assert _parse_amount("50 тыс") == 50_000

    def test_invalid(self):
        assert _parse_amount("нет") is None


class TestParseTermMonths:
    def test_months(self):
        assert _parse_term_months("12 мес") == 12

    def test_years(self):
        assert _parse_term_months("10 лет") == 120

    def test_plain_number(self):
        assert _parse_term_months("24") == 24

    def test_invalid(self):
        assert _parse_term_months("нет") is None


class TestParseDownpayment:
    def test_with_percent(self):
        assert _parse_downpayment("20%") == 20.0

    def test_without_percent(self):
        assert _parse_downpayment("15") == 15.0

    def test_invalid(self):
        assert _parse_downpayment("нет") is None


# ---- _finalize_turn --------------------------------------------------------

class TestFinalizeTurn:
    def test_returns_dict_not_full_state(self):
        state = _make_state()
        result = _finalize_turn(state, "Ответ", _default_dialog())
        assert isinstance(result, dict)
        assert "answer" in result
        assert "messages" in result
        assert "dialog" in result
        assert "session_id" not in result
        assert "user_id" not in result

    def test_new_messages_contain_human_and_ai(self):
        state = _make_state(user_text="вопрос")
        result = _finalize_turn(state, "ответ", _default_dialog())
        msgs = result["messages"]
        human_msgs = [m for m in msgs if isinstance(m, HumanMessage)]
        ai_msgs = [m for m in msgs if isinstance(m, AIMessage)]
        assert len(human_msgs) == 1
        assert human_msgs[0].content == "вопрос"
        assert len(ai_msgs) == 1
        assert ai_msgs[0].content == "ответ"

    def test_keyboard_options_passed(self):
        state = _make_state()
        buttons = ["A", "B"]
        result = _finalize_turn(state, "text", _default_dialog(), buttons)
        assert result["keyboard_options"] == ["A", "B"]

    def test_keyboard_options_none_by_default(self):
        state = _make_state()
        result = _finalize_turn(state, "text", _default_dialog())
        assert result["keyboard_options"] is None


# ---- _is_operator_request --------------------------------------------------

class TestIsOperatorRequest:
    def test_russian(self):
        assert _is_operator_request("хочу оператора")
        assert _is_operator_request("подключи оператора")
        assert _is_operator_request("живой оператор")

    def test_english(self):
        assert _is_operator_request("I want a live agent")
        assert _is_operator_request("connect me to an operator")

    def test_negative(self):
        assert not _is_operator_request("Какой кредит?")
        assert not _is_operator_request("Привет")
        assert not _is_operator_request("хочу ипотеку")


# ---- Fallback streak & operator button -------------------------------------

class TestFallbackStreakAndOperatorButton:
    def test_fallback_increments_streak(self):
        state = _make_state("gibberish")
        result = _finalize_turn(state, "some fallback", _default_dialog(), is_fallback=True)
        assert result["dialog"]["fallback_streak"] == 1
        assert result["show_operator_button"] is False

    def test_successful_answer_resets_streak(self):
        state = _make_state("привет")
        dialog = {**_default_dialog(), "fallback_streak": 2}
        result = _finalize_turn(state, "Добрый день!", dialog)
        assert result["dialog"]["fallback_streak"] == 0
        assert result["show_operator_button"] is False

    def test_three_fallbacks_shows_button(self):
        state = _make_state("test")
        dialog = {**_default_dialog(), "fallback_streak": 2}
        result = _finalize_turn(state, "some fallback", dialog, is_fallback=True)
        assert result["dialog"]["fallback_streak"] == 3
        assert result["show_operator_button"] is True

    def test_operator_request_text_shows_button(self):
        state = _make_state("хочу оператора")
        result = _finalize_turn(state, "some answer", _default_dialog())
        assert result["show_operator_button"] is True

    def test_operator_requested_flag_shows_button(self):
        dialog = {**_default_dialog(), "operator_requested": True}
        state = _make_state("connect me")
        result = _finalize_turn(state, "Connecting...", dialog)
        assert result["show_operator_button"] is True
        # flag cleared after use
        assert result["dialog"]["operator_requested"] is False

    def test_normal_answer_no_button(self):
        state = _make_state("какой кредит?")
        result = _finalize_turn(state, "У нас есть ипотека", _default_dialog())
        assert result["show_operator_button"] is False


# ---- _find_product_by_name -------------------------------------------------

class TestFindProductByName:
    def test_exact_match(self):
        products = [{"name": "Ипотека Стандарт"}, {"name": "Ипотека Льготная"}]
        assert _find_product_by_name("Ипотека Стандарт", products) == products[0]

    def test_contains_match(self):
        products = [{"name": "Ипотека Стандарт"}]
        assert _find_product_by_name("Стандарт", products) == products[0]

    def test_word_overlap_match(self):
        products = [{"name": "Ипотека Стандарт Плюс"}]
        assert _find_product_by_name("Стандарт кредит", products) == products[0]

    def test_no_match(self):
        products = [{"name": "Ипотека Стандарт"}]
        assert _find_product_by_name("Автокредит", products) is None


# ---- Greeting text ---------------------------------------------------------

class TestGreetingWithMenu:
    def test_russian(self):
        assert "Здравствуйте" in _greeting_with_menu("ru")

    def test_english(self):
        assert "Hello" in _greeting_with_menu("en")

    def test_uzbek(self):
        assert "Assalomu" in _greeting_with_menu("uz")


# ---- _fmt_rate -------------------------------------------------------------

class TestFmtRate:
    def test_range(self):
        result = _fmt_rate({"rate_min_pct": 10.0, "rate_max_pct": 15.0})
        assert "10.0" in result
        assert "15.0" in result

    def test_single_rate(self):
        assert _fmt_rate({"rate_min_pct": 10.0, "rate_max_pct": 10.0}) == "10.0%"

    def test_fallback(self):
        assert _fmt_rate({}) == "уточняется"

    def test_only_min(self):
        assert _fmt_rate({"rate_min_pct": 12.5}) == "12.5%"


# ---- LangGraph Tools (direct tests) ---------------------------------------

class TestToolGreetingResponse:
    def test_russian(self):
        result = _run(greeting_response.coroutine())
        assert "Здравствуйте" in result

    def test_english(self):
        from app.agent.constants import _REQUEST_LANGUAGE
        token = _REQUEST_LANGUAGE.set("en")
        try:
            result = _run(greeting_response.coroutine())
            assert "Hello" in result
        finally:
            _REQUEST_LANGUAGE.reset(token)


class TestToolThanksResponse:
    def test_returns_acknowledgment(self):
        result = _run(thanks_response.coroutine())
        assert "Пожалуйста" in result


class TestToolGetBranchInfo:
    def test_returns_branch_text(self):
        result = _run(get_branch_info.coroutine())
        assert "отделения" in result


class TestToolGetCurrencyInfo:
    def test_returns_currency_text(self):
        result = _run(get_currency_info.coroutine())
        assert "курс" in result.lower() or "USD" in result or "AsakaBank" in result


class TestToolShowCreditMenu:
    def test_returns_credit_types(self):
        result = _run(show_credit_menu.coroutine())
        assert "Ипотека" in result
        assert "Автокредит" in result


class TestToolBackToProductList:
    def test_with_products(self):
        dialog = {
            **_default_dialog(),
            "flow": "product_detail",
            "category": "mortgage",
            "products": [{"name": "Ипотека Стандарт", "rate": "14%"}],
        }
        token = _CURRENT_DIALOG.set(dialog)
        try:
            result = _run(back_to_product_list.coroutine())
            assert "Ипотека Стандарт" in result
        finally:
            _CURRENT_DIALOG.reset(token)

    def test_without_products(self):
        token = _CURRENT_DIALOG.set(_default_dialog())
        try:
            result = _run(back_to_product_list.coroutine())
            assert "категорию" in result.lower()
        finally:
            _CURRENT_DIALOG.reset(token)


class TestToolStartCalculator:
    def test_credit_returns_first_question(self):
        dialog = {**_default_dialog(), "category": "mortgage"}
        token = _CURRENT_DIALOG.set(dialog)
        try:
            result = _run(start_calculator.coroutine())
            assert "сумму" in result.lower()
        finally:
            _CURRENT_DIALOG.reset(token)

    def test_card_returns_instant_submit(self):
        dialog = {**_default_dialog(), "category": "debit_card"}
        token = _CURRENT_DIALOG.set(dialog)
        try:
            result = _run(start_calculator.coroutine())
            assert "заявка принята" in result.lower()
        finally:
            _CURRENT_DIALOG.reset(token)


class TestToolSelectProduct:
    def test_finds_product(self):
        dialog = {
            **_default_dialog(),
            "category": "mortgage",
            "products": [{"name": "Ипотека Стандарт", "rate": "14%", "amount": "500 млн"}],
        }
        token = _CURRENT_DIALOG.set(dialog)
        try:
            result = _run(select_product.coroutine("Ипотека Стандарт"))
            assert "Ипотека Стандарт" in result
        finally:
            _CURRENT_DIALOG.reset(token)

    def test_not_found(self):
        token = _CURRENT_DIALOG.set(_default_dialog())
        try:
            result = _run(select_product.coroutine("Несуществующий"))
            assert "не найден" in result.lower()
        finally:
            _CURRENT_DIALOG.reset(token)


# ---- _update_dialog_from_tools --------------------------------------------

class TestUpdateDialogFromTools:
    def test_greeting_resets_dialog(self):
        dialog = {**_default_dialog(), "flow": "show_products"}
        new_dialog, keyboard = _run(
            _update_dialog_from_tools(dialog, [{"name": "greeting_response", "args": {}}], "", "ru")
        )
        assert new_dialog["flow"] is None
        assert keyboard is not None
        assert len(keyboard) == 6

    def test_thanks_keeps_dialog(self):
        dialog = {**_default_dialog(), "flow": "show_products"}
        new_dialog, keyboard = _run(
            _update_dialog_from_tools(dialog, [{"name": "thanks_response", "args": {}}], "", "ru")
        )
        assert new_dialog["flow"] == "show_products"
        assert keyboard is None

    def test_credit_menu_buttons(self):
        new_dialog, keyboard = _run(
            _update_dialog_from_tools(_default_dialog(), [{"name": "show_credit_menu", "args": {}}], "", "ru")
        )
        assert keyboard is not None
        assert len(keyboard) == 4

    def test_start_calculator_sets_calc_flow(self):
        dialog = {**_default_dialog(), "category": "mortgage"}
        new_dialog, keyboard = _run(
            _update_dialog_from_tools(dialog, [{"name": "start_calculator", "args": {}}], "", "ru")
        )
        assert new_dialog["flow"] == "calc_flow"
        assert new_dialog["calc_step"] == "amount"

    def test_back_to_list_sets_show_products(self):
        dialog = {
            **_default_dialog(),
            "flow": "product_detail",
            "products": [{"name": "Test"}],
            "selected_product": {"name": "Test"},
        }
        new_dialog, keyboard = _run(
            _update_dialog_from_tools(dialog, [{"name": "back_to_product_list", "args": {}}], "", "ru")
        )
        assert new_dialog["flow"] == "show_products"
        assert new_dialog["selected_product"] is None
        assert keyboard == ["Test"]

    def test_select_product_sets_detail(self):
        dialog = {
            **_default_dialog(),
            "category": "mortgage",
            "products": [{"name": "Ипотека Стандарт"}],
        }
        new_dialog, keyboard = _run(
            _update_dialog_from_tools(
                dialog, [{"name": "select_product", "args": {"product_name": "Ипотека Стандарт"}}], "", "ru",
            )
        )
        assert new_dialog["flow"] == "product_detail"
        assert new_dialog["selected_product"]["name"] == "Ипотека Стандарт"
        assert "✅ Рассчитать платёж" in keyboard

    def test_no_tools_reattaches_keyboard(self):
        dialog = {
            **_default_dialog(),
            "flow": "show_products",
            "products": [{"name": "A"}, {"name": "B"}],
        }
        new_dialog, keyboard = _run(_update_dialog_from_tools(dialog, [], "", "ru"))
        assert keyboard == ["A", "B"]

    def test_faq_lookup_reattaches_keyboard(self):
        dialog = {
            **_default_dialog(),
            "flow": "product_detail",
            "category": "mortgage",
        }
        new_dialog, keyboard = _run(
            _update_dialog_from_tools(dialog, [{"name": "faq_lookup", "args": {"query": "test"}}], "", "ru")
        )
        assert "✅ Рассчитать платёж" in keyboard

    def test_request_operator_sets_flag(self):
        dialog = _default_dialog()
        new_dialog, keyboard = _run(
            _update_dialog_from_tools(dialog, [{"name": "request_operator", "args": {}}], "", "ru")
        )
        assert new_dialog.get("operator_requested") is True
        assert keyboard is None


# ---- _reattach_keyboard ---------------------------------------------------

class TestReattachKeyboard:
    def test_product_detail_credit(self):
        dialog = {**_default_dialog(), "flow": "product_detail", "category": "mortgage"}
        _, keyboard = _reattach_keyboard(dialog, "ru")
        assert "✅ Рассчитать платёж" in keyboard

    def test_product_detail_card(self):
        dialog = {**_default_dialog(), "flow": "product_detail", "category": "debit_card"}
        _, keyboard = _reattach_keyboard(dialog, "ru")
        assert "📋 Подать заявку" in keyboard

    def test_show_products(self):
        dialog = {**_default_dialog(), "flow": "show_products", "products": [{"name": "A"}]}
        _, keyboard = _reattach_keyboard(dialog, "ru")
        assert keyboard == ["A"]

    def test_no_flow(self):
        _, keyboard = _reattach_keyboard(_default_dialog(), "ru")
        assert keyboard is None


# ---- Router (3 routes) ----------------------------------------------------

class TestNodeRouter:
    def test_human_mode(self):
        state = _make_state("текст")
        state["human_mode"] = True
        result = _run(node_router(state))
        assert isinstance(result, Command)
        assert result.goto == "human_mode"

    def test_calc_flow_resume(self):
        state = _make_state("500 млн")
        state["dialog"] = {**_default_dialog(), "flow": "calc_flow", "calc_step": "amount"}
        result = _run(node_router(state))
        assert result.goto == "calc_flow"

    def test_lead_step_resume(self):
        state = _make_state("Да")
        state["dialog"] = {**_default_dialog(), "lead_step": "offer"}
        result = _run(node_router(state))
        assert result.goto == "calc_flow"

    def test_faq_for_everything_else(self):
        state = _make_state("Привет!")
        result = _run(node_router(state))
        assert result.goto == "faq"


# ---- node_calc_flow (lead_step sub-flow) -----------------------------------

class TestNodeCalcFlowLeadStep:
    def test_offer_decline_resets(self):
        state = _make_state("Нет, спасибо")
        state["dialog"] = {**_default_dialog(), "lead_step": "offer", "flow": "calc_flow"}
        result = _run(node_calc_flow(state))
        assert "понадобится помощь" in result["answer"]
        assert result["dialog"]["lead_step"] is None

    def test_offer_accept_asks_name(self):
        state = _make_state("Да, позвоните мне")
        state["dialog"] = {**_default_dialog(), "lead_step": "offer", "flow": "calc_flow"}
        result = _run(node_calc_flow(state))
        assert "зовут" in result["answer"].lower()
        assert result["dialog"]["lead_step"] == "name"

    def test_name_asks_phone(self):
        state = _make_state("Иван")
        state["dialog"] = {**_default_dialog(), "lead_step": "name", "flow": "calc_flow"}
        result = _run(node_calc_flow(state))
        assert "телефон" in result["answer"].lower()
        assert result["dialog"]["lead_step"] == "phone"
        assert result["dialog"]["lead_slots"]["name"] == "Иван"


# ---- Graph structure -------------------------------------------------------

class TestGraphStructure:
    def test_node_count(self):
        from app.agent import build_graph
        g = build_graph()
        nodes = set(g.get_graph().nodes) - {"__start__", "__end__"}
        assert len(nodes) == 4  # router + faq + calc_flow + human_mode
        assert "router" in nodes
        assert "faq" in nodes
        assert "calc_flow" in nodes
        assert "human_mode" in nodes


# ---- Rate lookup helpers ---------------------------------------------------

from app.agent.nodes.calc_flow import _lookup_credit_rate, _lookup_deposit_rate


class TestLookupCreditRate:
    def test_exact_match_term_and_downpayment(self):
        product = {"rate_matrix": [
            {"income_type": None, "rate_min_pct": 14.0, "rate_max_pct": 14.0,
             "term_min_months": 12, "term_max_months": 60,
             "downpayment_min_pct": 20, "downpayment_max_pct": 50},
            {"income_type": None, "rate_min_pct": 18.0, "rate_max_pct": 18.0,
             "term_min_months": 61, "term_max_months": 120,
             "downpayment_min_pct": 20, "downpayment_max_pct": 50},
        ]}
        assert _lookup_credit_rate(product, {"term_months": 36, "downpayment": 25}) == 14.0
        assert _lookup_credit_rate(product, {"term_months": 84, "downpayment": 25}) == 18.0

    def test_term_only_match(self):
        product = {"rate_matrix": [
            {"income_type": None, "rate_min_pct": 20.0, "rate_max_pct": 20.0,
             "term_min_months": 12, "term_max_months": 60,
             "downpayment_min_pct": None, "downpayment_max_pct": None},
        ]}
        assert _lookup_credit_rate(product, {"term_months": 24}) == 20.0

    def test_fallback_no_matrix(self):
        product = {"rate_min_pct": 22.0}
        assert _lookup_credit_rate(product, {"term_months": 12}) == 22.0

    def test_fallback_empty_matrix(self):
        product = {"rate_matrix": [], "rate_min_pct": 25.0}
        assert _lookup_credit_rate(product, {"term_months": 12}) == 25.0

    def test_fallback_default(self):
        product = {}
        assert _lookup_credit_rate(product, {"term_months": 12}) == 20.0

    def test_out_of_range_uses_min(self):
        product = {"rate_matrix": [
            {"income_type": None, "rate_min_pct": 14.0, "rate_max_pct": 14.0,
             "term_min_months": 12, "term_max_months": 60,
             "downpayment_min_pct": None, "downpayment_max_pct": None},
            {"income_type": None, "rate_min_pct": 18.0, "rate_max_pct": 18.0,
             "term_min_months": 61, "term_max_months": 120,
             "downpayment_min_pct": None, "downpayment_max_pct": None},
        ]}
        # term_months=200 is out of range for both entries → fallback to min
        assert _lookup_credit_rate(product, {"term_months": 200}) == 14.0


class TestLookupDepositRate:
    def test_exact_term_match_uzs(self):
        product = {"rate_schedule": [
            {"currency": "UZS", "term_months": 6, "rate_pct": 17.0},
            {"currency": "UZS", "term_months": 12, "rate_pct": 20.0},
            {"currency": "USD", "term_months": 12, "rate_pct": 3.0},
        ]}
        assert _lookup_deposit_rate(product, {"term_months": 12}) == 20.0

    def test_exact_term_match_any_currency(self):
        product = {"rate_schedule": [
            {"currency": "USD", "term_months": 6, "rate_pct": 2.5},
        ]}
        assert _lookup_deposit_rate(product, {"term_months": 6}) == 2.5

    def test_closest_term_match(self):
        product = {"rate_schedule": [
            {"currency": "UZS", "term_months": 6, "rate_pct": 17.0},
            {"currency": "UZS", "term_months": 12, "rate_pct": 20.0},
        ]}
        # 9 months is closer to 6 (diff=3) than 12 (diff=3), but equal → first wins
        assert _lookup_deposit_rate(product, {"term_months": 9}) == 17.0

    def test_fallback_no_schedule(self):
        product = {"rate_pct": 15.0}
        assert _lookup_deposit_rate(product, {"term_months": 12}) == 15.0

    def test_fallback_no_term(self):
        product = {"rate_schedule": [
            {"currency": "UZS", "term_months": 6, "rate_pct": 17.0},
        ], "rate_pct": 15.0}
        assert _lookup_deposit_rate(product, {}) == 15.0

    def test_fallback_default(self):
        product = {}
        assert _lookup_deposit_rate(product, {"term_months": 12}) == 15.0


class TestFormatProductCardRichData:
    """Test that _format_product_card shows rate_matrix and rate_schedule."""

    def test_credit_shows_rate_matrix(self):
        from app.agent.products import _format_product_card
        product = {
            "name": "Ипотека Тест",
            "rate": "14.0–22.0%",
            "amount": "до 500 млн",
            "term": "до 240 мес",
            "downpayment": "от 15%",
            "purpose": "", "collateral": "",
            "rate_matrix": [
                {"income_type": "payroll", "rate_min_pct": 14.0, "rate_max_pct": 14.0,
                 "rate_condition_text": "", "term_min_months": 12, "term_max_months": 120,
                 "downpayment_min_pct": None, "downpayment_max_pct": None},
                {"income_type": "official", "rate_min_pct": 22.0, "rate_max_pct": 22.0,
                 "rate_condition_text": "", "term_min_months": 12, "term_max_months": 120,
                 "downpayment_min_pct": None, "downpayment_max_pct": None},
            ],
        }
        result = _format_product_card(product, "mortgage")
        assert "Ипотека Тест" in result
        assert "Ставки по условиям" in result
        assert "зарплатный проект" in result
        assert "14.0%" in result
        assert "22.0%" in result

    def test_deposit_shows_rate_schedule(self):
        from app.agent.products import _format_product_card
        product = {
            "name": "Вклад Тест",
            "rate": "15.0–20.0%",
            "min_amount": "100 000",
            "currency": "UZS",
            "topup": "", "payout": "",
            "rate_schedule": [
                {"currency": "UZS", "term_months": 1, "term_text": "1 мес", "rate_pct": 15.0, "rate_text": ""},
                {"currency": "UZS", "term_months": 12, "term_text": "12 мес", "rate_pct": 20.0, "rate_text": ""},
            ],
        }
        result = _format_product_card(product, "deposit")
        assert "Вклад Тест" in result
        assert "Ставки по срокам" in result
        assert "1 мес" in result
        assert "12 мес" in result

    def test_card_shows_extended_fields(self):
        from app.agent.products import _format_product_card
        product = {
            "name": "Uzcard Test",
            "network": "uzcard", "currency": "UZS",
            "issue_fee": "50 000 сум", "annual_fee": "",
            "cashback": "", "validity": "5 лет",
            "delivery": True, "mobile_order": True, "pickup": True,
            "reissue_fee": "30 000 сум",
            "transfer_fee": "0.4%",
            "issuance_time": "15 мин",
            "payroll": None,
        }
        result = _format_product_card(product, "debit_card")
        assert "Uzcard Test" in result
        assert "Перевыпуск" in result
        assert "30 000 сум" in result
        assert "Переводы" in result
        assert "Время выпуска" in result
        assert "Доставка" in result
        assert "приложение" in result
        assert "Самовывоз" in result


# ---- i18n: at() function ---------------------------------------------------

class TestAtFunction:
    def test_ru_default(self):
        assert "консультант" in at("system_policy", "ru")

    def test_en(self):
        assert "consultant" in at("system_policy", "en")

    def test_uz(self):
        assert "maslahatchi" in at("system_policy", "uz")

    def test_none_lang_defaults_to_ru(self):
        assert at("system_policy", None) == at("system_policy", "ru")

    def test_unknown_lang_defaults_to_ru(self):
        assert at("system_policy", "xx") == at("system_policy", "ru")

    def test_kwargs_formatting(self):
        result = at("product_unavailable", "en", label="deposits")
        assert "deposits" in result

    def test_missing_key_returns_key(self):
        assert at("nonexistent_key_xyz", "ru") == "nonexistent_key_xyz"


class TestCategoryLabel:
    def test_ru(self):
        from app.agent.i18n import category_label
        assert "ипотечные" in category_label("mortgage", "ru")

    def test_en(self):
        from app.agent.i18n import category_label
        assert "mortgage" in category_label("mortgage", "en")

    def test_uz(self):
        from app.agent.i18n import category_label
        assert "ipoteka" in category_label("mortgage", "uz")


class TestGetMainMenuButtons:
    def test_ru_6_buttons(self):
        from app.agent.i18n import get_main_menu_buttons
        buttons = get_main_menu_buttons("ru")
        assert len(buttons) == 6
        assert "🏠 Ипотека" in buttons

    def test_en_6_buttons(self):
        from app.agent.i18n import get_main_menu_buttons
        buttons = get_main_menu_buttons("en")
        assert len(buttons) == 6
        assert "🏠 Mortgage" in buttons


class TestGetCalcQuestions:
    def test_mortgage_ru(self):
        from app.agent.i18n import get_calc_questions
        qs = get_calc_questions("mortgage", "ru")
        assert len(qs) == 3
        keys = [k for k, _ in qs]
        assert keys == ["amount", "term", "downpayment"]
        assert "сумму" in qs[0][1].lower()

    def test_mortgage_en(self):
        from app.agent.i18n import get_calc_questions
        qs = get_calc_questions("mortgage", "en")
        assert len(qs) == 3
        assert "loan amount" in qs[0][1].lower()

    def test_deposit_has_2_steps(self):
        from app.agent.i18n import get_calc_questions
        qs = get_calc_questions("deposit", "ru")
        assert len(qs) == 2

    def test_card_has_0_steps(self):
        from app.agent.i18n import get_calc_questions
        qs = get_calc_questions("debit_card", "ru")
        assert len(qs) == 0


class TestLocalizedName:
    def test_ru_default(self):
        from app.agent.i18n import _localized_name
        p = {"name": "Ипотека Стандарт", "name_en": "Standard Mortgage", "name_uz": "Standart Ipoteka"}
        assert _localized_name(p, "ru") == "Ипотека Стандарт"

    def test_en(self):
        from app.agent.i18n import _localized_name
        p = {"name": "Ипотека Стандарт", "name_en": "Standard Mortgage"}
        assert _localized_name(p, "en") == "Standard Mortgage"

    def test_en_fallback_to_name(self):
        from app.agent.i18n import _localized_name
        p = {"name": "Ипотека Стандарт"}
        assert _localized_name(p, "en") == "Ипотека Стандарт"


# ---- i18n: Tools respond in English/Uzbek ----------------------------------

class TestToolsI18n:
    def test_thanks_en(self):
        from app.agent.constants import _REQUEST_LANGUAGE
        token = _REQUEST_LANGUAGE.set("en")
        try:
            result = _run(thanks_response.coroutine())
            assert "welcome" in result.lower()
        finally:
            _REQUEST_LANGUAGE.reset(token)

    def test_branch_info_en(self):
        from app.agent.constants import _REQUEST_LANGUAGE
        token = _REQUEST_LANGUAGE.set("en")
        try:
            result = _run(get_branch_info.coroutine())
            assert "branch" in result.lower()
        finally:
            _REQUEST_LANGUAGE.reset(token)

    def test_credit_menu_en(self):
        from app.agent.constants import _REQUEST_LANGUAGE
        token = _REQUEST_LANGUAGE.set("en")
        try:
            result = _run(show_credit_menu.coroutine())
            assert "Mortgage" in result
        finally:
            _REQUEST_LANGUAGE.reset(token)

    def test_operator_uz(self):
        from app.agent.constants import _REQUEST_LANGUAGE
        token = _REQUEST_LANGUAGE.set("uz")
        try:
            result = _run(request_operator.coroutine())
            assert "Operator" in result or "operator" in result.lower()
        finally:
            _REQUEST_LANGUAGE.reset(token)


# ---- i18n: Intent detection multilingual -----------------------------------

class TestIntentMultilingual:
    def test_greeting_en(self):
        assert _is_greeting("Hi there")

    def test_branch_en(self):
        assert _is_branch_question("Where is the nearest branch?")

    def test_currency_en(self):
        assert _is_currency_question("What is the dollar exchange rate?")

    def test_calc_en(self):
        assert _is_calc_trigger("Calculate payment")

    def test_back_en(self):
        assert _is_back_trigger("All products")

    def test_yes_en(self):
        assert _is_yes("Sure, call me")

    def test_comparison_en(self):
        assert _is_comparison_request("Compare these products")

    def test_operator_uz(self):
        assert _is_operator_request("Jonli operatorga ulang")

    def test_deposit_en(self):
        assert _detect_product_category("I want to open a deposit") == "deposit"

    def test_mortgage_uz(self):
        assert _detect_product_category("Ipoteka olmoqchiman") == "mortgage"

    def test_autoloan_en(self):
        assert _detect_product_category("Auto loan for my car") == "autoloan"


# ---- Parsers: multilingual suffixes ----------------------------------------

class TestParserMultilingual:
    def test_amount_million_en(self):
        assert _parse_amount("500 million") == 500_000_000

    def test_amount_mln(self):
        assert _parse_amount("500 mln") == 500_000_000

    def test_amount_bln(self):
        assert _parse_amount("1.5 bln") == 1_500_000_000

    def test_term_years_en(self):
        assert _parse_term_months("5 years") == 60

    def test_term_yil_uz(self):
        assert _parse_term_months("5 yil") == 60

    def test_term_oy_uz(self):
        assert _parse_term_months("12 oy") == 12


# ---- i18n: AGENT_TEXTS completeness ----------------------------------------

class TestAgentTextsCompleteness:
    def test_all_keys_have_three_languages(self):
        from app.agent.i18n import AGENT_TEXTS
        missing = []
        for key, variants in AGENT_TEXTS.items():
            for lang in ("ru", "en", "uz"):
                if lang not in variants:
                    missing.append(f"{key}.{lang}")
        assert not missing, f"Missing translations: {missing}"

    def test_no_empty_values(self):
        from app.agent.i18n import AGENT_TEXTS
        empty = []
        for key, variants in AGENT_TEXTS.items():
            for lang, text in variants.items():
                if not text.strip():
                    empty.append(f"{key}.{lang}")
        assert not empty, f"Empty translations: {empty}"
