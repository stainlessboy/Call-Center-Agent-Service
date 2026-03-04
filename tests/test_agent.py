"""Unit tests for app/services/agent.py helper functions, tools, and nodes."""
import asyncio
import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.types import Command

from app.services.agent import (
    BotState,
    FALLBACK_STREAK_THRESHOLD,
    SYSTEM_POLICY,
    _CURRENT_DIALOG,
    _default_dialog,
    _detect_product_category,
    _find_product_by_name,
    _finalize_turn,
    _fmt_rate,
    _greeting_with_menu,
    _is_back_trigger,
    _is_branch_question,
    _is_calc_trigger,
    _is_comparison_request,
    _is_currency_question,
    _is_greeting,
    _is_operator_request,
    _is_thanks,
    _is_yes,
    _parse_amount,
    _parse_downpayment,
    _parse_term_months,
    _reattach_keyboard,
    _update_dialog_from_tools,
    greeting_response,
    thanks_response,
    get_branch_info,
    get_currency_info,
    show_credit_menu,
    back_to_product_list,
    start_calculator,
    select_product,
    request_operator,
    node_router,
    node_calc_flow,
)
from app.tools.faq_tools import FAQ_FALLBACK_REPLY


# ---- helpers ---------------------------------------------------------------

def _make_state(user_text: str = "привет", messages=None) -> dict:
    return {
        "last_user_text": user_text,
        "messages": messages or [SystemMessage(content=SYSTEM_POLICY)],
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
        result = _finalize_turn(state, FAQ_FALLBACK_REPLY, _default_dialog())
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
        result = _finalize_turn(state, FAQ_FALLBACK_REPLY, dialog)
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
        from app.services.agent import _REQUEST_LANGUAGE
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
        assert "курсы валют" in result.lower() or "AsakaBank" in result


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
            _update_dialog_from_tools(dialog, [{"name": "greeting_response", "args": {}}], "")
        )
        assert new_dialog["flow"] is None
        assert keyboard is not None
        assert len(keyboard) == 6

    def test_thanks_keeps_dialog(self):
        dialog = {**_default_dialog(), "flow": "show_products"}
        new_dialog, keyboard = _run(
            _update_dialog_from_tools(dialog, [{"name": "thanks_response", "args": {}}], "")
        )
        assert new_dialog["flow"] == "show_products"
        assert keyboard is None

    def test_credit_menu_buttons(self):
        new_dialog, keyboard = _run(
            _update_dialog_from_tools(_default_dialog(), [{"name": "show_credit_menu", "args": {}}], "")
        )
        assert keyboard is not None
        assert len(keyboard) == 4

    def test_start_calculator_sets_calc_flow(self):
        dialog = {**_default_dialog(), "category": "mortgage"}
        new_dialog, keyboard = _run(
            _update_dialog_from_tools(dialog, [{"name": "start_calculator", "args": {}}], "")
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
            _update_dialog_from_tools(dialog, [{"name": "back_to_product_list", "args": {}}], "")
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
                dialog, [{"name": "select_product", "args": {"product_name": "Ипотека Стандарт"}}], "",
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
        new_dialog, keyboard = _run(_update_dialog_from_tools(dialog, [], ""))
        assert keyboard == ["A", "B"]

    def test_faq_lookup_reattaches_keyboard(self):
        dialog = {
            **_default_dialog(),
            "flow": "product_detail",
            "category": "mortgage",
        }
        new_dialog, keyboard = _run(
            _update_dialog_from_tools(dialog, [{"name": "faq_lookup", "args": {"query": "test"}}], "")
        )
        assert "✅ Рассчитать платёж" in keyboard

    def test_request_operator_sets_flag(self):
        dialog = _default_dialog()
        new_dialog, keyboard = _run(
            _update_dialog_from_tools(dialog, [{"name": "request_operator", "args": {}}], "")
        )
        assert new_dialog.get("operator_requested") is True
        assert keyboard is None


# ---- _reattach_keyboard ---------------------------------------------------

class TestReattachKeyboard:
    def test_product_detail_credit(self):
        dialog = {**_default_dialog(), "flow": "product_detail", "category": "mortgage"}
        _, keyboard = _reattach_keyboard(dialog)
        assert "✅ Рассчитать платёж" in keyboard

    def test_product_detail_card(self):
        dialog = {**_default_dialog(), "flow": "product_detail", "category": "debit_card"}
        _, keyboard = _reattach_keyboard(dialog)
        assert "📋 Подать заявку" in keyboard

    def test_show_products(self):
        dialog = {**_default_dialog(), "flow": "show_products", "products": [{"name": "A"}]}
        _, keyboard = _reattach_keyboard(dialog)
        assert keyboard == ["A"]

    def test_no_flow(self):
        _, keyboard = _reattach_keyboard(_default_dialog())
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
        from app.services.agent import build_graph
        g = build_graph()
        nodes = set(g.get_graph().nodes) - {"__start__", "__end__"}
        assert len(nodes) == 4  # router + faq + calc_flow + human_mode
        assert "router" in nodes
        assert "faq" in nodes
        assert "calc_flow" in nodes
        assert "human_mode" in nodes
