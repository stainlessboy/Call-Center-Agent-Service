from __future__ import annotations

import re
from typing import Any, Optional

from app.tools.data_loaders import _load_deposit_product_offers_sync, _fmt_pct


def _select_deposit_options(slots: dict[str, Any], limit: int = 3) -> list[dict[str, Any]]:
    deposits = [dict(x) for x in _load_deposit_product_offers_sync() if str(x.get("currency_code") or "").upper() == "UZS"]
    goal = slots.get("goal")
    desired_term = slots.get("term_months")
    payout_pref = slots.get("payout_pref")
    topup_needed = slots.get("topup_needed")
    scored: list[tuple[float, dict[str, Any]]] = []
    for row in deposits:
        dep = {
            "name": str(row.get("service_name") or "Вклад «X»"),
            "term": str(row.get("term_text") or "срок уточняется"),
            "rate": _fmt_pct(row.get("rate_pct")) or str(row.get("rate_text") or "ставка уточняется"),
            "topup": "доступно" if row.get("topup_allowed") is True else ("нет" if row.get("topup_allowed") is False else (str(row.get("topup_text") or "") or "уточняется")),
            "payout": "ежемесячно" if row.get("payout_monthly_available") else ("в конце срока" if row.get("payout_end_available") else (str(row.get("payout_text") or "") or "уточняется")),
            "currency": "UZS",
            "_source_row_order": int(row.get("source_row_order") or 0),
        }
        score = 0.0
        term_l = dep["term"].lower()
        payout_l = dep["payout"].lower()
        topup_l = dep["topup"].lower()
        if goal == "income" and "ежемесяч" in payout_l:
            score += 2.0
        if goal == "save" and ("в конце" in payout_l or "срока" in payout_l):
            score += 1.5
        if payout_pref == "monthly" and "ежемесяч" in payout_l:
            score += 1.2
        if payout_pref == "end" and "в конце" in payout_l:
            score += 1.0
        if topup_needed is True and ("доступ" in topup_l or "да" in topup_l):
            score += 0.8
        if topup_needed is False and ("нет" in topup_l or "недоступ" in topup_l):
            score += 0.5
        if desired_term:
            term_m = row.get("term_months")
            if term_m:
                closest = abs(int(term_m) - int(desired_term))
                score += max(0.0, 1.0 - closest / 24.0)
        rate_nums = re.findall(r"\d+", dep["rate"])
        if rate_nums:
            score += min(int(rate_nums[0]), 40) / 100.0
        scored.append((score, dep))
    scored.sort(key=lambda x: x[0], reverse=True)
    if not scored:
        fallback = _pick_deposit(goal or "save")
        return [fallback]
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for _, dep in scored:
        key = (dep["name"].strip().lower(), dep["term"])
        if key in seen:
            continue
        seen.add(key)
        result.append(dep)
        if len(result) >= limit:
            break
    return result


def _pick_deposit(goal: str) -> dict[str, Any]:
    options = _select_deposit_options({"goal": goal}, limit=1)
    if options:
        return options[0]
    return {
        "name": "Вклад «X»",
        "term": "6 месяцев",
        "rate": "22%",
        "topup": "доступно",
        "payout": "в конце срока" if goal == "save" else "ежемесячно",
        "currency": "UZS",
    }
