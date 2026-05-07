"""SQLAlchemy event listeners — auto-recompute FAQ embeddings on write.

Wired up at import time. Imported once from ``app.db.session`` (or the FastAPI
lifespan) so the listeners are registered before any FaqItem flush occurs.

Each listener walks the three ``question_<lang>`` fields. For an INSERT we
embed every non-empty question. For an UPDATE we re-embed only fields whose
text actually changed (uses ORM attribute history) — that way editing only
the *answer* costs zero OpenAI calls.

Failures from the embedding service are non-fatal: ``embed_one_sync`` returns
``None``, the column stays NULL, and the row still gets written. The backfill
button in ``/admin/seed`` can fill in the gaps later.
"""
from __future__ import annotations

import logging

from sqlalchemy import event, inspect

from app.db.models import FaqItem
from app.utils.embeddings import embed_one_sync

logger = logging.getLogger(__name__)

_LANGS = ("ru", "en", "uz")


def _question_changed(target: FaqItem, lang: str) -> bool:
    """True if ``question_<lang>`` was modified on this flush."""
    state = inspect(target)
    history = state.attrs[f"question_{lang}"].history
    return history.has_changes()


def _recompute_for_insert(_mapper, _connection, target: FaqItem) -> None:
    for lang in _LANGS:
        question = getattr(target, f"question_{lang}", None)
        if not question:
            setattr(target, f"embedding_{lang}", None)
            continue
        # Honour an explicitly-set embedding (bulk seed pre-fills these to
        # avoid 1-by-1 OpenAI calls). Only auto-embed when missing.
        if getattr(target, f"embedding_{lang}", None) is None:
            setattr(target, f"embedding_{lang}", embed_one_sync(question))


def _recompute_for_update(_mapper, _connection, target: FaqItem) -> None:
    for lang in _LANGS:
        question = getattr(target, f"question_{lang}", None)
        if not question:
            # Question was cleared — drop stale embedding too.
            setattr(target, f"embedding_{lang}", None)
            continue
        if not _question_changed(target, lang):
            continue
        setattr(target, f"embedding_{lang}", embed_one_sync(question))


def _invalidate_after_write(_mapper, _connection, _target) -> None:
    # In-memory cache for FAQ vectors lives in app.utils.faq_tools. We import
    # lazily to avoid a circular import at module load time.
    try:
        from app.utils import faq_tools
        faq_tools.invalidate_cache()
    except Exception:
        logger.debug("faq cache invalidation skipped", exc_info=True)


def register_faq_embedding_events() -> None:
    """Idempotent: safe to call multiple times (subsequent calls are no-ops)."""
    if getattr(register_faq_embedding_events, "_registered", False):
        return
    event.listen(FaqItem, "before_insert", _recompute_for_insert)
    event.listen(FaqItem, "before_update", _recompute_for_update)
    event.listen(FaqItem, "after_insert", _invalidate_after_write)
    event.listen(FaqItem, "after_update", _invalidate_after_write)
    event.listen(FaqItem, "after_delete", _invalidate_after_write)
    register_faq_embedding_events._registered = True  # type: ignore[attr-defined]
