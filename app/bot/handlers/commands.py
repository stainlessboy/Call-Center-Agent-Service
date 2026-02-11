from __future__ import annotations

import logging
import os
import re
from typing import Any, Sequence

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from app.bot.keyboards.common import contact_keyboard, location_keyboard
from app.bot.keyboards.feedback import feedback_keyboard, language_keyboard
from app.bot.keyboards.human import human_mode_keyboard
from app.bot.keyboards.menu import (
    BACK,
    BRANCHES,
    CHANGE_LANGUAGE,
    CURRENCY_RATES,
    END_SESSION,
    NEAREST_BRANCH,
    MY_SESSIONS,
    NEW_CHAT,
    chat_keyboard,
    main_menu_keyboard,
)
from app.config import get_settings
from app.services.chat_service import ChatService

router = Router()
logger = logging.getLogger(__name__)


def _normalize_region(text: str | None) -> str | None:
    if not text:
        return None
    lower = text.lower().strip()
    mapping = {
        "ташкент": "Ташкент",
        "tashkent": "Ташкент",
        "samarkand": "Самарканд",
        "самарканд": "Самарканд",
        "bukhara": "Бухара",
        "бухара": "Бухара",
        "andijan": "Андижан",
        "андижан": "Андижан",
    }
    return mapping.get(lower, text)


def _safe_cb(text: str) -> str:
    return text.replace(":", ";")


def _unsafecb(text: str) -> str:
    return text.replace(";", ":")


def _inline_keyboard(labels: list[str], prefix: str, row_size: int = 2) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for label in labels:
        row.append(InlineKeyboardButton(text=label, callback_data=f"{prefix}:{_safe_cb(label)}"))
        if len(row) >= row_size:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _strip_leading_symbols(text: str) -> str:
    return re.sub(r"^[^\wА-Яа-я]+", "", text).strip()


def _normalize_for_match(text: str) -> str:
    normalized_text = _strip_leading_symbols(text)
    lowered = re.sub(r"[^\w\s]+", " ", normalized_text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", lowered).strip().lower()


def _format_source_reply(text: str, source: str, name: str | None = None) -> str:
    if not text:
        return text
    label = "🤖 Бот" if source == "bot" else "👤 Оператор"
    if name:
        label = f"{label} ({name})"
    return f"{label}: {text}"


def _user_language_text(lang: str | None) -> dict[str, str]:
    return {
        "start": "Вы в чате с агентом банка. Я сохраню историю сообщений.\nКоманды: /new — новая сессия, /end — завершить текущую.",
        "share_phone": "Поделитесь, пожалуйста, номером телефона для связи.\nКоманды: /new — новая сессия, /end — завершить текущую.",
        "ask_language": "Выберите язык / Choose language / Tilni tanlang:",
        "session_closed_timeout": "Сессия закрыта из-за отсутствия активности. Оцените работу агента:",
        "feedback_saved": "Спасибо за оценку!",
        "feedback_failed": "Не удалось сохранить оценку. Попробуйте позже.",
    }


def format_branch(branch) -> str:
    lat = getattr(branch, "latitude", None)
    lon = getattr(branch, "longitude", None)
    lines = [
        f"🏦 {branch.name}",
        f"📌 Адрес: {branch.address or '-'}",
        f"🎯 Ориентиры: {branch.landmarks or '-'}",
        f"Ⓜ️ Метро: {branch.metro or '-'}",
        f"📞 Телефон: {branch.phone or '-'}",
        f"🕘 Время работы: {branch.hours or '-'}",
        f"❌ {branch.weekend or '-'}",
        "",
        "🧾 Реквизиты",
        f"🔢 ИНН: {branch.inn or '-'}",
        f"🏛 МФО: {branch.mfo or '-'}",
        f"📮 Индекс: {branch.postal_index or '-'}",
    ]
    if branch.uzcard_accounts:
        lines.append("")
        lines.append("💳 Транзитные счета Uzcard:\n" + branch.uzcard_accounts)
    if branch.humo_accounts:
        lines.append("")
        lines.append("💳 Транзитные счета HUMO:\n" + branch.humo_accounts)
    if lat and lon:
        lines.append("")
        lines.append(f"📍 Локация: https://maps.google.com/maps?q={lat},{lon}&ll={lat},{lon}&z=16")
    return "\n".join(lines)


def _branch_region_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🏙 Ташкент", callback_data="branches:tashkent"),
                InlineKeyboardButton(text="🌍 Регионы", callback_data="branches:regions"),
            ],
        ]
    )


def _build_branches_response(title: str, branches: Sequence[Any]) -> str:
    parts = [title, ""]
    for branch in branches:
        parts.append(format_branch(branch))
        parts.append("-" * 20)
    return "\n".join(parts)


async def _non_tashkent_regions(chat_service: ChatService) -> list[str]:
    return [r for r in await chat_service.list_regions() if _normalize_region(r) != "Ташкент"]


@router.message(CommandStart())
async def cmd_start(message: Message, chat_service: ChatService) -> None:
    tg_user = message.from_user
    if tg_user is None:
        return

    user = await chat_service.get_or_create_user(
        telegram_user_id=tg_user.id,
        username=tg_user.username,
        first_name=tg_user.first_name,
        last_name=tg_user.last_name,
    )

    texts = _user_language_text(user.language)
    if not user.phone:
        await message.answer(texts["share_phone"], reply_markup=contact_keyboard())
        return
    if not user.language:
        await message.answer(texts["ask_language"], reply_markup=language_keyboard())
        return

    await message.answer(texts["start"], reply_markup=main_menu_keyboard())


@router.message(Command("end"))
async def cmd_end(message: Message, chat_service: ChatService) -> None:
    tg_user = message.from_user
    if tg_user is None:
        return

    user = await chat_service.get_or_create_user(
        telegram_user_id=tg_user.id,
        username=tg_user.username,
        first_name=tg_user.first_name,
        last_name=tg_user.last_name,
    )
    ended = await chat_service.end_active_session(user.id)
    if ended:
        await message.answer("Текущая сессия завершена. Используйте /new, чтобы начать новую.", reply_markup=main_menu_keyboard())
    else:
        await message.answer("Нет активной сессии. Отправьте сообщение, чтобы начать.", reply_markup=main_menu_keyboard())


@router.message(Command("new"))
async def cmd_new(message: Message, chat_service: ChatService) -> None:
    tg_user = message.from_user
    if tg_user is None:
        return

    user = await chat_service.get_or_create_user(
        telegram_user_id=tg_user.id,
        username=tg_user.username,
        first_name=tg_user.first_name,
        last_name=tg_user.last_name,
    )
    await chat_service.start_new_session(user.id)
    await message.answer(
        "Подключил вас к колл-центру. Чем могу помочь?",
        reply_markup=chat_keyboard(),
    )


@router.message(Command("op_sessions"))
async def op_sessions(message: Message, chat_service: ChatService) -> None:
    settings = get_settings()
    if message.from_user is None or message.from_user.id not in settings.operator_ids:
        return
    sessions = await chat_service.list_human_sessions(limit=10)
    if not sessions:
        await message.answer("Нет сессий в режиме оператора.")
        return
    lines = ["Активные запросы оператору:"]
    for chat_session, user in sessions:
        started = chat_session.started_at.strftime("%Y-%m-%d %H:%M") if chat_session.started_at else "-"
        lines.append(
            f"• {chat_session.id}\n"
            f"  пользователь: @{user.username or '—'} ({user.telegram_user_id})\n"
            f"  c {started}"
        )
    await message.answer("\n".join(lines))


@router.message(Command("op"))
async def op_reply(message: Message, chat_service: ChatService) -> None:
    settings = get_settings()
    if message.from_user is None or message.from_user.id not in settings.operator_ids:
        return
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 3:
        await message.answer("Использование: /op <session_id> <текст ответа>")
        return
    _, session_id, reply_text = parts
    target_chat_id = await chat_service.send_operator_message(
        session_id=session_id,
        operator_telegram_id=message.from_user.id,
        text=reply_text,
    )
    if target_chat_id is None:
        await message.answer("Сессия не найдена или закрыта.")
        return
    try:
        operator_name = message.from_user.username or str(message.from_user.id)
        await message.bot.send_message(
            chat_id=target_chat_id,
            text=_format_source_reply(reply_text, "operator", operator_name),
        )
    except Exception as exc:  # pragma: no cover
        await message.answer(f"Не удалось отправить пользователю: {exc}")
        return
    await message.answer("Сообщение отправлено.")


@router.message(F.contact)
async def contact_shared(message: Message, chat_service: ChatService) -> None:
    tg_user = message.from_user
    if tg_user is None or message.contact is None:
        return

    user = await chat_service.get_or_create_user(
        telegram_user_id=tg_user.id,
        username=tg_user.username,
        first_name=tg_user.first_name,
        last_name=tg_user.last_name,
        phone=message.contact.phone_number,
    )
    texts = _user_language_text(user.language)
    if not user.language:
        await message.answer("Спасибо, номер телефона сохранен. Теперь выберите язык.", reply_markup=language_keyboard())
    else:
        await message.answer(texts["start"], reply_markup=main_menu_keyboard())


@router.message(F.text)
async def handle_text(message: Message, chat_service: ChatService) -> None:
    tg_user = message.from_user
    if tg_user is None or message.text is None:
        return

    raw_text = message.text
    lower_norm = _normalize_for_match(raw_text)
    logger.info("Incoming text: %r | normalized: %r", raw_text, lower_norm)

    if raw_text == BACK or lower_norm == "назад":
        await message.answer("Главное меню.", reply_markup=main_menu_keyboard())
        return
    if raw_text == "Отмена" or lower_norm == "отмена":
        await message.answer("Главное меню.", reply_markup=main_menu_keyboard())
        return
    if raw_text == END_SESSION or "заверш" in lower_norm:
        user = await chat_service.get_or_create_user(
            telegram_user_id=tg_user.id,
            username=tg_user.username,
            first_name=tg_user.first_name,
            last_name=tg_user.last_name,
        )
        ended = await chat_service.end_active_session(user.id)
        if ended:
            await message.answer("Текущая сессия завершена. Нажмите «📞 Колл-центр» для новой.", reply_markup=main_menu_keyboard())
        else:
            await message.answer("Нет активной сессии.", reply_markup=main_menu_keyboard())
        return

    user = await chat_service.get_or_create_user(
        telegram_user_id=tg_user.id,
        username=tg_user.username,
        first_name=tg_user.first_name,
        last_name=tg_user.last_name,
    )

    if raw_text == NEW_CHAT or ("колл" in lower_norm or "колл-центр" in lower_norm):
        await chat_service.start_new_session(user.id)
        await message.answer(
            "Подключил вас к колл-центру. Я могу:\n"
            "• Ответить на вопросы по продуктам (кредиты, карты, отделения)\n"
            "• Подобрать ипотеку/автокредит/микрозайм/образовательный кредит\n"
            "• Показать отделения и реквизиты\n"
            "• Рассчитать платеж и подготовить PDF-график\n\n"
            "Чем могу помочь?",
            reply_markup=chat_keyboard(),
        )
        return

    if raw_text == BRANCHES or "отдел" in lower_norm:
        logger.info("Branches command matched. text=%r norm=%r", raw_text, lower_norm)
        await message.answer("📍 Выберите регион:", reply_markup=_branch_region_keyboard())
        return

    if raw_text == "🏙 Ташкент" or lower_norm == "ташкент":
        logger.info("Branches tashkent matched. text=%r norm=%r", raw_text, lower_norm)
        districts = await chat_service.list_districts("Ташкент")
        if not districts:
            await message.answer("Нет данных по районам Ташкента.", reply_markup=main_menu_keyboard())
            return
        kb = _inline_keyboard(districts, "branches:district", row_size=2)
        await message.answer("🏢 Отделения Ташкента. Выберите район:", reply_markup=kb)
        return

    if raw_text == "🌍 Регионы" or "регион" in lower_norm:
        logger.info("Branches regions matched. text=%r norm=%r", raw_text, lower_norm)
        regions = await _non_tashkent_regions(chat_service)
        if not regions:
            await message.answer("Регионов не найдено.", reply_markup=main_menu_keyboard())
            return
        kb = _inline_keyboard(regions, "branches:region", row_size=2)
        await message.answer("🏢 Отделения. Выберите область:", reply_markup=kb)
        return

    # Dynamic match: district first, then region
    districts_all = await chat_service.list_districts()
    regions_all_raw = await chat_service.list_regions()
    regions_all = [_normalize_region(r) for r in regions_all_raw]
    region_lookup = { _normalize_region(r): r for r in regions_all_raw }

    if message.text in districts_all:
        branches = await chat_service.list_branches(district=message.text)
        if not branches:
            await message.answer("Нет отделений в этом районе.", reply_markup=main_menu_keyboard())
            return
        text = _build_branches_response(f"🏢 Отделения ({message.text}):", branches)
        await message.answer(text, reply_markup=main_menu_keyboard())
        return

    norm_region = _normalize_region(message.text)
    if norm_region in regions_all:
        stored_region = region_lookup.get(norm_region, norm_region)
        branches = await chat_service.list_branches(region=stored_region)
        if not branches:
            await message.answer("Нет отделений в этой области.", reply_markup=main_menu_keyboard())
            return
        text = _build_branches_response(f"🏢 Отделения ({norm_region}):", branches)
        await message.answer(text, reply_markup=main_menu_keyboard())
        return

    if message.text == NEAREST_BRANCH or ("ближай" in lower_norm and ("цбу" in lower_norm or "отдел" in lower_norm)):
        await message.answer(
            "📍 Отправьте геолокацию, чтобы найти ближайший ЦБУ.",
            reply_markup=location_keyboard(),
        )
        return

    if message.text == MY_SESSIONS or "сесси" in lower_norm:
        sessions = await chat_service.list_recent_sessions(user.id, limit=5)
        if not sessions:
            await message.answer("Сессий пока нет.", reply_markup=main_menu_keyboard())
            return
        lines = ["🗂️ Последние сессии:"]
        for s in sessions:
            started = s.started_at.strftime("%Y-%m-%d %H:%M") if s.started_at else "-"
            ended = s.ended_at.strftime("%Y-%m-%d %H:%M") if s.ended_at else "-"
            title = s.title or "Без названия"
            status = "Активна" if s.status == "active" else "Закрыта"
            lines.append(
                f"• {title}\n"
                f"  📌 Статус: {status}\n"
                f"  ⏱️ Начата: {started}\n"
                f"  ✅ Завершена: {ended}"
            )
        await message.answer("\n".join(lines), reply_markup=main_menu_keyboard())
        return

    if message.text == CHANGE_LANGUAGE or "язык" in lower_norm:
        await message.answer(_user_language_text(user.language)["ask_language"], reply_markup=language_keyboard())
        return

    if message.text == CURRENCY_RATES or "курс" in lower_norm:
        rates = [
            ("💵", "USD", "12 450", "12 650"),
            ("💶", "EUR", "13 300", "13 650"),
            ("₽", "RUB", "130", "145"),
            ("🇰🇿", "KZT", "24", "30"),
            ("💷", "GBP", "15 400", "15 900"),
        ]
        lines = ["Курс валют к суму (UZS), ориентировочно:", "", "Валюта   Покупка    Продажа"]
        for icon, code, buy, sell in rates:
            lines.append(f"{icon} {code:<3} {buy:>8} | {sell:<8}")
        lines.append("")
        lines.append("Данные справочные, актуальные курсы уточняйте в отделении.")

        rates_text = "<pre>" + "\n".join(lines) + "</pre>"
        await message.answer(rates_text, reply_markup=main_menu_keyboard(), parse_mode="HTML")
        return

    reply = await chat_service.handle_user_message(
        user=user,
        text=message.text,
        telegram_message_id=message.message_id,
    )
    if reply.text:
        await message.answer(_format_source_reply(reply.text, "bot"), reply_markup=chat_keyboard())
    if reply.pdf_path:
        await message.answer_document(
            FSInputFile(reply.pdf_path),
            caption=_format_source_reply("График выплат", "bot"),
            reply_markup=chat_keyboard(),
        )
        try:
            os.remove(reply.pdf_path)
        except OSError:
            pass
    if reply.session_id and not reply.human_mode:
        await message.answer(
            "Нужен живой оператор? Нажмите кнопку ниже.",
            reply_markup=human_mode_keyboard(reply.session_id),
        )


@router.callback_query(F.data.startswith("branches:"))
async def branches_callback(callback: CallbackQuery, chat_service: ChatService) -> None:
    if not callback.data or callback.message is None:
        return
    parts = callback.data.split(":", 2)
    action = parts[1] if len(parts) > 1 else ""
    payload = _unsafecb(parts[2]) if len(parts) > 2 else ""

    if action == "tashkent":
        districts = await chat_service.list_districts("Ташкент")
        if not districts:
            await callback.message.answer("Нет данных по районам Ташкента.", reply_markup=main_menu_keyboard())
            await callback.answer()
            return
        kb = _inline_keyboard(districts, "branches:district", row_size=2)
        await callback.message.edit_text("🏢 Отделения Ташкента. Выберите район:", reply_markup=kb)
        await callback.answer()
        return

    if action == "regions":
        regions = await _non_tashkent_regions(chat_service)
        if not regions:
            await callback.message.answer("Регионов не найдено.", reply_markup=main_menu_keyboard())
            await callback.answer()
            return
        kb = _inline_keyboard(regions, "branches:region", row_size=2)
        await callback.message.edit_text("🏢 Отделения. Выберите область:", reply_markup=kb)
        await callback.answer()
        return

    if action == "district":
        district = payload
        branches = await chat_service.list_branches(district=district)
        if not branches:
            await callback.message.answer("Нет отделений в этом районе.", reply_markup=main_menu_keyboard())
            await callback.answer()
            return
        text = _build_branches_response(f"🏢 Отделения ({district}):", branches)
        await callback.message.answer(text, reply_markup=main_menu_keyboard())
        await callback.answer()
        return

    if action == "region":
        region = payload
        branches = await chat_service.list_branches(region=region)
        if not branches:
            await callback.message.answer("Нет отделений в этой области.", reply_markup=main_menu_keyboard())
            await callback.answer()
            return
        text = _build_branches_response(f"🏢 Отделения ({region}):", branches)
        await callback.message.answer(text, reply_markup=main_menu_keyboard())
        await callback.answer()
        return

    await callback.answer()


@router.callback_query(F.data.startswith("lang:"))
async def set_language(callback: CallbackQuery, chat_service: ChatService) -> None:
    if callback.from_user is None:
        return
    _, lang_code = callback.data.split(":", 1)
    await chat_service.get_or_create_user(
        telegram_user_id=callback.from_user.id,
        username=callback.from_user.username,
        first_name=callback.from_user.first_name,
        last_name=callback.from_user.last_name,
        language=lang_code,
    )
    texts = _user_language_text(lang_code)
    await callback.message.answer(texts["start"], reply_markup=main_menu_keyboard())
    await callback.answer("Язык сохранён.")


@router.callback_query(F.data.startswith("human:"))
async def enable_human_mode(callback: CallbackQuery, chat_service: ChatService) -> None:
    if callback.from_user is None:
        return
    _, session_id = callback.data.split(":", 1)
    session = await chat_service.get_session_with_user(session_id)
    if session is None:
        await callback.answer("Сессия не найдена.", show_alert=True)
        return
    chat_session, user = session
    if user.telegram_user_id != callback.from_user.id:
        await callback.answer("Недоступно.", show_alert=True)
        return
    if chat_session.human_mode:
        await callback.answer("Уже подключаем оператора.")
        return
    await chat_service.set_human_mode(session_id, True)
    await callback.message.answer("Переключаю на оператора. Сообщения будут отвечать сотрудники поддержки.")
    await callback.answer("Запрос отправлен.")

    settings = get_settings()
    if settings.operator_ids:
        alert = (
            f"🚨 Пользователь запросил оператора\n"
            f"Сессия: {chat_session.id}\n"
            f"Пользователь: @{user.username or '—'} ({user.telegram_user_id})\n"
            "Ответьте командой /op <session_id> <текст>."
        )
        for op_id in settings.operator_ids:
            try:
                await callback.message.bot.send_message(chat_id=op_id, text=alert)
            except Exception:
                continue


@router.callback_query(F.data.startswith("fb:"))
async def feedback(callback: CallbackQuery, chat_service: ChatService) -> None:
    if callback.from_user is None:
        return
    try:
        _, session_id, rating_str = callback.data.split(":")
        rating = int(rating_str)
    except Exception:
        await callback.answer("Некорректные данные.")
        return

    ok = await chat_service.record_feedback(session_id=session_id, rating=rating)
    texts = _user_language_text(None)
    if ok:
        await callback.message.answer(texts["feedback_saved"])
        await callback.answer("Спасибо!")
    else:
        await callback.message.answer(texts["feedback_failed"])
        await callback.answer("Ошибка.")


@router.message(F.location)
async def handle_location(message: Message, chat_service: ChatService) -> None:
    loc = message.location
    if loc is None:
        return
    # Naive nearest search via in-memory distance
    branches = await chat_service.list_branches()
    if not branches:
        await message.answer("Не нашёл отделения.", reply_markup=main_menu_keyboard())
        return

    import math

    def haversine(lat1, lon1, lat2, lon2):
        R = 6371.0
        phi1, phi2 = math.radians(lat1), math.radians(lat2)
        dphi = math.radians(lat2 - lat1)
        dlambda = math.radians(lon2 - lon1)
        a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
        return R * c

    nearest = None
    for b in branches:
        if b.latitude is None or b.longitude is None:
            continue
        dist = haversine(loc.latitude, loc.longitude, b.latitude, b.longitude)
        if nearest is None or dist < nearest["dist"]:
            nearest = {"branch": b, "dist": dist}

    if nearest is None:
        await message.answer("Не нашёл отделения.", reply_markup=main_menu_keyboard())
        return

    b = nearest["branch"]
    dist_km = nearest["dist"]
    text = (
        f"📍 Ближайший ЦБУ: {b.name}\n"
        f"🏛 {b.address or '-'}\n"
        f"📏 Расстояние: {dist_km:.1f} км\n\n"
        f"🔗 Google: https://maps.google.com/?q={b.latitude},{b.longitude}\n"
        f"🔗 Yandex: https://yandex.com/maps/?ll={b.longitude},{b.latitude}&z=16&pt={b.longitude},{b.latitude}"
    )
    await message.answer(text, reply_markup=main_menu_keyboard())
