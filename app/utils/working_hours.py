"""Operator working-hours window, shared by the bot and the Mini App."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.config import get_settings


def is_within_working_hours() -> bool:
    """Окно работы операторов middleware (по дефолту 8:00–23:00 Asia/Tashkent)."""
    settings = get_settings()
    if not settings.middleware_working_hours_enabled:
        return True
    tz = timezone(timedelta(hours=settings.middleware_working_hours_tz_offset))
    now = datetime.now(tz)
    return settings.middleware_working_hours_start <= now.hour < settings.middleware_working_hours_end


def working_hours_info() -> dict:
    """Window description for clients that render it (Mini App operator screen)."""
    settings = get_settings()
    return {
        "enabled": settings.middleware_enabled,
        "hours_enforced": settings.middleware_working_hours_enabled,
        "start_hour": settings.middleware_working_hours_start,
        "end_hour": settings.middleware_working_hours_end,
        "tz_offset": settings.middleware_working_hours_tz_offset,
        "is_open_now": is_within_working_hours(),
    }
