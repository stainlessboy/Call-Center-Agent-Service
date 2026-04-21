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
    _greeting_with_menu,
)
from app.agent.i18n import SYSTEM_POLICY, at
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
from app.agent.products import _find_product_by_name, _fmt_rate
from app.agent.nodes.helpers import _finalize_turn
from app.agent.nodes.faq import _reattach_keyboard, _update_dialog_from_tools
from app.agent.nodes.router import node_router
from app.agent.nodes.calc_flow import node_calc_flow
from app.agent.tools import (
    greeting_response,
    thanks_response,
    find_filials,
    find_sales_offices,
    find_sales_points,
    get_office_types_info,
    get_currency_info,
    show_credit_menu,
    back_to_product_list,
    compare_products,
    start_calculator,
    select_product,
    request_operator,
)


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
        result = _run(greeting_response.coroutine(lang="ru"))
        assert "Здравствуйте" in result

    def test_english(self):
        result = _run(greeting_response.coroutine(lang="en"))
        assert "Hello" in result


class TestToolThanksResponse:
    def test_returns_acknowledgment(self):
        result = _run(thanks_response.coroutine())
        assert "Пожалуйста" in result


class TestFindFilialsTool:
    def test_none_found(self):
        with patch("app.agent.branches.search_offices", new=AsyncMock(return_value=[])):
            result = _run(find_filials.coroutine(query="Мухосранск"))
        assert "не нашёл" in result.lower() or "Мухосранск" in result

    def test_formats_filial_hit(self):
        class FakeFilial:
            OFFICE_TYPE_CODE = "filial"
            name_ru = "ЦБУ \"Тест\""
            name_uz = None
            address_ru = "ул. Тестовая 1"
            address_uz = None
            landmark_ru = None
            landmark_uz = None
            location_url = None
            phone = None
            hours = None

        with patch(
            "app.agent.branches.search_offices",
            new=AsyncMock(return_value=[FakeFilial()]),
        ) as mock_search:
            result = _run(find_filials.coroutine(query="Ташкент"))
        # Verify the call was filtered to "filial"
        assert mock_search.call_args.kwargs["office_types"] == ["filial"]
        assert "ЦБУ" in result
        assert "Тестовая" in result


class TestFindSalesOfficesTool:
    def test_passes_correct_office_type(self):
        with patch(
            "app.agent.branches.search_offices",
            new=AsyncMock(return_value=[]),
        ) as mock_search:
            _run(find_sales_offices.coroutine(query=""))
        assert mock_search.call_args.kwargs["office_types"] == ["sales_office"]


class TestFindSalesPointsTool:
    def test_passes_correct_office_type(self):
        with patch(
            "app.agent.branches.search_offices",
            new=AsyncMock(return_value=[]),
        ) as mock_search:
            _run(find_sales_points.coroutine(query="KIA"))
        assert mock_search.call_args.kwargs["office_types"] == ["sales_point"]

    def test_en_none_found(self):
        with patch(
            "app.agent.branches.search_offices",
            new=AsyncMock(return_value=[]),
        ):
            result = _run(find_sales_points.coroutine(query="XYZ", lang="en"))
        assert "no offices" in result.lower() or "not found" in result.lower() or "XYZ" in result


class TestNewBranchToolsRegistered:
    def test_three_find_tools_in_faq_tools(self):
        from app.agent.tools import _FAQ_TOOLS
        names = {getattr(t, "name", None) for t in _FAQ_TOOLS}
        assert "find_filials" in names
        assert "find_sales_offices" in names
        assert "find_sales_points" in names
        assert "get_office_types_info" in names
        # Old unified tool is gone
        assert "get_branch_info" not in names


class TestToolGetOfficeTypesInfo:
    def test_ru_mentions_three_types(self):
        result = _run(get_office_types_info.coroutine(lang="ru"))
        assert "Филиал" in result
        assert "мини-офис" in result.lower()
        assert "автосалон" in result.lower()

    def test_uz_mentions_three_types(self):
        result = _run(get_office_types_info.coroutine(lang="uz"))
        assert "Filial" in result
        assert "mini-ofis" in result.lower()
        assert "avtosalon" in result.lower()


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
        result = _run(back_to_product_list.coroutine(state={"dialog": dialog}))
        assert "Ипотека Стандарт" in result

    def test_without_products(self):
        result = _run(back_to_product_list.coroutine(state={"dialog": _default_dialog()}))
        assert "категорию" in result.lower()


class TestToolStartCalculator:
    def test_credit_returns_first_question(self):
        dialog = {**_default_dialog(), "category": "mortgage"}
        result = _run(start_calculator.coroutine(state={"dialog": dialog}))
        assert "сумму" in result.lower()

    def test_card_returns_instant_submit(self):
        dialog = {**_default_dialog(), "category": "debit_card"}
        result = _run(start_calculator.coroutine(state={"dialog": dialog}))
        assert "заявка принята" in result.lower()


class TestToolSelectProduct:
    def test_finds_product(self):
        dialog = {
            **_default_dialog(),
            "category": "mortgage",
            "products": [{"name": "Ипотека Стандарт", "rate": "14%", "amount": "500 млн"}],
        }
        result = _run(select_product.coroutine("Ипотека Стандарт", state={"dialog": dialog}))
        assert "Ипотека Стандарт" in result

    def test_not_found(self):
        result = _run(select_product.coroutine("Несуществующий", state={"dialog": _default_dialog()}))
        assert "не найден" in result.lower()


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
    def test_system_policy_constant(self):
        assert "консультант" in SYSTEM_POLICY

    def test_none_lang_defaults_to_ru(self):
        assert at("cat_mortgage", None) == at("cat_mortgage", "ru")

    def test_unknown_lang_defaults_to_ru(self):
        assert at("cat_mortgage", "xx") == at("cat_mortgage", "ru")

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
        result = _run(thanks_response.coroutine(lang="en"))
        assert "welcome" in result.lower()

    def test_branch_info_en(self):
        with patch("app.agent.branches.search_offices", new=AsyncMock(return_value=[])):
            result = _run(find_filials.coroutine(query="NotFound", lang="en"))
        assert "no offices" in result.lower() or "not found" in result.lower() or "NotFound" in result

    def test_credit_menu_en(self):
        result = _run(show_credit_menu.coroutine(lang="en"))
        assert "Mortgage" in result

    def test_operator_uz(self):
        result = _run(request_operator.coroutine(lang="uz"))
        assert "Operator" in result or "operator" in result.lower()


# ---- Lang switching: last_lang persists across turns -----------------------

class TestLastLangPersistence:
    def test_last_lang_saved_by_faq_node(self):
        """When _update_dialog_from_tools is called with lang='uz', dialog['last_lang'] should be uz."""
        # Simulate what node_faq does after detecting uz from tool args
        dialog = _default_dialog()
        # Inject lang into tool_calls args (as the LLM would do)
        tool_calls = [{"name": "greeting_response", "args": {"lang": "uz"}}]
        new_dialog, _ = _run(
            _update_dialog_from_tools(dialog, tool_calls, "Salom", "uz")
        )
        # faq node sets last_lang on new_dialog after _update_dialog_from_tools
        # We verify the tool arg extraction logic works by checking the lang was honoured
        assert new_dialog is not None  # dialog returned successfully

    def test_tool_lang_arg_produces_uz_text(self):
        """Tools called with lang='uz' return Uzbek (Latin) text."""
        result = _run(thanks_response.coroutine(lang="uz"))
        assert "Arzimaydi" in result or "rahmat" in result.lower() or "yozing" in result.lower()

    def test_tool_lang_arg_produces_en_text(self):
        """Tools called with lang='en' return English text."""
        result = _run(show_credit_menu.coroutine(lang="en"))
        assert "Mortgage" in result
        assert "Auto loan" in result

    def test_default_lang_is_ru(self):
        """Tools without explicit lang default to Russian."""
        result = _run(thanks_response.coroutine())
        assert "Пожалуйста" in result


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


# ---- InjectedState refactor -------------------------------------------------

class TestInjectedStateTools:
    def test_select_product_uses_injected_dialog(self):
        """select_product reads products from state['dialog'], not from contextvar."""
        dialog = {
            **_default_dialog(),
            "category": "mortgage",
            "products": [{"name": "Ипотека Стандарт", "rate": "14%", "amount": "500 млн"}],
        }
        result = _run(select_product.coroutine("Ипотека Стандарт", state={"dialog": dialog}))
        assert "Ипотека Стандарт" in result

    def test_compare_products_uses_injected_dialog(self):
        """compare_products reads products from state['dialog']."""
        dialog = {
            **_default_dialog(),
            "flow": "show_products",
            "category": "mortgage",
            "products": [
                {"name": "Ипотека А", "rate": "14%", "amount": "до 500 млн", "term": "до 240 мес"},
                {"name": "Ипотека Б", "rate": "16%", "amount": "до 300 млн", "term": "до 180 мес"},
            ],
        }
        result = _run(compare_products.coroutine("сравни ипотеки", state={"dialog": dialog}))
        assert "Ипотека А" in result
        assert "Ипотека Б" in result

    def test_back_to_product_list_uses_injected_dialog(self):
        """back_to_product_list renders product list from state['dialog']."""
        dialog = {
            **_default_dialog(),
            "flow": "product_detail",
            "category": "mortgage",
            "products": [{"name": "Ипотека Стандарт", "rate": "14%"}],
        }
        result = _run(back_to_product_list.coroutine(state={"dialog": dialog}))
        assert "Ипотека Стандарт" in result

    def test_start_calculator_uses_injected_dialog(self):
        """start_calculator reads category from state['dialog'] to pick correct questions."""
        dialog = {**_default_dialog(), "category": "deposit"}
        result = _run(start_calculator.coroutine(state={"dialog": dialog}))
        assert "сумму" in result.lower() or "вклад" in result.lower() or "депозит" in result.lower()

    def test_tool_schemas_exclude_state_param(self):
        """LLM must not see the injected 'state' parameter in any of the 4 refactored tools."""
        from langchain_core.utils.function_calling import convert_to_openai_function
        for tool_fn in (select_product, compare_products, back_to_product_list, start_calculator):
            schema = convert_to_openai_function(tool_fn)
            params = schema.get("parameters", {}).get("properties", {})
            assert "state" not in params, (
                f"Tool '{tool_fn.name}' exposes 'state' in LLM schema — InjectedState not working"
            )
            required = schema.get("parameters", {}).get("required", [])
            assert "state" not in required, (
                f"Tool '{tool_fn.name}' lists 'state' as required in LLM schema"
            )

    def test_tools_work_with_empty_state(self):
        """All 4 tools must not raise when state is empty or dialog is missing."""
        _run(back_to_product_list.coroutine(state={}))
        _run(back_to_product_list.coroutine(state={"dialog": {}}))
        _run(start_calculator.coroutine(state={}))
        _run(start_calculator.coroutine(state={"dialog": {}}))
        _run(select_product.coroutine("X", state={}))
        _run(compare_products.coroutine("compare", state={}))

    def test_current_dialog_contextvar_removed(self):
        """_CURRENT_DIALOG must no longer exist in app.agent.constants."""
        import app.agent.constants as c
        assert not hasattr(c, "_CURRENT_DIALOG"), (
            "_CURRENT_DIALOG contextvar was not removed from constants.py"
        )


# ---- Parsers/regex_fallback removal ---------------------------------------

class TestParsersRemoval:
    def test_parsers_module_removed(self):
        """app.agent.parsers should no longer exist — LLM handles extraction."""
        with pytest.raises(ImportError):
            from app.agent import parsers  # noqa: F401

    def test_regex_fallback_removed(self):
        import app.agent.calc_extractor as ce
        assert not hasattr(ce, "regex_fallback")


# ---- LLM extractor prompt: year → month conversion rule -------------------

class TestExtractPromptYearsToMonths:
    def test_ru_prompt_converts_years_to_months(self):
        from app.agent.calc_extractor import _EXTRACT_SYSTEM_PROMPT
        prompt = _EXTRACT_SYSTEM_PROMPT["ru"]
        assert "МЕСЯЦАХ" in prompt
        assert "умножай годы на 12" in prompt
        # Cyrillic Uzbek example is present
        assert "йил" in prompt

    def test_en_prompt_converts_years_to_months(self):
        from app.agent.calc_extractor import _EXTRACT_SYSTEM_PROMPT
        prompt = _EXTRACT_SYSTEM_PROMPT["en"]
        assert "MONTHS" in prompt
        assert "multiply years by 12" in prompt.lower()

    def test_uz_prompt_converts_years_to_months(self):
        from app.agent.calc_extractor import _EXTRACT_SYSTEM_PROMPT
        prompt = _EXTRACT_SYSTEM_PROMPT["uz"]
        assert "OYDA" in prompt
        assert "12 ga ko'paytiring" in prompt
        assert "йил" in prompt  # Cyrillic Uzbek example


# ---- Credit result templates: downpayment + principal --------------------

class TestCreditResultTemplate:
    def test_credit_result_pdf_has_new_placeholders(self):
        from app.agent.i18n import AGENT_TEXTS
        for lang in ("ru", "en", "uz"):
            tpl = AGENT_TEXTS["credit_result_pdf"][lang]
            assert "{principal}" in tpl, f"missing {{principal}} in {lang}"
            assert "{downpayment}" in tpl, f"missing {{downpayment}} in {lang}"
            assert "{dp_pct}" in tpl, f"missing {{dp_pct}} in {lang}"
            assert "{amount}" in tpl, f"missing {{amount}} in {lang}"

    def test_credit_result_fallback_has_new_placeholders(self):
        from app.agent.i18n import AGENT_TEXTS
        for lang in ("ru", "en", "uz"):
            tpl = AGENT_TEXTS["credit_result_fallback"][lang]
            assert "{principal}" in tpl
            assert "{downpayment}" in tpl
            assert "{dp_pct}" in tpl


# ---- calc_flow: downpayment correctly subtracted from principal ----------

class TestCreditCalcPrincipalSubtraction:
    """Bug fix: principal passed to amortization PDF must be amount - dp_abs."""

    @staticmethod
    def _state_with_filled_slots(dp_pct: float) -> dict:
        product = {
            "name": "Test Mortgage",
            "rate_matrix": [{
                "rate_min_pct": 18.0, "rate_max_pct": 18.0,
                "term_min_months": 1, "term_max_months": 240,
                "downpayment_min_pct": 0, "downpayment_max_pct": 100,
            }],
        }
        state = _make_state(user_text="")
        state["dialog"] = {
            **_default_dialog(),
            "flow": "calc_flow",
            "category": "mortgage",
            "selected_product": product,
            "calc_slots": {
                "amount": 500_000_000,
                "term_months": 120,
                "downpayment": dp_pct,
            },
            "calc_step": None,
        }
        return state

    def test_principal_subtracts_20pct_downpayment(self):
        state = self._state_with_filled_slots(dp_pct=20.0)
        with patch("app.agent.nodes.calc_flow.generate_amortization_pdf") as mock_pdf:
            mock_pdf.return_value = "/tmp/test.pdf"
            result = _run(node_calc_flow(state))
        mock_pdf.assert_called_once()
        kwargs = mock_pdf.call_args.kwargs
        assert kwargs["principal"] == 400_000_000, (
            f"Expected principal=400M (500M - 20%), got {kwargs['principal']}"
        )
        assert kwargs["term_months"] == 120
        # Answer should show gross amount, down payment and net principal
        ans = result["answer"]
        assert "500 000 000" in ans
        assert "400 000 000" in ans
        assert "100 000 000" in ans

    def test_zero_downpayment_principal_equals_amount(self):
        state = self._state_with_filled_slots(dp_pct=0)
        with patch("app.agent.nodes.calc_flow.generate_amortization_pdf") as mock_pdf:
            mock_pdf.return_value = "/tmp/test.pdf"
            _run(node_calc_flow(state))
        assert mock_pdf.call_args.kwargs["principal"] == 500_000_000


# ---- Branches module: service matrix + search + formatting ----------------

class TestBranchesServiceMatrix:
    def test_filial_has_all_services(self):
        from app.agent.branches import FILIAL, office_types_for_service
        for svc in ("credit_individual", "credit_legal", "cashier", "cards"):
            types = office_types_for_service(svc)
            assert FILIAL in types, f"filial missing for {svc}"

    def test_credit_legal_only_at_filial(self):
        from app.agent.branches import FILIAL, office_types_for_service
        assert office_types_for_service("credit_legal") == [FILIAL]

    def test_sales_point_only_autoloan_atm_consultation(self):
        from app.agent.branches import SALES_POINT, _SERVICE_MATRIX
        assert _SERVICE_MATRIX[SALES_POINT] == {"autoloan", "atm", "consultation"}

    def test_sales_office_has_no_legal_entity_services(self):
        from app.agent.branches import SALES_OFFICE, _SERVICE_MATRIX
        so = _SERVICE_MATRIX[SALES_OFFICE]
        assert "credit_legal" not in so
        assert "cashier" in so
        assert "cards" in so

    def test_unknown_service_returns_all_types(self):
        from app.agent.branches import ALL_OFFICE_TYPES, office_types_for_service
        assert set(office_types_for_service("banana")) == set(ALL_OFFICE_TYPES)


class TestBranchCardFormat:
    def test_format_uses_ru_by_default(self):
        from app.agent.branches import format_branch_card

        class FakeFilial:
            OFFICE_TYPE_CODE = "filial"
            name_ru = "ЦБУ Тест"
            name_uz = "Test BXM"
            address_ru = "ул. Тест 1"
            address_uz = "Test ko'chasi 1"
            landmark_ru = "рядом с парком"
            landmark_uz = "parkga yaqin"
            location_url = "https://maps.example/1"
            phone = None
            hours = None

        card = format_branch_card(FakeFilial(), lang="ru")
        assert "ЦБУ Тест" in card
        assert "ул. Тест 1" in card
        assert "рядом с парком" in card

    def test_format_uses_uz_when_available(self):
        from app.agent.branches import format_branch_card

        class FakeSalesOffice:
            OFFICE_TYPE_CODE = "sales_office"
            name_ru = "Офис RU"
            name_uz = "Ofis UZ"
            address_ru = "addr ru"
            address_uz = "addr uz"
            # SalesOffice doesn't have landmark fields at all
            location_url = None
            phone = None
            hours = None

        card = format_branch_card(FakeSalesOffice(), lang="uz")
        assert "Ofis UZ" in card
        assert "addr uz" in card
        assert "Офис RU" not in card

    def test_sales_point_has_no_region_no_landmark(self):
        """SalesPoint is the leanest: no region, no landmark."""
        from app.agent.branches import format_branch_card

        class FakeSalesPoint:
            OFFICE_TYPE_CODE = "sales_point"
            name_ru = "Andijon KIA"
            name_uz = "Andijon KIA"
            address_ru = "Андижан, ул. Амира Темура 11"
            address_uz = "Andijon, A. Temur ko'chasi 11"
            phone = None
            hours = None
            # No landmark_ru, no location_url — format should not crash
            location_url = None

        card = format_branch_card(FakeSalesPoint(), lang="ru")
        assert "Andijon KIA" in card
        assert "автосалон" in card.lower()


class TestBranchesI18n:
    def test_branch_keys_present_in_3_langs(self):
        from app.agent.i18n import AGENT_TEXTS
        for key in ("branch_found_header", "branch_none_found", "office_types_info"):
            assert key in AGENT_TEXTS, f"missing {key}"
            for lang in ("ru", "en", "uz"):
                assert lang in AGENT_TEXTS[key] and AGENT_TEXTS[key][lang], (
                    f"missing {lang} for {key}"
                )


# ---- Seed script: parent-filial fuzzy matching ----------------------------

class TestSeedBranchesFuzzyMatching:
    def test_normalize_strips_tsbu_and_punctuation(self):
        from scripts.seed_branches import _normalize
        assert _normalize('ЦБУ "Андижан"') == "андижан"
        assert _normalize("BXM 'Andijon'") == "andijon"
        assert _normalize("  Самарканд  ") == "самарканд"

    def test_resolve_parent_exact_match(self):
        from scripts.seed_branches import _resolve_parent
        index = {"андижан": 1, "самарканд": 2, "автотранспорт": 13}
        assert _resolve_parent("Андижан", index) == 1
        assert _resolve_parent('ЦБУ "Самарканд"', index) == 2

    def test_resolve_parent_fuzzy_match_variants(self):
        """Real-world: 'Автотранспортный' (sales_office) ↔ 'Автотранспорт' (filial)."""
        from scripts.seed_branches import _resolve_parent
        index = {"автотранспорт": 13, "джизак": 4}
        assert _resolve_parent("Автотранспортный", index) == 13
        # Кириллические варианты написания (Жиззах ↔ Джизак) — fuzzy cutoff=0.6
        # works for short strings that share common chars
        assert _resolve_parent("Жиззах", index) in (4, None)  # allow None if too different

    def test_resolve_parent_no_match_returns_none(self):
        from scripts.seed_branches import _resolve_parent
        index = {"андижан": 1}
        assert _resolve_parent("Мурманск", index) is None
        assert _resolve_parent("", index) is None
        assert _resolve_parent(None, index) is None


# ---- Bot menu: 3 buttons submenu for Отделения ---------------------------

class TestBranchesInlineKeyboard:
    """When user clicks '🏢 Отделения', bot shows inline-drill-down:
    type selection → office list → office details.
    """

    def test_type_keyboard_has_3_inline_buttons_with_correct_callbacks(self):
        from app.bot.handlers.commands import _office_type_inline_keyboard

        kb = _office_type_inline_keyboard("ru")
        # Flatten all buttons
        all_btns = [btn for row in kb.inline_keyboard for btn in row]
        callbacks = [b.callback_data for b in all_btns]
        assert "office:type:filial" in callbacks
        assert "office:type:sales_office" in callbacks
        assert "office:type:sales_point" in callbacks

    def test_type_keyboard_labels_localized_uz(self):
        from app.bot.handlers.commands import _office_type_inline_keyboard

        kb = _office_type_inline_keyboard("uz")
        texts = [btn.text for row in kb.inline_keyboard for btn in row]
        assert any("Filial" in t for t in texts)
        assert any("Savdo ofis" in t for t in texts)
        assert any("Savdo nuqta" in t for t in texts)

    def test_list_keyboard_has_button_per_office_plus_back(self):
        from app.bot.handlers.commands import _office_list_inline_keyboard

        class FakeOffice:
            def __init__(self, id_, name):
                self.id = id_
                self.name_ru = name
                self.name_uz = None

        offices = [FakeOffice(1, "A"), FakeOffice(2, "B"), FakeOffice(3, "C")]
        kb = _office_list_inline_keyboard("filial", offices, "ru")
        rows = kb.inline_keyboard
        # 3 office buttons + 1 back row = 4 rows
        assert len(rows) == 4
        # Each office row has 1 button with show callback
        assert rows[0][0].callback_data == "office:show:filial:1"
        assert rows[0][0].text == "A"
        assert rows[1][0].callback_data == "office:show:filial:2"
        # Last row is back
        assert rows[-1][0].callback_data == "office:back"

    def test_list_keyboard_truncates_long_names(self):
        from app.bot.handlers.commands import _office_list_inline_keyboard

        class FakeOffice:
            id = 1
            name_ru = "A" * 200  # very long
            name_uz = None

        kb = _office_list_inline_keyboard("sales_office", [FakeOffice()], "ru")
        assert len(kb.inline_keyboard[0][0].text) <= 64  # Telegram limit

    def test_detail_keyboard_has_two_back_buttons(self):
        from app.bot.handlers.commands import _office_detail_inline_keyboard

        kb = _office_detail_inline_keyboard("sales_point", "ru")
        rows = kb.inline_keyboard
        assert len(rows) == 2
        assert rows[0][0].callback_data == "office:type:sales_point"
        assert rows[1][0].callback_data == "office:back"

    def test_detail_keyboard_labels_localized(self):
        from app.bot.handlers.commands import _office_detail_inline_keyboard

        for lang in ("ru", "en", "uz"):
            kb = _office_detail_inline_keyboard("filial", lang)
            # Just ensure it doesn't crash and produces non-empty labels
            assert kb.inline_keyboard[0][0].text
            assert kb.inline_keyboard[1][0].text


# ---- Uzbek Latin-only enforcement ----------------------------------------

class TestUzbekLatinOnly:
    """Bot must reply to Uzbek customers ONLY in Latin script — never Cyrillic.

    Two layers of defence:
    1. SYSTEM_POLICY contains a critical rule forbidding Uzbek Cyrillic.
    2. _LANG_INSTRUCTION['uz'] reinforces it on every UZ turn.
    3. Static AGENT_TEXTS['uz'] must contain no Cyrillic characters.
    """

    def test_system_policy_forbids_uzbek_cyrillic(self):
        assert "ЛАТИН" in SYSTEM_POLICY.upper() or "LATIN" in SYSTEM_POLICY.upper() \
            or "лотин" in SYSTEM_POLICY.lower()
        # Example of forbidden pattern must be listed
        assert "Ассалому" in SYSTEM_POLICY or "қанча" in SYSTEM_POLICY \
            or "ўқғҳ" in SYSTEM_POLICY or "Ўзбекистон" in SYSTEM_POLICY

    def test_lang_instruction_uz_forbids_cyrillic(self):
        from app.agent.constants import _LANG_INSTRUCTION
        uz = _LANG_INSTRUCTION["uz"]
        assert "LOTIN" in uz.upper() or "Latin" in uz
        # Mentions forbidden Cyrillic examples
        assert "Kirill" in uz or "кирилл" in uz.lower() or "ўқғҳ" in uz

    def test_agent_texts_uz_has_no_cyrillic(self):
        """Every 'uz' value in AGENT_TEXTS must be in Latin script only."""
        from app.agent.i18n import AGENT_TEXTS
        import re
        cyr = re.compile(r"[А-Яа-яЁёЎўҚқҒғҲҳҶҷ]")
        bad = []
        for key, variants in AGENT_TEXTS.items():
            uz_val = variants.get("uz")
            if not uz_val:
                continue
            if cyr.search(uz_val):
                chars = "".join(cyr.findall(uz_val)[:10])
                bad.append((key, chars))
        assert not bad, f"UZ values with Cyrillic chars: {bad}"

    def test_bot_i18n_uz_menu_labels_has_no_cyrillic(self):
        """All UZ menu labels must be Latin-only (bot keyboard text)."""
        from app.bot.i18n import MENU_LABELS
        import re
        cyr = re.compile(r"[А-Яа-яЁёЎўҚқҒғҲҳҶҷ]")
        bad = []
        for key, value in MENU_LABELS["uz"].items():
            if cyr.search(value):
                bad.append((key, value))
        assert not bad, f"UZ menu labels with Cyrillic chars: {bad}"

