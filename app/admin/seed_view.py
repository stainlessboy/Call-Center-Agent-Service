"""Admin view: seed/import data from Excel files into the database.

All source files are uploaded via the admin form every time — nothing is
persisted on disk between imports. Intermediate JSON manifests used by the
product pipeline are written to a temporary directory and cleaned up after
the seed completes.
"""
from __future__ import annotations

import asyncio
import json
import logging
import tempfile
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqladmin import BaseView, expose
from starlette.requests import Request

_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Product pipeline: xlsx → JSON manifest → DB
# ---------------------------------------------------------------------------

def _run_export_json(xlsx_path: Path, work_dir: Path) -> tuple[Path, int]:
    """Parse Excel into a manifest + split JSON files under `work_dir`.

    Returns (manifest_path, section_count).
    """
    from app.admin.services.products_excel import _parse_sheet, _build_split_manifest

    workbook = {
        "credit_products": _parse_sheet(xlsx_path, "Кредитные продукты"),
        "noncredit_products": _parse_sheet(xlsx_path, "Некредитные продукты"),
    }
    manifest_path = work_dir / "ai_chat_info.json"
    split_dir = work_dir / "ai_chat_info"
    split_manifest = _build_split_manifest(workbook, split_dir, work_dir)

    result = {
        "source_file": str(xlsx_path),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "format_version": 2,
        "layout": split_manifest,
    }
    manifest_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest_path, sum(len(v) for v in split_manifest.values())


async def _run_seed_credits(manifest_path: Path, replace: bool) -> tuple[int, int]:
    from app.admin.services.credit_seed import _seed
    return await _seed(manifest_path, replace=replace)


async def _run_seed_deposits(manifest_path: Path, replace: bool) -> tuple[int, int]:
    from app.admin.services.deposit_seed import _seed
    return await _seed(manifest_path, replace=replace)


async def _run_seed_cards(manifest_path: Path, replace: bool) -> tuple[int, int]:
    from app.admin.services.card_seed import _seed
    return await _seed(manifest_path, replace=replace)


async def _run_seed_faq(xlsx_path: Path, replace: bool) -> dict[str, Any]:
    from app.admin.services.faq_import import _extract_multilingual_items, _import_items

    items = await asyncio.to_thread(_extract_multilingual_items, xlsx_path, None, None, None)
    await _import_items(items, replace=replace, dry_run=False)
    lang_counts = {
        "ru": sum(1 for i in items if i.get("question_ru") and i.get("answer_ru")),
        "en": sum(1 for i in items if i.get("question_en") and i.get("answer_en")),
        "uz": sum(1 for i in items if i.get("question_uz") and i.get("answer_uz")),
    }
    return {"inserted": len(items), "languages": lang_counts}


async def _run_seed_branches(
    filials_path: Path,
    offices_path: Path,
    points_path: Path,
    replace: bool,
) -> dict[str, int]:
    from sqlalchemy import func, select

    from app.admin.services.branches_seed import _seed
    from app.db.models import Filial, SalesOffice, SalesPoint
    from app.db.session import get_session

    await _seed(filials_path, offices_path, points_path, replace=replace)
    async with get_session() as session:
        counts: dict[str, int] = {}
        for model, key in (
            (Filial, "filials"),
            (SalesOffice, "sales_offices"),
            (SalesPoint, "sales_points"),
        ):
            result = await session.execute(select(func.count()).select_from(model))
            counts[key] = int(result.scalar() or 0)
    return counts


# ---------------------------------------------------------------------------
# Form helpers
# ---------------------------------------------------------------------------

async def _save_upload(form_field: Any, dest: Path) -> int:
    """Write an UploadFile's bytes to `dest`. Returns the byte count."""
    contents = await form_field.read()
    dest.write_bytes(contents)
    return len(contents)


def _has_upload(upload: Any) -> bool:
    return bool(upload) and hasattr(upload, "filename") and bool(upload.filename)


# ---------------------------------------------------------------------------
# Admin View
# ---------------------------------------------------------------------------

class SeedAdmin(BaseView):
    name = "Импорт данных"
    icon = "fa-solid fa-file-import"

    @expose("/seed", methods=["GET"])
    async def seed_page(self, request: Request):
        return await self.templates.TemplateResponse(
            request,
            "seed.html",
            context={"results": None},
        )

    @expose("/seed", methods=["POST"])
    async def seed_action(self, request: Request):
        form = await request.form()
        action = form.get("action", "")
        results: list[dict[str, Any]] = []

        try:
            if action == "products":
                results = await self._seed_products(form)
            elif action == "faq":
                results = await self._seed_faq(form)
            elif action == "branches":
                results = await self._seed_branches(form)
            else:
                results = [{"label": "Ошибка", "status": "error", "detail": f"Неизвестное действие: {action}"}]
        except Exception as exc:
            _logger.exception("Seed action '%s' failed", action)
            results.append({
                "label": f"Ошибка ({action})",
                "status": "error",
                "detail": f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}",
            })

        return await self.templates.TemplateResponse(
            request,
            "seed.html",
            context={"results": results},
        )

    # ── Products: export JSON + seed credits/deposits/cards ──────────────

    async def _seed_products(self, form: Any) -> list[dict]:
        results: list[dict] = []
        replace = form.get("mode") == "replace"
        results.append({"label": "Режим", "status": "ok", "detail": "Перезапись" if replace else "Дополнение"})

        upload = form.get("products_file")
        if not _has_upload(upload):
            results.append({
                "label": "Файл продуктов",
                "status": "error",
                "detail": "Загрузите xlsx-файл через форму — дефолтного больше нет.",
            })
            return results

        with tempfile.TemporaryDirectory(prefix="seed_products_") as tmp:
            work_dir = Path(tmp)
            xlsx_path = work_dir / upload.filename
            size = await _save_upload(upload, xlsx_path)
            results.append({
                "label": "Загрузка файла",
                "status": "ok",
                "detail": f"'{upload.filename}' загружен ({size:,} байт)",
            })

            try:
                manifest_path, sections = await asyncio.to_thread(_run_export_json, xlsx_path, work_dir)
                results.append({"label": "Excel → JSON", "status": "ok", "detail": f"Секций: {sections}"})
            except Exception as exc:
                results.append({"label": "Excel → JSON", "status": "error", "detail": str(exc)})
                return results

            for label, runner in (
                ("Кредитные продукты", _run_seed_credits),
                ("Депозитные продукты", _run_seed_deposits),
                ("Карточные продукты", _run_seed_cards),
            ):
                try:
                    inserted, skipped = await runner(manifest_path, replace)
                    results.append({
                        "label": label,
                        "status": "ok",
                        "detail": f"Добавлено: {inserted}, пропущено: {skipped}",
                    })
                except Exception as exc:
                    results.append({"label": label, "status": "error", "detail": str(exc)})

        return results

    # ── FAQ ───────────────────────────────────────────────────────────────

    async def _seed_faq(self, form: Any) -> list[dict]:
        results: list[dict] = []
        replace = form.get("mode") == "replace"
        results.append({"label": "Режим", "status": "ok", "detail": "Перезапись" if replace else "Дополнение"})

        upload = form.get("faq_file")
        if not _has_upload(upload):
            results.append({
                "label": "Файл FAQ",
                "status": "error",
                "detail": "Загрузите xlsx-файл через форму — дефолтного больше нет.",
            })
            return results

        with tempfile.TemporaryDirectory(prefix="seed_faq_") as tmp:
            xlsx_path = Path(tmp) / upload.filename
            size = await _save_upload(upload, xlsx_path)
            results.append({
                "label": "Загрузка файла",
                "status": "ok",
                "detail": f"'{upload.filename}' загружен ({size:,} байт)",
            })

            try:
                faq_result = await _run_seed_faq(xlsx_path, replace)
                lang_info = ", ".join(
                    f"{lang.upper()}: {cnt}" for lang, cnt in faq_result["languages"].items() if cnt
                )
                results.append({
                    "label": "FAQ",
                    "status": "ok",
                    "detail": f"Добавлено: {faq_result['inserted']} записей. Языки: {lang_info}",
                })
            except Exception as exc:
                results.append({"label": "FAQ", "status": "error", "detail": str(exc)})

        return results

    # ── Branches ──────────────────────────────────────────────────────────

    async def _seed_branches(self, form: Any) -> list[dict]:
        results: list[dict] = []
        replace = form.get("mode") == "replace"
        results.append({"label": "Режим", "status": "ok", "detail": "Перезапись" if replace else "Дополнение"})

        uploads = {key: form.get(key) for key in ("filials_file", "offices_file", "points_file")}
        missing = [key for key, up in uploads.items() if not _has_upload(up)]
        if missing:
            results.append({
                "label": "Файлы филиалов",
                "status": "error",
                "detail": f"Нужно загрузить все 3 xlsx через форму. Отсутствуют: {', '.join(missing)}.",
            })
            return results

        with tempfile.TemporaryDirectory(prefix="seed_branches_") as tmp:
            work_dir = Path(tmp)
            paths: dict[str, Path] = {}
            for key, upload in uploads.items():
                dest = work_dir / upload.filename
                size = await _save_upload(upload, dest)
                paths[key] = dest
                results.append({
                    "label": f"Загрузка {upload.filename}",
                    "status": "ok",
                    "detail": f"{size:,} байт",
                })

            try:
                counts = await _run_seed_branches(
                    paths["filials_file"], paths["offices_file"], paths["points_file"], replace
                )
                for label, key in (
                    ("Филиалы (ЦБУ)", "filials"),
                    ("Офисы продаж", "sales_offices"),
                    ("Точки продаж", "sales_points"),
                ):
                    results.append({"label": label, "status": "ok", "detail": f"В базе: {counts[key]}"})
            except Exception as exc:
                _logger.exception("Seed branches failed")
                results.append({
                    "label": "Импорт филиалов",
                    "status": "error",
                    "detail": f"{type(exc).__name__}: {exc}",
                })

        return results
