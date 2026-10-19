"""Tests for app/agent/tool_salvage.py and its use in node_faq.

The positive cases are the exact replies Gemma 4 26B-A4B QAT Q4_0 gave on
llama-server (RTX 4080, 2026-10-18, `scripts/diag_model.py --dump`) where it
should have called a tool. The negative cases are what the rescue must leave
alone: how-to / FAQ questions that merely name a product, and ordinary prose.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage

from app.agent.intent import _product_request_tool
from app.agent.tool_salvage import (
    looks_like_thought_leak,
    rescue_tool_call,
    salvage_tool_call,
)
from app.agent.tools import _FAQ_TOOLS

TOOL_NAMES = [t.name for t in _FAQ_TOOLS]


class TestSalvageToolCall:
    @pytest.mark.parametrize(
        "content, expected",
        [
            # captured: "Avtokreditlaringizni ko'rsating", 2 of 3 runs
            ('<get_products category="autoloan"/>', ("get_products", {"category": "autoloan"})),
            # quoted in MODEL_BENCHMARK_REPORT.md
            ('<call:get_products(category="autoloan")/>', ("get_products", {"category": "autoloan"})),
            # native markup rendered as text
            ('<|tool_call>call:get_products{category:<|"|>deposit<|"|>}<tool_call|>',
             ("get_products", {"category": "deposit"})),
            ("<show_credit_menu/>", ("show_credit_menu", {})),
            ('find_office(office_type="filial", query="Toshkent")',
             ("find_office", {"office_type": "filial", "query": "Toshkent"})),
            ("custom_loan_calculator(amount=100000000, term=24, rate=21.5)",
             ("custom_loan_calculator", {"amount": 100000000, "term": 24, "rate": 21.5})),
        ],
    )
    def test_recovers_call_written_as_text(self, content, expected) -> None:
        assert salvage_tool_call(content, TOOL_NAMES) == expected

    def test_leaked_reasoning_last_named_call_wins(self) -> None:
        # captured: "Qanday omonatlaringiz bor?" run 1 (abridged)
        content = (
            "<thought\nThinking Process:\n\n"
            "1.  **Identify User Intent:** The user is asking about deposits.\n"
            "2.  **Determine Tool to Use:** I should use `faq_lookup` only for help "
            "articles; for lists use `get_products(category=\"deposit\")`.\n"
            "*Action:* Call `get_products(category=\"deposit\")`."
        )
        assert looks_like_thought_leak(content)
        assert salvage_tool_call(content, TOOL_NAMES) == ("get_products", {"category": "deposit"})

    @pytest.mark.parametrize(
        "content",
        [
            "",
            "Vaalaykum assalom! Sizga qanday yordam bera olaman?",
            "Ставка (годовых) зависит от срока (от 12 до 60 месяцев).",
            '<b>Ipoteka turlari:</b> <a href="https://asaka-bank.uz/ru/products/mortgage"></a>',
            "<delete_account id=5/>",  # not one of our tools
        ],
    )
    def test_prose_is_not_a_call(self, content) -> None:
        assert salvage_tool_call(content, TOOL_NAMES) is None

    def test_unbound_tool_is_not_salvaged(self) -> None:
        names = [n for n in TOOL_NAMES if n != "clarify"]
        assert salvage_tool_call('<clarify question="Qaysi?"/>', names) is None


class TestThoughtLeak:
    @pytest.mark.parametrize(
        "content",
        ["<thought\nThinking Process:\n1. ...", "  <thought>…", "Thinking Process: 1. Identify", "<|channel>thought\n…"],
    )
    def test_detected(self, content) -> None:
        assert looks_like_thought_leak(content)

    @pytest.mark.parametrize(
        "content",
        ["Конечно! Вот наши вклады.", "Thoughtful choice — here is our deposit list.", ""],
    )
    def test_ordinary_reply_is_not_a_leak(self, content) -> None:
        assert not looks_like_thought_leak(content)


class TestProductRequestTool:
    @pytest.mark.parametrize(
        "text, expected",
        [
            # the five Uzbek misses from the 2026-10-18 benchmark
            ("Ipoteka olmoqchiman", ("get_products", {"category": "mortgage"})),
            ("Avtokreditlaringizni ko'rsating", ("get_products", {"category": "autoloan"})),
            ("Qanday omonatlaringiz bor?", ("get_products", {"category": "deposit"})),
            ("Karta ochmoqchiman", ("get_products", {"category": "debit_card"})),
            ("Menga kredit kerak", ("show_credit_menu", {})),
            # Russian equivalents
            ("Хочу оформить ипотеку", ("get_products", {"category": "mortgage"})),
            ("Покажите ваши автокредиты", ("get_products", {"category": "autoloan"})),
            ("Какие вклады у вас есть?", ("get_products", {"category": "deposit"})),
            ("Мне нужен кредит", ("show_credit_menu", {})),
            # Cyrillic Uzbek and the spellings customers type
            ("ипотека олмокчиман", ("get_products", {"category": "mortgage"})),
            ("омонатлар борми", ("get_products", {"category": "deposit"})),
            ("карта очмокчиман", ("get_products", {"category": "debit_card"})),
            ("kredit kere edi", ("show_credit_menu", {})),
            ("Avtokreditlaringizni ko‘rsating", ("get_products", {"category": "autoloan"})),
            # a card named by its currency is the FX card, not a debit card
            ("dollarga karta kerak", ("get_products", {"category": "fx_card"})),
            ("Хочу долларовую карту", ("get_products", {"category": "fx_card"})),
        ],
    )
    def test_catalog_request(self, text, expected) -> None:
        assert _product_request_tool(text) == expected

    @pytest.mark.parametrize(
        "text",
        [
            # how-to / problem questions that name a product
            "Kartani qanday bloklash mumkin?",
            "Как заблокировать карту?",
            "Ipoteka qanday rasmiylashtiriladi?",
            "Что такое ипотека?",
            "Kartamni yo'qotdim",
            "Какие документы нужны для автокредита?",
            "Kredit uchun qanday hujjatlar kerak?",
            # no product at all (a currency named without a card stays out)
            "Bugun dollar kursi qancha?",
            "Курс доллара на сегодня",
            "Toshkentda eng yaqin filial qayerda?",
            "Eskrou hisob nima?",
            "Assalomu alaykum!",
            # a product named without asking for the catalog
            "Ipoteka",
            # long message mixing several asks
            "Salom, men ertaga filialga borib karta ochmoqchiman lekin avval "
            "bilmoqchiman qaysi hujjatlarni olib borishim kerak va qancha vaqt ketadi",
        ],
    )
    def test_not_a_catalog_request(self, text) -> None:
        assert _product_request_tool(text) is None


class TestRescueToolCall:
    def test_call_as_text_beats_intent(self) -> None:
        assert rescue_tool_call(
            '<get_products category="autoloan"/>', "Ipoteka olmoqchiman", TOOL_NAMES,
            allow_intent=True,
        ) == ("get_products", {"category": "autoloan"}, "call_as_text")

    def test_intent_when_reply_is_free_text(self) -> None:
        reply = "Tushunarli, karta ochish juda yaxshi qaror! Sizga qaysi turdagi karta qiziqroq?"
        assert rescue_tool_call(reply, "Karta ochmoqchiman", TOOL_NAMES, allow_intent=True) == (
            "get_products", {"category": "debit_card"}, "intent",
        )

    def test_no_intent_after_a_tool_ran(self) -> None:
        reply = "Mana sizga mos kartalar ro'yxati."
        assert rescue_tool_call(reply, "Karta ochmoqchiman", TOOL_NAMES, allow_intent=False) is None

    @pytest.mark.parametrize(
        "text",
        ["operator bilan gaplashmoqchiman", "Позовите оператора", "ipoteka bo'yicha operatorga ulang"],
    )
    def test_promised_handoff_becomes_operator_call(self, text) -> None:
        # captured: the model wrote "Hozir sizni operatorga ulayman" and stopped
        reply = "Tushunaman, sizga mutaxassis bilan gaplashish kerak. Hozir sizni operatorga ulayman."
        assert rescue_tool_call(reply, text, TOOL_NAMES, allow_intent=True) == (
            "request_operator", {"reason": "user_request"}, "intent",
        )

    def test_currency_without_a_named_currency_is_left_to_the_model(self) -> None:
        # get_currency_info's own rule: no currency named → the model clarifies
        reply = "Siz qaysi valyuta kursini bilmoqchisiz? Dollar, yevro yoki boshqa?"
        assert rescue_tool_call(reply, "kurs qancha bugun", TOOL_NAMES, allow_intent=True) is None


# ---------------------------------------------------------------------------
# node_faq end to end, with a fake model
# ---------------------------------------------------------------------------

def _faq_state(text: str, lang: str = "uz") -> dict:
    from app.agent.state import _default_dialog

    # Agent._ainvoke_locked sets dialog["last_lang"] before the graph runs;
    # tools read the language from there (ToolNode gets messages + dialog).
    return {
        "last_user_text": text,
        "messages": [],
        "dialog": {**_default_dialog(), "last_lang": lang},
        "lang": lang,
        "session_id": "s-salvage",
        "user_id": 1,
        "user_profile": None,
    }


def _fake_llm(*replies: AIMessage):
    queue = list(replies)

    class _Bound:
        async def ainvoke(self, msgs, **kwargs):
            return queue.pop(0)

    class _LLM:
        def bind_tools(self, tools, **kwargs):
            return _Bound()

    return _LLM()


async def _run_node_faq_via_graph(state_in: dict) -> dict:
    """ToolNode needs the LangGraph runtime, so turns that execute a tool run
    through the real graph — same pattern as tests/test_phase4_tools.py."""
    from app.agent.graph import build_graph

    graph = build_graph()
    config = {"configurable": {"thread_id": f"test-salvage-{id(state_in)}"}}
    full_state = {
        "answer": "", "human_mode": False, "keyboard_options": None,
        "_route": "", "show_operator_button": False, "token_usage": None,
        "user_profile": None, "ui_blocks": None,
        **state_in,
    }
    return await graph.ainvoke(full_state, config=config)


class TestNodeFaqRescue:
    @pytest.fixture(autouse=True)
    def _no_faq_precheck(self, monkeypatch):
        from app.agent.nodes import faq as faq_module

        monkeypatch.setattr(faq_module, "faq_precheck_answer", AsyncMock(return_value=None))

    @pytest.mark.asyncio
    async def test_free_text_menu_becomes_credit_menu(self, monkeypatch) -> None:
        from app.agent.i18n import at
        from app.agent.nodes import faq as faq_module

        # captured shape: the model lists credit types itself, with raw HTML
        made_up = AIMessage(content=(
            "Tushunarli, sizga kredit kerak. <br><b>Kredit turlari:</b><ul>"
            "<li><b>Avtokredit</b></li></ul>"
        ))
        monkeypatch.setattr(faq_module, "_get_chat_openai", lambda role=None: _fake_llm(made_up))

        result = await _run_node_faq_via_graph(_faq_state("Menga kredit kerak"))

        assert result["answer"] == at("credit_menu_prompt", "uz")
        assert "<ul>" not in result["answer"]

    @pytest.mark.asyncio
    async def test_disabled_by_env(self, monkeypatch) -> None:
        from app.agent.nodes import faq as faq_module

        reply = AIMessage(content="Qaysi kredit turi sizga kerak?")
        monkeypatch.setenv("TOOL_SALVAGE_ENABLED", "false")
        monkeypatch.setattr(faq_module, "_get_chat_openai", lambda role=None: _fake_llm(reply))

        result = await faq_module.node_faq(_faq_state("Menga kredit kerak"))

        assert "Qaysi kredit turi sizga kerak?" in result["answer"]

    @pytest.mark.asyncio
    async def test_leaked_reasoning_never_reaches_the_customer(self, monkeypatch) -> None:
        from app.agent.nodes import faq as faq_module

        leak = AIMessage(content="<thought\nThinking Process:\n1. The user greets me. 2. Reply politely.")
        monkeypatch.setattr(faq_module, "_get_chat_openai", lambda role=None: _fake_llm(leak))

        result = await faq_module.node_faq(_faq_state("Assalomu alaykum!"))

        assert "Thinking Process" not in result["answer"]
        assert "<thought" not in result["answer"]

    @pytest.mark.asyncio
    async def test_tool_chosen_by_model_is_untouched(self, monkeypatch) -> None:
        from app.agent.i18n import at
        from app.agent.nodes import faq as faq_module

        real_call = AIMessage(content="", tool_calls=[{
            "name": "show_credit_menu", "args": {}, "id": "call_1", "type": "tool_call",
        }])
        monkeypatch.setattr(faq_module, "_get_chat_openai", lambda role=None: _fake_llm(real_call))
        spy = AsyncMock(side_effect=AssertionError("rescue must not run"))
        monkeypatch.setattr(faq_module, "rescue_tool_call", spy)

        result = await _run_node_faq_via_graph(_faq_state("Menga kredit kerak"))

        assert result["answer"] == at("credit_menu_prompt", "uz")
        spy.assert_not_called()
