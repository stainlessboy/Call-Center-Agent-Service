from __future__ import annotations

import asyncio
import difflib
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Optional

from sqlalchemy import select

from app.db.models import CardProductOffer, CreditProductOffer, DepositProductOffer
from app.db.session import get_session


@dataclass(frozen=True)
class CatalogProduct:
    category: str
    product_family: str
    section_name: str
    product_name: str
    search_text: str
    tags: tuple[str, ...]


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _normalize_text(text: str) -> str:
    lowered = (text or "").lower()
    lowered = re.sub(r"[^\w\s]+", " ", lowered, flags=re.UNICODE)
    return re.sub(r"\s+", " ", lowered).strip()


def _token_stem(token: str) -> str:
    token = token.strip()
    if len(token) <= 3:
        return token
    for suffix in (
        "ами",
        "ями",
        "ого",
        "ому",
        "ему",
        "ыми",
        "ими",
        "иях",
        "ах",
        "ях",
        "ов",
        "ев",
        "ей",
        "ой",
        "ый",
        "ий",
        "ая",
        "ое",
        "ые",
        "ую",
        "ам",
        "ям",
        "ом",
        "ем",
        "а",
        "я",
        "у",
        "ю",
        "е",
        "ы",
        "и",
    ):
        if len(token) > 4 and token.endswith(suffix):
            return token[: -len(suffix)]
    return token


def _token_set(text: str) -> set[str]:
    return {t for t in (_token_stem(x) for x in _normalize_text(text).split()) if t}


async def _load_catalog_products() -> list[CatalogProduct]:
    async with get_session() as session:
        credit_res = await session.execute(
            select(
                CreditProductOffer.section_name,
                CreditProductOffer.service_name,
                CreditProductOffer.purpose_text,
                CreditProductOffer.amount_text,
                CreditProductOffer.term_text,
                CreditProductOffer.rate_text,
                CreditProductOffer.collateral_text,
            ).where(CreditProductOffer.is_active.is_(True))
        )
        deposit_res = await session.execute(
            select(
                DepositProductOffer.service_name,
                DepositProductOffer.currency_code,
                DepositProductOffer.term_text,
                DepositProductOffer.rate_text,
                DepositProductOffer.payout_text,
                DepositProductOffer.topup_text,
                DepositProductOffer.open_channel_text,
            ).where(DepositProductOffer.is_active.is_(True))
        )
        card_res = await session.execute(
            select(
                CardProductOffer.service_name,
                CardProductOffer.card_network,
                CardProductOffer.currency_code,
                CardProductOffer.issue_fee_text,
                CardProductOffer.annual_fee_text,
                CardProductOffer.issuance_time_text,
            ).where(CardProductOffer.is_active.is_(True))
        )
        credit_rows = credit_res.all()
        deposit_rows = deposit_res.all()
        card_rows = card_res.all()

    products: list[CatalogProduct] = []
    for section_name, service_name, purpose_text, amount_text, term_text, rate_text, collateral_text in credit_rows:
        section = _clean_text(section_name)
        service = _clean_text(service_name)
        text = " | ".join(
            x for x in [
                service,
                _clean_text(purpose_text),
                _clean_text(amount_text),
                _clean_text(term_text),
                _clean_text(rate_text),
                _clean_text(collateral_text),
            ] if x
        )
        if not service:
            continue
        family = _infer_family("credit_products", section, text)
        tags = tuple(sorted(_infer_tags("credit_products", section, text)))
        products.append(
            CatalogProduct(
                category="credit",
                product_family=family,
                section_name=section,
                product_name=service,
                search_text=text,
                tags=tags,
            )
        )
    for service_name, currency_code, term_text, rate_text, payout_text, topup_text, open_channel_text in deposit_rows:
        service = _clean_text(service_name)
        section = "Вклады"
        text = " | ".join(
            x for x in [
                service,
                _clean_text(currency_code),
                _clean_text(term_text),
                _clean_text(rate_text),
                _clean_text(payout_text),
                _clean_text(topup_text),
                _clean_text(open_channel_text),
            ] if x
        )
        if not service:
            continue
        family = _infer_family("noncredit_products", section, text)
        tags = tuple(sorted(_infer_tags("noncredit_products", section, text)))
        products.append(
            CatalogProduct(
                category="noncredit",
                product_family=family,
                section_name=section,
                product_name=service,
                search_text=text,
                tags=tags,
            )
        )
    for service_name, card_network, currency_code, issue_fee_text, annual_fee_text, issuance_time_text in card_rows:
        service = _clean_text(service_name)
        section = "Карты"
        text = " | ".join(
            x for x in [
                service,
                _clean_text(card_network),
                _clean_text(currency_code),
                _clean_text(issue_fee_text),
                _clean_text(annual_fee_text),
                _clean_text(issuance_time_text),
            ] if x
        )
        if not service:
            continue
        family = _infer_family("noncredit_products", section, text)
        tags = tuple(sorted(_infer_tags("noncredit_products", section, text)))
        products.append(
            CatalogProduct(
                category="noncredit",
                product_family=family,
                section_name=section,
                product_name=service,
                search_text=text,
                tags=tags,
            )
        )
    return products


def _load_catalog_sync_uncached() -> list[CatalogProduct]:
    try:
        return asyncio.run(_load_catalog_products())
    except Exception:
        return []


@lru_cache(maxsize=1)
def _load_catalog_sync() -> tuple[CatalogProduct, ...]:
    return tuple(_load_catalog_sync_uncached())


def _infer_family(sheet_key: str, section_name: str, text: str) -> str:
    section = section_name.lower()
    lower = text.lower()
    if sheet_key == "credit_products":
        if "ипот" in section:
            return "mortgage"
        if "авто" in section:
            return "auto_loan"
        if "микро" in section:
            return "microloan"
        if "образов" in section:
            return "education_loan"
        return "credit_other"
    if "вклад" in section:
        return "deposit"
    if "карт" in section:
        if any(t in lower for t in ("visa", "mastercard", "доллар", "евро", "usd", "eur", "валют")):
            return "fx_card"
        if any(t in lower for t in ("зарплат", "пенси", "стипенд")):
            return "payroll_card"
        return "debit_card"
    return "noncredit_other"


def _infer_tags(sheet_key: str, section_name: str, text: str) -> set[str]:
    lower = text.lower()
    tags = {
        "credit" if sheet_key == "credit_products" else "noncredit",
        _infer_family(sheet_key, section_name, lower),
    }
    if any(t in lower for t in ("мобиль", "прилож", "asakabank")):
        tags.add("mobile_app_channel")
    if any(t in lower for t in ("цбу", "филиал", "отделен")):
        tags.add("branch_channel")
    if "visa" in lower:
        tags.add("visa")
    if "mastercard" in lower:
        tags.add("mastercard")
    if "доллар" in lower or "usd" in lower:
        tags.add("usd")
    if "евро" in lower or "eur" in lower:
        tags.add("eur")
    if "ежемесяч" in lower:
        tags.add("monthly_interest")
    return tags


def _score_product(item: CatalogProduct, query: str, families: set[str], preferred_tags: set[str]) -> float:
    if families and item.product_family not in families:
        return -1.0
    q = _normalize_text(query)
    s = _normalize_text(item.search_text)
    q_tokens = _token_set(q)
    s_tokens = _token_set(s)
    overlap = (len(q_tokens & s_tokens) / max(1, len(q_tokens))) if q_tokens else 0.0
    seq = difflib.SequenceMatcher(a=q, b=s).ratio() if q and s else 0.0
    tag_bonus = 0.0
    if preferred_tags:
        item_tags = set(item.tags)
        tag_bonus = 0.2 * len(item_tags & preferred_tags)
    return max(overlap, seq * 0.5) + tag_bonus


def _pick_product(
    query: str,
    families: set[str],
    preferred_tags: Optional[set[str]] = None,
    exclude_names: Optional[set[str]] = None,
) -> Optional[CatalogProduct]:
    preferred_tags = preferred_tags or set()
    exclude_names = {n.lower() for n in (exclude_names or set())}
    best: Optional[CatalogProduct] = None
    best_score = -1.0
    for item in _load_catalog_sync():
        if not item.product_name:
            continue
        if item.product_name.lower() in exclude_names:
            continue
        score = _score_product(item, query, families, preferred_tags)
        if score > best_score:
            best = item
            best_score = score
    if best_score < 0.05 and not best:
        return None
    return best


def _has_existing_cross_sell(reply: str) -> bool:
    lower = (reply or "").lower()
    markers = ("также рекоменд", "советую также", "хотите, подскажу, как удобнее оформить")
    return any(m in lower for m in markers)


def _is_deposit_scenario(text: str) -> bool:
    lower = text.lower()
    return any(k in lower for k in ("вклад", "депозит", "накоп", "процент на вклад"))


def _is_transfer_scenario(text: str) -> bool:
    lower = text.lower()
    return any(k in lower for k in ("перевод", "moneygram", "western union", "золотая корона", "contact"))


def _is_mobile_app_scenario(text: str) -> bool:
    lower = text.lower()
    return any(k in lower for k in ("мобильное приложение", "приложение банка", "asakabank app", "приложении"))


def _is_card_scenario(text: str) -> bool:
    lower = text.lower()
    return any(k in lower for k in ("карта", "visa", "mastercard", "uzcard", "humo"))


def _is_fx_card_scenario(text: str) -> bool:
    lower = text.lower()
    return _is_card_scenario(text) and any(k in lower for k in ("валют", "за границ", "visa", "mastercard", "usd", "eur"))


def _is_general_products_scenario(text: str) -> bool:
    lower = text.lower()
    return any(k in lower for k in ("какие услуги", "какие продукты", "что есть", "какие есть продукты", "расскажите обо всех"))


def _format_name(item: Optional[CatalogProduct], fallback: str) -> str:
    return item.product_name if item and item.product_name else fallback


def build_cross_sell_appendix(user_text: str, base_reply: str, flow: Optional[str] = None) -> Optional[str]:
    text = (user_text or "").strip()
    if not text or _has_existing_cross_sell(base_reply):
        return None

    lower = text.lower()

    if flow in {"mortgage", "auto_loan", "microloan", "education"} or ("кредит" in lower and "услов" in lower):
        card = _pick_product(text, {"debit_card", "payroll_card", "fx_card"}, {"mobile_app_channel"})
        card_name = _format_name(card, "дебетовую карту")
        return (
            f"Также рекомендую оформить {card_name}: на карту удобно получить средства и погашать кредит без лишних комиссий через мобильное приложение. "
            "Хотите, подскажу, как удобнее оформить: через филиал или сначала получить подробную консультацию?"
        )

    if _is_deposit_scenario(text):
        card = _pick_product(text, {"debit_card", "payroll_card"}, {"mobile_app_channel"})
        card_name = _format_name(card, "бесплатную карту")
        return (
            f"Чтобы удобно пополнять вклад онлайн и контролировать начисления, советую также оформить {card_name}. "
            "Через мобильное приложение банка можно пополнять вклад и управлять им без визита в отделение. Хотите оформить вклад сейчас?"
        )

    if _is_transfer_scenario(text):
        card = _pick_product(text, {"debit_card", "payroll_card", "fx_card"}, {"transfers", "mobile_app_channel"})
        card_name = _format_name(card, "карту банка")
        return (
            f"Также вы можете делать такие переводы самостоятельно через мобильное приложение, если оформить {card_name}. "
            "Хотите, подскажу, как оформить карту и выполнять переводы онлайн?"
        )

    if _is_mobile_app_scenario(text):
        card = _pick_product(text, {"debit_card", "payroll_card"}, {"mobile_app_channel"})
        deposit = _pick_product(text, {"deposit"}, {"mobile_app_channel"}, exclude_names={_format_name(card, "")})
        card_name = _format_name(card, "дебетовую карту")
        deposit_name = _format_name(deposit, "вклад")
        return (
            f"Через приложение вы также сможете оформить {card_name} и открыть {deposit_name} для накопления средств. "
            "Хотите, покажу, с чего начать: с карты, вклада или переводов?"
        )

    if _is_fx_card_scenario(text):
        card = _pick_product(text, {"fx_card"}, {"visa", "mastercard", "usd", "eur", "mobile_app_channel"})
        card_name = _format_name(card, "валютную карту")
        return (
            f"Для удобного управления валютой и конвертацией рекомендую также использовать мобильное приложение вместе с {card_name}. "
            "Хотите, подскажу, как оформить карту и выбрать филиал для получения?"
        )

    if _is_card_scenario(text):
        deposit = _pick_product(text, {"deposit"}, {"mobile_app_channel"})
        deposit_name = _format_name(deposit, "вклад")
        return (
            f"Также через мобильное приложение с картой можно открыть {deposit_name} и получать доход на свободные средства. "
            "Подсказать, как удобнее оформить карту?"
        )

    if _is_general_products_scenario(text):
        deposit = _pick_product(text, {"deposit"}, {"mobile_app_channel"})
        card = _pick_product(text, {"debit_card", "payroll_card", "fx_card"}, {"mobile_app_channel"})
        return (
            "Могу помочь выбрать продукт под вашу цель: накопление, карта, кредит или переводы. "
            f"Например, для накопления подойдёт {_format_name(deposit, 'вклад')}, а для переводов и ежедневных оплат — {_format_name(card, 'дебетовая карта')}. "
            "Что для вас сейчас актуальнее?"
        )

    return None


def append_cross_sell_to_reply(user_text: str, base_reply: str, flow: Optional[str] = None) -> str:
    appendix = build_cross_sell_appendix(user_text=user_text, base_reply=base_reply, flow=flow)
    if not appendix:
        return base_reply
    if not (base_reply or "").strip():
        return appendix
    return f"{base_reply}\n\n{appendix}"
