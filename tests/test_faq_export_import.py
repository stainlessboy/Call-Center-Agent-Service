"""Round-trip tests for FAQ export/import at /admin/seed.

Export (xlsx / csv / json) must be re-importable as-is, including items that
lack EN/UZ translations — the old 3-sheet xlsx layout misaligned languages on
such items because sheets were merged by row index.
"""
import csv
import io
import json

import pytest

from app.admin.seed_view import FAQ_EXPORT_COLUMNS, _build_faq_csv, _build_faq_xlsx
from app.admin.services.faq_import import (
    _extract_multilingual_items_any,
    _extract_wide_items_from_rows,
)

ROWS = [
    {
        "id": 1,
        "question_ru": "Q1ru", "answer_ru": "A1ru",
        "question_en": "Q1en", "answer_en": "A1en",
        "question_uz": "Q1uz", "answer_uz": "A1uz",
    },
    {
        # No EN/UZ translation — the case that used to misalign languages.
        "id": 2,
        "question_ru": "Q2ru", "answer_ru": "A2ru",
        "question_en": "", "answer_en": "",
        "question_uz": "", "answer_uz": "",
    },
    {
        "id": 3,
        "question_ru": "Q3ru", "answer_ru": "A3ru",
        "question_en": "Q3en", "answer_en": "A3en",
        "question_uz": "Q3uz", "answer_uz": "A3uz",
    },
]

EXPECTED = [
    {
        "question_ru": "Q1ru", "answer_ru": "A1ru",
        "question_en": "Q1en", "answer_en": "A1en",
        "question_uz": "Q1uz", "answer_uz": "A1uz",
    },
    {
        "question_ru": "Q2ru", "answer_ru": "A2ru",
        "question_en": None, "answer_en": None,
        "question_uz": None, "answer_uz": None,
    },
    {
        "question_ru": "Q3ru", "answer_ru": "A3ru",
        "question_en": "Q3en", "answer_en": "A3en",
        "question_uz": "Q3uz", "answer_uz": "A3uz",
    },
]


class TestRoundTrip:
    def test_xlsx(self, tmp_path):
        path = tmp_path / "export.xlsx"
        path.write_bytes(_build_faq_xlsx(ROWS))
        assert _extract_multilingual_items_any(path) == EXPECTED

    def test_csv(self, tmp_path):
        path = tmp_path / "export.csv"
        path.write_bytes(_build_faq_csv(ROWS))
        assert _extract_multilingual_items_any(path) == EXPECTED

    def test_csv_has_bom_for_excel(self):
        assert _build_faq_csv(ROWS).startswith(b"\xef\xbb\xbf")

    def test_json(self, tmp_path):
        path = tmp_path / "export.json"
        path.write_text(
            json.dumps({"exported_at": "x", "count": len(ROWS), "items": ROWS}, ensure_ascii=False),
            encoding="utf-8",
        )
        assert _extract_multilingual_items_any(path) == EXPECTED

    def test_json_bare_list(self, tmp_path):
        path = tmp_path / "export.json"
        path.write_text(json.dumps(ROWS, ensure_ascii=False), encoding="utf-8")
        assert _extract_multilingual_items_any(path) == EXPECTED


class TestSqladminListExport:
    def test_csv_with_id_column_and_label_headers(self, tmp_path):
        """The built-in list export (id + 'Вопрос (RU)'-style labels) imports too."""
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow([
            "id",
            "Вопрос (RU)", "Ответ (RU)",
            "Вопрос (EN)", "Ответ (EN)",
            "Вопрос (UZ)", "Ответ (UZ)",
        ])
        for row in ROWS:
            writer.writerow([row["id"]] + [row[col] for col in FAQ_EXPORT_COLUMNS])
        path = tmp_path / "sqladmin.csv"
        path.write_text(buf.getvalue(), encoding="utf-8")
        assert _extract_multilingual_items_any(path) == EXPECTED


class TestLegacyFormats:
    def test_per_language_sheets_still_parse(self, tmp_path):
        from openpyxl import Workbook

        wb = Workbook()
        wb.remove(wb.active)
        for lang in ("RU", "EN", "UZ"):
            ws = wb.create_sheet(title=lang)
            ws.append(["question", "answer"])
            for row in ROWS:
                ws.append([row[f"question_{lang.lower()}"], row[f"answer_{lang.lower()}"]])
        path = tmp_path / "legacy.xlsx"
        wb.save(path)

        items = _extract_multilingual_items_any(path)
        assert len(items) == 3
        assert items[0]["question_ru"] == "Q1ru"
        assert items[0]["answer_uz"] == "A1uz"

    def test_two_column_csv_treated_as_ru(self, tmp_path):
        path = tmp_path / "plain.csv"
        path.write_text("question,answer\nКак дела?,Хорошо\n", encoding="utf-8")
        items = _extract_multilingual_items_any(path)
        assert items == [{
            "question_ru": "Как дела?", "answer_ru": "Хорошо",
            "question_en": None, "answer_en": None,
            "question_uz": None, "answer_uz": None,
        }]

    def test_unsupported_json_shape_raises(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text('{"foo": 1}', encoding="utf-8")
        with pytest.raises(ValueError):
            _extract_multilingual_items_any(path)


class TestWideParser:
    def test_rows_without_ru_pair_are_skipped(self):
        rows = [
            ("question_ru", "answer_ru", "question_en", "answer_en"),
            ("Qru", "Aru", "Qen", "Aen"),
            ("", "", "Qen only", "Aen only"),  # no RU pair → NOT NULL in DB → skip
        ]
        items = _extract_wide_items_from_rows(rows, None)
        assert len(items) == 1
        assert items[0]["question_ru"] == "Qru"

    def test_half_pair_in_optional_language_dropped(self):
        rows = [
            ("question_ru", "answer_ru", "question_en", "answer_en"),
            ("Qru", "Aru", "Qen", ""),  # EN answer missing → drop the half pair
        ]
        items = _extract_wide_items_from_rows(rows, None)
        assert items[0]["question_en"] is None
        assert items[0]["answer_en"] is None

    def test_plain_question_answer_header_is_not_wide(self):
        rows = [("question", "answer"), ("Q", "A")]
        assert _extract_wide_items_from_rows(rows, None) is None
