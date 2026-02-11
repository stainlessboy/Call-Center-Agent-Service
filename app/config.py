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


@lru_cache
def get_settings() -> Settings:
    return Settings(
        bot_token=os.getenv("BOT_TOKEN", "").strip(),
        database_url=os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./bot.db").strip(),
        redis_url=(os.getenv("REDIS_URL") or "").strip() or None,
        log_level=os.getenv("LOG_LEVEL", "INFO").strip(),
        operator_ids=_parse_operator_ids(os.getenv("OPERATOR_IDS")),
        operator_api_key=(os.getenv("OPERATOR_API_KEY") or "").strip() or None,
    )
