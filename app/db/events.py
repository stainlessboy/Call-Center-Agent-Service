"""SQLAlchemy event listeners — keep the Weaviate FAQ index in step with Postgres.

Wired up at import time. Imported once from the FastAPI lifespan so the
listeners are registered before any FaqItem flush occurs.

Postgres is the source of truth; Weaviate holds a searchable mirror. Because
the two stores cannot commit atomically, writes are **buffered during the
flush and only pushed after the Postgres transaction commits** — a rolled-back
admin edit must not leave a phantom entry in the index. The previous version
of this module recomputed embedding columns inline in ``before_insert``, which
had no such concern: the vectors lived in the same row and the same
transaction.

Index writes are best-effort. If Weaviate is unreachable the FAQ row is still
written to Postgres and only a warning is logged — the "Переиндексировать FAQ"
button in ``/admin/seed`` rebuilds the index from Postgres and is the repair
path for any drift.
"""
from __future__ import annotations

import logging

from sqlalchemy import event
from sqlalchemy.orm import Session

from app.db.models import FaqItem
from app.utils import vector_store

logger = logging.getLogger(__name__)

_LANGS = ("ru", "en", "uz")
# Key under which pending index writes are stashed on the flushing Session.
_PENDING = "_faq_index_pending"


def _languages_of(target: FaqItem) -> dict[str, tuple[str | None, str | None]]:
    """Question/answer pair per language, as vector_store.upsert expects."""
    return {
        lang: (
            getattr(target, f"question_{lang}", None),
            getattr(target, f"answer_{lang}", None),
        )
        for lang in _LANGS
    }


def _queue(session: Session, action: str, faq_id: int, payload=None) -> None:
    pending = session.info.setdefault(_PENDING, [])
    pending.append((action, faq_id, payload))


def _on_insert_or_update(_mapper, _connection, target: FaqItem) -> None:
    session = Session.object_session(target)
    if session is None or target.id is None:
        # No id yet means an INSERT whose PK is server-generated; that case is
        # picked up by the after_flush pass below instead.
        return
    _queue(session, "upsert", int(target.id), _languages_of(target))


def _on_delete(_mapper, _connection, target: FaqItem) -> None:
    session = Session.object_session(target)
    if session is None or target.id is None:
        return
    _queue(session, "delete", int(target.id), None)


def _after_flush_postexec(session: Session, _ctx) -> None:
    """Catch INSERTs whose primary key was only assigned during the flush."""
    for obj in session.new | session.dirty:
        if isinstance(obj, FaqItem) and obj.id is not None:
            already = any(
                faq_id == int(obj.id) for _a, faq_id, _p in session.info.get(_PENDING, [])
            )
            if not already:
                _queue(session, "upsert", int(obj.id), _languages_of(obj))


def _after_commit(session: Session) -> None:
    """Push buffered index writes now that Postgres has committed."""
    pending = session.info.pop(_PENDING, None)
    if not pending:
        return
    for action, faq_id, payload in pending:
        try:
            if action == "upsert":
                vector_store.sync_upsert(faq_id, payload)
            else:
                vector_store.sync_delete(faq_id)
        except Exception:
            logger.warning(
                "faq index %s failed for id=%s — run a reindex to repair",
                action, faq_id, exc_info=True,
            )
    _invalidate()


def _after_rollback(session: Session, *_args) -> None:
    """Drop buffered writes — the rows they described were never committed.

    ``*_args`` absorbs the extra ``previous_transaction`` argument that
    ``after_soft_rollback`` passes but ``after_rollback`` does not.
    """
    session.info.pop(_PENDING, None)


def _invalidate() -> None:
    # Lazy import to avoid a circular import at module load time.
    try:
        from app.utils import faq_tools

        faq_tools.invalidate_cache()
    except Exception:
        logger.debug("faq cache invalidation skipped", exc_info=True)


def register_faq_index_events() -> None:
    """Idempotent: safe to call multiple times (subsequent calls are no-ops)."""
    if getattr(register_faq_index_events, "_registered", False):
        return
    event.listen(FaqItem, "after_insert", _on_insert_or_update)
    event.listen(FaqItem, "after_update", _on_insert_or_update)
    event.listen(FaqItem, "after_delete", _on_delete)
    event.listen(Session, "after_flush_postexec", _after_flush_postexec)
    event.listen(Session, "after_commit", _after_commit)
    event.listen(Session, "after_rollback", _after_rollback)
    event.listen(Session, "after_soft_rollback", _after_rollback)
    register_faq_index_events._registered = True  # type: ignore[attr-defined]
