"""Admin view: seed/import data from Excel files into the database.

All source files are uploaded via the admin form every time — nothing is
persisted on disk between imports. Intermediate JSON manifests used by the
product pipeline are written to a temporary directory and cleaned up after
the seed completes.
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import tempfile
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqladmin import BaseView, expose
from starlette.requests import Request
from starlette.responses import Response

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


async def _run_seed_faq(file_path: Path, replace: bool) -> dict[str, Any]:
    """Import FAQ from xlsx / csv / json (dispatched by file extension)."""
    from app.admin.services.faq_import import _extract_multilingual_items_any, _import_items

    items = await asyncio.to_thread(_extract_multilingual_items_any, file_path)
    await _import_items(items, replace=replace, dry_run=False)
    lang_counts = {
        "ru": sum(1 for i in items if i.get("question_ru") and i.get("answer_ru")),
        "en": sum(1 for i in items if i.get("question_en") and i.get("answer_en")),
        "uz": sum(1 for i in items if i.get("question_uz") and i.get("answer_uz")),
    }
    return {"inserted": len(items), "languages": lang_counts}


async def _run_recompute_faq_embeddings() -> dict[str, int]:
    """Backfill missing FAQ embeddings.

    Walks rows where any of the three embedding columns is NULL, batches the
    matching question texts to OpenAI per language, and UPDATEs the rows.
    Idempotent — repeat invocations skip rows that are already filled. Per-row
    failures (e.g. OpenAI quota, partial outage) are silently skipped; rerun
    later to fill them in.
    """
    from sqlalchemy import or_, select as sql_select, update

    from app.db.models import FaqItem
    from app.db.session import get_session
    from app.utils.embeddings import embed_texts

    BATCH_SIZE = 100
    counts = {"ru": 0, "en": 0, "uz": 0, "scanned": 0}

    async with get_session() as session:
        result = await session.execute(
            sql_select(
                FaqItem.id,
                FaqItem.question_ru,
                FaqItem.question_en,
                FaqItem.question_uz,
                FaqItem.embedding_ru,
                FaqItem.embedding_en,
                FaqItem.embedding_uz,
            ).where(
                or_(
                    FaqItem.embedding_ru.is_(None),
                    FaqItem.embedding_en.is_(None),
                    FaqItem.embedding_uz.is_(None),
                )
            )
        )
        rows = result.all()

    counts["scanned"] = len(rows)
    if not rows:
        return counts

    for lang in ("ru", "en", "uz"):
        # Collect rows where this language's embedding is missing AND the
        # question text exists.
        targets: list[tuple[int, str]] = []
        for row in rows:
            faq_id, q_ru, q_en, q_uz, e_ru, e_en, e_uz = row
            qmap = {"ru": q_ru, "en": q_en, "uz": q_uz}
            emap = {"ru": e_ru, "en": e_en, "uz": e_uz}
            if qmap[lang] and emap[lang] is None:
                targets.append((faq_id, qmap[lang]))
        if not targets:
            continue

        for start in range(0, len(targets), BATCH_SIZE):
            chunk = targets[start : start + BATCH_SIZE]
            vectors = await embed_texts([t for _, t in chunk])
            async with get_session() as session:
                col = {"ru": FaqItem.embedding_ru, "en": FaqItem.embedding_en, "uz": FaqItem.embedding_uz}[lang]
                for (faq_id, _), vec in zip(chunk, vectors):
                    if vec is None:
                        continue
                    await session.execute(
                        update(FaqItem).where(FaqItem.id == faq_id).values({col: vec})
                    )
                    counts[lang] += 1
                await session.commit()

    return counts


async def _export_faq_rows() -> list[dict[str, Any]]:
    """Fetch all FAQ rows as plain dicts ready for xlsx/json output."""
    from sqlalchemy import select as sql_select

    from app.db.models import FaqItem
    from app.db.session import get_session

    async with get_session() as session:
        result = await session.execute(sql_select(FaqItem).order_by(FaqItem.id))
        items = result.scalars().all()
    return [
        {
            "id": it.id,
            "question_ru": it.question_ru or "",
            "answer_ru": it.answer_ru or "",
            "question_en": it.question_en or "",
            "answer_en": it.answer_en or "",
            "question_uz": it.question_uz or "",
            "answer_uz": it.answer_uz or "",
        }
        for it in items
    ]


# Wide multilingual layout: one row = one FAQ item, languages never drift
# apart on re-import (the old 3-sheet layout merged sheets by row index and
# misaligned as soon as one item lacked a translation).
FAQ_EXPORT_COLUMNS = [
    "question_ru", "answer_ru",
    "question_en", "answer_en",
    "question_uz", "answer_uz",
]


def _build_faq_xlsx(rows: list[dict[str, Any]]) -> bytes:
    """Build a single-sheet wide multilingual FAQ xlsx.

    Header matches `faq_import` wide-format detection, so the file can be
    uploaded back via /admin/seed as-is (export → import round-trip).
    """
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "FAQ"
    ws.append(FAQ_EXPORT_COLUMNS)
    for row in rows:
        ws.append([row.get(col) or "" for col in FAQ_EXPORT_COLUMNS])

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _build_faq_csv(rows: list[dict[str, Any]]) -> bytes:
    """Build a wide multilingual FAQ csv (same columns as the xlsx export).

    UTF-8 with BOM so Excel opens Cyrillic correctly.
    """
    import csv

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(FAQ_EXPORT_COLUMNS)
    for row in rows:
        writer.writerow([row.get(col) or "" for col in FAQ_EXPORT_COLUMNS])
    return b"\xef\xbb\xbf" + buf.getvalue().encode("utf-8")


# ---------------------------------------------------------------------------
# Chat export: all sessions + their messages → readable transcript / JSON
# ---------------------------------------------------------------------------

# Russian role labels for the readable transcript (mirrors _ROLE_MAP in views.py).
_CHAT_ROLE_LABELS_RU = {
    "user": "Пользователь",
    "assistant": "Ассистент",
    "operator": "Оператор",
    "system": "Система",
}


def _fmt_export_dt(dt: Any) -> str:
    return dt.strftime("%d.%m.%Y %H:%M") if isinstance(dt, datetime) else (str(dt) if dt else "")


def _fmt_export_time(dt: Any) -> str:
    return dt.strftime("%H:%M") if isinstance(dt, datetime) else ""


def _iso_or_none(dt: Any) -> str | None:
    return dt.isoformat() if isinstance(dt, datetime) else None


async def _export_chat_rows() -> list[dict[str, Any]]:
    """Fetch all chat sessions with their messages, ordered, as plain dicts.

    Only the messages themselves (role/text/timestamp) plus minimal session
    identification (id, user, start time) — no analytics metadata (token usage,
    latency, feedback, etc.). Loads everything into memory in one pass.
    """
    from sqlalchemy import select as sql_select
    from sqlalchemy.orm import selectinload

    from app.db.models import ChatSession
    from app.db.session import get_session

    async with get_session() as session:
        result = await session.execute(
            sql_select(ChatSession)
            .options(
                selectinload(ChatSession.messages),
                selectinload(ChatSession.user),
            )
            .order_by(ChatSession.started_at, ChatSession.id)
        )
        sessions = result.scalars().all()

    rows: list[dict[str, Any]] = []
    for s in sessions:
        user = s.user
        user_label = ""
        if user is not None:
            user_label = user.username or user.first_name or str(user.telegram_user_id)
        rows.append(
            {
                "id": s.id,
                "user": user_label,
                "started_at": s.started_at,
                "messages": [
                    {"role": m.role, "text": m.text or "", "created_at": m.created_at}
                    for m in s.messages
                ],
            }
        )
    return rows


def _build_chats_txt(sessions: list[dict[str, Any]]) -> bytes:
    """Build a human-readable transcript of all chats (UTF-8 plain text)."""
    lines: list[str] = []
    for s in sessions:
        user = s["user"] or "—"
        lines.append(f"═══ Сессия {s['id']} — {user} — {_fmt_export_dt(s['started_at'])} ═══")
        if not s["messages"]:
            lines.append("(нет сообщений)")
        for m in s["messages"]:
            role = _CHAT_ROLE_LABELS_RU.get(m["role"], m["role"] or "?")
            ts = _fmt_export_time(m["created_at"])
            prefix = f"[{ts}] " if ts else ""
            lines.append(f"{prefix}{role}: {m['text']}")
        lines.append("")  # blank line between sessions
    return "\n".join(lines).encode("utf-8")


def _build_chats_json(sessions: list[dict[str, Any]]) -> bytes:
    """Build a structured JSON export: sessions with nested messages."""
    payload = {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "count": len(sessions),
        "sessions": [
            {
                "id": s["id"],
                "user": s["user"],
                "started_at": _iso_or_none(s["started_at"]),
                "messages": [
                    {
                        "role": m["role"],
                        "text": m["text"],
                        "created_at": _iso_or_none(m["created_at"]),
                    }
                    for m in s["messages"]
                ],
            }
            for s in sessions
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")


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
            elif action == "recompute_embeddings":
                results = await self._recompute_embeddings()
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
                "detail": "Загрузите файл (xlsx / csv / json) через форму — дефолтного больше нет.",
            })
            return results

        suffix = Path(upload.filename).suffix.lower()
        if suffix not in (".xlsx", ".xls", ".csv", ".json"):
            results.append({
                "label": "Файл FAQ",
                "status": "error",
                "detail": f"Неподдерживаемый формат '{suffix}'. Допустимы: xlsx, csv, json.",
            })
            return results

        with tempfile.TemporaryDirectory(prefix="seed_faq_") as tmp:
            file_path = Path(tmp) / upload.filename
            size = await _save_upload(upload, file_path)
            results.append({
                "label": "Загрузка файла",
                "status": "ok",
                "detail": f"'{upload.filename}' загружен ({size:,} байт)",
            })

            try:
                faq_result = await _run_seed_faq(file_path, replace)
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

    # ── Backfill FAQ embeddings ──────────────────────────────────────────

    async def _recompute_embeddings(self) -> list[dict]:
        results: list[dict] = []
        try:
            counts = await _run_recompute_faq_embeddings()
        except Exception as exc:
            _logger.exception("Recompute FAQ embeddings failed")
            results.append({
                "label": "Пересчёт эмбеддингов",
                "status": "error",
                "detail": f"{type(exc).__name__}: {exc}",
            })
            return results
        if counts["scanned"] == 0:
            results.append({
                "label": "Пересчёт эмбеддингов",
                "status": "ok",
                "detail": "Все эмбеддинги уже на месте — пересчитывать нечего.",
            })
            return results
        details = ", ".join(f"{lang.upper()}: {counts[lang]}" for lang in ("ru", "en", "uz"))
        results.append({
            "label": "Пересчёт эмбеддингов",
            "status": "ok",
            "detail": f"Просмотрено строк: {counts['scanned']}. Обновлено: {details}.",
        })
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


class FaqExportAdmin(BaseView):
    """FAQ export download endpoints, hidden from the admin menu.

    Kept in a separate BaseView on purpose: sqladmin overwrites `view.identity`
    with every @expose-d method (alphabetically first one wins) and builds the
    menu link from it — extra routes on SeedAdmin would turn the «Импорт
    данных» menu item into a file download.
    """

    name = "FAQ Export"
    icon = "fa-solid fa-file-export"

    def is_visible(self, request: Request) -> bool:
        return False

    @expose("/seed/export-faq.xlsx", methods=["GET"])
    async def export_faq_xlsx(self, request: Request):
        rows = await _export_faq_rows()
        data = await asyncio.to_thread(_build_faq_xlsx, rows)
        filename = f"faq_export_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.xlsx"
        return Response(
            content=data,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @expose("/seed/export-faq.csv", methods=["GET"])
    async def export_faq_csv(self, request: Request):
        rows = await _export_faq_rows()
        data = await asyncio.to_thread(_build_faq_csv, rows)
        filename = f"faq_export_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.csv"
        return Response(
            content=data,
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @expose("/seed/export-faq.json", methods=["GET"])
    async def export_faq_json(self, request: Request):
        rows = await _export_faq_rows()
        payload = {
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "count": len(rows),
            "items": rows,
        }
        filename = f"faq_export_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json"
        return Response(
            content=json.dumps(payload, ensure_ascii=False, indent=2),
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )


class ChatExportAdmin(BaseView):
    """Visible menu entry «Экспорт чатов» — a landing page with download buttons.

    Single @expose on purpose: the actual file downloads live in
    ChatExportDownloadAdmin (hidden), because multiple @expose-d methods make
    sqladmin build the menu link from the alphabetically-first route.
    """

    name = "Экспорт чатов"
    icon = "fa-solid fa-comments"

    @expose("/export-chats", methods=["GET"])
    async def export_page(self, request: Request):
        return await self.templates.TemplateResponse(request, "chat_export.html", context={})


class ChatExportDownloadAdmin(BaseView):
    """Chat export download endpoints, hidden from the admin menu.

    Same reasoning as FaqExportAdmin: the download routes are isolated in their
    own hidden view so they don't hijack a menu item's link.
    """

    name = "Chat Export Download"
    icon = "fa-solid fa-file-export"

    def is_visible(self, request: Request) -> bool:
        return False

    @expose("/export-chats.txt", methods=["GET"])
    async def export_chats_txt(self, request: Request):
        rows = await _export_chat_rows()
        data = await asyncio.to_thread(_build_chats_txt, rows)
        filename = f"chats_export_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.txt"
        return Response(
            content=data,
            media_type="text/plain; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @expose("/export-chats.json", methods=["GET"])
    async def export_chats_json(self, request: Request):
        rows = await _export_chat_rows()
        data = await asyncio.to_thread(_build_chats_json, rows)
        filename = f"chats_export_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json"
        return Response(
            content=data,
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
