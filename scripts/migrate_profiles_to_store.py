#!/usr/bin/env python3
"""Copy `user_profiles` rows into the LangGraph store (roadmap П.4).

Run this ONCE before flipping `PROFILE_BACKEND=table` to `store`. Until the
flip the table stays authoritative, so the migration is safe to run early, and
safe to re-run: every write is an idempotent `aput` of the row's current
contents.

    # dry run — shows what would be written, touches nothing
    python3 scripts/migrate_profiles_to_store.py

    # write
    python3 scripts/migrate_profiles_to_store.py --apply

    # then, in .env
    PROFILE_BACKEND=store

Nothing is deleted. The table is left intact so the flip can be reverted by
changing the env var back; drop it in a later migration once the store has
been authoritative long enough to trust.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from app.agent.store import PROFILE_KEY, create_store, profile_namespace  # noqa: E402
from app.db.models import User, UserProfile  # noqa: E402
from app.db.session import get_session  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
_logger = logging.getLogger("migrate_profiles")


async def _read_rows() -> list[tuple[int, dict, str]]:
    """Return `(telegram_user_id, facts, notes)` for every stored profile.

    The store is keyed by the TELEGRAM id, because that is the only id the
    agent layer ever has (see the module docstring in app/agent/profile.py),
    so the join back to `users` happens here rather than at read time.
    """
    async with get_session() as session:
        rows = (
            await session.execute(
                select(User.telegram_user_id, UserProfile.facts, UserProfile.notes)
                .join(UserProfile, UserProfile.user_id == User.id)
            )
        ).all()
    return [(int(tg_id), dict(facts or {}), str(notes or "")) for tg_id, facts, notes in rows]


async def main(apply: bool) -> int:
    rows = await _read_rows()
    if not rows:
        _logger.info("Профилей в таблице нет — мигрировать нечего.")
        return 0

    _logger.info("Найдено профилей: %d", len(rows))
    if not apply:
        for tg_id, facts, notes in rows[:10]:
            _logger.info(
                "  [сухой прогон] user=%s facts=%s notes=%s",
                tg_id, sorted(facts.keys()), (notes[:60] + "…") if len(notes) > 60 else notes,
            )
        if len(rows) > 10:
            _logger.info("  … и ещё %d", len(rows) - 10)
        _logger.info("Запустите с --apply, чтобы записать в store.")
        return 0

    store, cm = await create_store()
    written = 0
    try:
        for tg_id, facts, notes in rows:
            await store.aput(
                profile_namespace(tg_id), PROFILE_KEY, {"facts": facts, "notes": notes},
            )
            written += 1
    finally:
        if cm is not None:
            await cm.__aexit__(None, None, None)

    _logger.info("Перенесено профилей: %d", written)
    _logger.info("Теперь можно выставить PROFILE_BACKEND=store. Таблица не тронута.")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="действительно писать в store")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.apply)))
