"""Parse the tariff sheet into CreditRateRule records (product pipeline, rates only).

Source: the "Тарифы" workbook filled in by the business — one row per tariff,
i.e. one combination of (product, income type, term range, downpayment range)
and the single rate that applies to it. Unlike the "AI CHAT INFO" workbook this
file carries **rates only**: no amount, purpose, minimum age or collateral, so a
product created from here stays a stub until the product workbook is loaded
(see ``update_static=False`` in ``credit_seed.seed_records``).

Records produced here feed the very same ``seed_records()`` the manifest path
uses — the upsert, the "manual rules survive" rule and the qualify-tag
protection are not duplicated.

Percent columns are the one real trap. Excel stores a cell typed as "25%" as
the number 0.25 with a percent *format*, while a cell typed as "25" is the
number 25 — both mean the same tariff. ``_pct()`` reads the format to tell them
apart, so the sheet accepts either style.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from openpyxl import load_workbook

from app.admin.services.credit_seed import (
    _parse_pct_range,
    _parse_rate_range_from_line,
)

SHEET_NAME = "Тарифы"

# The three "Тип дохода" values the sheet offers, mapped onto the vocabulary
# used everywhere else in the agent (qualify.py, rate_rules.py, profile facts).
INCOME_TYPE_MAP: Dict[str, str] = {
    "зарплата на карту асакабанка": "payroll",
    "зарплата на карту другого банка": "official",
    "самозанятый": "no_official",
}

# Column header → record field. Headers are matched on a normalized form, so
# the sheet may reorder columns or vary the wording in brackets.
_HEADER_ALIASES: Dict[str, str] = {
    "категория": "section_name",
    "продукт": "service_name",
    "типдохода": "income_type",
    "срокот": "term_min_months",
    "срокдо": "term_max_months",
    "взносот": "downpayment_min_pct",
    "взносдо": "downpayment_max_pct",
    "ставка": "rate",
    "комментарий": "comment",
    "чтонужноотвас": "note",
    "вашответ": "answer",
}

# Which axis each product's rate actually varies by — read off the
# "условия.xlsx" matrix ("Влияет ли ...: да/нет" per product). Used as the
# product's ``rate_condition_kind``: the label shown next to the product, and
# the axis filter for sources parsed loosely. A product missing here falls back
# to the per-section default in credit_seed.
PRODUCT_CONDITION_KIND: Dict[str, str] = {
    # Микрозайм — matrix says the rate depends on neither term nor downpayment,
    # but the filled-in tariffs split four of them by term. Question 09 to the
    # business; until it is answered the sheet's own data wins.
    "Онлайн микрозайм": "term",
    "Микрозайм 2.6": "term",
    "Онлайн овердрафт": "flat",
    "Потребительский кредит": "flat",
    "Льготный микрозайм": "term",
    "Первый шаг к бизнесу 1.0": "term",
    "Первый шаг к бизнесу 2.0": "flat",
    "Первый шаг к бизнесу 3.0": "flat",
    "Помощь предпринимателю": "flat",
    # Автокредит — matrix: term yes; Автокредит 2.6 also downpayment.
    "Автокредит 2.6": "term_downpayment",
    "Онлайн автокредит": "term",
    "Uzauto Motors 2": "term",
    "Uzauto Motors 3": "term",
    "АДМ глобал 1": "term",
    "АДМ глобал 2": "term",
    "АДМ глобал 3": "term",
    "АДМ глобал 4": "term",
    "АДМ глобал 5": "term",
    "Чанган": "term",
    "Kaiyi": "term",
    # Ипотека — matrix: DAHO/NB by term, the rest by downpayment or flat.
    # Qulay makon/Universal/BI Group/NRG/Sharq Bahor state a minimum
    # downpayment at a single rate, i.e. an eligibility threshold rather than a
    # rate tier (question 13) — kept as 'downpayment' to match the matrix.
    "Qulay makon": "downpayment",
    "Universal": "downpayment",
    "Eko": "flat",
    "Eko Pyus": "flat",
    "Oila": "flat",
    "DAHO": "term",
    "NB texnologies": "term",
    "BI Group": "downpayment",
    "NRG": "downpayment",
    "Sharq Bahor": "downpayment",
}

# Categories the sheet may use, mapped onto CreditProductOffer.section_name.
# Values already match CREDIT_SECTION_MAP in app/agent/constants.py.
KNOWN_SECTIONS = ("Микрозайм", "Автокредит", "Ипотека", "Образовательный")


class TariffIssue(dict):
    """One problem found in the sheet: reported, never silently imported."""

    def __init__(self, row: Optional[int], product: str, message: str) -> None:
        super().__init__(row=row, product=product, message=message)


def _norm_header(value: object) -> str:
    text = re.sub(r"\(.*?\)", "", str(value or "")).lower()
    return re.sub(r"[^a-zа-яё0-9]", "", text)


def _clean(value: object) -> str:
    return str(value).strip() if value is not None else ""


def _is_blank(value: object) -> bool:
    """A cell carrying no constraint: empty, or the sheet's «—» placeholder."""
    text = _clean(value)
    return text == "" or text in {"-", "—", "–", "нет", "н/д"}


def _pct(cell: Any) -> Optional[float]:
    """A percent cell as a plain number of percent (25.0 for 25%).

    Accepts every way the sheet can hold one:
      * 25            → 25.0    (plain number)
      * 0.25 fmt '0%' → 25.0    (Excel's percent-typed cell)
      * "25 %"        → 25.0    (text)
    """
    if cell is None:
        return None
    value = getattr(cell, "value", cell)
    if _is_blank(value):
        return None
    if isinstance(value, (int, float)):
        fmt = str(getattr(cell, "number_format", "") or "")
        return round(float(value) * 100, 4) if "%" in fmt else float(value)
    low, high = _parse_pct_range(_clean(value))
    return low if low is not None else high


def _months(value: object) -> Optional[int]:
    """A term cell as a whole number of months.

    Numbers pass through; the sheet's shorthands («на 48», «до 240») yield the
    number they carry. A blank or «—» means the axis is unbounded on that side.
    """
    if _is_blank(value):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    digits = re.findall(r"\d+", _clean(value))
    return int(digits[0]) if digits else None


# A rate cell holding several tariffs at once — «24 oygacha - 25 % / 48 oygacha
# - 27 %», or the Kredit A/B pairs. The sheet's rule is one row per tariff, and
# guessing the tier boundaries here would invent data: the cell says 13–60
# months while the text describes three different terms. Detected and refused
# rather than parsed, because a silent parse yields a plausible wrong rate.
_TIER_MARKER = re.compile(r"\b(oygacha|oyga|мес\.?|месяц\w*|kredit\s+[ab])\b", re.IGNORECASE)


def _counts_multiple_tariffs(text: str) -> bool:
    if "\n" in text.strip():
        return True
    return len(_TIER_MARKER.findall(text)) >= 2


def _rate(cell: Any) -> Tuple[Optional[float], Optional[float]]:
    """A rate cell as (min, max) percent. A single value sets both ends to it."""
    if cell is None:
        return None, None
    value = getattr(cell, "value", cell)
    if _is_blank(value):
        return None, None
    if isinstance(value, (int, float)):
        one = _pct(cell)
        return one, one
    text = _clean(value)
    low, high = _parse_rate_range_from_line(text)
    if low is None and high is None:
        return None, None
    if low is not None and high is None:
        return low, low
    if low is None:
        return high, high
    return low, high


def _overlaps(a_min: Optional[float], a_max: Optional[float],
              b_min: Optional[float], b_max: Optional[float]) -> bool:
    """Whether two ranges intersect, treating a missing bound as unbounded."""
    lo_a = a_min if a_min is not None else float("-inf")
    hi_a = a_max if a_max is not None else float("inf")
    lo_b = b_min if b_min is not None else float("-inf")
    hi_b = b_max if b_max is not None else float("inf")
    return lo_a <= hi_b and lo_b <= hi_a


def _check_overlaps(records: List[Dict[str, Any]]) -> List[TariffIssue]:
    """Two tariffs of one product and income type must not both match an input.

    Overlapping ranges make the rate ambiguous: whichever rule ``select_rate``
    happens to reach first decides what the customer is quoted.
    """
    issues: List[TariffIssue] = []
    buckets: Dict[Tuple[str, str, Optional[str]], List[Dict[str, Any]]] = {}
    for rec in records:
        key = (rec["section_name"], rec["service_name"], rec.get("income_type"))
        buckets.setdefault(key, []).append(rec)

    for (_section, service, income), recs in buckets.items():
        for i, a in enumerate(recs):
            for b in recs[i + 1:]:
                terms_hit = _overlaps(
                    a.get("term_min_months"), a.get("term_max_months"),
                    b.get("term_min_months"), b.get("term_max_months"),
                )
                dp_hit = _overlaps(
                    a.get("downpayment_min_pct"), a.get("downpayment_max_pct"),
                    b.get("downpayment_min_pct"), b.get("downpayment_max_pct"),
                )
                if terms_hit and dp_hit:
                    issues.append(TariffIssue(
                        b["_row"],
                        service,
                        f"диапазоны пересекаются со строкой {a['_row']} "
                        f"(тип дохода «{income}») — ставка неоднозначна",
                    ))
    return issues


def parse_tariffs(path: Path) -> Tuple[List[Dict[str, Any]], List[TariffIssue]]:
    """Read the tariff workbook into ``seed_records()`` records + a problem list.

    A row is skipped (with an issue logged) rather than imported when it names
    no product, carries no readable rate, or uses an income type outside the
    three the sheet offers. Category banner rows — a category name with nothing
    else on the line — are skipped silently, they carry no data.
    """
    workbook = load_workbook(path, data_only=True)
    sheet = workbook[SHEET_NAME] if SHEET_NAME in workbook.sheetnames else workbook.worksheets[0]

    rows = list(sheet.iter_rows())
    if not rows:
        return [], [TariffIssue(None, "", "лист пуст")]

    columns: Dict[str, int] = {}
    header_row_idx = 0
    for idx, row in enumerate(rows[:10]):
        mapping = {}
        for pos, cell in enumerate(row):
            field = _HEADER_ALIASES.get(_norm_header(cell.value))
            if field and field not in mapping:
                mapping[field] = pos
        if {"service_name", "income_type", "rate"} <= set(mapping):
            columns, header_row_idx = mapping, idx
            break
    if not columns:
        return [], [TariffIssue(None, "", "не найдена строка заголовков")]

    def cell_at(row: Sequence[Any], field: str) -> Any:
        pos = columns.get(field)
        return row[pos] if pos is not None and pos < len(row) else None

    records: List[Dict[str, Any]] = []
    issues: List[TariffIssue] = []
    section = ""

    for offset, row in enumerate(rows[header_row_idx + 1:], start=header_row_idx + 2):
        product = _clean(getattr(cell_at(row, "service_name"), "value", None))
        category = _clean(getattr(cell_at(row, "section_name"), "value", None))
        income_raw = _clean(getattr(cell_at(row, "income_type"), "value", None))

        if category:
            section = category
        if not product:
            # Category banner or a spacer row — nothing to import, nothing wrong.
            continue
        if not section:
            issues.append(TariffIssue(offset, product, "не указана категория"))
            continue
        if section not in KNOWN_SECTIONS:
            issues.append(TariffIssue(offset, product, f"неизвестная категория «{section}»"))
            continue

        income_type = INCOME_TYPE_MAP.get(income_raw.lower())
        if income_raw and income_type is None:
            issues.append(TariffIssue(offset, product, f"неизвестный тип дохода «{income_raw}»"))
            continue

        rate_cell = cell_at(row, "rate")
        rate_raw = _clean(getattr(rate_cell, "value", None))
        if rate_raw and _counts_multiple_tariffs(rate_raw):
            issues.append(TariffIssue(
                offset,
                product,
                "в ячейке ставки несколько тарифов сразу — разнесите их "
                "по отдельным строкам и проставьте срок/взнос для каждой",
            ))
            continue

        rate_min, rate_max = _rate(rate_cell)
        if rate_min is None and rate_max is None:
            issues.append(TariffIssue(offset, product, "ставка не заполнена — строка пропущена"))
            continue

        comment = _clean(getattr(cell_at(row, "comment"), "value", None))
        records.append({
            "_row": offset,
            "section_name": section,
            "service_name": product,
            "income_type": income_type,
            "term_min_months": _months(getattr(cell_at(row, "term_min_months"), "value", None)),
            "term_max_months": _months(getattr(cell_at(row, "term_max_months"), "value", None)),
            "downpayment_min_pct": _pct(cell_at(row, "downpayment_min_pct")),
            "downpayment_max_pct": _pct(cell_at(row, "downpayment_max_pct")),
            "rate_min_pct": rate_min,
            "rate_max_pct": rate_max,
            "rate_condition_text": comment or None,
            "rate_condition_kind": PRODUCT_CONDITION_KIND.get(product),
            "source_path": path.name,
        })

    issues.extend(_check_overlaps(records))
    return records, issues


async def seed_tariffs(path: Path, replace: bool) -> Dict[str, Any]:
    """Parse the tariff workbook and load it, returning a per-row report.

    Nothing is written when the sheet yields no usable row — an empty import
    that reports success is worse than a loud refusal.
    """
    from app.admin.services.credit_seed import seed_records

    records, issues = parse_tariffs(path)
    if not records:
        return {
            "inserted": 0,
            "products": 0,
            "skipped": len(issues),
            "issues": issues,
        }

    payload = [{k: v for k, v in rec.items() if k != "_row"} for rec in records]
    inserted, _ = await seed_records(
        payload,
        replace=replace,
        # Every row states its own bounds — keep them all (the product card
        # reads the term range off the rules).
        trim_axes=False,
        # The sheet has no amount/purpose/collateral; never null them out.
        update_static=False,
    )
    products = len({(r["section_name"], r["service_name"]) for r in records})
    return {
        "inserted": inserted,
        "products": products,
        "skipped": len(issues),
        "issues": issues,
    }
