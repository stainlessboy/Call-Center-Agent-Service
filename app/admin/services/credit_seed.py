"""Seed CreditProductOffer rows from a JSON manifest (product pipeline stage 2)."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from sqlalchemy import delete

from app.db.models import CreditProductOffer
from app.db.session import get_session

SECTION_FIELD_INDEXES: Dict[str, Dict[str, int]] = {
    "Микрозайм": {
        "service_name": 0,
        "min_age": 1,
        "purpose": 2,
        "amount": 3,
        "term": 4,
        "rate": 5,
        "collateral": 7,
    },
    "Ипотека": {
        "service_name": 0,
        "min_age": 1,
        "purpose": 2,
        "amount": 3,
        "term": 4,
        "downpayment": 5,
        "rate": 7,
        "collateral": 8,
    },
    "Автокредит": {
        "service_name": 0,
        "min_age": 1,
        "purpose": 2,
        "amount": 3,
        "term": 4,
        "downpayment": 5,
        "rate_payroll": 6,
        "rate_official": 7,
        "rate_no_official": 8,
        "collateral": 9,
    },
    "Образовательный": {
        "service_name": 0,
        "min_age": 1,
        "purpose": 2,
        "amount": 3,
        "term": 4,
        "rate": 5,
        "collateral": 7,
    },
}

INCOME_TYPE_MAP: Dict[str, str] = {
    "payroll": "payroll",
    "official": "official",
    "no_official": "no_official",
}


def _clean(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _normalize_number_chunks(text: str) -> str:
    return re.sub(r"(?<=\d)\s+(?=\d)", "", text)


def _extract_numbers(text: str) -> List[float]:
    normalized = _normalize_number_chunks(text)
    numbers: List[float] = []
    for chunk in re.findall(r"\d+(?:[.,]\d+)?", normalized):
        try:
            numbers.append(float(chunk.replace(",", ".")))
        except ValueError:
            continue
    return numbers


def _has_range_sign(text: str) -> bool:
    return any(sign in text for sign in ("-", "—", "–", "−"))


def _convert_amount_units(values: List[float], text: str) -> List[int]:
    lower = text.lower()
    result: List[int] = []
    for value in values:
        amount = float(value)
        if "млрд" in lower or "миллиард" in lower:
            amount *= 1_000_000_000
        elif "млн" in lower or "million" in lower:
            if amount < 100_000:
                amount *= 1_000_000
        elif "тыс" in lower or "тысяч" in lower:
            if amount < 100_000:
                amount *= 1_000
        result.append(int(amount))
    return result


def _parse_amount_range(text: str) -> Tuple[Optional[int], Optional[int]]:
    if not text or "%" in text:
        return None, None
    nums = _extract_numbers(text)
    if not nums:
        return None, None
    values = _convert_amount_units(nums, text)
    lower = text.lower()
    if len(values) == 1:
        if "до" in lower:
            return 0, values[0]
        if "от" in lower:
            return values[0], None
        return values[0], values[0]
    if _has_range_sign(text):
        first, second = values[0], values[1]
        return (first, second) if first <= second else (second, first)
    return min(values), max(values)


def _parse_term_range_months(text: str) -> Tuple[Optional[int], Optional[int]]:
    if not text:
        return None, None
    lower = text.lower()
    term_tokens = ("мес", "месяц", "месяцев", "month", "год", "года", "лет", "year")
    if not any(token in lower for token in term_tokens):
        return None, None

    cleaned = re.sub(r"\d+(?:[.,]\d+)?\s*%", "", text)
    nums = _extract_numbers(cleaned)
    if not nums:
        return None, None
    multiplier = 12 if any(token in lower for token in ("год", "лет", "year")) else 1
    values = [int(n * multiplier) for n in nums]
    if len(values) == 1:
        if "до" in lower:
            return 0, values[0]
        if "от" in lower:
            return values[0], None
        return values[0], values[0]
    if _has_range_sign(text):
        first, second = values[0], values[1]
        return (first, second) if first <= second else (second, first)
    return min(values), max(values)


def _parse_pct_range(text: str) -> Tuple[Optional[float], Optional[float]]:
    if not text:
        return None, None
    nums = _extract_numbers(text)
    if not nums:
        return None, None
    lower = text.lower()
    values = [float(n) * 100.0 if float(n) <= 1.0 else float(n) for n in nums]
    if len(values) == 1:
        if "до" in lower:
            return None, values[0]
        if "не менее" in lower or "от" in lower:
            return values[0], None
        return values[0], values[0]
    if _has_range_sign(text):
        first, second = values[0], values[1]
        return (first, second) if first <= second else (second, first)
    return min(values), max(values)


def _parse_age(value: object) -> Tuple[Optional[int], Optional[str]]:
    text = _clean(value)
    if not text:
        return None, None
    nums = _extract_numbers(text)
    if not nums:
        return None, text
    return int(nums[0]), text


def _detect_income_type(text: str) -> Optional[str]:
    lower = text.lower()
    if "без официаль" in lower or "оборот" in lower:
        return INCOME_TYPE_MAP["no_official"]
    if "зарплат" in lower or "заработн" in lower:
        return INCOME_TYPE_MAP["payroll"]
    if "официаль" in lower:
        return INCOME_TYPE_MAP["official"]
    return None


def _parse_rate_range_from_line(text: str) -> Tuple[Optional[float], Optional[float]]:
    percent_tokens = [
        float(token.replace(",", "."))
        for token in re.findall(r"(\d+(?:[.,]\d+)?)\s*%", text)
    ]
    if percent_tokens:
        if len(percent_tokens) >= 2 and _has_range_sign(text):
            low = min(percent_tokens[0], percent_tokens[1])
            high = max(percent_tokens[0], percent_tokens[1])
            return low, high
        if len(percent_tokens) == 1:
            return percent_tokens[0], percent_tokens[0]
        return min(percent_tokens), max(percent_tokens)

    nums = _extract_numbers(text)
    if not nums:
        return None, None
    value = nums[-1]
    if value <= 1:
        value *= 100
    return value, value


def _parse_rate_lines(
    rate_text: str,
    default_income_type: Optional[str],
    base_term_text: str,
    base_downpayment_text: str,
) -> List[Dict[str, Any]]:
    lines = [line.strip(" -\t") for line in rate_text.splitlines() if line.strip()]
    if not lines:
        return []

    base_term_min, base_term_max = _parse_term_range_months(base_term_text)
    base_down_min, base_down_max = _parse_pct_range(base_downpayment_text)

    rules: List[Dict[str, Any]] = []
    current_income = default_income_type
    for line in lines:
        income_in_line = _detect_income_type(line)
        if income_in_line:
            current_income = income_in_line
            if not re.search(r"\d", line):
                continue

        rate_min, rate_max = _parse_rate_range_from_line(line)
        if rate_min is None and rate_max is None:
            continue

        term_min, term_max = _parse_term_range_months(line)
        if term_min is None and term_max is None:
            term_min, term_max = base_term_min, base_term_max

        down_min, down_max = base_down_min, base_down_max
        if "взнос" in line.lower():
            line_down_min, line_down_max = _parse_pct_range(line)
            if line_down_min is not None or line_down_max is not None:
                down_min, down_max = line_down_min, line_down_max

        rules.append(
            {
                "income_type": current_income,
                "rate_text": line,
                "rate_condition_text": line if len(lines) > 1 else None,
                "rate_min_pct": rate_min,
                "rate_max_pct": rate_max,
                "term_min_months": term_min,
                "term_max_months": term_max,
                "downpayment_min_pct": down_min,
                "downpayment_max_pct": down_max,
            }
        )

    if rules:
        return rules

    rate_min, rate_max = _parse_rate_range_from_line(rate_text)
    if rate_min is None and rate_max is None:
        return []
    return [
        {
            "income_type": default_income_type,
            "rate_text": rate_text,
            "rate_condition_text": None,
            "rate_min_pct": rate_min,
            "rate_max_pct": rate_max,
            "term_min_months": base_term_min,
            "term_max_months": base_term_max,
            "downpayment_min_pct": base_down_min,
            "downpayment_max_pct": base_down_max,
        }
    ]


def _get_cell(row: List[Any], idx: Optional[int]) -> Any:
    if idx is None or idx < 0 or idx >= len(row):
        return None
    return row[idx]


def _extract_rules_for_section(section_name: str, row: List[Any], mapping: Dict[str, int]) -> List[Dict[str, Any]]:
    term_text = _clean(_get_cell(row, mapping.get("term")))
    downpayment_text = _clean(_get_cell(row, mapping.get("downpayment")))

    if section_name == "Автокредит":
        rules: List[Dict[str, Any]] = []
        for income_key, col_name in (
            ("payroll", "rate_payroll"),
            ("official", "rate_official"),
            ("no_official", "rate_no_official"),
        ):
            raw_rate = _clean(_get_cell(row, mapping.get(col_name)))
            if not raw_rate or raw_rate in {"-", "—"}:
                continue
            rules.extend(
                _parse_rate_lines(
                    rate_text=raw_rate,
                    default_income_type=INCOME_TYPE_MAP[income_key],
                    base_term_text=term_text,
                    base_downpayment_text=downpayment_text,
                )
            )
        return rules

    raw_rate = _clean(_get_cell(row, mapping.get("rate")))
    if not raw_rate or raw_rate in {"-", "—"}:
        return []
    return _parse_rate_lines(
        rate_text=raw_rate,
        default_income_type=None,
        base_term_text=term_text,
        base_downpayment_text=downpayment_text,
    )


def _load_credit_sections(manifest_path: Path) -> List[Tuple[str, str, Dict[str, Any]]]:
    manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
    layout = manifest_data.get("layout")
    if not isinstance(layout, dict):
        raise ValueError("Invalid manifest: layout is missing")
    credit_layout = layout.get("credit_products")
    if not isinstance(credit_layout, dict):
        raise ValueError("Invalid manifest: layout.credit_products is missing")

    loaded: List[Tuple[str, str, Dict[str, Any]]] = []
    for section_name, rel_path in credit_layout.items():
        if not isinstance(section_name, str) or not isinstance(rel_path, str):
            continue
        section_path = (manifest_path.parent / rel_path).resolve()
        if not section_path.exists():
            raise FileNotFoundError(f"Section file not found: {section_path}")
        payload = json.loads(section_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            continue
        loaded.append((section_name, rel_path, payload))
    return loaded


def _iter_structured_records(manifest_path: Path) -> Iterable[Dict[str, Any]]:
    sections = _load_credit_sections(manifest_path)
    for section_name, source_path, payload in sections:
        mapping = SECTION_FIELD_INDEXES.get(section_name)
        if not mapping:
            continue
        rows = payload.get("rows_normalized") or payload.get("rows_raw") or []
        if not isinstance(rows, list):
            continue

        for row_order, raw_row in enumerate(rows, start=1):
            if not isinstance(raw_row, list):
                continue
            service_name = _clean(_get_cell(raw_row, mapping.get("service_name")))
            if not service_name:
                continue
            if service_name.lower() in {"тип кредита", "тип карты", "тип вклада"}:
                continue

            min_age, min_age_text = _parse_age(_get_cell(raw_row, mapping.get("min_age")))
            purpose_text = _clean(_get_cell(raw_row, mapping.get("purpose"))) or None
            amount_text = _clean(_get_cell(raw_row, mapping.get("amount"))) or None
            amount_min, amount_max = _parse_amount_range(amount_text or "")
            term_text = _clean(_get_cell(raw_row, mapping.get("term"))) or None
            term_min_months, term_max_months = _parse_term_range_months(term_text or "")
            downpayment_text = _clean(_get_cell(raw_row, mapping.get("downpayment"))) or None
            down_min, down_max = _parse_pct_range(downpayment_text or "")
            collateral_text = _clean(_get_cell(raw_row, mapping.get("collateral"))) or None

            rules = _extract_rules_for_section(section_name, raw_row, mapping)
            if not rules:
                rules = [
                    {
                        "income_type": None,
                        "rate_text": None,
                        "rate_condition_text": None,
                        "rate_min_pct": None,
                        "rate_max_pct": None,
                        "term_min_months": term_min_months,
                        "term_max_months": term_max_months,
                        "downpayment_min_pct": down_min,
                        "downpayment_max_pct": down_max,
                    }
                ]

            for idx, rule in enumerate(rules, start=1):
                yield {
                    "section_name": section_name,
                    "service_name": service_name,
                    "min_age": min_age,
                    "min_age_text": min_age_text,
                    "purpose_text": purpose_text,
                    "amount_text": amount_text,
                    "amount_min": amount_min,
                    "amount_max": amount_max,
                    "term_text": term_text,
                    "term_min_months": rule.get("term_min_months", term_min_months),
                    "term_max_months": rule.get("term_max_months", term_max_months),
                    "downpayment_text": downpayment_text,
                    "downpayment_min_pct": rule.get("downpayment_min_pct", down_min),
                    "downpayment_max_pct": rule.get("downpayment_max_pct", down_max),
                    "income_type": rule.get("income_type"),
                    "rate_text": rule.get("rate_text"),
                    "rate_condition_text": rule.get("rate_condition_text"),
                    "rate_min_pct": rule.get("rate_min_pct"),
                    "rate_max_pct": rule.get("rate_max_pct"),
                    "collateral_text": collateral_text,
                    "source_path": source_path,
                    "source_row_order": row_order,
                    "rate_order": idx,
                }


async def _seed(manifest_path: Path, replace: bool) -> Tuple[int, int]:
    inserted = 0
    skipped = 0
    records = list(_iter_structured_records(manifest_path))

    async with get_session() as session:
        if replace:
            await session.execute(delete(CreditProductOffer))

        for payload in records:
            session.add(CreditProductOffer(**payload, is_active=True))
            inserted += 1
        await session.commit()
    return inserted, skipped
