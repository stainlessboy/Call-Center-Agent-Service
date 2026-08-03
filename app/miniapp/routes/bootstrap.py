"""Launch payload, user settings and session history."""
from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.bot.i18n import SUPPORTED_LANGS, normalize_lang
from app.config import get_settings
from app.db.models import User
from app.miniapp.auth import TelegramUser, require_telegram_user
from app.miniapp.deps import get_chat_service, get_db_user, user_lang
from app.services.chat_service import ChatService
from app.utils.working_hours import working_hours_info

router = APIRouter()

_PHONE_RE = re.compile(r"^\+?\d{9,15}$")


def _mask_phone(phone: str | None) -> str:
    if not phone:
        return ""
    digits = re.sub(r"\D", "", phone)
    if len(digits) < 4:
        return phone
    return f"+{digits[:3]} ** *** {digits[-4:-2]} {digits[-2:]}"


@router.get("/bootstrap")
async def bootstrap(
    tg_user: TelegramUser = Depends(require_telegram_user),
    user: User = Depends(get_db_user),
    chat_service: ChatService = Depends(get_chat_service),
) -> dict:
    settings = get_settings()

    # First launch: seed the stored language from the Telegram client locale.
    if not user.language:
        user = await chat_service.get_or_create_user(
            telegram_user_id=user.telegram_user_id,
            username=None,
            first_name=None,
            last_name=None,
            language=normalize_lang(tg_user.language_code),
        )

    active = await chat_service.get_active_session(user.id)
    used_today = await chat_service.count_user_messages_today(user.id)

    return {
        "user": {
            "telegram_user_id": user.telegram_user_id,
            "first_name": user.first_name or tg_user.first_name,
            "last_name": user.last_name or tg_user.last_name,
            "username": user.username or tg_user.username,
            "photo_url": tg_user.photo_url,
            "phone": user.phone or "",
            "phone_masked": _mask_phone(user.phone),
            "has_phone": bool(user.phone),
            "lang": user_lang(user),
            "theme": user.theme or "auto",
        },
        "session": {
            "id": active.id if active else None,
            "human_mode": bool(active.human_mode) if active else False,
            "started_at": active.started_at.isoformat() if active else None,
        },
        "limits": {
            "daily_used": used_today,
            "daily_max": settings.daily_message_limit,
            "max_message_length": settings.max_message_length,
        },
        "operator": working_hours_info(),
        "langs": sorted(SUPPORTED_LANGS),
        "dev_mode": tg_user.is_dev,
    }


class LanguagePayload(BaseModel):
    lang: str = Field(min_length=2, max_length=8)


@router.post("/settings/language")
async def set_language(
    payload: LanguagePayload,
    user: User = Depends(get_db_user),
    chat_service: ChatService = Depends(get_chat_service),
) -> dict:
    lang = normalize_lang(payload.lang)
    if payload.lang.strip().lower() not in SUPPORTED_LANGS:
        raise HTTPException(status_code=422, detail="Unsupported language")
    await chat_service.get_or_create_user(
        telegram_user_id=user.telegram_user_id,
        username=None,
        first_name=None,
        last_name=None,
        language=lang,
    )
    return {"ok": True, "lang": lang}


THEMES = ("auto", "light", "dark")


class ThemePayload(BaseModel):
    theme: str = Field(min_length=3, max_length=8)


@router.post("/settings/theme")
async def set_theme(
    payload: ThemePayload,
    user: User = Depends(get_db_user),
    chat_service: ChatService = Depends(get_chat_service),
) -> dict:
    theme = payload.theme.strip().lower()
    if theme not in THEMES:
        raise HTTPException(status_code=422, detail=f"Theme must be one of {THEMES}")
    await chat_service.set_user_theme(user.telegram_user_id, theme)
    return {"ok": True, "theme": theme}


class PhonePayload(BaseModel):
    phone: str = Field(min_length=6, max_length=32)


@router.post("/settings/phone")
async def set_phone(
    payload: PhonePayload,
    user: User = Depends(get_db_user),
    chat_service: ChatService = Depends(get_chat_service),
) -> dict:
    raw = payload.phone.strip().replace(" ", "").replace("-", "")
    if not _PHONE_RE.match(raw):
        raise HTTPException(status_code=422, detail="Invalid phone format")
    updated = await chat_service.get_or_create_user(
        telegram_user_id=user.telegram_user_id,
        username=None,
        first_name=None,
        last_name=None,
        phone=raw,
    )
    return {"ok": True, "phone_masked": _mask_phone(updated.phone)}


@router.get("/session/history")
async def session_history(
    limit: int = 50,
    user: User = Depends(get_db_user),
    chat_service: ChatService = Depends(get_chat_service),
) -> dict:
    active = await chat_service.get_active_session(user.id)
    if active is None:
        return {"session_id": None, "items": []}
    messages = await chat_service.get_recent_messages(active.id, limit=max(1, min(limit, 200)))
    return {
        "session_id": active.id,
        "started_at": active.started_at.isoformat(),
        "items": [
            {
                "id": m.id,
                "role": m.role,
                "text": m.text,
                "created_at": m.created_at.isoformat() if m.created_at else None,
            }
            for m in messages
        ],
    }


@router.post("/session/end")
async def end_session(
    user: User = Depends(get_db_user),
    chat_service: ChatService = Depends(get_chat_service),
) -> dict:
    session_id = await chat_service.end_active_session(user.id, reason="miniapp_end")
    return {"ok": True, "ended_session_id": session_id}
