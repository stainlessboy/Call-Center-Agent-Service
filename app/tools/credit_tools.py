from __future__ import annotations

from typing import Any, Optional

from app.tools.data_loaders import _load_credit_product_offers_sync


def _credit_offers_by_section(section_name: str) -> list[dict[str, Any]]:
    rows = [dict(item) for item in _load_credit_product_offers_sync() if item.get("section_name") == section_name]
    rows.sort(key=lambda x: (x.get("source_row_order", 0), x.get("rate_order", 0)))
    return rows


def _credit_program_names(section_name: str, limit: int = 8) -> list[str]:
    rows = _credit_offers_by_section(section_name)
    names: list[str] = []
    seen: set[str] = set()
    for item in rows:
        name = str(item.get("service_name") or "").strip()
        if not name:
            continue
        lower = name.lower()
        if lower in {"тип кредита"}:
            continue
        if lower in seen:
            continue
        seen.add(lower)
        names.append(name)
        if len(names) >= limit:
            break
    return names


def _all_credit_categories_overview() -> str:
    lines = [
        "Смотрите, у нас есть несколько кредитных направлений:",
        "• Потребительский кредит — на любые личные цели, оформляется в филиале",
        "• Автокредит — на покупку автомобиля",
        "• Ипотека — на покупку жилья или ремонт",
        "• Микрозайм — небольшая сумма на короткий срок",
        "• Образовательный кредит — на оплату контракта",
        "",
        "Что из этого вас интересует?",
    ]
    return "\n".join(lines)


def _fmt_rate_range(offer: dict[str, Any]) -> str:
    low = offer.get("rate_min_pct")
    high = offer.get("rate_max_pct")
    if low is None and high is None:
        raw = str(offer.get("rate_text") or "").strip()
        return raw or "уточняется"
    if low is not None and high is not None and abs(float(low) - float(high)) > 0.01:
        return f"{float(low):.1f}-{float(high):.1f}%"
    v = float(low if low is not None else high)
    return f"{v:.1f}%"


def _fmt_term_range(offer: dict[str, Any]) -> str:
    tmin = offer.get("term_min_months")
    tmax = offer.get("term_max_months")
    if tmin is None and tmax is None:
        raw = str(offer.get("term_text") or "").strip()
        return raw or "уточняется"
    if tmin is not None and tmax is not None and tmin != tmax:
        return f"{int(tmin)}-{int(tmax)} мес."
    t = int(tmin if tmin is not None else tmax)
    return f"{t} мес."


def _fmt_downpayment_range(offer: dict[str, Any]) -> str:
    dmin = offer.get("downpayment_min_pct")
    dmax = offer.get("downpayment_max_pct")
    if dmin is None and dmax is None:
        raw = str(offer.get("downpayment_text") or "").strip()
        return raw or "уточняется"
    if dmin is not None and dmax is not None and abs(float(dmin) - float(dmax)) > 0.01:
        return f"{float(dmin):.0f}-{float(dmax):.0f}%"
    d = float(dmin if dmin is not None else dmax)
    return f"{d:.0f}%"


def _normalize_income_type(text: str | None) -> Optional[str]:
    lower = (text or "").lower()
    if "зарплат" in lower:
        return "payroll"
    if "без официаль" in lower or "оборот" in lower:
        return "no_official"
    if "официаль" in lower:
        return "official"
    return None


def _offer_matches_amount(offer: dict[str, Any], amount: Optional[int]) -> bool:
    if amount is None:
        return True
    amount_min = offer.get("amount_min")
    amount_max = offer.get("amount_max")
    if amount_min is not None and amount < int(amount_min):
        return False
    if amount_max is not None and int(amount_max) > 0 and amount > int(amount_max):
        return False
    return True


def _offer_matches_term(offer: dict[str, Any], term_months: Optional[int]) -> bool:
    if term_months is None:
        return True
    tmin = offer.get("term_min_months")
    tmax = offer.get("term_max_months")
    if tmin is not None and term_months < int(tmin):
        return False
    if tmax is not None and int(tmax) > 0 and term_months > int(tmax):
        return False
    return True


def _offer_matches_downpayment(offer: dict[str, Any], down_pct: Optional[int]) -> bool:
    if down_pct is None:
        return True
    dmin = offer.get("downpayment_min_pct")
    dmax = offer.get("downpayment_max_pct")
    if dmin is not None and float(down_pct) < float(dmin):
        return False
    if dmax is not None and float(dmax) > 0 and float(down_pct) > float(dmax):
        return False
    return True


def _offer_matches_income(offer: dict[str, Any], income_type: Optional[str]) -> bool:
    if not income_type:
        return True
    row_income = offer.get("income_type")
    if not row_income:
        return True
    return str(row_income) == income_type


def _offer_matches_program_hint(offer: dict[str, Any], hint: Optional[str]) -> bool:
    if not hint:
        return True
    service_name = str(offer.get("service_name") or "").lower()
    hint_l = str(hint).lower()
    if "sonet" in hint_l:
        return "sonet" in service_name
    if "onix" in hint_l:
        return "onix" in service_name
    if "tracker" in hint_l:
        return "tracker" in service_name
    if "damas" in hint_l:
        return "damas" in service_name
    if "онлайн" in hint_l or "online" in hint_l:
        return "онлайн" in service_name
    if "2.5" in hint_l or "2,5" in hint_l:
        return "2.5" in service_name
    return True


def _distance_to_range(value: Optional[float], low: Optional[float], high: Optional[float]) -> float:
    if value is None:
        return 0.0
    if low is None and high is None:
        return 0.1
    if low is not None and value < float(low):
        base = max(1.0, abs(float(low)))
        return abs(float(low) - value) / base
    if high is not None and float(high) > 0 and value > float(high):
        base = max(1.0, abs(float(high)))
        return abs(value - float(high)) / base
    return 0.0


def _select_exact_auto_loan_offers(slots: dict[str, Any]) -> list[dict[str, Any]]:
    amount = slots.get("amount")
    term = slots.get("term_months")
    down = slots.get("downpayment_pct")
    income = slots.get("income_type_code")
    hint = slots.get("program_hint")
    offers = []
    for offer in _credit_offers_by_section("Автокредит"):
        if not _offer_matches_program_hint(offer, hint):
            continue
        if not _offer_matches_amount(offer, amount):
            continue
        if not _offer_matches_term(offer, term):
            continue
        if not _offer_matches_downpayment(offer, down):
            continue
        if not _offer_matches_income(offer, income):
            continue
        offers.append(offer)
    return offers


def _select_near_auto_loan_offers(slots: dict[str, Any], limit: int = 3) -> list[dict[str, Any]]:
    amount = slots.get("amount")
    term = slots.get("term_months")
    down = slots.get("downpayment_pct")
    income = slots.get("income_type_code")
    hint = slots.get("program_hint")
    scored: list[tuple[float, dict[str, Any]]] = []
    for offer in _credit_offers_by_section("Автокредит"):
        score = 0.0
        if hint:
            score += 0.0 if _offer_matches_program_hint(offer, hint) else 4.0
        if income:
            row_income = offer.get("income_type")
            if row_income and str(row_income) != income:
                score += 1.5
        score += _distance_to_range(float(amount), offer.get("amount_min"), offer.get("amount_max")) if amount else 0.0
        score += _distance_to_range(float(term), offer.get("term_min_months"), offer.get("term_max_months")) if term else 0.0
        score += _distance_to_range(float(down), offer.get("downpayment_min_pct"), offer.get("downpayment_max_pct")) if down else 0.0
        if not offer.get("rate_min_pct") and not offer.get("rate_max_pct") and not offer.get("rate_text"):
            score += 0.5
        scored.append((score, offer))
    scored.sort(key=lambda x: (x[0], x[1].get("source_row_order", 0), x[1].get("rate_order", 0)))
    result: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for score, offer in scored:
        key = (
            offer.get("service_name"),
            offer.get("income_type"),
            offer.get("term_min_months"),
            offer.get("term_max_months"),
            offer.get("downpayment_min_pct"),
            offer.get("downpayment_max_pct"),
        )
        if key in seen:
            continue
        seen.add(key)
        item = dict(offer)
        item["_near_score"] = round(float(score), 4)
        result.append(item)
        if len(result) >= limit:
            break
    return result


def _select_exact_mortgage_offers(slots: dict[str, Any]) -> list[dict[str, Any]]:
    amount = slots.get("amount")
    term = slots.get("term_months")
    down = slots.get("downpayment_pct")
    purpose_hint = str(slots.get("purpose_hint") or "").lower()
    offers = []
    for offer in _credit_offers_by_section("Ипотека"):
        purpose_text = str(offer.get("purpose_text") or "").lower()
        if purpose_hint:
            if "ремонт" in purpose_hint and "ремонт" not in purpose_text:
                continue
            if any(t in purpose_hint for t in ("вторич",)) and "вторич" not in purpose_text:
                continue
            if any(t in purpose_hint for t in ("первич", "новост")) and "первич" not in purpose_text:
                continue
        if not _offer_matches_amount(offer, amount):
            continue
        if not _offer_matches_term(offer, term):
            continue
        if not _offer_matches_downpayment(offer, down):
            continue
        offers.append(offer)
    return offers


def _select_near_mortgage_offers(slots: dict[str, Any], limit: int = 3) -> list[dict[str, Any]]:
    amount = slots.get("amount")
    term = slots.get("term_months")
    down = slots.get("downpayment_pct")
    purpose_hint = str(slots.get("purpose_hint") or "").lower()
    scored: list[tuple[float, dict[str, Any]]] = []
    for offer in _credit_offers_by_section("Ипотека"):
        purpose_text = str(offer.get("purpose_text") or "").lower()
        score = 0.0
        if purpose_hint:
            if "ремонт" in purpose_hint and "ремонт" not in purpose_text:
                score += 2.0
            if "вторич" in purpose_hint and "вторич" not in purpose_text:
                score += 2.0
            if any(t in purpose_hint for t in ("первич", "новост")) and "первич" not in purpose_text:
                score += 2.0
        score += _distance_to_range(float(amount), offer.get("amount_min"), offer.get("amount_max")) if amount else 0.0
        score += _distance_to_range(float(term), offer.get("term_min_months"), offer.get("term_max_months")) if term else 0.0
        score += _distance_to_range(float(down), offer.get("downpayment_min_pct"), offer.get("downpayment_max_pct")) if down else 0.0
        scored.append((score, offer))
    scored.sort(key=lambda x: (x[0], x[1].get("source_row_order", 0), x[1].get("rate_order", 0)))
    return [dict(offer) for _, offer in scored[:limit]]


def _select_exact_microloan_offers(slots: dict[str, Any]) -> list[dict[str, Any]]:
    amount = slots.get("amount")
    term = slots.get("term_months")
    purpose_hint = str(slots.get("purpose_hint") or "").lower()
    offers = []
    for offer in _credit_offers_by_section("Микрозайм"):
        purpose_text = f"{offer.get('service_name', '')} {offer.get('purpose_text', '')}".lower()
        if purpose_hint:
            if "бизнес" in purpose_hint and not any(t in purpose_text for t in ("бизнес", "предприним", "самозан")):
                continue
            if any(t in purpose_hint for t in ("личн", "для себя")) and any(t in purpose_text for t in ("бизнес", "предприним")):
                continue
        if not _offer_matches_amount(offer, amount):
            continue
        if not _offer_matches_term(offer, term):
            continue
        offers.append(offer)
    return offers


def _select_near_microloan_offers(slots: dict[str, Any], limit: int = 3) -> list[dict[str, Any]]:
    amount = slots.get("amount")
    term = slots.get("term_months")
    purpose_hint = str(slots.get("purpose_hint") or "").lower()
    scored: list[tuple[float, dict[str, Any]]] = []
    for offer in _credit_offers_by_section("Микрозайм"):
        purpose_text = f"{offer.get('service_name', '')} {offer.get('purpose_text', '')}".lower()
        score = 0.0
        if purpose_hint:
            if "бизнес" in purpose_hint and not any(t in purpose_text for t in ("бизнес", "предприним", "самозан")):
                score += 2.0
            if any(t in purpose_hint for t in ("личн", "для себя")) and any(t in purpose_text for t in ("бизнес", "предприним")):
                score += 1.5
        score += _distance_to_range(float(amount), offer.get("amount_min"), offer.get("amount_max")) if amount else 0.0
        score += _distance_to_range(float(term), offer.get("term_min_months"), offer.get("term_max_months")) if term else 0.0
        scored.append((score, offer))
    scored.sort(key=lambda x: (x[0], x[1].get("source_row_order", 0), x[1].get("rate_order", 0)))
    return [dict(offer) for _, offer in scored[:limit]]


def _format_exact_credit_offers_reply(title: str, offers: list[dict[str, Any]], limit: int = 3) -> str:
    if not offers:
        return (
            f"По указанным параметрам точных совпадений для {title.lower()} не найдено. "
            "Могу помочь скорректировать параметры или передать запрос специалисту."
        )
    lines = [f"Подобрал {len(offers[:limit])} вариант{'а' if len(offers[:limit]) > 1 else ''} по {title.lower()}:"]
    for idx, offer in enumerate(offers[:limit], start=1):
        parts = [f"{idx}) {offer.get('service_name') or 'Программа'}"]
        parts.append(f"ставка: {_fmt_rate_range(offer)}")
        parts.append(f"срок: {_fmt_term_range(offer)}")
        down = _fmt_downpayment_range(offer)
        if down and down != "уточняется":
            parts.append(f"взнос: {down}")
        amount_text = str(offer.get('amount_text') or "").strip()
        if amount_text:
            parts.append(f"сумма: {amount_text}")
        lines.append(" — ".join(parts))
    return "\n".join(lines)


def _format_near_credit_offers_reply(title: str, offers: list[dict[str, Any]], limit: int = 3) -> str:
    if not offers:
        names = _credit_program_names(title, limit=4)
        if names:
            return (
                f"Точного совпадения по вашим параметрам для {title.lower()} не нашлось. "
                f"Доступные программы: {', '.join(names)}. "
                "Специалист банка поможет подобрать подходящий вариант."
            )
        return (
            f"По вашим параметрам подходящих программ {title.lower()} сейчас нет в базе. "
            "Специалист банка подберёт актуальный вариант — оставьте номер телефона или спросите ближайший филиал."
        )
    lines = [f"Точного совпадения по вашим параметрам нет, но вот ближайшие варианты по {title.lower()}:"]
    for idx, offer in enumerate(offers[:limit], start=1):
        parts = [f"{idx}) {offer.get('service_name') or 'Программа'}"]
        parts.append(f"ставка: {_fmt_rate_range(offer)}")
        parts.append(f"срок: {_fmt_term_range(offer)}")
        down = _fmt_downpayment_range(offer)
        if down and down != "уточняется":
            parts.append(f"взнос: {down}")
        amount_text = str(offer.get("amount_text") or "").strip()
        if amount_text:
            parts.append(f"сумма: {amount_text}")
        lines.append(" — ".join(parts))
    lines.append("Могу помочь скорректировать параметры для более точного подбора.")
    return "\n".join(lines)
