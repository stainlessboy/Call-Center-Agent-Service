from __future__ import annotations

import logging as _logging
import os
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


def _require_persistent() -> bool:
    """True if env demands a non-in-memory checkpointer.

    When set, the runtime must fail fast rather than silently fall back
    to ``MemorySaver`` — otherwise a transient DB outage at startup
    leaves us running without session persistence.
    """
    val = (os.getenv("REQUIRE_PERSISTENT_CHECKPOINTER") or "").strip().lower()
    return val in ("1", "true", "yes", "on")


async def _create_async_checkpointer(backend: str, url: Optional[str]) -> tuple[Any, Any]:
    _lg = _logging.getLogger(__name__)
    explicit_postgres = backend in ("postgres", "pg")

    pg_url = url
    if backend in ("auto", "postgres", "pg"):
        if not pg_url:
            from app.config import get_settings as _get_settings
            pg_url = _derive_postgres_url(_get_settings().database_url)
        if pg_url:
            try:
                from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
                cm = AsyncPostgresSaver.from_conn_string(pg_url)
                saver = await cm.__aenter__()
                try:
                    await saver.setup()
                except Exception as exc:
                    _lg.debug("Postgres checkpointer setup (tables may already exist): %s", exc)
                _lg.info("Using Postgres checkpointer")
                return saver, cm
            except Exception as e:
                if explicit_postgres:
                    # The operator explicitly asked for persistence. Do NOT
                    # silently degrade to in-memory — re-raise so the process
                    # fails fast and k8s/systemd restarts cleanly.
                    _lg.error("Postgres checkpointer failed and backend=%s requires persistence: %s", backend, e)
                    raise
                _lg.error("Postgres checkpointer failed (%s), falling back to MemorySaver", e)
        else:
            if explicit_postgres:
                raise RuntimeError(
                    "LANGGRAPH_CHECKPOINT_BACKEND=postgres but no DATABASE_URL / checkpoint URL is configured"
                )
            _lg.warning("No postgres URL available for checkpointer, using MemorySaver")

    if _require_persistent():
        raise RuntimeError(
            "REQUIRE_PERSISTENT_CHECKPOINTER=true but resolved checkpointer is MemorySaver "
            "(backend=%s). Refusing to start without persistent checkpointing." % backend
        )

    return MemorySaver(), None
