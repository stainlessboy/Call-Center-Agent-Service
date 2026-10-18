"""Long-term memory store (roadmap П.4).

A checkpointer persists ONE conversation thread; a store persists facts about
a PERSON across every thread they ever open. The "personal consultant" memory
is the second kind, and it currently lives in a hand-rolled `user_profiles`
table with its own JSONB merge SQL. LangGraph's `BaseStore` provides the same
thing with namespaces, batching and (on Postgres) semantic search, and hands
it to tools through `runtime.store` without any of them importing the ORM.

Deviation from the roadmap worth knowing: it proposed renaming
`checkpointer.py` to `persistence.py` and creating both there. The rename buys
nothing and breaks every existing import and `tests/test_checkpointer.py`, so
the store lives in its own module instead and `checkpointer.py` is untouched.

Rollout: `PROFILE_BACKEND` selects who owns the profile data —
`table` (default, the existing `user_profiles` rows) or `store`. Both
implementations live behind the same functions in `app/agent/profile.py`, so
switching is one env var plus running `scripts/migrate_profiles_to_store.py`.
"""
from __future__ import annotations

import logging as _logging
import os
from typing import Any, Optional

_logger = _logging.getLogger(__name__)

# Namespace layout. `("users", "<telegram_user_id>")` keeps one person's data
# together; keys inside it separate the standing profile from the episodic
# memories, because they have different lifetimes and different readers.
_NS_ROOT = "users"
PROFILE_KEY = "profile"
MEMORIES_NS = "memories"


def profile_namespace(user_id: int | str) -> tuple[str, str]:
    """Namespace holding one customer's standing profile."""
    return (_NS_ROOT, str(user_id))


def memories_namespace(user_id: int | str) -> tuple[str, str, str]:
    """Namespace holding one customer's episodic memories ("in March they
    asked about a resale-market mortgage"). Searchable by meaning when the
    backing store has an embedding index."""
    return (_NS_ROOT, str(user_id), MEMORIES_NS)


def profile_backend() -> str:
    """`PROFILE_BACKEND` — "table" (default) or "store"."""
    value = (os.getenv("PROFILE_BACKEND") or "table").strip().lower()
    return value if value in {"table", "store"} else "table"


def store_backed_profiles() -> bool:
    return profile_backend() == "store"


def _semantic_index() -> Optional[dict]:
    """Embedding index config for `asearch`, or None to index nothing.

    Off unless `STORE_SEMANTIC_SEARCH` is set: it makes every write call the
    embeddings API, which is only worth paying for once episodic memories are
    actually being searched.
    """
    if (os.getenv("STORE_SEMANTIC_SEARCH") or "").strip().lower() not in {
        "1", "true", "yes", "on",
    }:
        return None
    model = os.getenv("FAQ_EMBEDDING_MODEL") or "text-embedding-3-small"
    dims = int(os.getenv("STORE_EMBEDDING_DIMS") or 1536)
    return {"dims": dims, "embed": f"openai:{model}"}


async def create_store(backend: str = "auto", url: Optional[str] = None) -> tuple[Any, Any]:
    """Build the long-term store. Returns `(store, context_manager | None)`.

    Mirrors `checkpointer._create_async_checkpointer`'s contract, including its
    failure policy: a Postgres store that cannot be reached degrades to an
    in-memory one with a loud log rather than blocking startup — losing
    long-term memory is a quality regression, not an outage, and the bot
    answers fine without it.
    """
    if backend in ("memory", "none"):
        from langgraph.store.memory import InMemoryStore

        return InMemoryStore(), None

    pg_url = url
    if not pg_url:
        from app.agent.checkpointer import _derive_postgres_url
        from app.config import get_settings

        pg_url = _derive_postgres_url(get_settings().database_url)

    if pg_url:
        try:
            from langgraph.store.postgres.aio import AsyncPostgresStore

            index = _semantic_index()
            cm = (
                AsyncPostgresStore.from_conn_string(pg_url, index=index)
                if index
                else AsyncPostgresStore.from_conn_string(pg_url)
            )
            store = await cm.__aenter__()
            try:
                await store.setup()
            except Exception as exc:
                _logger.debug("Store setup (tables may already exist): %s", exc)
            _logger.info(
                "Using Postgres long-term store (semantic search: %s)",
                "on" if index else "off",
            )
            return store, cm
        except Exception as exc:
            _logger.error("Postgres store unavailable (%s), falling back to in-memory", exc)

    from langgraph.store.memory import InMemoryStore

    return InMemoryStore(), None
