"""Session history — screens 26 / 26a of the design handoff.

A session lives ``SESSION_INACTIVITY_TIMEOUT_MINUTES`` (24 h by default) from
the last message; the inactivity watcher then closes it with
``closed_reason="timeout"``. Those are the "expired" ones: readable, not
writable. Sessions the user closed themselves stay openable as plain history.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException

from app.config import get_settings
from app.db.models import ChatSession, SessionStatus, User
from app.miniapp.deps import get_chat_service, get_db_user
from app.services.chat_service import ChatService

router = APIRouter()

# How long a closed conversation stays readable (design note on screen 26).
ARCHIVE_DAYS = 30

STATE_ACTIVE = "active"
STATE_ENDED = "ended"
STATE_EXPIRED = "expired"


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def session_state(chat_session: ChatSession) -> str:
    if chat_session.status == SessionStatus.ACTIVE:
        return STATE_ACTIVE
    # "timeout" is what the inactivity watcher writes; anything else means the
    # user or the operator closed the chat deliberately.
    return STATE_EXPIRED if chat_session.closed_reason == "timeout" else STATE_ENDED


def _expires_at(chat_session: ChatSession) -> datetime | None:
    minutes = get_settings().session_inactivity_timeout_minutes
    last = _as_utc(chat_session.last_activity_at)
    if minutes <= 0 or last is None:
        return None
    return last + timedelta(minutes=minutes)


def serialize_session(chat_session: ChatSession, message_count: int, is_current: bool) -> dict:
    state = session_state(chat_session)
    ended_at = _as_utc(chat_session.ended_at)
    expires_at = _expires_at(chat_session) if state == STATE_ACTIVE else None
    readable_until = ended_at + timedelta(days=ARCHIVE_DAYS) if ended_at else None

    return {
        "id": chat_session.id,
        "title": chat_session.title or "",
        "state": state,
        "is_current": is_current,
        "human_mode": bool(chat_session.human_mode),
        "message_count": message_count,
        "started_at": _as_utc(chat_session.started_at).isoformat() if chat_session.started_at else None,
        "ended_at": ended_at.isoformat() if ended_at else None,
        "expires_at": expires_at.isoformat() if expires_at else None,
        "readable_until": readable_until.isoformat() if readable_until else None,
        "closed_reason": chat_session.closed_reason or "",
        "feedback_rating": chat_session.feedback_rating,
    }


@router.get("/sessions")
async def list_sessions(
    limit: int = 20,
    user: User = Depends(get_db_user),
    chat_service: ChatService = Depends(get_chat_service),
) -> dict:
    active = await chat_service.get_active_session(user.id)
    rows = await chat_service.list_sessions_with_counts(user.id, limit=max(1, min(limit, 100)))
    items = [
        serialize_session(chat_session, count, is_current=bool(active and active.id == chat_session.id))
        for chat_session, count in rows
    ]
    return {
        "items": items,
        "archive_days": ARCHIVE_DAYS,
        "ttl_minutes": get_settings().session_inactivity_timeout_minutes,
        "counts": {
            "active": sum(1 for i in items if i["state"] == STATE_ACTIVE),
            "ended": sum(1 for i in items if i["state"] == STATE_ENDED),
            "expired": sum(1 for i in items if i["state"] == STATE_EXPIRED),
        },
    }


@router.get("/sessions/{session_id}")
async def session_detail(
    session_id: str,
    limit: int = 200,
    user: User = Depends(get_db_user),
    chat_service: ChatService = Depends(get_chat_service),
) -> dict:
    chat_session = await chat_service.get_user_session(user.id, session_id)
    if chat_session is None:
        raise HTTPException(status_code=404, detail="Session not found")

    active = await chat_service.get_active_session(user.id)
    messages = await chat_service.get_recent_messages(
        session_id, limit=max(1, min(limit, 500)), roles=("user", "agent", "operator")
    )
    payload = serialize_session(
        chat_session,
        message_count=len(messages),
        is_current=bool(active and active.id == chat_session.id),
    )
    payload["writable"] = payload["state"] == STATE_ACTIVE
    payload["messages"] = [
        {
            "id": m.id,
            "role": m.role,
            "text": m.text,
            "created_at": _as_utc(m.created_at).isoformat() if m.created_at else None,
            # Structured cards for this turn, or [] — see docs/MINIAPP.md
            # "UI blocks". Only ever populated on role="agent" rows. Matches
            # /chat/history's shape (Phase 4 fix — this endpoint was missing it).
            "ui_blocks": m.ui_blocks or [],
        }
        for m in messages
    ]
    return payload
