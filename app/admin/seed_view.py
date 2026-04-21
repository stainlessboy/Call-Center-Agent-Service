"""Admin view: seed/import data from Excel files into the database."""
from __future__ import annotations

import asyncio
import logging
import shutil
import traceback
from pathlib import Path
from typing import Any

from sqladmin import BaseView, expose
from starlette.requests import Request

_logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS_DIR = _PROJECT_ROOT / "scripts"
_DEFAULT_PRODUCTS_XLSX = _SCRIPTS_DIR / "AI CHAT INFO.xlsx"
_DEFAULT_FAQ_XLSX = _SCRIPTS_DIR / "FAQ (3 языка).xlsx"
_FALLBACK_FAQ_XLSX = _SCRIPTS_DIR / "FAQ.xlsx"
_MANIFEST_PATH = _PROJECT_ROOT / "app" / "data" / "ai_chat_info.json"

# Branch xlsx sources (must match paths hardcoded in scripts/seed_branches.py)
_BRANCH_FILIALS_XLSX = _SCRIPTS_DIR / "Локации филиалов.xlsx"
_BRANCH_OFFICES_XLSX = _SCRIPTS_DIR / "Локации офисов продаж.xlsx"
_BRANCH_POINTS_XLSX = _SCRIPTS_DIR / "Локации точек продаж.xlsx"


# ---------------------------------------------------------------------------
# Helpers: run seed logic in-process (not subprocess) for reliability
# ---------------------------------------------------------------------------

def _run_export_json(xlsx_path: Path) -> dict[str, Any]:
    """Run export_ai_chat_info_json logic and return stats."""
    from scripts.export_ai_chat_info_json import _parse_sheet, _build_split_manifest

    import json
    from datetime import datetime, timezone

    workbook = {
        "credit_products": _parse_sheet(xlsx_path, "Кредитные продукты"),
        "noncredit_products": _parse_sheet(xlsx_path, "Некредитные продукты"),
    }
    split_dir = _MANIFEST_PATH.parent / "ai_chat_info"
    split_manifest = _build_split_manifest(workbook, split_dir, _MANIFEST_PATH.parent)

    result = {
        "source_file": str(xlsx_path),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "format_version": 2,
        "layout": split_manifest,
    }
    _MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    _MANIFEST_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    section_count = sum(len(v) for v in split_manifest.values())
    return {"sections": section_count, "manifest": str(_MANIFEST_PATH)}


async def _run_seed_credits(replace: bool) -> tuple[int, int]:
    from scripts.seed_credit_product_offers import _seed
    return await _seed(_MANIFEST_PATH, replace=replace)


async def _run_seed_deposits(replace: bool) -> tuple[int, int]:
    from scripts.seed_deposit_product_offers import _seed
    return await _seed(_MANIFEST_PATH, replace=replace)


async def _run_seed_cards(replace: bool) -> tuple[int, int]:
    from scripts.seed_card_product_offers import _seed
    return await _seed(_MANIFEST_PATH, replace=replace)


async def _run_seed_faq(xlsx_path: Path, replace: bool) -> dict[str, Any]:
    from scripts.import_faq_xlsx import _extract_multilingual_items, _import_items
    items = await asyncio.to_thread(_extract_multilingual_items, xlsx_path, None, None, None)
    await _import_items(items, replace=replace, dry_run=False)
    lang_counts = {
        "ru": sum(1 for i in items if i.get("question_ru") and i.get("answer_ru")),
        "en": sum(1 for i in items if i.get("question_en") and i.get("answer_en")),
        "uz": sum(1 for i in items if i.get("question_uz") and i.get("answer_uz")),
    }
    return {"inserted": len(items), "languages": lang_counts}


async def _run_seed_branches(replace: bool) -> dict[str, int]:
    """Run branches seed and return per-table row counts from the DB."""
    from sqlalchemy import func, select

    from app.db.models import Filial, SalesOffice, SalesPoint
    from app.db.session import get_session
    from scripts.seed_branches import _seed

    await _seed(replace=replace)
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
            context={
                "results": None,
                "products_xlsx": _DEFAULT_PRODUCTS_XLSX.name if _DEFAULT_PRODUCTS_XLSX.exists() else None,
                "faq_xlsx": _DEFAULT_FAQ_XLSX.name if _DEFAULT_FAQ_XLSX.exists() else (_FALLBACK_FAQ_XLSX.name if _FALLBACK_FAQ_XLSX.exists() else None),
            },
        )

    @expose("/seed", methods=["POST"])
    async def seed_action(self, request: Request):
        form = await request.form()
        # Note: the UploadFile objects in form are only valid during this
        # request, so we must read/save them before rendering the response.
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
            context={
                "results": results,
                "products_xlsx": _DEFAULT_PRODUCTS_XLSX.name if _DEFAULT_PRODUCTS_XLSX.exists() else None,
                "faq_xlsx": _DEFAULT_FAQ_XLSX.name if _DEFAULT_FAQ_XLSX.exists() else (_FALLBACK_FAQ_XLSX.name if _FALLBACK_FAQ_XLSX.exists() else None),
                "filials_xlsx": _BRANCH_FILIALS_XLSX.name if _BRANCH_FILIALS_XLSX.exists() else None,
                "offices_xlsx": _BRANCH_OFFICES_XLSX.name if _BRANCH_OFFICES_XLSX.exists() else None,
                "points_xlsx": _BRANCH_POINTS_XLSX.name if _BRANCH_POINTS_XLSX.exists() else None,
            },
        )

    # ── Products: export JSON + seed credits/deposits/cards ──────────────

    async def _seed_products(self, form: Any) -> list[dict]:
        results: list[dict] = []
        replace = form.get("mode") == "replace"
        mode_label = "Перезапись" if replace else "Дополнение"
        results.append({"label": "Режим", "status": "ok", "detail": mode_label})

        # Determine xlsx path: uploaded file or default
        xlsx_path = _DEFAULT_PRODUCTS_XLSX
        upload = form.get("products_file")
        if upload and hasattr(upload, "filename") and upload.filename:
            # Save uploaded file to scripts/
            dest = _SCRIPTS_DIR / upload.filename
            contents = await upload.read()
            dest.write_bytes(contents)
            xlsx_path = dest
            # Also overwrite the default location so future seeds use it
            if dest.name != _DEFAULT_PRODUCTS_XLSX.name:
                shutil.copy2(dest, _DEFAULT_PRODUCTS_XLSX)
            results.append({
                "label": "Загрузка файла",
                "status": "ok",
                "detail": f"Файл '{upload.filename}' загружен ({len(contents):,} байт)",
            })

        if not xlsx_path.exists():
            results.append({
                "label": "Файл продуктов",
                "status": "error",
                "detail": f"Файл не найден: {xlsx_path.name}",
            })
            return results

        # Step 1: Export JSON
        try:
            export_stats = await asyncio.to_thread(_run_export_json, xlsx_path)
            results.append({
                "label": "Экспорт Excel → JSON",
                "status": "ok",
                "detail": f"Секций: {export_stats['sections']}",
            })
        except Exception as exc:
            results.append({
                "label": "Экспорт Excel → JSON",
                "status": "error",
                "detail": str(exc),
            })
            return results

        # Step 2: Seed credits
        try:
            inserted, skipped = await _run_seed_credits(replace)
            results.append({
                "label": "Кредитные продукты",
                "status": "ok",
                "detail": f"Добавлено: {inserted}, пропущено: {skipped}",
            })
        except Exception as exc:
            results.append({
                "label": "Кредитные продукты",
                "status": "error",
                "detail": str(exc),
            })

        # Step 3: Seed deposits
        try:
            inserted, skipped = await _run_seed_deposits(replace)
            results.append({
                "label": "Депозитные продукты",
                "status": "ok",
                "detail": f"Добавлено: {inserted}, пропущено: {skipped}",
            })
        except Exception as exc:
            results.append({
                "label": "Депозитные продукты",
                "status": "error",
                "detail": str(exc),
            })

        # Step 4: Seed cards
        try:
            inserted, skipped = await _run_seed_cards(replace)
            results.append({
                "label": "Карточные продукты",
                "status": "ok",
                "detail": f"Добавлено: {inserted}, пропущено: {skipped}",
            })
        except Exception as exc:
            results.append({
                "label": "Карточные продукты",
                "status": "error",
                "detail": str(exc),
            })

        return results

    # ── FAQ ───────────────────────────────────────────────────────────────

    async def _seed_faq(self, form: Any) -> list[dict]:
        results: list[dict] = []
        replace = form.get("mode") == "replace"
        mode_label = "Перезапись" if replace else "Дополнение"
        results.append({"label": "Режим", "status": "ok", "detail": mode_label})

        xlsx_path = _DEFAULT_FAQ_XLSX if _DEFAULT_FAQ_XLSX.exists() else _FALLBACK_FAQ_XLSX
        upload = form.get("faq_file")
        if upload and hasattr(upload, "filename") and upload.filename:
            dest = _SCRIPTS_DIR / upload.filename
            contents = await upload.read()
            dest.write_bytes(contents)
            xlsx_path = dest
            results.append({
                "label": "Загрузка файла",
                "status": "ok",
                "detail": f"Файл '{upload.filename}' загружен ({len(contents):,} байт)",
            })

        if not xlsx_path.exists():
            results.append({
                "label": "Файл FAQ",
                "status": "error",
                "detail": f"Файл не найден: {xlsx_path.name}",
            })
            return results

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
            results.append({
                "label": "FAQ",
                "status": "error",
                "detail": str(exc),
            })

        return results

    # ── Branches ──────────────────────────────────────────────────────────

    async def _seed_branches(self, form: Any) -> list[dict]:
        results: list[dict] = []
        replace = form.get("mode") == "replace"
        mode_label = "Перезапись" if replace else "Дополнение"
        results.append({"label": "Режим", "status": "ok", "detail": mode_label})

        # Save any uploaded xlsx files over the default scripts/*.xlsx paths
        # so seed_branches.py picks them up
        for form_field, dest_path in (
            ("filials_file", _BRANCH_FILIALS_XLSX),
            ("offices_file", _BRANCH_OFFICES_XLSX),
            ("points_file", _BRANCH_POINTS_XLSX),
        ):
            upload = form.get(form_field)
            if upload and hasattr(upload, "filename") and upload.filename:
                contents = await upload.read()
                dest_path.parent.mkdir(parents=True, exist_ok=True)
                dest_path.write_bytes(contents)
                results.append({
                    "label": f"Загрузка {dest_path.name}",
                    "status": "ok",
                    "detail": f"Файл '{upload.filename}' сохранён ({len(contents):,} байт)",
                })

        # Verify all 3 source files are present
        missing = [p.name for p in (
            _BRANCH_FILIALS_XLSX, _BRANCH_OFFICES_XLSX, _BRANCH_POINTS_XLSX,
        ) if not p.exists()]
        if missing:
            results.append({
                "label": "Файлы филиалов",
                "status": "error",
                "detail": f"Не найдены: {', '.join(missing)}. Загрузите их через форму.",
            })
            return results

        try:
            counts = await _run_seed_branches(replace)
            results.append({
                "label": "Филиалы (ЦБУ)",
                "status": "ok",
                "detail": f"В базе: {counts['filials']}",
            })
            results.append({
                "label": "Офисы продаж",
                "status": "ok",
                "detail": f"В базе: {counts['sales_offices']}",
            })
            results.append({
                "label": "Точки продаж",
                "status": "ok",
                "detail": f"В базе: {counts['sales_points']}",
            })
        except Exception as exc:
            _logger.exception("Seed branches failed")
            results.append({
                "label": "Импорт филиалов",
                "status": "error",
                "detail": f"{type(exc).__name__}: {exc}",
            })

        return results
