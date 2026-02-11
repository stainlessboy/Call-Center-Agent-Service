#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from openpyxl import load_workbook


def _is_blank(value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and not value.strip():
        return True
    return False


def _section_title(row: Sequence[object]) -> Optional[str]:
    if not row:
        return None
    first = row[0]
    if _is_blank(first):
        return None
    if any(not _is_blank(cell) for cell in row[1:]):
        return None
    return str(first).strip() or None


def _clean_row(row: Sequence[object], max_cols: int) -> List[Any]:
    cleaned: List[Any] = []
    for i in range(max_cols):
        value = row[i] if i < len(row) else None
        if isinstance(value, str):
            value = value.strip()
        cleaned.append(value)
    return cleaned


def _normalize_rows(rows: List[List[Any]]) -> List[List[Any]]:
    if not rows:
        return []
    max_cols = max(len(r) for r in rows)
    last: List[Any] = [None] * max_cols
    normalized: List[List[Any]] = []
    for row in rows:
        filled: List[Any] = []
        for idx in range(max_cols):
            value = row[idx] if idx < len(row) else None
            if _is_blank(value):
                value = last[idx]
            else:
                last[idx] = value
            filled.append(value)
        normalized.append(filled)
    return normalized


def _parse_sheet(path: Path, sheet_name: str) -> Dict[str, Any]:
    wb = load_workbook(path, read_only=True, data_only=True)
    if sheet_name not in wb.sheetnames:
        raise ValueError(f"Sheet '{sheet_name}' not found. Available: {', '.join(wb.sheetnames)}")

    ws = wb[sheet_name]
    rows_raw = list(ws.iter_rows(values_only=True))
    sections: Dict[str, Dict[str, Any]] = {}

    i = 0
    while i < len(rows_raw):
        row = rows_raw[i]
        title = _section_title(row)
        if not title:
            i += 1
            continue

        # find header row
        i += 1
        while i < len(rows_raw) and all(_is_blank(v) for v in rows_raw[i]):
            i += 1
        if i >= len(rows_raw):
            break

        header_row = rows_raw[i]
        max_cols = max(len(header_row), 1)
        header = _clean_row(header_row, max_cols)
        i += 1

        data_rows: List[List[Any]] = []
        while i < len(rows_raw):
            next_row = rows_raw[i]
            if _section_title(next_row):
                break
            if all(_is_blank(v) for v in next_row):
                i += 1
                continue
            data_rows.append(_clean_row(next_row, max_cols))
            i += 1

        sections[title] = {
            "header": header,
            "rows_raw": data_rows,
            "rows_normalized": _normalize_rows(data_rows),
        }

    return {
        "sheet": sheet_name,
        "sections": sections,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Export AI CHAT INFO.xlsx into JSON.")
    parser.add_argument("path", type=Path, help="Path to AI CHAT INFO.xlsx")
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("app/data/ai_chat_info.json"),
        help="Output JSON path (default: app/data/ai_chat_info.json)",
    )
    args = parser.parse_args()

    if not args.path.exists():
        raise SystemExit(f"File not found: {args.path}")

    result = {
        "source_file": str(args.path),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "workbook": {
            "credit_products": _parse_sheet(args.path, "Кредитные продукты"),
            "noncredit_products": _parse_sheet(args.path, "Некредитные продукты"),
        },
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {args.out}")


if __name__ == "__main__":
    main()
