#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path
from typing import Any, Iterable, Optional

from sqlalchemy import delete

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.db.models import DepositProductOffer
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


def _parse_amount(text: str, currency_code: str) -> Optional[int]:
    if not text or text in {"-", "—"}:
        return None
    nums = _extract_numbers(text)
    if not nums:
        return None
    value = nums[0]
    if currency_code == "UZS" and value < 1_000_000:
        # 100.000 in xlsx often means 100000
        norm = re.sub(r"\D", "", text)
        if norm:
            try:
                return int(norm)
            except ValueError:
                pass
    return int(value)


def _parse_term_months(text: str) -> Optional[int]:
    lower = (text or "").lower()
    nums = _extract_numbers(lower)
    if not nums:
        return None
    value = int(nums[0])
    if any(t in lower for t in ("год", "лет")):
        return value * 12
    return value


def _parse_rate_pct(value: object) -> Optional[float]:
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
    v = float(nums[0])
    if 0 < v <= 1 and "%" not in text:
        v *= 100
    return v


def _load_deposit_payload(manifest_path: Path) -> tuple[str, list[list[Any]]]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    rel = (((manifest.get("layout") or {}).get("noncredit_products") or {}).get("Вклады"))
    if not isinstance(rel, str):
        raise ValueError("Manifest missing noncredit_products['Вклады']")
    payload_path = (manifest_path.parent / rel).resolve()
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    rows = payload.get("rows_normalized") or payload.get("rows_raw") or []
    if not isinstance(rows, list):
        raise ValueError("Invalid deposits payload rows")
    return rel, rows


def _iter_records(manifest_path: Path) -> Iterable[dict[str, Any]]:
    source_path, rows = _load_deposit_payload(manifest_path)
    currency_cols = [("UZS", 1, 5), ("USD", 2, 6), ("EUR", 3, 7)]
    for row_order, row in enumerate(rows, start=1):
        if not isinstance(row, list):
            continue
        service_name = _clean(row[0] if len(row) > 0 else None)
        if not service_name:
            continue
        lower_name = service_name.lower()
        if lower_name in {"тип вклада", "вклады"}:
            continue

        term_text = _clean(row[4] if len(row) > 4 else None)
        term_months = _parse_term_months(term_text)
        open_channel_text = _clean(row[8] if len(row) > 8 else None) or None
        payout_text = _clean(row[9] if len(row) > 9 else None) or None
        topup_text = _clean(row[10] if len(row) > 10 else None) or None
        notes_text = _clean(row[11] if len(row) > 11 else None) or None

        payout_lower = (payout_text or "").lower()
        payout_monthly = True if "ежемесяч" in payout_lower else None
        payout_end = True if ("в конце" in payout_lower or "окончан" in payout_lower) else None
        topup_lower = (topup_text or "").lower()
        topup_allowed = True if "пополн" in topup_lower and "да" in topup_lower else (False if "пополн" in topup_lower and "нет" in topup_lower else None)
        partial_withdrawal_allowed = True if "списани" in topup_lower and "да" in topup_lower else (False if "списани" in topup_lower and "нет" in topup_lower else None)

        for currency_code, min_idx, rate_idx in currency_cols:
            min_amount_text = _clean(row[min_idx] if len(row) > min_idx else None)
            rate_value = row[rate_idx] if len(row) > rate_idx else None
            rate_pct = _parse_rate_pct(rate_value)
            if not min_amount_text and rate_pct is None and _clean(rate_value) in {"", "-", "—"}:
                continue
            rate_text = _clean(rate_value) if _clean(rate_value) not in {"", "-", "—"} else (f"{rate_pct:.2f}%" if rate_pct is not None else "")
            yield {
                "service_name": service_name,
                "currency_code": currency_code,
                "min_amount_text": min_amount_text or None,
                "min_amount": _parse_amount(min_amount_text, currency_code),
                "term_text": term_text or None,
                "term_months": term_months,
                "rate_text": rate_text or None,
                "rate_pct": rate_pct,
                "open_channel_text": open_channel_text,
                "payout_text": payout_text,
                "payout_monthly_available": payout_monthly,
                "payout_end_available": payout_end,
                "topup_text": topup_text,
                "topup_allowed": topup_allowed,
                "partial_withdrawal_allowed": partial_withdrawal_allowed,
                "notes_text": notes_text,
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
            await session.execute(delete(DepositProductOffer))

        for rec in records:
            session.add(DepositProductOffer(**rec))
            inserted += 1
        await session.commit()
    return inserted, skipped


def main() -> None:
    p = argparse.ArgumentParser(description="Seed normalized deposit offers from ai_chat_info manifest.")
    p.add_argument("--manifest", type=Path, default=Path("app/data/ai_chat_info.json"))
    p.add_argument("--replace", action="store_true")
    args = p.parse_args()
    manifest = args.manifest.resolve()
    if not manifest.exists():
        raise SystemExit(f"Manifest not found: {manifest}")
    if not args.replace:
        raise SystemExit("Use --replace for deterministic reseed.")
    inserted, skipped = asyncio.run(_seed(manifest, args.replace))
    print(f"Done. Inserted: {inserted}, skipped: {skipped}")


if __name__ == "__main__":
    main()
