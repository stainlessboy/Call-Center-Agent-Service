"""Tests for the tariff-sheet parser (app/admin/services/tariffs_excel.py)."""
from pathlib import Path
from types import SimpleNamespace

import pytest
from openpyxl import Workbook

from app.admin.services.credit_seed import _product_condition_kind
from app.admin.services.tariffs_excel import (
    INCOME_TYPE_MAP,
    PRODUCT_CONDITION_KIND,
    _months,
    _overlaps,
    _pct,
    _rate,
    parse_tariffs,
)

HEADERS = [
    "Категория", "Продукт", "Тип дохода",
    "Срок от (мес.)", "Срок до (мес.)",
    "Взнос от (%)", "Взнос до (%)",
    "Ставка (% годовых)", "Что нужно от вас", "Ваш ответ",
]

PAYROLL = "Зарплата на карту Асакабанка"


def _cell(value, number_format="General"):
    """Stand-in for an openpyxl cell: the parser reads .value and .number_format."""
    return SimpleNamespace(value=value, number_format=number_format)


def _build_sheet(tmp_path: Path, rows, name="tariffs.xlsx") -> Path:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Тарифы"
    sheet.append(HEADERS)
    for row in rows:
        sheet.append(row)
    path = tmp_path / name
    workbook.save(path)
    return path


class TestPercentCells:
    """Excel stores a cell typed "25%" as 0.25 with a percent format, and a cell
    typed "25" as 25. Both mean the same tariff, so the format decides."""

    def test_plain_number_is_already_percent(self):
        assert _pct(_cell(25)) == 25.0

    def test_percent_formatted_number_is_scaled(self):
        assert _pct(_cell(0.25, "0%")) == 25.0
        assert _pct(_cell(0.2099, "0.00%")) == 20.99

    def test_zero_is_a_rate_not_a_blank(self):
        # Uzauto Motors is a 0% programme — a dropped 0 would silently become
        # "no tariff" and the product would lose its rule.
        assert _pct(_cell(0)) == 0.0
        assert _rate(_cell(0)) == (0.0, 0.0)

    def test_text_percent_is_read(self):
        assert _pct(_cell("25 %")) == 25.0

    def test_blank_and_dash_are_none(self):
        assert _pct(_cell(None)) is None
        assert _pct(_cell("—")) is None


class TestTermCells:
    def test_number_passes_through(self):
        assert _months(48) == 48

    def test_sheet_shorthands_yield_the_number(self):
        assert _months("на 48") == 48
        assert _months("до 240") == 240

    def test_dash_means_unbounded(self):
        assert _months("—") is None
        assert _months(None) is None


class TestRateCells:
    def test_single_value_sets_both_ends(self):
        assert _rate(_cell(28)) == (28.0, 28.0)

    def test_range_text_is_split(self):
        low, high = _rate(_cell("от- 18,9%, до -21,9%"))
        assert (low, high) == (18.9, 21.9)

    def test_blank_yields_no_rate(self):
        assert _rate(_cell(None)) == (None, None)


class TestOverlapDetection:
    def test_touching_ranges_overlap(self):
        assert _overlaps(12, 24, 24, 36) is True

    def test_disjoint_ranges_do_not(self):
        assert _overlaps(12, 24, 25, 36) is False

    def test_missing_bound_is_unbounded(self):
        assert _overlaps(None, 24, 36, None) is False
        assert _overlaps(None, None, 36, 48) is True


class TestParseSheet:
    def test_reads_rows_and_maps_income_types(self, tmp_path):
        path = _build_sheet(tmp_path, [
            ["Микрозайм", None, None, None, None, None, None, None, None, None],
            ["Микрозайм", "Потребительский кредит", PAYROLL, None, 48, None, None, 28, "", ""],
            ["Микрозайм", "Потребительский кредит", "Самозанятый", 3, 36, None, None, 25, "", ""],
        ])
        records, issues = parse_tariffs(path)

        assert issues == []
        assert len(records) == 2
        assert [r["income_type"] for r in records] == ["payroll", "no_official"]
        assert records[0]["term_max_months"] == 48
        assert records[0]["rate_min_pct"] == 28.0

    def test_category_banner_row_is_skipped_silently(self, tmp_path):
        path = _build_sheet(tmp_path, [
            ["Ипотека", None, None, None, None, None, None, None, None, None],
            ["Ипотека", "Qulay makon", PAYROLL, None, 240, 15, None, 16, "", ""],
        ])
        records, issues = parse_tariffs(path)
        assert len(records) == 1
        assert issues == []

    def test_category_carries_down_to_following_rows(self, tmp_path):
        path = _build_sheet(tmp_path, [
            ["Ипотека", None, None, None, None, None, None, None, None, None],
            [None, "Qulay makon", PAYROLL, None, 240, 15, None, 16, "", ""],
        ])
        records, _ = parse_tariffs(path)
        assert records[0]["section_name"] == "Ипотека"

    def test_missing_rate_is_reported_not_imported(self, tmp_path):
        path = _build_sheet(tmp_path, [
            ["Микрозайм", "Онлайн овердрафт", PAYROLL, "—", "—", None, None, None, "", ""],
        ])
        records, issues = parse_tariffs(path)

        assert records == []
        assert len(issues) == 1
        assert issues[0]["product"] == "Онлайн овердрафт"
        assert "ставка" in issues[0]["message"]

    def test_unknown_income_type_is_reported(self, tmp_path):
        path = _build_sheet(tmp_path, [
            ["Микрозайм", "Микрозайм 2.6", "Пенсионер", 12, 24, None, None, 25, "", ""],
        ])
        records, issues = parse_tariffs(path)

        assert records == []
        assert "неизвестный тип дохода" in issues[0]["message"]

    def test_unknown_category_is_reported(self, tmp_path):
        path = _build_sheet(tmp_path, [
            ["Овердрафт", "Что-то", PAYROLL, 12, 24, None, None, 25, "", ""],
        ])
        records, issues = parse_tariffs(path)

        assert records == []
        assert "неизвестная категория" in issues[0]["message"]

    def test_overlapping_ranges_are_reported(self, tmp_path):
        path = _build_sheet(tmp_path, [
            ["Микрозайм", "Онлайн микрозайм", PAYROLL, 13, 24, None, None, 25, "", ""],
            ["Микрозайм", "Онлайн микрозайм", PAYROLL, 20, 48, None, None, 27, "", ""],
        ])
        records, issues = parse_tariffs(path)

        # Both rows parse; the clash is reported so a human decides the fix.
        assert len(records) == 2
        assert any("пересекаются" in i["message"] for i in issues)

    def test_adjacent_term_tiers_do_not_clash(self, tmp_path):
        path = _build_sheet(tmp_path, [
            ["Микрозайм", "Онлайн микрозайм", PAYROLL, 13, 24, None, None, 25, "", ""],
            ["Микрозайм", "Онлайн микрозайм", PAYROLL, 25, 48, None, None, 27, "", ""],
        ])
        _records, issues = parse_tariffs(path)
        assert issues == []

    def test_same_ranges_for_different_income_types_do_not_clash(self, tmp_path):
        path = _build_sheet(tmp_path, [
            ["Ипотека", "Qulay makon", PAYROLL, None, 240, 15, None, 16, "", ""],
            ["Ипотека", "Qulay makon", "Зарплата на карту другого банка", None, 240, 15, None, 17, "", ""],
        ])
        _records, issues = parse_tariffs(path)
        assert issues == []

    def test_condition_kind_comes_from_the_matrix(self, tmp_path):
        path = _build_sheet(tmp_path, [
            ["Ипотека", "Qulay makon", PAYROLL, None, 240, 15, None, 16, "", ""],
        ])
        records, _ = parse_tariffs(path)
        assert records[0]["rate_condition_kind"] == PRODUCT_CONDITION_KIND["Qulay makon"]

    def test_percent_typed_sheet_parses_to_the_same_numbers(self, tmp_path):
        """A business user retyping "15%" must not change the imported value."""
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Тарифы"
        sheet.append(HEADERS)
        sheet.append(["Ипотека", "Qulay makon", PAYROLL, None, 240, 0.15, None, 0.16, "", ""])
        for column in ("F", "H"):
            sheet[f"{column}2"].number_format = "0%"
        path = tmp_path / "percent.xlsx"
        workbook.save(path)

        records, issues = parse_tariffs(path)
        assert issues == []
        assert records[0]["downpayment_min_pct"] == 15.0
        assert records[0]["rate_min_pct"] == 16.0


class TestIncomeTypeVocabulary:
    def test_maps_onto_the_agent_vocabulary(self):
        assert set(INCOME_TYPE_MAP.values()) == {"payroll", "official", "no_official"}


class TestRecordKindDoesNotBreakTheOldPath:
    """seed_records() gained a record-stated axis; the manifest path must be
    unaffected and a hand-picked axis in SQLAdmin must still win."""

    def test_record_kind_used_when_product_has_none(self):
        assert _product_condition_kind(None, "Ипотека", "term") == "term"

    def test_product_choice_still_beats_record_kind(self):
        product = SimpleNamespace(rate_condition_kind="downpayment")
        assert _product_condition_kind(product, "Ипотека", "term") == "downpayment"

    def test_no_record_kind_keeps_section_default(self):
        assert _product_condition_kind(None, "Ипотека") == "downpayment"
        assert _product_condition_kind(None, "Ипотека", None) == "downpayment"
        assert _product_condition_kind(None, "Ипотека", "  ") == "downpayment"


class TestMultipleTariffsInOneCell:
    """The sheet's most common filling mistake: three tariffs typed into one
    rate cell while the term columns hold the whole span. Parsing it yields a
    plausible but wrong rate, so it is refused instead."""

    def test_multiline_rate_is_refused(self, tmp_path):
        path = _build_sheet(tmp_path, [
            ["Микрозайм", "Онлайн микрозайм", PAYROLL, 13, 60, None, None,
             "24 oygacha - yillik 25 %\n48 oygacha - yillik 27 %\n60 oygacha - yillik 30 %", "", ""],
        ])
        records, issues = parse_tariffs(path)

        assert records == []
        assert "несколько тарифов" in issues[0]["message"]

    def test_repeated_term_markers_are_refused(self, tmp_path):
        path = _build_sheet(tmp_path, [
            ["Микрозайм", "Льготный микрозайм", PAYROLL, 3, 12, None, None,
             "3 oyga - 20 %, 6 oygacha - 22 %", "", ""],
        ])
        records, issues = parse_tariffs(path)

        assert records == []
        assert "несколько тарифов" in issues[0]["message"]

    def test_kredit_a_b_pair_is_refused(self, tmp_path):
        path = _build_sheet(tmp_path, [
            ["Ипотека", "BI Group", PAYROLL, 120, 240, 15, None,
             "Kredit A: 19% Kredit B: 22%", "", ""],
        ])
        records, issues = parse_tariffs(path)

        assert records == []
        assert "несколько тарифов" in issues[0]["message"]

    def test_a_plain_range_is_still_accepted(self, tmp_path):
        """«от 18,9% до 21,9%» is one tariff stated as a range, not two."""
        path = _build_sheet(tmp_path, [
            ["Автокредит", "Автокредит 2.6", PAYROLL, 12, 60, 20, 50,
             "от- 18,9%, до -21,9%", "", ""],
        ])
        records, issues = parse_tariffs(path)

        assert issues == []
        assert (records[0]["rate_min_pct"], records[0]["rate_max_pct"]) == (18.9, 21.9)
