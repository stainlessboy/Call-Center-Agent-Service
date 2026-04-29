from __future__ import annotations

import asyncio
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any

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
from app.bot.keyboards.feedback import language_keyboard
from app.bot.keyboards.human import human_mode_keyboard
from app.bot.keyboards.menu import (
    BACK,
    CHANGE_LANGUAGE,
    CHANGE_PHONE,
    CONTACTS_COMPLAINTS,
    CURRENCY_RATES,
    ELECTRONIC_QUEUE,
    END_DIALOG,
    MOBILE_APP_LINK,
    MY_SESSIONS,
    NEAREST_BRANCH,
    OFFICE_LIST,
    OUR_BRANCHES,
    RATES_ATM,
    RATES_CORPORATE,
    RATES_INDIVIDUALS,
    RATES_ONLINE,
    SETTINGS,
    SOCIAL_LINKS,
    START_DIALOG,
    USEFUL_LINKS,
    WEEKEND_DAYS,
    branches_submenu_keyboard,
    chat_keyboard,
    links_submenu_keyboard,
    main_menu_keyboard,
    rates_submenu_keyboard,
    settings_submenu_keyboard,
)
from app.bot.links import (
    ANDROID_APP_URL,
    APP_HEADER,
    APP_LABELS,
    CONTACTS_BODY,
    CONTACTS_HEADER,
    FACEBOOK_URL,
    INSTAGRAM_URL,
    IOS_APP_URL,
    SOCIAL_HEADER,
    SOCIAL_LABELS,
    TELEGRAM_CHANNEL_URL,
    WEBSITE_URL,
)
from app.config import get_settings
from app.services.chat_service import ChatService

router = Router()
TELEGRAM_SAFE_CHUNK = 3800


def _is_within_working_hours() -> bool:
    """Окно работы операторов middleware (по дефолту 8:00–23:00 Asia/Tashkent)."""
    settings = get_settings()
    if not settings.middleware_working_hours_enabled:
        return True
    tz = timezone(timedelta(hours=settings.middleware_working_hours_tz_offset))
    now = datetime.now(tz)
    return settings.middleware_working_hours_start <= now.hour < settings.middleware_working_hours_end


def _unsafecb(text: str) -> str:
    return text.replace(";", ":")


# ── Language switch suggestion (heuristic mismatch) ─────────────────────────
# Surfaced when the heuristic in app/agent/lang_heuristic.py thinks the user
# wrote in a different language than User.language. The text is in the
# *target* language (the one the user appears to be writing in) — they
# obviously understand it since they just typed in it.
_LANG_SWITCH_PROMPT = {
    "ru": "Я заметил, что вы пишете по-русски — переключиться?",
    "en": "Looks like you switched to English — switch the bot too?",
    "uz": "O'zbek tiliga o'tasizmi?",
}
_LANG_SWITCH_YES = {"ru": "✅ Да", "en": "✅ Yes", "uz": "✅ Ha"}
_LANG_SWITCH_NO = {"ru": "❌ Нет", "en": "❌ No", "uz": "❌ Yo'q"}


def _lang_switch_keyboard(target_lang: str) -> InlineKeyboardMarkup:
    """Two-button inline keyboard: confirm switch to *target_lang* or decline."""
    yes_label = _LANG_SWITCH_YES.get(target_lang, _LANG_SWITCH_YES["ru"])
    no_label = _LANG_SWITCH_NO.get(target_lang, _LANG_SWITCH_NO["ru"])
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=yes_label, callback_data=f"lang_switch:{target_lang}"),
        InlineKeyboardButton(text=no_label, callback_data="lang_switch:keep"),
    ]])


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
        "main_menu": t("main_menu_prompt", code),
        "dialog_ended": t("dialog_ended", code),
        "dialog_no_active": t("dialog_no_active", code),
        "dialog_already_active": t("dialog_already_active", code),
        "start_dialog_greeting": t("start_dialog_greeting", code),
        "feature_coming_soon": t("feature_coming_soon", code),
        "history_header": t("history_header", code),
        "history_empty": t("history_empty", code),
        "change_phone_prompt": t("change_phone_prompt", code),
        "phone_updated": t("phone_updated", code),
        "branches_menu_prompt": t("branches_menu_prompt", code),
        "rates_menu_prompt": t("rates_menu_prompt", code),
        "links_menu_prompt": t("links_menu_prompt", code),
        "settings_menu_prompt": t("settings_menu_prompt", code),
        "language_saved": t("language_saved", code),
        "phone_saved_choose_language": t("phone_saved_choose_language", code),
        "send_location_prompt": {
            "ru": "📍 Отправьте геолокацию, чтобы найти ближайший ЦБУ.",
            "en": "📍 Send your location to find the nearest branch.",
            "uz": "📍 Eng yaqin filialni topish uchun geolokatsiyani yuboring.",
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


def _mobile_app_inline_keyboard(lang: str) -> InlineKeyboardMarkup:
    """URL buttons for Android / iOS app stores."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=APP_LABELS["android"][lang], url=ANDROID_APP_URL)],
        [InlineKeyboardButton(text=APP_LABELS["ios"][lang], url=IOS_APP_URL)],
    ])


def _social_links_inline_keyboard(lang: str) -> InlineKeyboardMarkup:
    """URL buttons for the four official social-media channels."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=SOCIAL_LABELS["instagram"][lang], url=INSTAGRAM_URL)],
        [InlineKeyboardButton(text=SOCIAL_LABELS["telegram"][lang], url=TELEGRAM_CHANNEL_URL)],
        [InlineKeyboardButton(text=SOCIAL_LABELS["facebook"][lang], url=FACEBOOK_URL)],
        [InlineKeyboardButton(text=SOCIAL_LABELS["website"][lang], url=WEBSITE_URL)],
    ])


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
    texts = _user_language_text(user.language)
    if ended:
        await message.answer(texts["dialog_ended"], reply_markup=main_menu_keyboard(user.language))
    else:
        await message.answer(texts["dialog_no_active"], reply_markup=main_menu_keyboard(user.language))


@router.message(Command("new"))
async def cmd_new(message: Message, chat_service: ChatService) -> None:
    """Single-session model: /new ends the current session (if any) and starts a fresh one.

    Kept for backward compatibility with the old /new command. The user-facing
    flow is the same as pressing "💬 Начать диалог" on the keyboard.
    """
    tg_user = message.from_user
    if tg_user is None:
        return

    user = await chat_service.get_or_create_user(
        telegram_user_id=tg_user.id,
        username=tg_user.username,
        first_name=tg_user.first_name,
        last_name=tg_user.last_name,
    )
    await chat_service.end_active_session(user.id)
    await chat_service.start_new_session(user.id)
    texts = _user_language_text(user.language)
    await message.answer(
        texts["start_dialog_greeting"],
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


@router.message(F.photo | F.video | F.audio | F.voice | F.document)
async def handle_media(message: Message, chat_service: ChatService) -> None:
    """
    Медиа от пользователя в human_mode пересылается оператору через
    MinIO upload + middleware send-message (как в call_center_bot).
    Если human_mode выключен или middleware не настроен — пишем подсказку.
    """
    tg_user = message.from_user
    if tg_user is None:
        return

    user = await chat_service.get_or_create_user(
        telegram_user_id=tg_user.id,
        username=tg_user.username,
        first_name=tg_user.first_name,
        last_name=tg_user.last_name,
    )
    lang = normalize_lang(user.language)

    chat_session = await chat_service.ensure_active_session(user.id)
    if not chat_session.human_mode:
        await message.answer(t("agent_unavailable", lang))
        return

    from app.api.fastapi_app import app as _fastapi_app
    middleware_client = getattr(getattr(_fastapi_app, "state", None), "middleware_client", None)
    if middleware_client is None or not middleware_client.has_active_chat(chat_session.id):
        await message.answer(t("connection_lost", lang))
        return

    settings = get_settings()
    if not (settings.minio_base_url and settings.minio_username and settings.minio_password):
        await message.answer(t("message_send_failed", lang))
        return

    file_path: str | None = None
    try:
        if message.photo:
            ph = message.photo[-1]
            file_path = f"/tmp/{ph.file_id}.jpg"
            await message.bot.download(ph, file_path)
        elif message.video:
            file_path = f"/tmp/{message.video.file_id}.mp4"
            await message.bot.download(message.video, file_path)
        elif message.audio:
            file_path = f"/tmp/{message.audio.file_id}.mp3"
            await message.bot.download(message.audio, file_path)
        elif message.voice:
            file_path = f"/tmp/{message.voice.file_id}.mp3"
            await message.bot.download(message.voice, file_path)
        elif message.document:
            ext = os.path.splitext(message.document.file_name or "")[1] or ".bin"
            file_path = f"/tmp/{message.document.file_id}{ext}"
            await message.bot.download(message.document, file_path)

        if not file_path:
            return

        from app.services.middleware_files import upload_file_to_minio

        minio_path = await upload_file_to_minio(
            file_path,
            base_url=settings.minio_base_url,
            username=settings.minio_username,
            password=settings.minio_password,
            verify_ssl=settings.middleware_verify_ssl,
        )
        if not minio_path:
            await message.answer(t("message_send_failed", lang))
            return

        ok = await middleware_client.send_message(chat_session.id, minio_path)
        if not ok:
            await message.answer(t("message_send_failed", lang))
        else:
            await chat_service._save_message(
                session_id=chat_session.id,
                role="user",
                text=f"[file] {minio_path}",
                telegram_message_id=message.message_id,
            )
    except Exception:
        await message.answer(t("message_send_failed", lang))
    finally:
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
            except OSError:
                pass


async def _show_cbu_rates(message: Message, lang: str, texts: dict[str, str]) -> None:
    """Display CBU exchange rates as an inline list. Used for «Курс физ.лиц»."""
    from app.utils.cbu_rates import fetch_cbu_rates

    cbu_data = await fetch_cbu_rates(("USD", "EUR", "RUB", "GBP", "KZT", "CNY"))
    if not cbu_data:
        await message.answer(texts["rates_ref"], reply_markup=rates_submenu_keyboard(lang))
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


async def _show_session_history(message: Message, chat_service: ChatService, user_id: int, lang: str, texts: dict[str, str]) -> None:
    """Read-only view of the most recent session's last messages."""
    recent = await chat_service.list_recent_sessions(user_id, limit=1)
    if not recent:
        await message.answer(texts["history_empty"], reply_markup=settings_submenu_keyboard(lang))
        return
    msgs = await chat_service.get_recent_messages(recent[0].id, limit=20)
    if not msgs:
        await message.answer(texts["history_empty"], reply_markup=settings_submenu_keyboard(lang))
        return
    lines = [texts["history_header"], ""]
    for m in msgs:
        prefix = "👤" if m.role == "user" else "🤖"
        body = (m.text or "").strip()
        if len(body) > 300:
            body = body[:297] + "…"
        lines.append(f"{prefix} {body}")
    await _answer_safe(
        message,
        "\n\n".join(lines),
        reply_markup=settings_submenu_keyboard(lang),
    )


async def _back_keyboard(chat_service: ChatService, user_id: int, lang: str):
    """Pick the right top-level keyboard based on whether a dialog is active."""
    active = await chat_service.get_active_session(user_id)
    return chat_keyboard(lang) if active else main_menu_keyboard(lang)


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

    # ── Navigation: BACK returns to the appropriate top-level keyboard ──────
    if action in {BACK, "cancel"}:
        kb = await _back_keyboard(chat_service, user.id, lang)
        await message.answer(texts["main_menu"], reply_markup=kb)
        return

    # ── Top-level: start / end dialog ───────────────────────────────────────
    if action == START_DIALOG:
        await chat_service.ensure_active_session(user.id)
        await message.answer(texts["start_dialog_greeting"], reply_markup=chat_keyboard(lang))
        return

    if action == END_DIALOG:
        ended = await chat_service.end_active_session(user.id)
        msg = texts["dialog_ended"] if ended else texts["dialog_no_active"]
        await message.answer(msg, reply_markup=main_menu_keyboard(lang))
        return

    # ── Top-level: open a section (4 reply submenus) ────────────────────────
    if action == OUR_BRANCHES:
        await message.answer(texts["branches_menu_prompt"], reply_markup=branches_submenu_keyboard(lang))
        return

    if action == CURRENCY_RATES:
        await message.answer(texts["rates_menu_prompt"], reply_markup=rates_submenu_keyboard(lang))
        return

    if action == USEFUL_LINKS:
        await message.answer(texts["links_menu_prompt"], reply_markup=links_submenu_keyboard(lang))
        return

    if action == SETTINGS:
        await message.answer(texts["settings_menu_prompt"], reply_markup=settings_submenu_keyboard(lang))
        return

    # ── Branches submenu leaves ─────────────────────────────────────────────
    if action == OFFICE_LIST:
        await message.answer(_OFFICE_TYPE_PROMPT[lang], reply_markup=_office_type_inline_keyboard(lang))
        return

    if action == NEAREST_BRANCH:
        await message.answer(texts["send_location_prompt"], reply_markup=location_keyboard(lang))
        return

    if action in {ELECTRONIC_QUEUE, WEEKEND_DAYS}:
        await message.answer(texts["feature_coming_soon"], reply_markup=branches_submenu_keyboard(lang))
        return

    # ── Rates submenu leaves ────────────────────────────────────────────────
    if action == RATES_INDIVIDUALS:
        await _show_cbu_rates(message, lang, texts)
        return

    if action in {RATES_CORPORATE, RATES_ONLINE, RATES_ATM}:
        await message.answer(texts["feature_coming_soon"], reply_markup=rates_submenu_keyboard(lang))
        return

    # ── Useful links submenu leaves ─────────────────────────────────────────
    if action == MOBILE_APP_LINK:
        await message.answer(APP_HEADER[lang], reply_markup=_mobile_app_inline_keyboard(lang))
        return

    if action == SOCIAL_LINKS:
        await message.answer(SOCIAL_HEADER[lang], reply_markup=_social_links_inline_keyboard(lang))
        return

    if action == CONTACTS_COMPLAINTS:
        body = f"{CONTACTS_HEADER[lang]}\n\n{CONTACTS_BODY[lang]}"
        await message.answer(body, reply_markup=links_submenu_keyboard(lang), parse_mode="HTML")
        return

    # ── Settings submenu leaves ─────────────────────────────────────────────
    if action == CHANGE_LANGUAGE:
        await message.answer(texts["ask_language"], reply_markup=language_keyboard())
        return

    if action == CHANGE_PHONE:
        await message.answer(texts["change_phone_prompt"], reply_markup=contact_keyboard(lang))
        return

    if action == MY_SESSIONS:
        await _show_session_history(message, chat_service, user.id, lang, texts)
        return

    # ── Free-form text → agent (only when a dialog is active) ───────────────
    # If the user hasn't pressed «💬 Начать диалог» yet (or has just ended a
    # dialog), we don't auto-start one on every typed message. Otherwise the
    # bot would feel chatty before the user opted in.
    active_session = await chat_service.get_active_session(user.id)
    if active_session is None:
        await message.answer(texts["dialog_no_active"], reply_markup=main_menu_keyboard(lang))
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
    if reply.suggested_language and reply.suggested_language != lang:
        target = reply.suggested_language
        prompt = _LANG_SWITCH_PROMPT.get(target, _LANG_SWITCH_PROMPT["ru"])
        await message.answer(prompt, reply_markup=_lang_switch_keyboard(target))


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
    if reply.suggested_language and reply.suggested_language != lang:
        target = reply.suggested_language
        prompt = _LANG_SWITCH_PROMPT.get(target, _LANG_SWITCH_PROMPT["ru"])
        await callback.message.answer(prompt, reply_markup=_lang_switch_keyboard(target))


@router.callback_query(F.data.startswith("lang_switch:"))
async def lang_switch_callback(callback: CallbackQuery, chat_service: ChatService) -> None:
    """Handle the heuristic switch-language offer.

    `lang_switch:keep` dismisses the prompt; `lang_switch:<code>` updates
    User.language (mirrors the existing `lang:` callback for /start).
    """
    if callback.from_user is None or callback.message is None or not callback.data:
        await callback.answer()
        return
    _, action = callback.data.split(":", 1)

    if action == "keep":
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        await callback.answer()
        return

    if action not in {"ru", "en", "uz"}:
        await callback.answer()
        return

    await chat_service.get_or_create_user(
        telegram_user_id=callback.from_user.id,
        username=callback.from_user.username,
        first_name=callback.from_user.first_name,
        last_name=callback.from_user.last_name,
        language=action,
    )
    texts = _user_language_text(action)
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    # Visible chat message in the new language so the user sees the switch
    # took effect (toast via callback.answer is too easy to miss).
    confirmation = {
        "ru": "✅ Готово, продолжаем по-русски.",
        "en": "✅ Done, switching to English.",
        "uz": "✅ Tayyor, o'zbek tilida davom etamiz.",
    }[action]
    await callback.message.answer(confirmation, reply_markup=chat_keyboard(action))
    await callback.answer(texts["language_saved"])


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

    lang = normalize_lang(user.language)

    # Working hours guard (Asia/Tashkent 8–23 by default, configurable)
    if not _is_within_working_hours():
        await callback.message.answer(t("working_hours", lang))
        await callback.answer()
        return

    # Chat Middleware
    from app.api.fastapi_app import app as _fastapi_app
    middleware_client = getattr(getattr(_fastapi_app, "state", None), "middleware_client", None)

    if middleware_client is None:
        # Middleware not configured — tell user operators are unavailable
        await callback.message.answer(t("middleware_unavailable", lang))
        await callback.answer()
        return

    # Phone is required by middleware (used as requestId/userPhone)
    if not user.phone:
        await callback.message.answer(
            t("phone_required_for_operator", lang),
            reply_markup=contact_keyboard(lang),
        )
        await callback.answer()
        return

    await chat_service.set_human_mode(session_id, True)
    customer_name = user.first_name or (f"@{user.username}" if user.username else str(user.telegram_user_id))

    await callback.message.answer(t("searching_operator", lang))
    await callback.answer()

    ok = await middleware_client.start_chat(
        session_id=session_id,
        phone=user.phone,
        user_name=customer_name,
        lang=lang,
        telegram_id=user.telegram_user_id,
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
