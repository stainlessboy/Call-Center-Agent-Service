from __future__ import annotations

import asyncio
import os
import re
from datetime import datetime, timezone
from typing import Any, Sequence

from aiogram import F, Router
from aiogram.enums import ChatAction
from aiogram.filters import Command, CommandStart
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from app.bot.i18n import menu_action_from_text, menu_label, normalize_lang, t
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
TELEGRAM_SAFE_CHUNK = 3800


def _unsafecb(text: str) -> str:
    return text.replace(";", ":")


def _flow_keyboard(options: list[str], row_size: int = 2) -> InlineKeyboardMarkup:
    """Build an inline keyboard for flow answer buttons (prefix: flow:).

    Uses numeric index as callback_data to stay within Telegram's 64-byte limit.
    The button text is recovered in flow_answer_callback via reply_markup lookup.
    """
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for i, label in enumerate(options):
        row.append(InlineKeyboardButton(text=label, callback_data=f"flow:{i}"))
        if len(row) >= row_size:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _with_typing(message: Message, coro) -> Any:
    """Run *coro* while continuously sending typing action every 4 s."""
    stop = asyncio.Event()

    async def _keep_typing() -> None:
        while not stop.is_set():
            try:
                await message.bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.TYPING)
            except Exception:
                pass
            try:
                await asyncio.wait_for(asyncio.shield(stop.wait()), timeout=4.0)
            except asyncio.TimeoutError:
                pass

    task = asyncio.create_task(_keep_typing())
    try:
        return await coro
    finally:
        stop.set()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


def _strip_leading_symbols(text: str) -> str:
    return re.sub(r"^[^\wА-Яа-я]+", "", text).strip()


def _normalize_for_match(text: str) -> str:
    normalized_text = _strip_leading_symbols(text)
    lowered = re.sub(r"[^\w\s]+", " ", normalized_text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", lowered).strip().lower()


def _md_to_html(text: str) -> str:
    """Convert common Markdown formatting to Telegram-compatible HTML."""
    # Bold: **text** or __text__
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"__(.+?)__", r"<b>\1</b>", text)
    # Italic: *text* or _text_ (but not inside HTML tags or words with underscores)
    text = re.sub(r"(?<!\w)\*(?!\*)(.+?)(?<!\*)\*(?!\w)", r"<i>\1</i>", text)
    # Inline code: `text`
    text = re.sub(r"`(.+?)`", r"<code>\1</code>", text)
    return text


def _format_source_reply(text: str, source: str, name: str | None = None) -> str:
    if not text:
        return text
    # Operator messages get a human label so clients can tell who's writing.
    # Bot messages are sent without a prefix to feel more natural.
    if source != "bot":
        label = "👤"
        if name:
            label = f"{label} ({name})"
        return f"{label}: {text}"
    return _md_to_html(text)


def _display_user_alias(username: str | None, first_name: str | None, telegram_user_id: int) -> str:
    base = (username or first_name or str(telegram_user_id)).strip()
    cleaned = re.sub(r"[^\w]+", "", base, flags=re.UNICODE)
    return cleaned or str(telegram_user_id)


def _format_relative_time(dt: datetime, lang: str) -> str:
    """Return a human-readable relative time string for a datetime."""
    aware = dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
    diff = datetime.now(timezone.utc) - aware
    minutes = int(diff.total_seconds() / 60)
    if minutes < 1:
        return {"ru": "только что", "en": "just now", "uz": "hozir"}[lang]
    if minutes < 60:
        return {"ru": f"{minutes} мин. назад", "en": f"{minutes}m ago", "uz": f"{minutes} daq. oldin"}[lang]
    hours = minutes // 60
    if hours < 24:
        return {"ru": f"{hours} ч. назад", "en": f"{hours}h ago", "uz": f"{hours} soat oldin"}[lang]
    days = hours // 24
    if days == 1:
        return {"ru": "вчера", "en": "yesterday", "uz": "kecha"}[lang]
    if days < 7:
        return {"ru": f"{days} дн. назад", "en": f"{days}d ago", "uz": f"{days} kun oldin"}[lang]
    return aware.strftime("%d.%m")


def _sessions_inline_keyboard(sessions: list, lang: str) -> InlineKeyboardMarkup:
    """Build InlineKeyboard with one resume-button per active session + 'New session' button."""
    no_title = {"ru": "Без названия", "en": "Untitled", "uz": "Nomsiz"}[lang]
    buttons = []
    for s in sessions:
        title = (s.title or no_title)[:38]
        time_str = _format_relative_time(s.last_activity_at, lang) if s.last_activity_at else ""
        label = f"▶️ {title}"
        if time_str:
            label += f"  •  {time_str}"
        buttons.append([InlineKeyboardButton(text=label, callback_data=f"session:resume:{s.id}")])
    new_label = {"ru": "➕ Новая сессия", "en": "➕ New session", "uz": "➕ Yangi sessiya"}[lang]
    buttons.append([InlineKeyboardButton(text=new_label, callback_data="session:new")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _parse_session_switch_request(raw_text: str, alias: str) -> int | str | None:
    raw = (raw_text or "").strip()
    if not raw:
        return None

    alias_norm = _normalize_for_match(alias)
    raw_norm = _normalize_for_match(raw)

    alias_direct = re.fullmatch(rf"{re.escape(alias_norm)}\s+(\d+)", raw_norm)
    if alias_direct:
        return int(alias_direct.group(1))

    lead_cmd = re.match(r"^(?:сессия|session|chat|чат)\s+(.+)$", raw, flags=re.IGNORECASE)
    if lead_cmd:
        payload = lead_cmd.group(1).strip()
        payload_norm = _normalize_for_match(payload)
        if payload.isdigit():
            return int(payload)
        alias_in_payload = re.fullmatch(rf"{re.escape(alias_norm)}\s+(\d+)", payload_norm)
        if alias_in_payload:
            return int(alias_in_payload.group(1))
        if re.fullmatch(r"[0-9a-fA-F-]{6,36}", payload):
            return payload.lower()

    if re.fullmatch(r"[0-9a-fA-F-]{8,36}", raw) and ("-" in raw or len(raw) >= 24):
        return raw.lower()
    return None


def _resolve_session_ref(sessions: list[Any], session_ref: int | str) -> tuple[Any | None, str | None]:
    if isinstance(session_ref, int):
        idx = session_ref - 1
        if idx < 0 or idx >= len(sessions):
            return None, "index_out_of_range"
        return sessions[idx], None

    ref = session_ref.lower()
    exact = [s for s in sessions if str(getattr(s, "id", "")).lower() == ref]
    if len(exact) == 1:
        return exact[0], None
    if len(exact) > 1:
        return None, "ambiguous"

    pref = [s for s in sessions if str(getattr(s, "id", "")).lower().startswith(ref)]
    if len(pref) == 1:
        return pref[0], None
    if len(pref) > 1:
        return None, "ambiguous"
    return None, "not_found"


def _user_language_text(lang: str | None) -> dict[str, str]:
    code = normalize_lang(lang)
    lang_name = {"ru": "русский", "en": "English", "uz": "o'zbek tili"}[code]
    names = {"ru": "Русский", "en": "English", "uz": "O'zbek"}
    return {
        "start": t("start_after_language", code),
        "share_phone": t("share_phone_first", code),
        "ask_language": t("ask_language", code),
        "session_closed_timeout": t("session_closed_timeout", code),
        "feedback_saved": t("feedback_saved", code),
        "feedback_failed": t("feedback_failed", code),
        "main_menu": {
            "ru": "Главное меню.",
            "en": "Main menu.",
            "uz": "Asosiy menyu.",
        }[code],
        "end_ok": {
            "ru": "Текущая сессия завершена. Используйте /new, чтобы начать новую.",
            "en": "Current session has been ended. Use /new to start a new one.",
            "uz": "Joriy sessiya yakunlandi. Yangisini boshlash uchun /new dan foydalaning.",
        }[code],
        "end_none": {
            "ru": "Нет активной сессии. Отправьте сообщение, чтобы начать.",
            "en": "There is no active session. Send a message to start one.",
            "uz": "Faol sessiya yo‘q. Boshlash uchun xabar yuboring.",
        }[code],
        "new_chat_connected": {
            "ru": "Подключил вас к колл-центру. Чем могу помочь?",
            "en": "Connected you to the call center. How can I help?",
            "uz": "Sizni koll-markazga uladim. Qanday yordam bera olaman?",
        }[code],
        "id_session": {
            "ru": "ID сессии",
            "en": "Session ID",
            "uz": "Sessiya ID",
        }[code],
        "session_code": {
            "ru": "Код сессии",
            "en": "Session code",
            "uz": "Sessiya kodi",
        }[code],
        "language_saved": t("language_saved", code),
        "phone_saved_choose_language": t("phone_saved_choose_language", code),
        "send_location_prompt": {
            "ru": "📍 Отправьте геолокацию, чтобы найти ближайший ЦБУ.",
            "en": "📍 Send your location to find the nearest branch.",
            "uz": "📍 Eng yaqin filialni topish uchun geolokatsiyani yuboring.",
        }[code],
        "no_sessions": {
            "ru": "Сессий пока нет.",
            "en": "No sessions yet.",
            "uz": "Hozircha sessiyalar yo‘q.",
        }[code],
        "rates_title": {
            "ru": "Курс валют к суму (UZS), ориентировочно:",
            "en": "Approximate exchange rates to UZS:",
            "uz": "UZS ga nisbatan taxminiy valyuta kurslari:",
        }[code],
        "rates_ref": {
            "ru": "Данные справочные, актуальные курсы уточняйте в отделении.",
            "en": "Reference data only. Please confirm current rates at a branch.",
            "uz": "Bu ma’lumot ma’lumot uchun. Amaldagi kurslarni filialda aniqlang.",
        }[code],
        "bot_pdf_caption": t("pdf_caption", code),
        "lang_name_for_prompt": lang_name,
        "lang_display_name": names[code],
    }


def format_office(obj, lang: str = "ru") -> str:
    """Format any office (Filial / SalesOffice / SalesPoint) for Telegram.

    Duck-typed by field presence: landmark_*/location_url only exist on Filial,
    region_* only on SalesOffice.
    """
    def _loc(field: str) -> str:
        if lang == "uz":
            val = getattr(obj, f"{field}_uz", None)
            if val:
                return val
        return getattr(obj, f"{field}_ru", None) or ""

    name = _loc("name")
    address = _loc("address") or "-"
    lines = [f"🏦 {name}", f"📌 {address}"]

    if hasattr(obj, "region_ru"):
        region = _loc("region")
        if region:
            lines.append(f"🌍 {region}")
    if hasattr(obj, "landmark_ru"):
        landmark = _loc("landmark")
        if landmark:
            lines.append(f"🧭 {landmark}")
    if getattr(obj, "location_url", None):
        lines.append(f"🗺 {obj.location_url}")
    if getattr(obj, "phone", None):
        lines.append(f"📞 {obj.phone}")
    if getattr(obj, "hours", None):
        lines.append(f"🕘 {obj.hours}")
    lat = getattr(obj, "latitude", None)
    lon = getattr(obj, "longitude", None)
    if lat and lon:
        lines.append(f"📍 https://maps.google.com/maps?q={lat},{lon}&z=16")
    return "\n".join(lines)


def _office_type_inline_keyboard(lang: str) -> InlineKeyboardMarkup:
    """Level 1: choose office type (3 buttons)."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=menu_label("branches_filials", lang),
            callback_data="office:type:filial",
        )],
        [InlineKeyboardButton(
            text=menu_label("branches_sales_offices", lang),
            callback_data="office:type:sales_office",
        )],
        [InlineKeyboardButton(
            text=menu_label("branches_sales_points", lang),
            callback_data="office:type:sales_point",
        )],
    ])


def _office_list_inline_keyboard(office_type: str, offices: list, lang: str) -> InlineKeyboardMarkup:
    """Level 2: list every office of a given type as a clickable button."""
    rows: list[list[InlineKeyboardButton]] = []
    for o in offices:
        name = (o.name_uz if lang == "uz" and getattr(o, "name_uz", None) else o.name_ru) or ""
        # Telegram inline-button text max 64 chars — truncate defensively
        if len(name) > 60:
            name = name[:57] + "..."
        rows.append([InlineKeyboardButton(
            text=name,
            callback_data=f"office:show:{office_type}:{o.id}",
        )])
    back_label = {"ru": "⬅ К типам офисов", "en": "⬅ To office types", "uz": "⬅ Ofis turlariga"}[lang]
    rows.append([InlineKeyboardButton(text=back_label, callback_data="office:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _office_detail_inline_keyboard(office_type: str, lang: str) -> InlineKeyboardMarkup:
    """Level 3: office details view → back to list / back to types."""
    back_to_list = {
        "ru": "⬅ К списку", "en": "⬅ Back to list", "uz": "⬅ Ro'yxatga",
    }[lang]
    back_to_types = {
        "ru": "⬅ К типам", "en": "⬅ To types", "uz": "⬅ Turlarga",
    }[lang]
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=back_to_list, callback_data=f"office:type:{office_type}")],
        [InlineKeyboardButton(text=back_to_types, callback_data="office:back")],
    ])


_OFFICE_TYPE_HEADERS = {
    "filial": {
        "ru": "🏦 Филиалы (ЦБУ) — выберите:",
        "en": "🏦 Filials — pick one:",
        "uz": "🏦 Filiallar (BXM) — tanlang:",
    },
    "sales_office": {
        "ru": "🏪 Офисы продаж — выберите:",
        "en": "🏪 Sales offices — pick one:",
        "uz": "🏪 Savdo ofislari — tanlang:",
    },
    "sales_point": {
        "ru": "🚗 Точки продаж — выберите:",
        "en": "🚗 Sales points — pick one:",
        "uz": "🚗 Savdo nuqtalari — tanlang:",
    },
}

_OFFICE_TYPE_PROMPT = {
    "ru": "🏢 Выберите тип офиса:",
    "en": "🏢 Choose office type:",
    "uz": "🏢 Ofis turini tanlang:",
}


def _split_message_text(text: str, limit: int = TELEGRAM_SAFE_CHUNK) -> list[str]:
    content = (text or "").strip()
    if not content:
        return []
    if len(content) <= limit:
        return [content]

    chunks: list[str] = []
    rest = content
    while rest:
        if len(rest) <= limit:
            chunks.append(rest)
            break
        split_idx = rest.rfind("\n", 0, limit)
        if split_idx <= 0:
            split_idx = limit
        chunk = rest[:split_idx].strip()
        if not chunk:
            chunk = rest[:limit]
            split_idx = limit
        chunks.append(chunk)
        rest = rest[split_idx:].lstrip("\n")
    return chunks


async def _answer_safe(message: Message, text: str, reply_markup=None, parse_mode: str | None = None) -> None:
    chunks = _split_message_text(text, limit=TELEGRAM_SAFE_CHUNK)
    if not chunks:
        return
    for idx, chunk in enumerate(chunks):
        markup = reply_markup if idx == len(chunks) - 1 else None
        try:
            await message.answer(chunk, reply_markup=markup, parse_mode=parse_mode)
        except TelegramBadRequest as exc:
            if "message is too long" not in str(exc).lower():
                raise
            # Emergency fallback for edge cases with Telegram entity handling.
            tiny_chunks = _split_message_text(chunk, limit=2000)
            for tiny_idx, tiny in enumerate(tiny_chunks):
                tiny_markup = markup if tiny_idx == len(tiny_chunks) - 1 else None
                await message.answer(tiny, reply_markup=tiny_markup, parse_mode=None)


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
        await message.answer(texts["share_phone"], reply_markup=contact_keyboard(user.language))
        return
    if not user.language:
        await message.answer(texts["ask_language"], reply_markup=language_keyboard())
        return

    await message.answer(texts["start"], reply_markup=main_menu_keyboard(user.language))


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
        await message.answer(_user_language_text(user.language)["end_ok"], reply_markup=main_menu_keyboard(user.language))
    else:
        await message.answer(_user_language_text(user.language)["end_none"], reply_markup=main_menu_keyboard(user.language))


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
    new_session = await chat_service.start_new_session(user.id)
    alias = _display_user_alias(user.username, user.first_name, user.telegram_user_id)
    active_sessions = await chat_service.list_active_sessions(user.id, limit=50)
    session_index = next((idx for idx, s in enumerate(active_sessions, start=1) if s.id == new_session.id), 1)
    texts = _user_language_text(user.language)
    await message.answer(
        (
            f"{texts['new_chat_connected']}\n"
            f"{texts['id_session']}: {new_session.id}\n"
            f"{texts['session_code']}: {alias}-{session_index}"
        ),
        reply_markup=chat_keyboard(user.language),
    )


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
        await message.answer(texts["phone_saved_choose_language"], reply_markup=language_keyboard())
    else:
        await message.answer(texts["start"], reply_markup=main_menu_keyboard(user.language))


@router.message(F.text)
async def handle_text(message: Message, chat_service: ChatService) -> None:
    tg_user = message.from_user
    if tg_user is None or message.text is None:
        return

    raw_text = message.text
    max_len = get_settings().max_message_length
    if len(raw_text) > max_len:
        raw_text = raw_text[:max_len]
    user = await chat_service.get_or_create_user(
        telegram_user_id=tg_user.id,
        username=tg_user.username,
        first_name=tg_user.first_name,
        last_name=tg_user.last_name,
    )
    lang = normalize_lang(user.language)
    texts = _user_language_text(lang)
    action = menu_action_from_text(raw_text)

    if action in {BACK, "cancel"}:
        await message.answer(texts["main_menu"], reply_markup=main_menu_keyboard(lang))
        return

    if action == END_SESSION:
        ended = await chat_service.end_active_session(user.id)
        if ended:
            end_ok = {
                "ru": "Текущая сессия завершена. Нажмите «📞 Колл-центр» для новой.",
                "en": "Current session ended. Press “📞 Call center” to start a new one.",
                "uz": "Joriy sessiya yakunlandi. Yangisini boshlash uchun “📞 Koll-markaz”ni bosing.",
            }[lang]
            await message.answer(end_ok, reply_markup=main_menu_keyboard(lang))
        else:
            no_active = {
                "ru": "Нет активной сессии.",
                "en": "No active session.",
                "uz": "Faol sessiya yo‘q.",
            }[lang]
            await message.answer(no_active, reply_markup=main_menu_keyboard(lang))
        return
    alias = _display_user_alias(user.username, user.first_name, user.telegram_user_id)

    switch_ref = _parse_session_switch_request(raw_text, alias)
    if switch_ref is not None:
        active_sessions = await chat_service.list_active_sessions(user.id, limit=20)
        if not active_sessions:
            no_active_sessions = {
                "ru": "У вас нет активных сессий. Нажмите «📞 Колл-центр», чтобы начать новую.",
                "en": "You have no active sessions. Press “📞 Call center” to start a new one.",
                "uz": "Sizda faol sessiyalar yo‘q. Yangisini boshlash uchun “📞 Koll-markaz”ni bosing.",
            }[lang]
            await message.answer(no_active_sessions, reply_markup=chat_keyboard(lang))
            return
        target, error = _resolve_session_ref(active_sessions, switch_ref)
        if target is None:
            if error == "index_out_of_range":
                text = {
                    "ru": "Неверный номер сессии. Откройте «🗂️ Мои сессии» и выберите номер из списка.",
                    "en": "Invalid session number. Open “🗂️ My sessions” and choose a number from the list.",
                    "uz": "Sessiya raqami noto‘g‘ri. “🗂️ Mening sessiyalarim”ni ochib, ro‘yxatdan tanlang.",
                }[lang]
                await message.answer(text)
            elif error == "ambiguous":
                text = {
                    "ru": "Нашлось несколько сессий с таким ID. Укажите полный ID сессии.",
                    "en": "Several sessions match this ID. Please specify the full session ID.",
                    "uz": "Bu ID bo‘yicha bir nechta sessiya topildi. To‘liq sessiya ID sini yuboring.",
                }[lang]
                await message.answer(text)
            else:
                text = {
                    "ru": "Активная сессия с таким ID не найдена.",
                    "en": "No active session found with this ID.",
                    "uz": "Bunday ID li faol sessiya topilmadi.",
                }[lang]
                await message.answer(text)
            return
        switched = await chat_service.switch_active_session(user.id, target.id)
        if switched is None:
            text = {
                "ru": "Не удалось переключить сессию. Попробуйте еще раз.",
                "en": "Could not switch the session. Please try again.",
                "uz": "Sessiyani almashtirib bo‘lmadi. Qayta urinib ko‘ring.",
            }[lang]
            await message.answer(text)
            return
        switched_title = switched.title or {
            "ru": "Без названия",
            "en": "Untitled",
            "uz": "Nomsiz",
        }[lang]
        switched_prefix = {
            "ru": "Переключил на сессию",
            "en": "Switched to session",
            "uz": "Sessiyaga o‘tkazildi",
        }[lang]
        id_label = {"ru": "ID", "en": "ID", "uz": "ID"}[lang]
        await message.answer(
            f"{switched_prefix}: {switched_title}\n{id_label}: {switched.id}",
            reply_markup=chat_keyboard(lang),
        )
        return

    if action == NEW_CHAT:
        await chat_service.start_new_session(user.id)
        msg = {
            "ru": "Привет! Чем могу помочь?",
            "en": "Hi! How can I help?",
            "uz": "Salom! Qanday yordam bera olaman?",
        }[lang]
        await message.answer(msg, reply_markup=chat_keyboard(lang))
        return

    if action == BRANCHES:
        await message.answer(
            _OFFICE_TYPE_PROMPT[lang],
            reply_markup=_office_type_inline_keyboard(lang),
        )
        return

    if action == NEAREST_BRANCH:
        await message.answer(
            texts["send_location_prompt"],
            reply_markup=location_keyboard(lang),
        )
        return

    if action == MY_SESSIONS:
        active_sessions = await chat_service.list_active_sessions(user.id, limit=8)
        if not active_sessions:
            no_active_msg = {
                "ru": "У вас нет активных сессий.\nНажмите «➕ Новая сессия» или напишите вопрос — начнётся автоматически.",
                "en": "You have no active sessions.\nTap «➕ New session» or just send a message to start one.",
                "uz": "Faol sessiyalaringiz yo’q.\n«➕ Yangi sessiya» tugmasini bosing yoki xabar yuboring.",
            }[lang]
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(
                    text={"ru": "➕ Новая сессия", "en": "➕ New session", "uz": "➕ Yangi sessiya"}[lang],
                    callback_data="session:new",
                )
            ]])
            await message.answer(no_active_msg, reply_markup=kb)
            return
        header = {
            "ru": "🗂 Активные сессии — нажмите, чтобы продолжить:",
            "en": "🗂 Active sessions — tap to resume:",
            "uz": "🗂 Faol sessiyalar — davom ettirish uchun bosing:",
        }[lang]
        kb = _sessions_inline_keyboard(active_sessions, lang)
        await message.answer(header, reply_markup=kb)
        return

    if action == CHANGE_LANGUAGE:
        await message.answer(texts["ask_language"], reply_markup=language_keyboard())
        return

    if action == CURRENCY_RATES:
        from app.utils.cbu_rates import fetch_cbu_rates

        cbu_data = await fetch_cbu_rates(("USD", "EUR", "RUB", "GBP", "KZT", "CNY"))
        if not cbu_data:
            await message.answer(texts["rates_ref"], reply_markup=main_menu_keyboard(lang))
            return

        date_str = cbu_data[0].get("date", "")
        title = {
            "ru": f"💱 Курс ЦБ Узбекистана на {date_str}:",
            "en": f"💱 CBU exchange rates for {date_str}:",
            "uz": f"💱 O'zbekiston MB kursi {date_str}:",
        }[lang]

        buttons: list[list[InlineKeyboardButton]] = []
        for r in cbu_data:
            nominal = r["nominal"]
            nom_str = f"{nominal} " if str(nominal) != "1" else ""
            diff = float(r["diff"]) if r["diff"] else 0
            arrow = "📈" if diff > 0 else ("📉" if diff < 0 else "")
            label = f"{r['icon']} {nom_str}{r['code']}  =  {r['rate']} сум {arrow}"
            buttons.append([InlineKeyboardButton(text=label, callback_data=f"noop:{r['code']}")])

        footer = {
            "ru": "Источник: cbu.uz (курс ЦБ)",
            "en": "Source: cbu.uz (CBU rate)",
            "uz": "Manba: cbu.uz (MB kursi)",
        }[lang]
        buttons.append([InlineKeyboardButton(text=f"ℹ️ {footer}", callback_data="noop:info")])

        kb = InlineKeyboardMarkup(inline_keyboard=buttons)
        await message.answer(title, reply_markup=kb, parse_mode="HTML")
        return

    reply = await _with_typing(
        message,
        chat_service.handle_user_message(
            user=user,
            text=message.text,
            telegram_message_id=message.message_id,
        ),
    )
    if reply.human_mode and reply.session_id:
        mode_markup = human_mode_keyboard(reply.session_id, human_mode=True, lang=lang)
    elif reply.show_operator_button and reply.session_id:
        mode_markup = human_mode_keyboard(reply.session_id, human_mode=False, lang=lang)
    else:
        mode_markup = None
    # Build inline keyboard from agent-suggested options (questions / product selection)
    flow_markup = _flow_keyboard(reply.keyboard_options) if reply.keyboard_options else None

    if reply.text:
        await _answer_safe(
            message,
            _format_source_reply(reply.text, "bot"),
            reply_markup=flow_markup or mode_markup or chat_keyboard(lang),
            parse_mode="HTML",
        )
    if reply.pdf_path:
        await message.answer_document(
            FSInputFile(reply.pdf_path),
            caption=_format_source_reply(texts["bot_pdf_caption"], "bot"),
            reply_markup=mode_markup if mode_markup and not reply.text else chat_keyboard(lang),
        )
        try:
            os.remove(reply.pdf_path)
        except OSError:
            pass


@router.callback_query(F.data.startswith("noop:"))
async def noop_callback(callback: CallbackQuery) -> None:
    """Non-clickable display buttons (currency rates, etc.)."""
    await callback.answer()


@router.callback_query(F.data.startswith("office:"))
async def office_callback(callback: CallbackQuery, chat_service: ChatService) -> None:
    """Inline drill-down: office types → list → details."""
    if not callback.data or callback.message is None:
        return

    # Resolve user language
    lang = normalize_lang(getattr(callback.from_user, "language_code", None))
    if callback.from_user is not None:
        db_user = await chat_service.get_or_create_user(
            telegram_user_id=callback.from_user.id,
            username=callback.from_user.username,
            first_name=callback.from_user.first_name,
            last_name=callback.from_user.last_name,
        )
        lang = normalize_lang(db_user.language)

    parts = callback.data.split(":")
    # parts[0] == "office"
    action = parts[1] if len(parts) > 1 else ""

    if action == "back":
        # Level 1: type selection
        try:
            await callback.message.edit_text(
                _OFFICE_TYPE_PROMPT[lang],
                reply_markup=_office_type_inline_keyboard(lang),
            )
        except TelegramBadRequest:
            await callback.message.answer(
                _OFFICE_TYPE_PROMPT[lang],
                reply_markup=_office_type_inline_keyboard(lang),
            )
        await callback.answer()
        return

    if action == "type":
        office_type = parts[2] if len(parts) > 2 else ""
        if office_type == "filial":
            offices = await chat_service.list_filials()
        elif office_type == "sales_office":
            offices = await chat_service.list_sales_offices()
        elif office_type == "sales_point":
            offices = await chat_service.list_sales_points()
        else:
            await callback.answer()
            return

        if not offices:
            empty_msg = {
                "ru": "Ничего не найдено.",
                "en": "Nothing found.",
                "uz": "Hech narsa topilmadi.",
            }[lang]
            await callback.message.edit_text(
                empty_msg, reply_markup=_office_type_inline_keyboard(lang)
            )
            await callback.answer()
            return

        header = _OFFICE_TYPE_HEADERS[office_type][lang]
        kb = _office_list_inline_keyboard(office_type, offices, lang)
        try:
            await callback.message.edit_text(header, reply_markup=kb)
        except TelegramBadRequest:
            await callback.message.answer(header, reply_markup=kb)
        await callback.answer()
        return

    if action == "show":
        office_type = parts[2] if len(parts) > 2 else ""
        try:
            office_id = int(parts[3]) if len(parts) > 3 else 0
        except ValueError:
            await callback.answer()
            return
        office = await chat_service.get_office_by_id(office_type, office_id)
        if office is None:
            not_found = {
                "ru": "Офис не найден.",
                "en": "Office not found.",
                "uz": "Ofis topilmadi.",
            }[lang]
            await callback.answer(not_found, show_alert=True)
            return

        text = format_office(office, lang)
        kb = _office_detail_inline_keyboard(office_type, lang)
        try:
            await callback.message.edit_text(
                text, reply_markup=kb, disable_web_page_preview=True
            )
        except TelegramBadRequest:
            await callback.message.answer(
                text, reply_markup=kb, disable_web_page_preview=True
            )
        await callback.answer()
        return

    await callback.answer()


@router.callback_query(F.data.startswith("flow:"))
async def flow_answer_callback(callback: CallbackQuery, chat_service: ChatService) -> None:
    """Handle inline button answers for flow questions (credit/cross_sell/greeting)."""
    if not callback.data or callback.message is None or callback.from_user is None:
        await callback.answer()
        return

    # Resolve button index → actual label text from message reply_markup
    raw = callback.data[len("flow:"):]
    value = raw  # fallback (in case markup is unavailable)
    if callback.message and callback.message.reply_markup:
        for btn_row in callback.message.reply_markup.inline_keyboard:
            for btn in btn_row:
                if btn.callback_data == callback.data:
                    value = btn.text
                    break
    value = _unsafecb(value)
    tg_user = callback.from_user
    user = await chat_service.get_or_create_user(
        telegram_user_id=tg_user.id,
        username=tg_user.username,
        first_name=tg_user.first_name,
        last_name=tg_user.last_name,
    )
    lang = normalize_lang(user.language)

    # Dismiss the loading spinner and remove buttons from the previous message
    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    # Echo user's button selection as a message (so it appears in chat history)
    try:
        await callback.message.answer(value)
    except Exception:
        pass

    # Forward the selection to the agent
    reply = await _with_typing(
        callback.message,
        chat_service.handle_user_message(
            user=user,
            text=value,
            telegram_message_id=callback.message.message_id,
        ),
    )
    if reply.human_mode and reply.session_id:
        mode_markup = human_mode_keyboard(reply.session_id, human_mode=True, lang=lang)
    elif reply.show_operator_button and reply.session_id:
        mode_markup = human_mode_keyboard(reply.session_id, human_mode=False, lang=lang)
    else:
        mode_markup = None
    flow_markup = _flow_keyboard(reply.keyboard_options) if reply.keyboard_options else None

    if reply.text:
        await _answer_safe(
            callback.message,
            _format_source_reply(reply.text, "bot"),
            reply_markup=flow_markup or mode_markup or chat_keyboard(lang),
            parse_mode="HTML",
        )
    if reply.pdf_path:
        await callback.message.answer_document(
            FSInputFile(reply.pdf_path),
            caption=_format_source_reply({"ru": "График платежей", "en": "Payment schedule", "uz": "To'lov jadvali"}.get(lang, "График платежей"), "bot"),
            reply_markup=mode_markup if mode_markup and not reply.text else chat_keyboard(lang),
        )
        try:
            os.remove(reply.pdf_path)
        except OSError:
            pass


@router.callback_query(F.data.startswith("lang:"))
async def set_language(callback: CallbackQuery, chat_service: ChatService) -> None:
    if callback.from_user is None:
        return
    _, lang_code = callback.data.split(":", 1)
    if callback.message is None:
        await callback.answer()
        return
    await chat_service.get_or_create_user(
        telegram_user_id=callback.from_user.id,
        username=callback.from_user.username,
        first_name=callback.from_user.first_name,
        last_name=callback.from_user.last_name,
        language=lang_code,
    )
    texts = _user_language_text(lang_code)
    await callback.message.answer(texts["start"], reply_markup=main_menu_keyboard(lang_code))
    await callback.answer(texts["language_saved"])


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
        await callback.answer(
            {
                "ru": "Уже подключаем оператора.",
                "en": "Operator request is already in progress.",
                "uz": "Operatorga ulash jarayoni allaqachon boshlangan.",
            }[normalize_lang(user.language)]
        )
        return
    await chat_service.set_human_mode(session_id, True)
    lang = normalize_lang(user.language)

    # Chat Middleware
    from app.api.fastapi_app import app as _fastapi_app
    middleware_client = getattr(getattr(_fastapi_app, "state", None), "middleware_client", None)

    if middleware_client is None:
        # Middleware not configured — tell user operators are unavailable
        await chat_service.set_human_mode(session_id, False)
        await callback.message.answer(t("middleware_unavailable", lang))
        await callback.answer()
        return

    recent = await chat_service.get_recent_messages(session_id, limit=10)
    context = "\n".join(
        f"{'User' if m.role == 'user' else 'Bot'}: {m.text}"
        for m in recent if m.text
    )
    customer_name = f"@{user.username}" if user.username else (user.first_name or str(user.telegram_user_id))

    await callback.message.answer(t("searching_operator", lang))
    await callback.answer()

    ok = await middleware_client.start_chat(
        session_id=session_id,
        user_name=customer_name,
        initial_message=context or "Клиент запросил оператора",
    )
    if not ok:
        await chat_service.set_human_mode(session_id, False)
        await callback.message.answer(t("middleware_unavailable", lang))


@router.callback_query(F.data.startswith("bot:"))
async def disable_human_mode(callback: CallbackQuery, chat_service: ChatService) -> None:
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
    if not chat_session.human_mode:
        await callback.answer(
            {
                "ru": "Сессия уже в режиме бота.",
                "en": "The session is already in bot mode.",
                "uz": "Sessiya allaqachon bot rejimida.",
            }[normalize_lang(user.language)]
        )
        return
    # End middleware chat if active
    from app.api.fastapi_app import app as _fastapi_app
    middleware_client = getattr(getattr(_fastapi_app, "state", None), "middleware_client", None)
    if middleware_client is not None:
        await middleware_client.end_chat(session_id)

    await chat_service.set_human_mode(session_id, False)
    lang = normalize_lang(user.language)
    await callback.message.answer(
        {
            "ru": "Переключил сессию в режим бота. Продолжаем.",
            "en": "Switched the session back to bot mode. Let's continue.",
            "uz": "Sessiya bot rejimiga qaytarildi. Davom etamiz.",
        }[lang]
    )
    await callback.answer({"ru": "Готово", "en": "Done", "uz": "Tayyor"}[lang])


@router.callback_query(F.data.startswith("fb:"))
async def feedback(callback: CallbackQuery, chat_service: ChatService) -> None:
    if callback.from_user is None:
        return
    user = await chat_service.get_or_create_user(
        telegram_user_id=callback.from_user.id,
        username=callback.from_user.username,
        first_name=callback.from_user.first_name,
        last_name=callback.from_user.last_name,
    )
    lang = normalize_lang(user.language)
    try:
        _, session_id, rating_str = callback.data.split(":")
        rating = int(rating_str)
    except Exception:
        await callback.answer(
            {"ru": "Некорректные данные.", "en": "Invalid data.", "uz": "Noto‘g‘ri ma’lumot."}[lang]
        )
        return

    ok = await chat_service.record_feedback(session_id=session_id, rating=rating)
    texts = _user_language_text(lang)
    stars = "⭐" * rating
    if ok:
        if callback.message is not None:
            await callback.message.edit_text(
                f"{texts['feedback_saved']} {stars}",
                reply_markup=None,
            )
        await callback.answer({"ru": "Спасибо!", "en": "Thank you!", "uz": "Rahmat!"}[lang])
    else:
        if callback.message is not None:
            await callback.message.edit_text(
                texts["feedback_failed"],
                reply_markup=None,
            )
        await callback.answer({"ru": "Ошибка.", "en": "Error.", "uz": "Xatolik."}[lang])


@router.callback_query(F.data.startswith("session:"))
async def session_callback(callback: CallbackQuery, chat_service: ChatService) -> None:
    """Handle session resume and new-session actions from InlineKeyboard."""
    if callback.from_user is None or callback.message is None:
        await callback.answer()
        return
    user = await chat_service.get_or_create_user(
        telegram_user_id=callback.from_user.id,
        username=callback.from_user.username,
        first_name=callback.from_user.first_name,
        last_name=callback.from_user.last_name,
    )
    lang = normalize_lang(user.language)
    data = callback.data or ""

    if data == "session:new":
        await chat_service.start_new_session(user.id)
        msg = {
            "ru": (
                "Новая сессия начата. Чем могу помочь?\n\n"
                "• Кредит — ипотека, автокредит, микрозайм, образовательный\n"
                "• Вклад — накопление или ежемесячный доход\n"
                "• Карта — дебетовая или валютная\n"
                "• Вопрос — условия, документы, отделения"
            ),
            "en": (
                "New session started. How can I help?\n\n"
                "• Loan — mortgage, auto, microloan, education\n"
                "• Deposit — savings or monthly income\n"
                "• Card — debit or FX\n"
                "• Question — terms, documents, branches"
            ),
            "uz": (
                "Yangi sessiya boshlandi. Qanday yordam bera olaman?\n\n"
                "• Kredit — ipoteka, avtokredit, mikroqarz, ta'lim\n"
                "• Omonat — jamg'arma yoki oylik daromad\n"
                "• Karta — debet yoki valyuta\n"
                "• Savol — shartlar, hujjatlar, filiallar"
            ),
        }[lang]
        await callback.message.answer(msg, reply_markup=chat_keyboard(lang))
        await callback.answer()
        return

    if data.startswith("session:resume:"):
        session_id = data[len("session:resume:"):]
        switched = await chat_service.switch_active_session(user.id, session_id)
        if switched is None:
            err = {
                "ru": "Сессия не найдена или уже закрыта.",
                "en": "Session not found or already closed.",
                "uz": "Sessiya topilmadi yoki yopilgan.",
            }[lang]
            await callback.answer(err, show_alert=True)
            return
        no_title = {"ru": "Без названия", "en": "Untitled", "uz": "Nomsiz"}[lang]
        title = switched.title or no_title
        header = {
            "ru": f"✅ Сессия «{title[:45]}» активна.\n\n📜 *Последние сообщения:*",
            "en": f"✅ Session «{title[:45]}» is active.\n\n📜 *Recent messages:*",
            "uz": f"✅ Sessiya «{title[:45]}» faol.\n\n📜 *Oxirgi xabarlar:*",
        }[lang]

        recent = await chat_service.get_recent_messages(switched.id, limit=10)
        if recent:
            lines = []
            for m in recent:
                if m.role == "user":
                    prefix = "👤"
                else:
                    prefix = "🤖"
                # Trim long messages
                text = (m.text or "").strip()
                if len(text) > 300:
                    text = text[:297] + "…"
                lines.append(f"{prefix} {text}")
            history_block = "\n\n".join(lines)
            full_msg = f"{header}\n\n{history_block}"
        else:
            full_msg = {
                "ru": f"✅ Сессия «{title[:45]}» активна. Продолжайте — вся история сохранена.",
                "en": f"✅ Session «{title[:45]}» is active. Continue — full history is saved.",
                "uz": f"✅ Sessiya «{title[:45]}» faol. Davom eting — barcha tarix saqlangan.",
            }[lang]

        # Send in chunks if needed
        for chunk_start in range(0, max(len(full_msg), 1), TELEGRAM_SAFE_CHUNK):
            chunk = full_msg[chunk_start : chunk_start + TELEGRAM_SAFE_CHUNK]
            is_last = (chunk_start + TELEGRAM_SAFE_CHUNK) >= len(full_msg)
            await callback.message.answer(
                chunk,
                reply_markup=chat_keyboard(lang) if is_last else None,
                parse_mode="Markdown",
            )
        await callback.answer()
        return

    await callback.answer()


@router.message(F.location)
async def handle_location(message: Message, chat_service: ChatService) -> None:
    loc = message.location
    if loc is None:
        return
    lang = "ru"
    if message.from_user is not None:
        user = await chat_service.get_or_create_user(
            telegram_user_id=message.from_user.id,
            username=message.from_user.username,
            first_name=message.from_user.first_name,
            last_name=message.from_user.last_name,
        )
        lang = normalize_lang(user.language)
    # Naive nearest search across all office types with coords
    offices = await chat_service.list_all_offices_with_coords()
    if not offices:
        await message.answer(
            {
                "ru": "Не нашёл отделения с координатами.",
                "en": "No offices with coordinates found.",
                "uz": "Koordinatali filiallar topilmadi.",
            }[lang],
            reply_markup=main_menu_keyboard(lang),
        )
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
    for o in offices:
        dist = haversine(loc.latitude, loc.longitude, o.latitude, o.longitude)
        if nearest is None or dist < nearest["dist"]:
            nearest = {"office": o, "dist": dist}

    o = nearest["office"]
    dist_km = nearest["dist"]
    name = (o.name_uz if lang == "uz" and o.name_uz else o.name_ru)
    address = (o.address_uz if lang == "uz" and o.address_uz else o.address_ru) or "-"
    nearest_label = {"ru": "📍 Ближайший офис", "en": "📍 Nearest office", "uz": "📍 Eng yaqin ofis"}[lang]
    distance_label = {"ru": "📏 Расстояние", "en": "📏 Distance", "uz": "📏 Masofa"}[lang]
    text = (
        f"{nearest_label}: {name}\n"
        f"🏛 {address}\n"
        f"{distance_label}: {dist_km:.1f} км\n\n"
        f"🔗 Google: https://maps.google.com/?q={o.latitude},{o.longitude}\n"
        f"🔗 Yandex: https://yandex.com/maps/?ll={o.longitude},{o.latitude}&z=16&pt={o.longitude},{o.latitude}"
    )
    await message.answer(text, reply_markup=main_menu_keyboard(lang))
