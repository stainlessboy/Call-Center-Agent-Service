from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache

from dotenv import load_dotenv

load_dotenv()


@dataclass
class Settings:
    bot_token: str
    database_url: str
    redis_url: str | None
    log_level: str
    operator_ids: list[int]
    operator_api_key: str | None
    webhook_base_url: str | None
    webhook_path: str
    webhook_secret: str | None
    langgraph_checkpoint_backend: str
    langgraph_checkpoint_url: str | None
    langgraph_dialog_ttl_minutes: int
    langgraph_ttl_store_path: str
    session_inactivity_timeout_minutes: int
    human_mode_operator_timeout_minutes: int


def _parse_operator_ids(raw: str | None) -> list[int]:
    if not raw:
        return []
    parts = raw.replace(";", ",").split(",")
    ids: list[int] = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        try:
            ids.append(int(p))
        except ValueError:
            continue
    return ids


def _parse_webhook_path(raw: str | None) -> str:
    path = (raw or "/telegram/webhook").strip()
    if not path:
        return "/telegram/webhook"
    if not path.startswith("/"):
        return f"/{path}"
    return path


def _parse_positive_int(raw: str | None, default: int) -> int:
    if raw is None:
        return default
    try:
        value = int(str(raw).strip())
        return value if value >= 0 else default
    except ValueError:
        return default


@lru_cache
def get_settings() -> Settings:
    return Settings(
        bot_token=os.getenv("BOT_TOKEN", "").strip(),
        database_url=os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./bot.db").strip(),
        redis_url=(os.getenv("REDIS_URL") or "").strip() or None,
        log_level=os.getenv("LOG_LEVEL", "INFO").strip(),
        operator_ids=_parse_operator_ids(os.getenv("OPERATOR_IDS")),
        operator_api_key=(os.getenv("OPERATOR_API_KEY") or "").strip() or None,
        webhook_base_url=(os.getenv("WEBHOOK_BASE_URL") or "").strip() or None,
        webhook_path=_parse_webhook_path(os.getenv("WEBHOOK_PATH")),
        webhook_secret=(os.getenv("WEBHOOK_SECRET") or "").strip() or None,
        langgraph_checkpoint_backend=(os.getenv("LANGGRAPH_CHECKPOINT_BACKEND") or "auto").strip().lower(),
        langgraph_checkpoint_url=(os.getenv("LANGGRAPH_CHECKPOINT_URL") or "").strip() or None,
        langgraph_dialog_ttl_minutes=_parse_positive_int(os.getenv("LANGGRAPH_DIALOG_TTL_MINUTES"), default=720),
        langgraph_ttl_store_path=(os.getenv("LANGGRAPH_TTL_STORE_PATH") or ".langgraph_ttl.sqlite3").strip(),
        session_inactivity_timeout_minutes=_parse_positive_int(
            os.getenv("SESSION_INACTIVITY_TIMEOUT_MINUTES"),
            default=60,
        ),
        human_mode_operator_timeout_minutes=_parse_positive_int(
            os.getenv("HUMAN_MODE_OPERATOR_TIMEOUT_MINUTES"),
            default=10,
        ),
    )
