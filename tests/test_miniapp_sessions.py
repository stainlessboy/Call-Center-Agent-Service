"""Session lifecycle as the Mini App history screens see it."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.db.models import ChatSession, SessionStatus
from app.miniapp.routes.sessions import (
    ARCHIVE_DAYS,
    STATE_ACTIVE,
    STATE_ENDED,
    STATE_EXPIRED,
    serialize_session,
    session_state,
)


def _session(**overrides) -> ChatSession:
    now = datetime.now(timezone.utc)
    defaults = dict(
        id="s-1",
        user_id=1,
        title="Какие есть автокредиты?",
        status=SessionStatus.ACTIVE,
        started_at=now - timedelta(hours=2),
        last_activity_at=now - timedelta(minutes=30),
        ended_at=None,
        closed_reason=None,
        human_mode=False,
        feedback_rating=None,
    )
    defaults.update(overrides)
    return ChatSession(**defaults)


def test_active_session_is_active():
    assert session_state(_session()) == STATE_ACTIVE


def test_watcher_timeout_marks_the_session_expired():
    closed = _session(
        status=SessionStatus.ENDED,
        ended_at=datetime.now(timezone.utc),
        closed_reason="timeout",
    )
    assert session_state(closed) == STATE_EXPIRED


@pytest.mark.parametrize("reason", ["manual_end", "miniapp_end", "operator_left", ""])
def test_deliberate_close_is_not_an_expiry(reason):
    """Only the inactivity watcher produces the read-only 'expired' state."""
    closed = _session(
        status=SessionStatus.ENDED,
        ended_at=datetime.now(timezone.utc),
        closed_reason=reason,
    )
    assert session_state(closed) == STATE_ENDED


def test_active_session_reports_when_it_expires():
    payload = serialize_session(_session(), message_count=4, is_current=True)
    assert payload["state"] == STATE_ACTIVE
    assert payload["expires_at"] is not None
    assert payload["is_current"] is True
    assert payload["message_count"] == 4


def test_closed_session_reports_how_long_it_stays_readable():
    ended = datetime.now(timezone.utc)
    payload = serialize_session(
        _session(status=SessionStatus.ENDED, ended_at=ended, closed_reason="timeout"),
        message_count=0,
        is_current=False,
    )
    assert payload["expires_at"] is None
    readable_until = datetime.fromisoformat(payload["readable_until"])
    assert (readable_until - ended).days == ARCHIVE_DAYS


def test_naive_timestamps_are_treated_as_utc():
    """SQLite/Postgres can hand back naive datetimes; isoformat must not lie."""
    naive = datetime.now(timezone.utc).replace(tzinfo=None)
    payload = serialize_session(
        _session(started_at=naive, last_activity_at=naive), message_count=1, is_current=False
    )
    assert payload["started_at"].endswith("+00:00")
