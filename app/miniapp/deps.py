"""Shared FastAPI dependencies for the Mini App API."""
from __future__ import annotations

from fastapi import Depends, HTTPException, Request

from app.bot.i18n import normalize_lang
from app.db.models import User
from app.miniapp.auth import TelegramUser, require_telegram_user
from app.services.chat_service import ChatService


def get_chat_service(request: Request) -> ChatService:
    service: ChatService | None = getattr(request.app.state, "chat_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="Chat service is not ready")
    return service


async def get_db_user(
    tg_user: TelegramUser = Depends(require_telegram_user),
    chat_service: ChatService = Depends(get_chat_service),
) -> User:
    """Authenticated Telegram user mapped onto the `users` row.

    `language` is deliberately not passed: the user's stored language is their
    explicit choice and must not be silently overwritten by the Telegram client
    locale on every launch. A first-time user gets their locale as the default
    in the bootstrap route instead.
    """
    return await chat_service.get_or_create_user(
        telegram_user_id=tg_user.telegram_user_id,
        username=tg_user.username or None,
        first_name=tg_user.first_name or None,
        last_name=tg_user.last_name or None,
    )


def user_lang(user: User) -> str:
    return normalize_lang(user.language)
