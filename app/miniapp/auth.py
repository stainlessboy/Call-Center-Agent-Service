"""Telegram Mini App authentication — ``initData`` verification.

Telegram signs the launch parameters with the bot token; we recompute the HMAC
and compare it in constant time. See
https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app

The signed payload is passed by the client in the ``Authorization`` header as
``tma <raw init data>`` (the convention used by @telegram-apps/sdk), with a
``X-Telegram-Init-Data`` header accepted as a fallback for the WebSocket
handshake where custom auth headers are awkward.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import parse_qsl

from fastapi import Depends, Header, HTTPException, Query

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

# Telegram's HMAC covers every received field except `hash` itself — including
# `signature`, the separate Ed25519 value meant for third-party validation.
# Only the Ed25519 check drops `signature`; conflating the two is what makes a
# real launch fail with "signature mismatch".
_HASH_FIELD = "hash"
_ED25519_FIELD = "signature"


@dataclass(frozen=True)
class TelegramUser:
    """Authenticated Telegram user as reported by the Mini App launch params."""

    telegram_user_id: int
    first_name: str = ""
    last_name: str = ""
    username: str = ""
    language_code: str = ""
    photo_url: str = ""
    is_dev: bool = False

    @property
    def display_name(self) -> str:
        parts = [p for p in (self.first_name, self.last_name) if p]
        return " ".join(parts) or self.username or str(self.telegram_user_id)


class InitDataError(Exception):
    """Raised when initData is missing, malformed, expired or forged."""


def _data_check_string(pairs: list[tuple[str, str]]) -> str:
    return "\n".join(f"{k}={v}" for k, v in sorted(pairs, key=lambda kv: kv[0]))


def verify_init_data(raw: str, bot_token: str, ttl_seconds: int = 86400) -> dict:
    """Verify raw initData and return its parsed fields.

    Raises ``InitDataError`` when the signature does not match, the payload is
    malformed, or ``auth_date`` is older than *ttl_seconds* (0 disables the
    freshness check).
    """
    if not raw:
        raise InitDataError("initData is empty")
    if not bot_token:
        raise InitDataError("BOT_TOKEN is not configured")

    # keep_blank_values: Telegram may send empty optional fields, and they are
    # part of the signed string.
    pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=False)
    if not pairs:
        raise InitDataError("initData is not a valid query string")

    received_hash = ""
    checked: list[tuple[str, str]] = []
    for key, value in pairs:
        if key == _HASH_FIELD:
            received_hash = value
        else:
            checked.append((key, value))

    if not received_hash:
        raise InitDataError("initData has no hash")

    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()

    def digest(items: list[tuple[str, str]]) -> str:
        return hmac.new(
            secret_key, _data_check_string(items).encode(), hashlib.sha256
        ).hexdigest()

    # Canonical variant first. The `signature`-less variant is accepted as a
    # fallback for clients that predate Bot API 8.0 conventions — both strings
    # are built from the same received fields, so neither weakens the check.
    candidates = [checked]
    if any(key == _ED25519_FIELD for key, _ in checked):
        candidates.append([(k, v) for k, v in checked if k != _ED25519_FIELD])

    if not any(hmac.compare_digest(digest(candidate), received_hash) for candidate in candidates):
        # Field names only — values carry the user's profile and must not be logged.
        logger.warning(
            "initData signature mismatch (bot id %s, fields: %s)",
            bot_token.split(":", 1)[0] or "?",
            ",".join(sorted(key for key, _ in checked)),
        )
        raise InitDataError("initData signature mismatch")

    fields = dict(checked)

    if ttl_seconds > 0:
        try:
            auth_date = int(fields.get("auth_date", "0"))
        except ValueError as exc:
            raise InitDataError("initData auth_date is not an integer") from exc
        if auth_date <= 0:
            raise InitDataError("initData has no auth_date")
        age = time.time() - auth_date
        if age > ttl_seconds:
            raise InitDataError(f"initData expired ({int(age)}s old)")

    return fields


def _user_from_fields(fields: dict) -> TelegramUser:
    raw_user = fields.get("user")
    if not raw_user:
        raise InitDataError("initData has no user payload")
    try:
        data = json.loads(raw_user)
    except json.JSONDecodeError as exc:
        raise InitDataError("initData user payload is not valid JSON") from exc

    tg_id = data.get("id")
    if not isinstance(tg_id, int):
        raise InitDataError("initData user has no numeric id")

    return TelegramUser(
        telegram_user_id=tg_id,
        first_name=str(data.get("first_name") or ""),
        last_name=str(data.get("last_name") or ""),
        username=str(data.get("username") or ""),
        language_code=str(data.get("language_code") or ""),
        photo_url=str(data.get("photo_url") or ""),
    )


def _dev_user(settings: Settings) -> TelegramUser:
    return TelegramUser(
        telegram_user_id=settings.miniapp_dev_user_id,
        first_name="Dev",
        last_name="User",
        username="dev",
        language_code="ru",
        is_dev=True,
    )


def authenticate(raw_init_data: str | None) -> TelegramUser:
    """Resolve a Telegram user from raw initData, honouring dev mode."""
    settings = get_settings()
    raw = (raw_init_data or "").strip()

    if not raw:
        if settings.miniapp_dev_mode:
            return _dev_user(settings)
        raise InitDataError("initData is missing")

    try:
        fields = verify_init_data(
            raw,
            bot_token=settings.bot_token,
            ttl_seconds=settings.miniapp_init_data_ttl_seconds,
        )
    except InitDataError:
        if settings.miniapp_dev_mode:
            logger.warning("MINIAPP_DEV_MODE: accepting invalid initData as dev user")
            return _dev_user(settings)
        raise
    return _user_from_fields(fields)


def _extract_raw(authorization: str | None, header_init_data: str | None) -> str | None:
    if authorization:
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() == "tma" and value.strip():
            return value.strip()
    return header_init_data


async def require_telegram_user(
    authorization: Optional[str] = Header(default=None),
    x_telegram_init_data: Optional[str] = Header(default=None, alias="X-Telegram-Init-Data"),
) -> TelegramUser:
    """FastAPI dependency: authenticated Telegram user or 401."""
    try:
        return authenticate(_extract_raw(authorization, x_telegram_init_data))
    except InitDataError as exc:
        raise HTTPException(status_code=401, detail=f"Unauthorized: {exc}") from exc


async def require_telegram_user_ws(
    init_data: Optional[str] = Query(default=None, alias="init_data"),
) -> TelegramUser:
    """WebSocket variant — initData arrives as a query parameter."""
    try:
        return authenticate(init_data)
    except InitDataError as exc:
        raise HTTPException(status_code=401, detail=f"Unauthorized: {exc}") from exc


CurrentUser = Depends(require_telegram_user)
