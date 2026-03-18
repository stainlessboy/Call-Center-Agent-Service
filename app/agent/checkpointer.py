from __future__ import annotations

import logging as _logging
from typing import Any, Optional

from langgraph.checkpoint.memory import MemorySaver


def _derive_postgres_url(database_url: str) -> str | None:
    """Convert SQLAlchemy DATABASE_URL to a plain psycopg-compatible URL."""
    if not database_url:
        return None
    # postgresql+asyncpg://... → postgresql://...
    for prefix in ("postgresql+asyncpg://", "postgres+asyncpg://"):
        if database_url.startswith(prefix):
            return "postgresql://" + database_url[len(prefix):]
    if database_url.startswith(("postgresql://", "postgres://")):
        return database_url
    return None


async def _create_async_checkpointer(backend: str, url: Optional[str]) -> tuple[Any, Any]:
    _lg = _logging.getLogger(__name__)

    # For "auto" backend: try postgres first (derive URL from DATABASE_URL if needed)
    pg_url = url
    if backend in ("auto", "postgres", "pg"):
        if not pg_url and backend == "auto":
            from app.config import get_settings as _get_settings
            pg_url = _derive_postgres_url(_get_settings().database_url)
        if pg_url:
            try:
                from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
                cm = AsyncPostgresSaver.from_conn_string(pg_url)
                saver = await cm.__aenter__()
                try:
                    await saver.setup()
                except Exception:
                    pass
                _lg.info("Using Postgres checkpointer")
                return saver, cm
            except Exception as e:
                _lg.warning("Postgres checkpointer failed (%s), falling back to MemorySaver", e)
        else:
            _lg.warning("No postgres URL available for checkpointer, using MemorySaver")

    return MemorySaver(), None
