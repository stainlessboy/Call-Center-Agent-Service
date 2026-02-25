"""Unit tests for local_agent helper functions."""
import pytest
from unittest.mock import patch, MagicMock
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

# Import helpers
from app.services.agent import (
    _is_conversational_followup,
    _is_question_like,
    _normalize_text,
    _classify_new_intent_rules,
    _find_last_human_and_ai,
    _is_bank_related,
    _is_greeting,
    _is_mortgage_intent,
    _is_auto_loan_intent,
    _extract_amount_sum,
    _extract_term_months,
    _default_dialog,
    _clear_flow,
    _set_flow,
)


class TestNormalizeText:
    def test_lowercases(self):
        assert _normalize_text("Привет") == "привет"

    def test_removes_punctuation(self):
        assert _normalize_text("Привет!") == "привет"

    def test_collapses_whitespace(self):
        assert _normalize_text("  привет   мир  ") == "привет мир"

    def test_empty(self):
        assert _normalize_text("") == ""


class TestIsQuestionLike:
    def test_question_mark(self):
        assert _is_question_like("доступно?") is True

    def test_kak_prefix(self):
        assert _is_question_like("как открыть счет") is True

    def test_a_chto_prefix(self):
        assert _is_question_like("а что потом делать") is True

    def test_a_kak_prefix(self):
        assert _is_question_like("а как это работает") is True

    def test_simple_statement(self):
        assert _is_question_like("хочу кредит") is False

    def test_empty(self):
        assert _is_question_like("") is False


class TestIsConversationalFollowup:
    def test_tochno(self):
        assert _is_conversational_followup("точно?") is True

    def test_tochno_dostupno(self):
        assert _is_conversational_followup("точно доступно?") is True

    def test_a_chto_potom(self):
        assert _is_conversational_followup("а что потом сделать") is True

    def test_banking_keyword_excluded(self):
        # Has banking keyword, should NOT be treated as follow-up
        assert _is_conversational_followup("точно это кредит?") is False

    def test_long_text_excluded(self):
        assert _is_conversational_followup("вот это очень длинный текст который не является follow-up вопросом наверное") is False

    def test_new_banking_intent(self):
        assert _is_conversational_followup("хочу ипотеку") is False


class TestClassifyIntentRules:
    def test_greeting(self):
        assert _classify_new_intent_rules("привет") == "greeting"

    def test_mortgage(self):
        assert _classify_new_intent_rules("хочу ипотеку") == "mortgage"
        assert _classify_new_intent_rules("купить квартиру") == "mortgage"

    def test_auto_loan(self):
        assert _classify_new_intent_rules("автокредит на машину") == "auto_loan"

    def test_microloan(self):
        assert _classify_new_intent_rules("микрозайм") == "microloan"

    def test_deposit(self):
        assert _classify_new_intent_rules("хочу вклад") == "deposit"

    def test_unknown(self):
        assert _classify_new_intent_rules("погода сегодня") == "unknown"

    def test_mobile_app(self):
        assert _classify_new_intent_rules("мобильное приложение банка") == "mobile_app"


class TestFindLastHumanAndAi:
    def test_basic(self):
        msgs = [
            SystemMessage(content="system"),
            HumanMessage(content="user msg"),
            AIMessage(content="ai msg"),
        ]
        human, ai = _find_last_human_and_ai(msgs)
        assert human == "user msg"
        assert ai == "ai msg"

    def test_empty(self):
        human, ai = _find_last_human_and_ai([])
        assert human is None
        assert ai is None

    def test_only_human(self):
        msgs = [HumanMessage(content="hello")]
        human, ai = _find_last_human_and_ai(msgs)
        assert human == "hello"
        assert ai is None

    def test_multiple_exchanges(self):
        msgs = [
            HumanMessage(content="first question"),
            AIMessage(content="first answer"),
            HumanMessage(content="second question"),
            AIMessage(content="second answer"),
        ]
        human, ai = _find_last_human_and_ai(msgs)
        assert human == "second question"
        assert ai == "second answer"


class TestExtractAmountSum:
    def test_millions(self):
        assert _extract_amount_sum("500 млн") == 500_000_000

    def test_thousands(self):
        assert _extract_amount_sum("300 тыс") == 300_000

    def test_plain_number(self):
        result = _extract_amount_sum("100000")
        assert result == 100_000

    def test_none_on_empty(self):
        assert _extract_amount_sum("без суммы") is None


class TestExtractTermMonths:
    def test_years(self):
        result = _extract_term_months("10 лет")
        assert result == 120

    def test_months(self):
        result = _extract_term_months("24 месяца")
        assert result == 24


class TestDialogHelpers:
    def test_default_dialog(self):
        d = _default_dialog()
        assert d["flow"] is None

    def test_clear_flow(self):
        d = _clear_flow()
        assert d.get("flow") is None

    def test_set_flow(self):
        d = _set_flow("mortgage", "ask_amount", {"amount": 100})
        assert d["flow"] == "mortgage"
        assert d["step"] == "ask_amount"
        assert d["slots"]["amount"] == 100
