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
    log_level: str
    webhook_base_url: str | None
    webhook_path: str
    webhook_secret: str | None
    langgraph_checkpoint_backend: str
    langgraph_checkpoint_url: str | None
    langgraph_dialog_ttl_minutes: int
    session_inactivity_timeout_minutes: int
    human_mode_operator_timeout_minutes: int
    admin_username: str
    admin_password: str
    admin_secret_key: str
    agent_timeout_seconds: float
    max_message_length: int
    db_pool_size: int
    db_pool_max_overflow: int
    middleware_enabled: bool
    middleware_url: str | None
    middleware_login: str | None
    middleware_password: str | None
    middleware_nginx_ws_url: str | None
    middleware_is_test_request: bool
    middleware_verify_ssl: bool
    middleware_working_hours_enabled: bool
    middleware_working_hours_start: int
    middleware_working_hours_end: int
    middleware_working_hours_tz_offset: int
    minio_base_url: str | None
    minio_username: str | None
    minio_password: str | None


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
        database_url=os.getenv("DATABASE_URL", "postgresql+asyncpg://bankbot:bankbot@localhost:5432/bankbot").strip(),
        log_level=os.getenv("LOG_LEVEL", "INFO").strip(),
        webhook_base_url=(os.getenv("WEBHOOK_BASE_URL") or "").strip() or None,
        webhook_path=_parse_webhook_path(os.getenv("WEBHOOK_PATH")),
        webhook_secret=(os.getenv("WEBHOOK_SECRET") or "").strip() or None,
        langgraph_checkpoint_backend=(os.getenv("LANGGRAPH_CHECKPOINT_BACKEND") or "auto").strip().lower(),
        langgraph_checkpoint_url=(os.getenv("LANGGRAPH_CHECKPOINT_URL") or "").strip() or None,
        langgraph_dialog_ttl_minutes=_parse_positive_int(os.getenv("LANGGRAPH_DIALOG_TTL_MINUTES"), default=720),
        session_inactivity_timeout_minutes=_parse_positive_int(
            os.getenv("SESSION_INACTIVITY_TIMEOUT_MINUTES"),
            default=1440,  # 24 hours
        ),
        human_mode_operator_timeout_minutes=_parse_positive_int(
            os.getenv("HUMAN_MODE_OPERATOR_TIMEOUT_MINUTES"),
            default=10,
        ),
        admin_username=os.getenv("ADMIN_USERNAME", "admin").strip(),
        admin_password=os.getenv("ADMIN_PASSWORD", "admin").strip(),
        admin_secret_key=os.getenv("ADMIN_SECRET_KEY", "change-me-in-production").strip(),
        agent_timeout_seconds=float(os.getenv("AGENT_TIMEOUT_SECONDS", "25").strip()),
        max_message_length=_parse_positive_int(os.getenv("MAX_MESSAGE_LENGTH"), default=4000),
        db_pool_size=_parse_positive_int(os.getenv("DB_POOL_SIZE"), default=10),
        db_pool_max_overflow=_parse_positive_int(os.getenv("DB_POOL_MAX_OVERFLOW"), default=20),
        middleware_enabled=os.getenv("MIDDLEWARE_ENABLED", "false").strip().lower() == "true",
        middleware_url=(os.getenv("MIDDLEWARE_URL") or "").strip() or None,
        middleware_login=(os.getenv("MIDDLEWARE_LOGIN") or "").strip() or None,
        middleware_password=(os.getenv("MIDDLEWARE_PASSWORD") or "").strip() or None,
        middleware_nginx_ws_url=(os.getenv("MIDDLEWARE_NGINX_WS_URL") or "").strip() or None,
        middleware_is_test_request=os.getenv("MIDDLEWARE_IS_TEST_REQUEST", "false").strip().lower() == "true",
        middleware_verify_ssl=os.getenv("MIDDLEWARE_VERIFY_SSL", "false").strip().lower() == "true",
        middleware_working_hours_enabled=os.getenv("MIDDLEWARE_WORKING_HOURS_ENABLED", "false").strip().lower() == "true",
        middleware_working_hours_start=_parse_positive_int(os.getenv("MIDDLEWARE_WORKING_HOURS_START"), default=8),
        middleware_working_hours_end=_parse_positive_int(os.getenv("MIDDLEWARE_WORKING_HOURS_END"), default=23),
        middleware_working_hours_tz_offset=_parse_positive_int(os.getenv("MIDDLEWARE_WORKING_HOURS_TZ_OFFSET"), default=5),
        minio_base_url=(os.getenv("MINIO_BASE_URL") or "").strip() or None,
        minio_username=(os.getenv("MINIO_USERNAME") or "").strip() or None,
        minio_password=(os.getenv("MINIO_PASSWORD") or "").strip() or None,
    )
