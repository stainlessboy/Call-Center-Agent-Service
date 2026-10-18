""""Personal consultant" client memory — load/merge `UserProfile` rows.

`UserProfile.user_id` is a FK to `users.id` (the internal PK), but every
call site in the agent layer only ever has the **Telegram** user id
(`BotState.user_id` / `Agent._ainvoke`'s `user_id` param — see
`app/services/chat_service.py`, which passes `user.telegram_user_id` to
`agent_client.send_message`). Rather than threading the internal PK through
`Agent`/`ChatService`/`AgentClient` (a real integration-layer change), the
functions here accept the Telegram id and resolve it to `users.id` locally,
the same way `app/agent/tools.py::select_office` and
`app/agent/nodes/helpers.py::_save_lead_async` open their own `get_session()`
and query models directly from the agent layer.

Two entry points:
  * `load_user_profile` — read, called once per turn from
    `Agent._ainvoke_locked` (agent.py). Returns `None` when no row exists yet
    for this user (not an error — most users have no profile the first N
    turns). Errors propagate to the caller, which is responsible for not
    letting a profile-load failure break the turn.
  * `upsert_user_profile` — merge-write, called from the background
    memory-extractor (`app/agent/memory_extract.py`). Uses a single atomic
    `INSERT ... ON CONFLICT (user_id) DO UPDATE` with Postgres's JSONB `||`
    concat operator for the `facts` merge, instead of a read-modify-write
    round trip — this matters because two turns for the same Telegram user
    (different sessions) can run their background extractors concurrently,
    and a read-then-write merge would race and lose one side's facts.
"""
from __future__ import annotations

import logging as _logging
from typing import Any, Optional

from sqlalchemy import func as _sa_func
from sqlalchemy import select as sql_select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db.models import User, UserProfile
from app.db.session import get_session

_logger = _logging.getLogger(__name__)


async def _resolve_internal_user_id(session, telegram_user_id: int) -> Optional[int]:
    return (
        await session.execute(
            sql_select(User.id).where(User.telegram_user_id == telegram_user_id)
        )
    ).scalar_one_or_none()


async def _load_from_table(user_id: int) -> Optional[dict]:
    """Load the profile for the Telegram user *user_id*.

    Returns `None` when the user has no `users` row yet, or has a `users` row
    but no `user_profiles` row yet (nothing learned about them so far).
    Otherwise returns `{"facts": dict, "notes": str, "updated_at": str | None}`
    — `facts`/`notes` may themselves be empty; callers that only want to
    render non-empty profiles (e.g. `node_faq`'s `<client_profile>` block)
    check that explicitly rather than relying on `None` for "empty".

    Raises on DB errors — does NOT swallow them. `Agent._ainvoke_locked` is
    responsible for catching and logging (a profile-load failure must not
    break the turn), matching the same division of responsibility used by
    `Agent._aload_existing_state` for checkpoint reads.
    """
    async with get_session() as session:
        internal_id = await _resolve_internal_user_id(session, user_id)
        if internal_id is None:
            return None
        row = (
            await session.execute(
                sql_select(UserProfile).where(UserProfile.user_id == internal_id)
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        return {
            "facts": dict(row.facts or {}),
            "notes": row.notes or "",
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }


async def _upsert_to_table(
    user_id: int,
    facts_delta: Optional[dict[str, Any]] = None,
    notes: Optional[str] = None,
) -> None:
    """Merge *facts_delta* into the stored facts and optionally replace *notes*.

    - `facts_delta`: only the keys present here are written — existing keys
      not mentioned are left untouched (shallow merge, one level of dict
      keys; a re-sent key fully replaces its old value, it does not deep-merge
      nested lists/dicts).
    - `notes`: `None` means "leave the stored notes as-is" (the extractor had
      nothing new to say about the relationship this turn); any string
      (including `""`) REPLACES the stored notes wholesale — it is a standing
      summary, not an appended log.
    - Calling with `facts_delta={}` (or `None`) AND `notes=None` is a no-op —
      no row is read, created, or written. This is the expected outcome when
      the extractor LLM found nothing new (see memory_extract.py).
    - Silently no-ops (with a warning log) if *user_id* has no `users` row —
      this runs from a fire-and-forget background task, so raising here would
      only be caught by the task's done-callback logger anyway; a clear
      warning is more useful than a bare traceback.
    """
    facts_delta = facts_delta or {}
    if not facts_delta and notes is None:
        return

    async with get_session() as session:
        internal_id = await _resolve_internal_user_id(session, user_id)
        if internal_id is None:
            _logger.warning(
                "upsert_user_profile: no users row for telegram_user_id=%s, skipping", user_id,
            )
            return

        insert_stmt = pg_insert(UserProfile).values(
            user_id=internal_id,
            facts=facts_delta,
            notes=notes or "",
        )
        set_: dict[str, Any] = {
            # Postgres JSONB concat: right-hand keys win on collision, keys
            # absent from facts_delta are left untouched — exactly the merge
            # semantics documented above, done atomically in one statement.
            "facts": UserProfile.facts.op("||")(insert_stmt.excluded.facts),
            "updated_at": _sa_func.now(),
        }
        if notes is not None:
            set_["notes"] = insert_stmt.excluded.notes
        # else: omit "notes" from SET entirely — ON CONFLICT DO UPDATE leaves
        # unmentioned columns unchanged, which is exactly "leave as-is".

        stmt = insert_stmt.on_conflict_do_update(
            index_elements=[UserProfile.user_id], set_=set_
        )
        await session.execute(stmt)
        await session.commit()


# ---------------------------------------------------------------------------
# Store-backed implementation (roadmap П.4)
#
# Same two operations against LangGraph's `BaseStore` instead of the
# `user_profiles` table. Selected by `PROFILE_BACKEND=store`; see
# `app/agent/store.py` for the namespace layout and why the switch exists.
#
# The merge semantics are reproduced deliberately: `facts_delta` updates only
# the keys it names, and `notes=None` leaves stored notes untouched. The
# difference is that a store has no atomic JSONB `||`, so this is a
# read-modify-write. Two background extractors racing on the same customer can
# therefore lose one side's facts — acceptable because the extractor re-derives
# facts from every turn anyway, and the alternative (a lock) would cost more
# than the rare loss.
# ---------------------------------------------------------------------------

def _get_store() -> Any:
    """The store bound to the running graph, or None outside a graph run."""
    try:
        from langgraph.config import get_store

        return get_store()
    except Exception:
        return None


async def _load_from_store(user_id: int, store: Any = None) -> Optional[dict]:
    from app.agent.store import PROFILE_KEY, profile_namespace

    store = store or _get_store()
    if store is None:
        _logger.debug("PROFILE_BACKEND=store but no store is bound; treating as no profile")
        return None

    item = await store.aget(profile_namespace(user_id), PROFILE_KEY)
    if item is None:
        return None
    value = dict(getattr(item, "value", None) or {})
    updated_at = getattr(item, "updated_at", None)
    return {
        "facts": dict(value.get("facts") or {}),
        "notes": str(value.get("notes") or ""),
        "updated_at": updated_at.isoformat() if updated_at else None,
    }


async def _upsert_to_store(
    user_id: int,
    facts_delta: Optional[dict] = None,
    notes: Optional[str] = None,
    store: Any = None,
) -> None:
    from app.agent.store import PROFILE_KEY, profile_namespace

    store = store or _get_store()
    if store is None:
        _logger.debug("PROFILE_BACKEND=store but no store is bound; profile write skipped")
        return

    namespace = profile_namespace(user_id)
    existing = await store.aget(namespace, PROFILE_KEY)
    current = dict(getattr(existing, "value", None) or {})

    facts = dict(current.get("facts") or {})
    facts.update(facts_delta or {})
    value = {"facts": facts, "notes": current.get("notes") or ""}
    if notes is not None:
        value["notes"] = notes

    await store.aput(namespace, PROFILE_KEY, value)


# ---------------------------------------------------------------------------
# Public API — dispatches on PROFILE_BACKEND
# ---------------------------------------------------------------------------

async def load_user_profile(user_id: int, store: Any = None) -> Optional[dict]:
    """Load the profile for the Telegram user *user_id*.

    Returns `None` when nothing is known about them yet. Errors propagate:
    the caller (`node_prepare_turn`) is responsible for not letting a profile
    read break the turn.
    """
    from app.agent.store import store_backed_profiles

    if store_backed_profiles():
        return await _load_from_store(user_id, store)
    return await _load_from_table(user_id)


async def upsert_user_profile(
    user_id: int,
    facts_delta: Optional[dict] = None,
    notes: Optional[str] = None,
    store: Any = None,
) -> None:
    """Merge `facts_delta` into the stored profile and optionally replace notes.

    `facts_delta={}` with `notes=None` is a no-op and costs no I/O — the
    background extractor produces exactly that on most turns.
    """
    if not facts_delta and notes is None:
        return

    from app.agent.store import store_backed_profiles

    if store_backed_profiles():
        await _upsert_to_store(user_id, facts_delta, notes, store)
        return
    await _upsert_to_table(user_id, facts_delta, notes)
