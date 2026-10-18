"""Offline doubles for the evaluation harness: fixture data + a stub LLM.

WHY this exists: the harness itself is code, and code that only runs when
`OPENAI_API_KEY`, Postgres and Weaviate are all reachable is code that rots.
CI runs the whole pipeline — dataset → graph → evaluators → report — against
these doubles on every PR, so a broken runner is caught for free; the live run
(paid, keyed, on demand) is then the only thing that measures model quality.

The doubles are deliberately installed at the DATA/LLM seams, not at the node
or tool level: `node_faq`, `node_calc_flow`, `node_qualify_flow`, the router,
`_finalize_turn`, the real 16 tools and the real product/office formatting all
execute unmodified. What is replaced:

* ``app.agent.llm._get_chat_openai`` → a scripted chat model for the
  "consultant" role and ``None`` for every other role. Returning ``None`` is
  not a cop-out: `calc_extractor` and `faq_rephrase` both already treat "no
  LLM" as a supported degraded path (regex parsing / verbatim FAQ text), so
  the deterministic nodes stay on their real code paths.
* ``app.utils.data_loaders._load_*`` → in-memory rows shaped exactly like the
  SQL loaders' output, so `products.py` builds real product dicts.
* ``app.agent.branches.search_offices`` and ``app.db.session.get_session`` →
  in-memory ORM instances (SQLAlchemy models can be constructed without a
  session, which keeps `format_branch_card` / `office_public_dict` real).
* ``app.utils.faq_tools.faq_search`` → fixture FAQ rows (no Weaviate).
* ``app.utils.cbu_rates.fetch_cbu_rates`` → fixed rates (no HTTP).
* ``app.utils.pdf_generator.generate_amortization_pdf`` → a fake path (no
  file writes in CI).

Patching is done by scanning ``sys.modules`` for every module that holds a
binding of a given name, because this codebase imports helpers by value
(``from app.agent.products import _get_products_by_category``). Patching only
the defining module would leave every importer pointing at the real function —
and the ongoing `app/agent/` refactor keeps moving those importers around, so
an explicit list would go stale.
"""
from __future__ import annotations

import contextlib
import sys
from typing import Any, Iterator, Optional
from unittest.mock import patch

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

# ---------------------------------------------------------------------------
# Fixture data
# ---------------------------------------------------------------------------

def _rule(rate: float, *, term_max: int = 240, dp_min: Optional[float] = None,
          income_type: Optional[str] = None) -> dict[str, Any]:
    """One `credit_rate_rules` row in `_rule_to_dict()` shape."""
    return {
        "income_type": income_type,
        "age_min": 18,
        "age_max": 65,
        "amount_min": 10_000_000,
        "amount_max": 1_000_000_000,
        "term_min_months": 6,
        "term_max_months": term_max,
        "downpayment_min_pct": dp_min,
        "downpayment_max_pct": None,
        "currency_code": "UZS",
        "rate_min_pct": rate,
        "rate_max_pct": rate,
        "condition_text": "",
        "priority": 0,
    }


CREDIT_OFFERS: list[dict[str, Any]] = [
    {
        "id": 1, "section_name": "Ипотека", "service_name": "Ипотека Асака",
        "service_name_en": "Asaka Mortgage", "service_name_uz": "Asaka ipotekasi",
        "min_age": 18, "amount_text": "от 100 000 000 до 1 000 000 000 UZS",
        "amount_min": 100_000_000, "amount_max": 1_000_000_000,
        "purpose_text": "Покупка жилья", "collateral_text": "Залог приобретаемого жилья",
        "rate_condition_kind": "income_type", "for_market_primary": True,
        "for_market_secondary": True, "for_renovation": False,
        "rate_rules": [_rule(17.0, dp_min=25.0, income_type="payroll"),
                       _rule(19.0, dp_min=25.0, income_type="official")],
    },
    {
        "id": 2, "section_name": "Ипотека", "service_name": "Ипотека Молодая семья",
        "service_name_en": "Young Family Mortgage", "service_name_uz": "Yosh oila ipotekasi",
        "min_age": 18, "amount_text": "от 50 000 000 до 800 000 000 UZS",
        "amount_min": 50_000_000, "amount_max": 800_000_000,
        "purpose_text": "Первое жильё", "collateral_text": "Залог жилья",
        "rate_condition_kind": "income_type", "for_market_primary": True,
        "for_market_secondary": False, "for_renovation": False,
        "rate_rules": [_rule(15.0, dp_min=15.0, income_type="payroll")],
    },
    {
        "id": 3, "section_name": "Автокредит", "service_name": "Автокредит GM",
        "service_name_en": "GM Auto Loan", "service_name_uz": "GM avtokrediti",
        "min_age": 18, "amount_text": "до 400 000 000 UZS",
        "amount_min": 20_000_000, "amount_max": 400_000_000,
        "purpose_text": "Покупка автомобиля", "collateral_text": "Залог автомобиля",
        "rate_condition_kind": "income_type", "for_brand_gm": True, "for_brand_other": False,
        "rate_rules": [_rule(21.0, term_max=84, dp_min=25.0, income_type="payroll")],
    },
    {
        "id": 4, "section_name": "Микрозайм", "service_name": "Микрозайм Онлайн",
        "service_name_en": "Online Microloan", "service_name_uz": "Onlayn mikroqarz",
        "min_age": 18, "amount_text": "до 100 000 000 UZS",
        "amount_min": 1_000_000, "amount_max": 100_000_000,
        "purpose_text": "Любые цели", "collateral_text": "Без залога",
        "rate_condition_kind": None, "channel_online": True, "channel_cbu": True,
        "rate_rules": [_rule(26.0, term_max=36)],
    },
    {
        "id": 5, "section_name": "Образовательный", "service_name": "Образовательный кредит",
        "service_name_en": "Education Loan", "service_name_uz": "Ta'lim krediti",
        "min_age": 18, "amount_text": "до 200 000 000 UZS",
        "amount_min": 5_000_000, "amount_max": 200_000_000,
        "purpose_text": "Оплата контракта", "collateral_text": "Поручительство",
        "rate_condition_kind": None,
        "rate_rules": [_rule(18.0, term_max=120)],
    },
]

DEPOSIT_OFFERS: list[dict[str, Any]] = [
    {
        "service_name": "Вклад Асака Онлайн", "service_name_en": "Asaka Online Deposit",
        "service_name_uz": "Asaka Onlayn omonati", "currency_code": "UZS",
        "min_amount_text": "от 1 000 000 UZS", "min_amount": 1_000_000,
        "term_text": "12 мес.", "term_months": 12, "rate_text": "24%", "rate_pct": 24.0,
        "open_channel_text": "Онлайн", "payout_text": "Ежемесячно",
        "payout_monthly_available": True, "payout_end_available": False,
        "topup_text": "Пополняемый", "topup_allowed": True,
        "partial_withdrawal_allowed": False, "notes_text": "",
    },
    {
        "service_name": "Вклад Валютный", "service_name_en": "FX Deposit",
        "service_name_uz": "Valyuta omonati", "currency_code": "USD",
        "min_amount_text": "от 100 USD", "min_amount": 100,
        "term_text": "24 мес.", "term_months": 24, "rate_text": "6%", "rate_pct": 6.0,
        "open_channel_text": "В отделении", "payout_text": "В конце срока",
        "payout_monthly_available": False, "payout_end_available": True,
        "topup_text": "Непополняемый", "topup_allowed": False,
        "partial_withdrawal_allowed": False, "notes_text": "",
    },
]

CARD_OFFERS: list[dict[str, Any]] = [
    {
        "service_name": "Карта UzCard", "service_name_en": "UzCard", "service_name_uz": "UzCard",
        "card_network": "UZCARD", "currency_code": "UZS", "is_fx_card": False,
        "is_debit_card": True, "payroll_supported": True, "issue_fee_text": "Бесплатно",
        "issue_fee_free": True, "reissue_fee_text": "20 000 UZS",
        "transfer_fee_text": "0,5%", "cashback_pct": 1.0, "cashback_text": "1%",
        "annual_fee_text": "Бесплатно", "validity_text": "3 года",
        "delivery_available": True, "mobile_order_available": True, "pickup_available": True,
        "issuance_time_text": "1 день",
    },
    {
        "service_name": "Карта Visa Gold", "service_name_en": "Visa Gold",
        "service_name_uz": "Visa Gold", "card_network": "VISA", "currency_code": "USD",
        "is_fx_card": True, "is_debit_card": True, "payroll_supported": False,
        "issue_fee_text": "100 000 UZS", "issue_fee_free": False,
        "reissue_fee_text": "100 000 UZS", "transfer_fee_text": "1%",
        "cashback_pct": None, "cashback_text": "", "annual_fee_text": "150 000 UZS",
        "validity_text": "3 года", "delivery_available": True,
        "mobile_order_available": True, "pickup_available": True,
        "issuance_time_text": "3 дня",
    },
]

# Fixture FAQ rows, one per language like the real `faq` table (which stores
# question_ru/en/uz + answer_ru/en/uz). `triggers` is offline-only metadata,
# stripped before the rows reach production code: the real search is a
# Weaviate hybrid query plus an LLM rerank, and the closest honest offline
# stand-in is an explicit trigger list. A loose substring match was tried
# first and was actively harmful — "кредита" inside "автокредиты" made a
# product request look like an FAQ hit, exactly the confusion the production
# rerank's BOUNDARY rule exists to prevent.
FAQ_ITEMS: list[dict[str, Any]] = [
    {
        "triggers": ["эскроу", "eskrou", "escrow"],
        "question": {
            "ru": "Что такое эскроу-счёт?",
            "en": "What is an escrow account?",
            "uz": "Eskrou hisob nima?",
        },
        "answer": {
            "ru": "Эскроу-счёт — это специальный счёт, на котором деньги покупателя "
                  "жилья хранятся до сдачи дома в эксплуатацию. Банк выдаёт средства "
                  "застройщику только после выполнения условий договора.",
            "en": "An escrow account is a special account where the home buyer's money "
                  "is held until the building is commissioned. The bank releases the "
                  "funds to the developer only after the contract terms are met.",
            "uz": "Eskrou hisob — bu uy sotib oluvchining puli uy foydalanishga "
                  "topshirilgunga qadar saqlanadigan maxsus hisob. Bank mablag'ni "
                  "quruvchiga faqat shartnoma shartlari bajarilgach beradi.",
        },
    },
    {
        "triggers": ["заблокировать карт", "блокировка карт", "kartani qanday bloklash",
                     "block the card", "block my card"],
        "question": {
            "ru": "Как заблокировать карту?",
            "en": "How do I block my card?",
            "uz": "Kartani qanday bloklash mumkin?",
        },
        "answer": {
            "ru": "Карту можно заблокировать в мобильном приложении Asaka 24/7, "
                  "по телефону контакт-центра 1148 или в любом отделении банка.",
            "en": "You can block the card in the Asaka 24/7 mobile app, by calling the "
                  "contact centre at 1148, or at any bank branch.",
            "uz": "Kartani Asaka 24/7 mobil ilovasida, 1148 raqamli aloqa markazi orqali "
                  "yoki istalgan bank bo'limida bloklash mumkin.",
        },
    },
    {
        "triggers": ["документы", "hujjatlar", "documents"],
        "question": {
            "ru": "Какие документы нужны для кредита?",
            "en": "What documents are needed for a loan?",
            "uz": "Kredit uchun qanday hujjatlar kerak?",
        },
        "answer": {
            "ru": "Для оформления кредита нужны паспорт, справка о доходах за последние "
                  "6 месяцев и заявление-анкета. По отдельным продуктам может "
                  "потребоваться справка с места работы.",
            "en": "To apply for a loan you need a passport, an income statement for the "
                  "last 6 months and an application form. Some products may also require "
                  "a letter from your employer.",
            "uz": "Kredit rasmiylashtirish uchun pasport, so'nggi 6 oylik daromad "
                  "ma'lumotnomasi va ariza-anketa kerak. Ayrim mahsulotlar bo'yicha ish "
                  "joyidan ma'lumotnoma ham talab qilinishi mumkin.",
        },
    },
]

CBU_RATES: list[dict[str, Any]] = [
    {"code": "USD", "name_ru": "Доллар США", "name_en": "US Dollar", "name_uz": "AQSH dollari",
     "nominal": "1", "rate": "12 690.15", "diff": "5.20", "icon": "🇺🇸", "date": "2026-09-04"},
    {"code": "EUR", "name_ru": "Евро", "name_en": "Euro", "name_uz": "Evro",
     "nominal": "1", "rate": "13 850.40", "diff": "-3.10", "icon": "🇪🇺", "date": "2026-09-04"},
]


def _build_offices() -> list[Any]:
    """In-memory Filial rows. Real ORM instances (never added to a session) so
    every downstream formatter — `format_branch_card`, `office_public_dict` —
    runs its production code path against production attribute names."""
    from app.db.models import Filial

    return [
        Filial(
            id=1, name_ru="ЦБУ Юнусабад", name_uz="Yunusobod BXM",
            address_ru="г. Ташкент, ул. Амира Темура, 1",
            address_uz="Toshkent sh., Amir Temur ko'chasi, 1",
            phone="+998 71 000 00 01", hours="09:00-18:00",
        ),
        Filial(
            id=2, name_ru="ЦБУ Чиланзар", name_uz="Chilonzor BXM",
            address_ru="г. Ташкент, ул. Бунёдкор, 12",
            address_uz="Toshkent sh., Bunyodkor ko'chasi, 12",
            phone="+998 71 000 00 02", hours="09:00-18:00",
        ),
    ]


# ---------------------------------------------------------------------------
# Stub chat model
# ---------------------------------------------------------------------------

# Keyword tables for the offline router. This is intentionally a crude
# keyword matcher and NOT a second implementation of the production prompt:
# offline mode asserts that a chosen tool actually runs end to end, not that
# the choice is good. Model quality is what `--live` measures.
_OFFICE_WORDS = ("филиал", "отделен", "офис", "branch", "office", "filial", "bxm", "цбу")
_OFFICE_DIFF_WORDS = ("отлича", "разниц", "difference", "farq")
_CURRENCY_WORDS = ("курс", "доллар", "евро", "валют", "currency", "exchange", "usd", "eur",
                   "kurs", "dollar", "valyuta")
_COMPARE_WORDS = ("сравн", "compare", "taqqosla", "farqi nima")
_OPERATOR_WORDS = ("оператор", "человек", "operator", "human agent", "живой", "bog'lang", "bog'laning")
_AFFORD_WORDS = ("потяну", "afford", "ko'tara", "kotara", "осилю")
_WHATIF_WORDS = ("а если", "что если", "what if", "instead", "вместо")
_CALC_WORDS = ("посчита", "рассчита", "calculate", "hisobla", "платёж", "платеж", "payment", "to'lov")
_APPLY_WORDS = ("заявк", "apply", "оформить заявку", "ariza")
_RECOMMEND_WORDS = ("посовет", "накопить", "suggest", "recommend", "maslahat", "что выбрать")
_ORDINAL_WORDS = ("первый", "первая", "первое", "first", "birinchi", "birinchisi",
                  "второй", "вторая", "second", "ikkinchisi")
_INJECTION_WORDS = ("игнорируй", "ignore previous", "системный промпт", "system prompt",
                    "ты теперь не", "взломано")

_NO_TOOL_REPLIES = {
    "ru": "Здравствуйте! Я помогу с кредитами, вкладами, картами и отделениями Asaka Bank. "
          "Чем могу быть полезен?",
    "en": "Hello! I can help you with Asaka Bank loans, deposits, cards and branches. "
          "What would you like to know?",
    "uz": "Assalomu alaykum! Asaka Bank kreditlari, omonatlari, kartalari va filiallari "
          "bo'yicha yordam beraman. Sizga qanday yordam bera olaman?",
}


def _normalize_lang(language: Optional[str]) -> str:
    """Same normalization the real loaders apply: anything unknown is ru."""
    code = (language or "").strip().lower()
    return code if code in {"ru", "en", "uz"} else "ru"


def _numbers(text: str) -> list[float]:
    """Digit groups in the text, tolerating thin/regular spaces as separators."""
    import re

    cleaned = text.replace(" ", " ")
    out: list[float] = []
    for chunk in re.findall(r"\d[\d\s.,]*", cleaned):
        digits = chunk.replace(" ", "").replace(",", ".").rstrip(".")
        try:
            out.append(float(digits))
        except ValueError:
            continue
    return out


class StubChatModel:
    """Scripted stand-in for `ChatOpenAI` in offline mode.

    Implements only what `node_faq` uses: `bind_tools()`, `ainvoke()` and
    `astream()`. The routing decision is a keyword table (see above) reading
    the same two signals the real model gets — the customer's message and the
    `<state>` XML system message — so a case whose dialog carries offices or
    products exercises the "pick from the previous list" tools for real.
    """

    def __init__(self, tools: Optional[list] = None) -> None:
        self._tool_names = {getattr(t, "name", str(t)) for t in (tools or [])}

    # -- LangChain surface -------------------------------------------------
    def bind_tools(self, tools: list, **_kwargs: Any) -> "StubChatModel":
        return StubChatModel(tools)

    async def ainvoke(self, messages: list, *_args: Any, **_kwargs: Any) -> AIMessage:
        return self._respond(messages)

    async def astream(self, messages: list, *_args: Any, **_kwargs: Any):
        yield self._respond(messages)

    # -- routing -----------------------------------------------------------
    def _respond(self, messages: list) -> AIMessage:
        user_text, state_xml, lang, after_tool = _read_messages(messages)
        if after_tool:
            # A tool already ran this turn: end the loop with a short wrapper.
            # node_faq's display short-circuit usually breaks before we get
            # here; this covers faq_lookup sentinels.
            return AIMessage(content=_NO_TOOL_REPLIES.get(lang, _NO_TOOL_REPLIES["ru"]))

        decision = self._route(user_text, state_xml, lang)
        if decision is None:
            return AIMessage(content=_NO_TOOL_REPLIES.get(lang, _NO_TOOL_REPLIES["ru"]))
        name, args = decision
        if self._tool_names and name not in self._tool_names:
            # The node stripped this tool for the rest of the turn (clarify
            # anti-loop guard) — answer directly instead of calling a tool
            # that is no longer bound.
            return AIMessage(content=_NO_TOOL_REPLIES.get(lang, _NO_TOOL_REPLIES["ru"]))
        return AIMessage(
            content="",
            tool_calls=[{"name": name, "args": args, "id": f"call_{name}", "type": "tool_call"}],
        )

    def _route(self, text: str, state_xml: str, lang: str) -> Optional[tuple[str, dict]]:
        from app.agent.intent import _detect_product_category, _is_greeting, _is_thanks

        low = (text or "").strip().lower()
        if not low:
            return None
        has_offices = "<offices>" in state_xml
        has_products = "<products>" in state_xml
        has_selected = "<selected_product>" in state_xml

        if any(w in low for w in _INJECTION_WORDS):
            return None
        # A greeting only ends the turn when that is ALL the message is: real
        # customers open with "Salom, I need ипотека", and treating that as a
        # bare hello would silently drop the actual request.
        if len(low.split()) <= 3 and (_is_greeting(text) or _is_thanks(text)):
            return None
        if has_offices and (low.isdigit() or any(low.startswith(w) for w in _ORDINAL_WORDS)):
            return "select_office", {"office_name": text.strip()}
        if any(w in low for w in _OFFICE_DIFF_WORDS) and any(w in low for w in _OFFICE_WORDS):
            return "get_office_types_info", {}
        if any(w in low for w in _CURRENCY_WORDS):
            return "get_currency_info", {}
        if any(w in low for w in _OFFICE_WORDS):
            return "find_office", {"office_type": "filial", "query": ""}
        if any(w in low for w in _OPERATOR_WORDS):
            return "request_operator", {"reason": text[:120]}
        if any(w in low for w in _AFFORD_WORDS):
            return "affordability_check", {"monthly_payment": _first_amount(low) or 1_000_000.0}
        if has_selected and any(w in low for w in _WHATIF_WORDS):
            return "what_if_scenario", {"term_months": _term_months(low) or 60}
        if has_products and any(w in low for w in _COMPARE_WORDS):
            return "compare_products", {"product_names": _products_from_state(state_xml)[:2]}
        if has_products and _product_name_in(text, state_xml):
            return "select_product", {"product_name": text.strip()}
        if has_selected and any(w in low for w in _APPLY_WORDS):
            return "start_calculator", {}
        if any(w in low for w in _CALC_WORDS) and len(_numbers(low)) >= 2:
            # Argument names must match the real tool signature exactly
            # (`term_months`, no `rate` — the tool refuses to take a rate from
            # the model): a mismatch is a ToolNode validation error, which the
            # node hides from the user and the harness would report as a
            # mysterious missing answer.
            return "custom_loan_calculator", {
                "amount": _first_amount(low) or 0.0,
                "term_months": _term_months(low) or 12,
                "downpayment": 0.0,
            }
        if any(w in low for w in _RECOMMEND_WORDS):
            return "recommend_product", {"goal": text[:200]}
        category = _detect_product_category(text)
        if category == "credit_menu":
            return "show_credit_menu", {}
        if category:
            return "get_products", {"category": category}
        return "faq_lookup", {"query": text[:200]}


class StubExtractorModel:
    """Scripted stand-in for the "extractor" role (`app/agent/calc_extractor.py`).

    Needed because the calculator has NO regex fallback: when the extractor
    LLM is unavailable every answer comes back ``{"type": "unparsed"}`` and
    `node_calc_flow` can never accept a number. Returning ``None`` for this
    role would therefore make the calculator flow untestable offline, so the
    stub emits the same strict JSON contract the real prompt asks for — a
    number when the message contains one, ``unparsed`` otherwise.
    """

    def bind_tools(self, tools: list, **_kwargs: Any) -> "StubExtractorModel":
        return self

    async def ainvoke(self, messages: list, *_args: Any, **_kwargs: Any) -> AIMessage:
        import json as _json

        user_text = ""
        for msg in messages:
            if isinstance(msg, HumanMessage):
                user_text = str(msg.content or "")
        value = _first_amount(user_text)
        if value is None:
            return AIMessage(content=_json.dumps({"type": "unparsed"}))
        return AIMessage(content=_json.dumps({"type": "value", "value": value}))

    async def astream(self, messages: list, *_args: Any, **_kwargs: Any):
        yield await self.ainvoke(messages)


_MULTIPLIERS = (
    (("млрд", "mlrd", "миллиард", "milliard"), 1_000_000_000),
    (("млн", "mln", "миллион", "million"), 1_000_000),
    (("тыс", "ming", "thousand"), 1_000),
)


_YEAR_WORDS = ("год", "лет", "year", "yil")


def _term_months(text: str) -> Optional[int]:
    """Term in months: the SECOND number in the text (the first is the amount),
    converted from years when the text says so."""
    numbers = _numbers(text)
    if len(numbers) < 2:
        value = numbers[0] if numbers else None
    else:
        value = numbers[1]
    if value is None:
        return None
    low = (text or "").lower()
    if any(word in low for word in _YEAR_WORDS):
        return int(value * 12)
    return int(value)


def _first_amount(text: str) -> Optional[float]:
    """First number in the text, applying a trailing млн/млрд-style multiplier."""
    numbers = _numbers(text)
    if not numbers:
        return None
    value = numbers[0]
    low = (text or "").lower()
    for words, factor in _MULTIPLIERS:
        if any(word in low for word in words):
            return value * factor
    return value


def _read_messages(messages: list) -> tuple[str, str, str, bool]:
    """Extract (last user text, concatenated system text, lang, tool_ran_already).

    The language is re-derived from the customer's own message with the
    production heuristic (`app.agent.lang_heuristic`) rather than parsed out
    of the system policy: the policy wording is not a stable contract, the
    heuristic is, and it is exactly what the language evaluator uses.
    """
    user_text = ""
    system_text: list[str] = []
    after_tool = False
    for msg in messages:
        if isinstance(msg, SystemMessage):
            system_text.append(str(msg.content or ""))
        elif isinstance(msg, HumanMessage):
            user_text = str(msg.content or "")
        elif isinstance(msg, ToolMessage):
            after_tool = True
    from app.agent.lang_heuristic import _classify

    joined = "\n".join(system_text)
    return user_text, joined, _classify(user_text) or "ru", after_tool


def _products_from_state(state_xml: str) -> list[str]:
    import re

    return re.findall(r"<product index=\"\d+\">([^<]*)</product>", state_xml)


def _product_name_in(text: str, state_xml: str) -> bool:
    low = (text or "").strip().lower()
    return any(low and (low in name.lower() or name.lower() in low)
               for name in _products_from_state(state_xml))


# ---------------------------------------------------------------------------
# Fake DB session (only `select_office` and lead saving need one)
# ---------------------------------------------------------------------------

class _FakeResult:
    def __init__(self, objects: list[Any]) -> None:
        self._objects = objects

    def scalar_one_or_none(self) -> Any:
        return self._objects[0] if self._objects else None

    def scalars(self) -> "_FakeResult":
        return self

    def all(self) -> list[Any]:
        return list(self._objects)

    def first(self) -> Any:
        return self._objects[0] if self._objects else None


class _FakeSession:
    """Answers every SELECT with the office fixtures and swallows writes.

    Crude on purpose: the only production code that reaches a raw session in
    an eval turn is `select_office`'s id lookup and the lead insert. Neither
    is what the harness measures — they just must not raise.
    """

    def __init__(self, offices: list[Any]) -> None:
        self._offices = offices

    async def execute(self, *_args: Any, **_kwargs: Any) -> _FakeResult:
        return _FakeResult(self._offices)

    def add(self, _obj: Any) -> None:
        return None

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None

    async def flush(self) -> None:
        return None


# ---------------------------------------------------------------------------
# Installing the doubles
# ---------------------------------------------------------------------------

def _bindings(name: str, package_prefix: str = "app.") -> list[str]:
    """Every imported module that currently holds an attribute called *name*.

    See the module docstring for why patching the defining module is not
    enough in this codebase.
    """
    return [
        mod_name
        for mod_name, module in list(sys.modules.items())
        if mod_name.startswith(package_prefix)
        and module is not None
        and hasattr(module, name)
    ]


def _patch_everywhere(stack: contextlib.ExitStack, name: str, replacement: Any) -> None:
    for mod_name in _bindings(name):
        stack.enter_context(patch(f"{mod_name}.{name}", replacement))


@contextlib.contextmanager
def offline_doubles() -> Iterator[None]:
    """Install every offline double for the duration of the block.

    Import the agent BEFORE entering (the runner does): the patch targets are
    discovered from `sys.modules`, so a module imported later would keep the
    real function.
    """
    # Imported here rather than at module import time: this module is also
    # imported by the pure-data path (tests of the fixtures), which must not
    # pay for loading the agent.
    from app.utils.faq_tools import FaqCandidate, FaqSearch

    # Force-import every module whose binding we need to replace. Several are
    # imported lazily INSIDE a function body (`get_currency_info` does
    # `from app.utils.cbu_rates import fetch_cbu_rates` per call, `select_office`
    # imports `get_session` per call), so at patch time they may not be in
    # `sys.modules` yet — and `_bindings` would then find nothing to patch,
    # silently letting a live HTTP/DB call through in "offline" mode.
    import app.agent.branches  # noqa: F401
    import app.db.models  # noqa: F401
    import app.db.session  # noqa: F401
    import app.utils.cbu_rates  # noqa: F401
    import app.utils.data_loaders  # noqa: F401
    import app.utils.pdf_generator  # noqa: F401

    offices = _build_offices()

    async def _fake_credit_offers() -> list[dict[str, Any]]:
        return [dict(row) for row in CREDIT_OFFERS]

    async def _fake_deposit_offers() -> list[dict[str, Any]]:
        return [dict(row) for row in DEPOSIT_OFFERS]

    async def _fake_card_offers() -> list[dict[str, Any]]:
        return [dict(row) for row in CARD_OFFERS]

    async def _fake_faq_items(language: str | None = None) -> list[dict[str, str]]:
        # Mirrors the real loader exactly: "q"/"a" keys, localized to the
        # requested language, falling back to ru. `triggers` is harness-only
        # metadata and never leaves this module.
        lang = _normalize_lang(language)
        return [{"q": row["question"][lang], "a": row["answer"][lang]} for row in FAQ_ITEMS]

    async def _fake_search_offices(query: str = "", office_types=None, limit: int = 5):
        return list(offices)[:limit]

    async def _fake_faq_search(query: str, language: str | None = None) -> FaqSearch:
        """Trigger-word match over the fixture FAQ, mirroring the real tiers.

        `strict` when a fixture row's trigger appears in the query, `none`
        otherwise — the same two outcomes the production rerank produces, so
        both node_faq's deterministic pre-check and the faq_lookup sentinel
        path stay exercised offline.
        """
        low = (query or "").lower()
        lang = _normalize_lang(language)
        for item in FAQ_ITEMS:
            if any(trigger in low for trigger in item["triggers"]):
                answer = item["answer"][lang]
                return FaqSearch(
                    answer=answer, tier="strict", score=0.9,
                    candidates=[FaqCandidate(item["question"][lang], answer, 0.9)],
                )
        return FaqSearch(answer=None, tier="none", score=0.0, candidates=[])

    async def _fake_cbu_rates(currencies=None):
        return [dict(row) for row in CBU_RATES]

    def _fake_pdf(*_args: Any, **_kwargs: Any) -> str:
        return "/tmp/eval-offline-schedule.pdf"

    async def _fake_rephrase(source_answer: str, _question: str, _lang: str) -> str:
        return source_answer

    async def _fake_load_profile(_user_id: int):
        return None

    async def _fake_detect_language(_text: str, fallback: str = "ru") -> str:
        return fallback

    def _fake_get_chat_openai(role: str = "consultant", **_kwargs: Any):
        # One stub per role, mirroring the production split: a tool-calling
        # model for the customer-facing consultant, a strict-JSON model for
        # the calculator's extractor.
        return StubExtractorModel() if role == "extractor" else StubChatModel()

    @contextlib.asynccontextmanager
    async def _fake_get_session():
        yield _FakeSession(offices)

    with contextlib.ExitStack() as stack:
        _patch_everywhere(stack, "_get_chat_openai", _fake_get_chat_openai)
        _patch_everywhere(stack, "_load_credit_product_offers", _fake_credit_offers)
        _patch_everywhere(stack, "_load_deposit_product_offers", _fake_deposit_offers)
        _patch_everywhere(stack, "_load_card_product_offers", _fake_card_offers)
        _patch_everywhere(stack, "_load_faq_items", _fake_faq_items)
        _patch_everywhere(stack, "search_offices", _fake_search_offices)
        _patch_everywhere(stack, "faq_search", _fake_faq_search)
        _patch_everywhere(stack, "fetch_cbu_rates", _fake_cbu_rates)
        _patch_everywhere(stack, "generate_amortization_pdf", _fake_pdf)
        _patch_everywhere(stack, "rephrase_faq_answer", _fake_rephrase)
        _patch_everywhere(stack, "load_user_profile", _fake_load_profile)
        _patch_everywhere(stack, "detect_language", _fake_detect_language)
        _patch_everywhere(stack, "get_session", _fake_get_session)
        yield
