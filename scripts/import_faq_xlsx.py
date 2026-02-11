#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import re
import sys
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from openpyxl import load_workbook
from sqlalchemy import delete

from app.db.models import FaqItem
from app.db.session import get_session

HEADER_ALIASES = {
    "question": {
        "question",
        "questions",
        "q",
        "faq question",
        "вопрос",
        "вопросы",
        "вопрос/проблема",
        "вопросы/проблемы",
    },
    "answer": {
        "answer",
        "answers",
        "a",
        "response",
        "ответ",
        "ответы",
        "ответ/решение",
        "ответы/решения",
    },
}


def _normalize_header(value: object) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip().lower()
    if not text:
        return None
    text = re.sub(r"[^\w\s]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def _find_header_row(rows: Sequence[Sequence[object]]) -> Optional[Tuple[int, int, int]]:
    alias_map: dict[str, str] = {}
    for key, aliases in HEADER_ALIASES.items():
        for alias in aliases:
            alias_map[alias] = key

    for row_index, row in enumerate(rows):
        header_positions: dict[str, int] = {}
        for col_index, value in enumerate(row):
            normalized = _normalize_header(value)
            if not normalized:
                continue
            target = alias_map.get(normalized)
            if target and target not in header_positions:
                header_positions[target] = col_index
        if "question" in header_positions and "answer" in header_positions:
            return row_index, header_positions["question"], header_positions["answer"]
    return None


def _iter_rows(path: Path, sheet: Optional[str]) -> Iterable[Sequence[object]]:
    workbook = load_workbook(path, read_only=True, data_only=True)
    if sheet:
        if sheet not in workbook.sheetnames:
            raise ValueError(f"Sheet '{sheet}' not found. Available: {', '.join(workbook.sheetnames)}")
        worksheet = workbook[sheet]
    else:
        worksheet = workbook.active
    for row in worksheet.iter_rows(values_only=True):
        yield row


def _extract_items(path: Path, sheet: Optional[str], limit: Optional[int]) -> List[Tuple[str, str]]:
    rows = list(_iter_rows(path, sheet))
    if not rows:
        return []

    header = _find_header_row(rows[: min(len(rows), 10)])
    if header:
        header_row, question_idx, answer_idx = header
        data_rows = rows[header_row + 1 :]
    else:
        # Infer columns by non-empty counts if there is no header row.
        max_cols = max(len(row) for row in rows)
        counts = [0] * max_cols
        for row in rows:
            for idx in range(min(len(row), max_cols)):
                val = row[idx]
                if val is None:
                    continue
                if str(val).strip():
                    counts[idx] += 1
        if max_cols < 2 or sorted(counts, reverse=True)[:2].count(0) > 0:
            return []
        top_two = sorted(range(max_cols), key=lambda i: (-counts[i], i))[:2]
        question_idx, answer_idx = sorted(top_two)
        data_rows = rows

    items: List[Tuple[str, str]] = []
    seen: set[Tuple[str, str]] = set()
    for row in data_rows:
        if question_idx >= len(row) or answer_idx >= len(row):
            continue
        question_raw = row[question_idx]
        answer_raw = row[answer_idx]
        if question_raw is None or answer_raw is None:
            continue
        question = str(question_raw).strip()
        answer = str(answer_raw).strip()
        if not question or not answer:
            continue
        key = (question, answer)
        if key in seen:
            continue
        seen.add(key)
        items.append(key)
        if limit and len(items) >= limit:
            break
    return items


async def _import_items(items: List[Tuple[str, str]], replace: bool, dry_run: bool) -> None:
    if dry_run:
        return
    async with get_session() as session:
        if replace:
            await session.execute(delete(FaqItem))
        session.add_all([FaqItem(question=q, answer=a) for q, a in items])
        await session.commit()


def main() -> None:
    parser = argparse.ArgumentParser(description="Import FAQ items from an .xlsx file into the database.")
    parser.add_argument("path", type=Path, help="Path to FAQ .xlsx file")
    parser.add_argument("--sheet", type=str, help="Sheet name (defaults to active sheet)")
    parser.add_argument("--replace", action="store_true", help="Delete existing FAQ items before import")
    parser.add_argument("--limit", type=int, help="Import only first N rows (for testing)")
    parser.add_argument("--dry-run", action="store_true", help="Parse and report rows without writing to DB")
    args = parser.parse_args()

    if not args.path.exists():
        raise SystemExit(f"File not found: {args.path}")

    items = _extract_items(args.path, args.sheet, args.limit)
    print(f"Parsed {len(items)} FAQ rows from {args.path}")
    if items:
        sample = items[:3]
        print("Sample:")
        for question, answer in sample:
            print(f"- Q: {question}")
            print(f"  A: {answer}")
    if args.dry_run:
        print("Dry-run mode: no changes were written to the database.")
        return

    asyncio.run(_import_items(items, args.replace, args.dry_run))
    print("Import complete.")


if __name__ == "__main__":
    main()
