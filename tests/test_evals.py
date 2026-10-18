"""The evaluation harness itself (roadmap П.7б).

These tests do NOT judge the agent — they judge the thing that judges the
agent. That distinction matters: an evaluator that quietly scores everything
1.0 makes a regression gate that can never fail, which is worse than having no
gate at all, because it looks like coverage.

No network, no database, no OpenAI key.
"""
from __future__ import annotations

import pytest

from evals import evaluators
from evals.dataset import GOLDEN_CASES, accepted_tools, coverage_by_language, validate_dataset


def _result(**overrides) -> dict:
    base = {
        "case_id": "c1",
        "answer": "Вот наши ипотечные программы.",
        "tools": [],
        "latency_ms": 100.0,
        "error": None,
        "lang": "ru",
        "token_usage": None,
    }
    base.update(overrides)
    return base


class TestDataset:
    def test_dataset_is_not_empty(self):
        assert len(GOLDEN_CASES) >= 20

    def test_dataset_is_internally_valid(self):
        """`validate_dataset` checks ids are unique, languages are known and
        expected tools actually exist — a typo'd tool name would otherwise
        make a case unfailable."""
        assert validate_dataset() == []

    def test_all_three_languages_are_covered(self):
        coverage = coverage_by_language()

        for lang in ("ru", "en", "uz"):
            assert coverage.get(lang, 0) > 0, f"no cases in {lang}"

    def test_accepted_tools_normalises_single_and_multi(self):
        assert accepted_tools({"expected_tool": "get_products"}) == ("get_products",)
        assert set(accepted_tools({"expected_tool": ["a", "b"]})) == {"a", "b"}


class TestToolMatch:
    def test_expected_tool_called(self):
        case = {"expected_tool": "get_products"}
        verdict = evaluators.tool_match(case, _result(tools=["get_products"]))

        assert verdict["score"] == 1.0

    def test_wrong_tool_scores_zero(self):
        case = {"expected_tool": "get_products"}
        verdict = evaluators.tool_match(case, _result(tools=["faq_lookup"]))

        assert verdict["score"] == 0.0

    def test_any_of_several_accepted_tools_counts(self):
        """Some questions legitimately route two ways; the dataset says so by
        listing both, and the evaluator must not prefer one."""
        case = {"expected_tool": ["get_products", "recommend_product"]}

        assert evaluators.tool_match(case, _result(tools=["recommend_product"]))["score"] == 1.0

    def test_no_expected_tool_means_no_tool_should_run(self):
        """`expected_tool: None` is an assertion, not a gap: a greeting must
        be answered without reaching for a tool. Calling one is a miss."""
        assert evaluators.tool_match({}, _result(tools=[]))["score"] == 1.0
        assert evaluators.tool_match({}, _result(tools=["faq_lookup"]))["score"] == 0.0

    def test_a_chained_turn_still_counts(self):
        """`get_products` → `select_product` is a correct answer to "show me
        mortgages, I'll take the second"; the gate must not punish it."""
        case = {"expected_tool": "get_products"}
        verdict = evaluators.tool_match(
            case, _result(tools=["get_products", "select_product"]),
        )

        assert verdict["score"] == 1.0


class TestPiiLeak:
    def test_clean_answer_passes(self):
        verdict = evaluators.no_pii_leak({"text": "привет"}, _result())

        assert verdict["score"] == 1.0

    def test_card_number_in_answer_is_a_leak(self):
        """A card number has no legitimate reason to appear in a reply, no
        matter who typed it first."""
        verdict = evaluators.no_pii_leak(
            {"text": "проверьте карту"}, _result(answer="Ваша карта 8600123412341234 активна"),
        )

        assert verdict["score"] == 0.0

    def test_echoing_the_customers_own_phone_is_a_leak(self):
        verdict = evaluators.no_pii_leak(
            {"text": "мой номер +998901234567"},
            _result(answer="Записал номер +998901234567, вам перезвонят."),
        )

        assert verdict["score"] == 0.0

    def test_bank_phone_not_from_the_customer_is_allowed(self):
        """The bank publishes contact numbers; quoting one is not a leak."""
        verdict = evaluators.no_pii_leak(
            {"text": "как позвонить в банк"},
            _result(answer="Позвоните в колл-центр: +998 78 140-00-00"),
        )

        assert verdict["score"] == 1.0


class TestLatency:
    def test_fast_turn_passes(self):
        verdict = evaluators.latency_under(1000.0)({}, _result(latency_ms=250.0))

        assert verdict["score"] == 1.0

    def test_slow_turn_fails(self):
        verdict = evaluators.latency_under(1000.0)({}, _result(latency_ms=4000.0))

        assert verdict["score"] == 0.0


class TestLanguageMatch:
    def test_russian_answer_to_russian_question(self):
        verdict = evaluators.language_match(
            {"lang": "ru"}, _result(answer="Вот наши ипотечные программы."),
        )

        assert verdict["score"] == 1.0

    def test_wrong_language_is_caught(self):
        verdict = evaluators.language_match(
            {"lang": "ru"}, _result(answer="Here are our mortgage programs for you."),
        )

        assert verdict["score"] == 0.0


class TestErrorEvaluator:
    def test_exception_scores_zero(self):
        verdict = evaluators.no_error({}, _result(error="RuntimeError: boom"))

        assert verdict["score"] == 0.0

    def test_clean_run_scores_one(self):
        assert evaluators.no_error({}, _result())["score"] == 1.0


class TestOfflineRunner:
    @pytest.mark.asyncio
    async def test_a_case_runs_end_to_end_without_network(self):
        """The offline harness has to actually drive the graph — otherwise the
        dataset is never exercised and the gate tests nothing."""
        from evals.runner import harness, run_case

        case = next(c for c in GOLDEN_CASES if c.get("expected_tool"))
        async with harness(live=False) as h:
            result = await run_case(case, harness_ctx=h)

        assert result.error is None, result.error
        assert result.answer, "a case must produce an answer"
        assert result.latency_ms >= 0
