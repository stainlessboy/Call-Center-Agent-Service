"""Seed CardProductOffer rows from a JSON manifest."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable, Optional

from sqlalchemy import delete

from app.db.models import CardProductOffer
from app.db.session import get_session


def _clean(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _extract_numbers(text: str) -> list[float]:
    norm = re.sub(r"(?<=\d)\s+(?=\d)", "", text or "")
    vals: list[float] = []
    for token in re.findall(r"\d+(?:[.,]\d+)?", norm):
        try:
            vals.append(float(token.replace(",", ".")))
        except ValueError:
            continue
    return vals


def _parse_pct(value: object) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        if 0 < v <= 1:
            v *= 100
        return v
    text = _clean(value)
    if not text or text in {"-", "—"}:
        return None
    nums = _extract_numbers(text)
    if not nums:
        return None
    v = nums[0]
    if 0 < v <= 1 and "%" not in text:
        v *= 100
    return v


def _parse_months(text: str) -> Optional[int]:
    lower = (text or "").lower()
    nums = _extract_numbers(lower)
    if not nums:
        return None
    val = int(nums[0])
    if any(t in lower for t in ("год", "года", "лет")):
        return val * 12
    if any(t in lower for t in ("мес", "месяц")):
        return val
    return None


def _infer_network(name: str) -> Optional[str]:
    lower = name.lower()
    if "mastercard" in lower:
        return "mastercard"
    if "visa" in lower:
        return "visa"
    if "uzcard" in lower:
        return "uzcard"
    if "humo" in lower:
        return "humo"
    return None


def _infer_currency(name: str, issue_fee_text: str, transfer_fee_text: str) -> str:
    txt = f"{name} {issue_fee_text} {transfer_fee_text}".lower()
    has_usd = any(t in txt for t in ("usd", "доллар", "$"))
    has_eur = any(t in txt for t in ("eur", "евро"))
    if has_usd and has_eur:
        return "MULTI"
    if has_usd:
        return "USD"
    if has_eur:
        return "EUR"
    if any(t in txt for t in ("сум", "uzs")):
        return "UZS"
    return "UNKNOWN"


def _contains_any(text: str, tokens: tuple[str, ...]) -> bool:
    lower = (text or "").lower()
    return any(t in lower for t in tokens)


def _is_free(text: str) -> Optional[bool]:
    lower = (text or "").lower().strip()
    if not lower or lower in {"-", "—"}:
        return None
    if "бесплат" in lower:
        return True
    return False


def _load_cards_payload(manifest_path: Path) -> tuple[str, list[list[Any]]]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    rel = (((manifest.get("layout") or {}).get("noncredit_products") or {}).get("Карты"))
    if not isinstance(rel, str):
        raise ValueError("Manifest missing noncredit_products['Карты']")
    payload_path = (manifest_path.parent / rel).resolve()
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    rows = payload.get("rows_normalized") or payload.get("rows_raw") or []
    if not isinstance(rows, list):
        raise ValueError("Invalid cards payload rows")
    return rel, rows


def _iter_records(manifest_path: Path) -> Iterable[dict[str, Any]]:
    source_path, rows = _load_cards_payload(manifest_path)
    for row_order, row in enumerate(rows, start=1):
        if not isinstance(row, list):
            continue
        name = _clean(row[0] if len(row) > 0 else None)
        if not name:
            continue
        lower_name = name.lower()
        if lower_name in {"тип карты", "карты"}:
            continue

        issue_fee_text = _clean(row[1] if len(row) > 1 else None) or None
        reissue_fee_text = _clean(row[2] if len(row) > 2 else None) or None
        transfer_fee_text = _clean(row[3] if len(row) > 3 else None) or None
        cashback_raw = row[4] if len(row) > 4 else None
        cashback_text = _clean(cashback_raw) or None
        validity_text = _clean(row[5] if len(row) > 5 else None) or None
        issuance_time_text = _clean(row[6] if len(row) > 6 else None) or None
        pin_setup_cbu_text = _clean(row[7] if len(row) > 7 else None) or None
        sms_setup_cbu_text = _clean(row[8] if len(row) > 8 else None) or None
        pin_setup_mobile_text = _clean(row[9] if len(row) > 9 else None) or None
        sms_setup_mobile_text = _clean(row[10] if len(row) > 10 else None) or None
        annual_fee_text = _clean(row[11] if len(row) > 11 else None) or None

        card_network = _infer_network(name)
        is_fx = card_network in {"visa", "mastercard"}
        currency_code = _infer_currency(name, issue_fee_text or "", transfer_fee_text or "")
        payroll_supported = True if _contains_any(" ".join(filter(None, [issue_fee_text, reissue_fee_text, annual_fee_text])), ("заработной плат", "зарплат")) else None

        mobile_blob = " ".join(filter(None, [issuance_time_text, pin_setup_mobile_text, sms_setup_mobile_text]))
        mobile_order_available = True if _contains_any(mobile_blob, ("мобил", "приложен", "asakabank")) else None
        delivery_available = True if _contains_any(mobile_blob, ("доставк",)) else None
        pickup_available = True if _contains_any(mobile_blob + " " + (issuance_time_text or ""), ("самовывоз", "цбу", "терминал")) else None

        yield {
            "service_name": name,
            "card_network": card_network,
            "currency_code": currency_code,
            "is_fx_card": bool(is_fx),
            "is_debit_card": True,
            "payroll_supported": payroll_supported,
            "issue_fee_text": issue_fee_text,
            "issue_fee_free": _is_free(issue_fee_text or ""),
            "reissue_fee_text": reissue_fee_text,
            "transfer_fee_text": transfer_fee_text,
            "cashback_text": cashback_text,
            "cashback_pct": _parse_pct(cashback_raw),
            "validity_text": validity_text,
            "validity_months": _parse_months(validity_text or ""),
            "issuance_time_text": issuance_time_text,
            "pin_setup_cbu_text": pin_setup_cbu_text,
            "sms_setup_cbu_text": sms_setup_cbu_text,
            "pin_setup_mobile_text": pin_setup_mobile_text,
            "sms_setup_mobile_text": sms_setup_mobile_text,
            "annual_fee_text": annual_fee_text,
            "annual_fee_free": _is_free(annual_fee_text or ""),
            "mobile_order_available": mobile_order_available,
            "delivery_available": delivery_available,
            "pickup_available": pickup_available,
            "source_path": source_path,
            "source_row_order": row_order,
            "is_active": True,
        }


async def _seed(manifest_path: Path, replace: bool) -> tuple[int, int]:
    records = list(_iter_records(manifest_path))
    inserted = 0
    skipped = 0
    async with get_session() as session:
        if replace:
            await session.execute(delete(CardProductOffer))

        for rec in records:
            session.add(CardProductOffer(**rec))
            inserted += 1
        await session.commit()
    return inserted, skipped
