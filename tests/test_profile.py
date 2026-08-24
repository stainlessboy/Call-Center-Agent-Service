"""Tests for app.agent.profile — "personal consultant" UserProfile load/merge.

`upsert_user_profile`'s actual `facts` merge happens INSIDE Postgres (the
JSONB `||` concat operator, not Python), so — following this repo's existing
convention of not requiring a live DB connection for unit tests (see
tests/test_checkpointer.py, which mocks AsyncPostgresSaver entirely) — these
tests verify the CONSTRUCTED SQL statement instead of executing it: that
`facts` always goes through the `||` operator (Postgres's own documented
merge semantics — not re-tested here), and that `notes` is only present in
the UPDATE SET clause when the caller passed a non-None value (the
"None = leave as-is" contract described in profile.py's docstring).
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import MagicMock

from app.agent import profile as profile_module


def _run(coro):
    return asyncio.run(coro)


class _FakeSession:
    """Minimal async session stand-in: first execute() answers the internal
    users.id lookup, subsequent execute() calls are captured for inspection."""

    def __init__(self, internal_user_id):
        self._internal_user_id = internal_user_id
        self.executed: list = []
        self.committed = False

    async def execute(self, stmt):
        self.executed.append(stmt)
        result = MagicMock()
        if len(self.executed) == 1:
            result.scalar_one_or_none.return_value = self._internal_user_id
        return result

    async def commit(self):
        self.committed = True


def _patch_get_session(monkeypatch, session):
    @asynccontextmanager
    async def _fake_get_session():
        yield session

    monkeypatch.setattr(profile_module, "get_session", _fake_get_session)


class TestUpsertUserProfileNoOp:
    def test_empty_facts_and_none_notes_is_noop(self, monkeypatch):
        """{} + None is the documented valid empty extraction outcome — no
        DB read or write at all, not even the id-resolution SELECT."""
        session = _FakeSession(internal_user_id=42)
        _patch_get_session(monkeypatch, session)
        _run(profile_module.upsert_user_profile(111, facts_delta={}, notes=None))
        assert session.executed == []
        assert not session.committed

    def test_unknown_telegram_user_skips_write(self, monkeypatch):
        """No `users` row for this telegram_user_id — logs a warning (not
        asserted here) and skips the upsert instead of violating the FK."""
        session = _FakeSession(internal_user_id=None)
        _patch_get_session(monkeypatch, session)
        _run(profile_module.upsert_user_profile(111, facts_delta={"age": 30}, notes=None))
        assert len(session.executed) == 1  # only the id lookup ran
        assert not session.committed


class TestUpsertUserProfileMergeSemantics:
    def test_facts_delta_alone_uses_jsonb_concat_and_commits(self, monkeypatch):
        session = _FakeSession(internal_user_id=42)
        _patch_get_session(monkeypatch, session)
        _run(profile_module.upsert_user_profile(111, facts_delta={"age": 30}, notes=None))
        assert session.committed
        sql = str(session.executed[-1])
        assert "ON CONFLICT" in sql
        assert "facts = (user_profiles.facts || excluded.facts)" in sql

    def test_notes_none_is_excluded_from_update_set(self, monkeypatch):
        """notes=None must NOT appear in the UPDATE SET clause at all — that's
        what makes it "leave the stored notes untouched" rather than
        overwriting them with an empty string."""
        session = _FakeSession(internal_user_id=42)
        _patch_get_session(monkeypatch, session)
        _run(profile_module.upsert_user_profile(111, facts_delta={"age": 30}, notes=None))
        set_clause = str(session.executed[-1]).split("DO UPDATE SET", 1)[1]
        assert "notes" not in set_clause

    def test_notes_value_replaces_wholesale(self, monkeypatch):
        session = _FakeSession(internal_user_id=42)
        _patch_get_session(monkeypatch, session)
        _run(profile_module.upsert_user_profile(111, facts_delta={}, notes="client recap"))
        set_clause = str(session.executed[-1]).split("DO UPDATE SET", 1)[1]
        assert "notes = excluded.notes" in set_clause

    def test_empty_string_notes_is_a_real_value_not_a_skip(self, monkeypatch):
        """notes="" is a deliberate wholesale-replace (per profile.py's
        docstring: "any string (including ``\"\"``) REPLACES the stored
        notes"), distinct from notes=None."""
        session = _FakeSession(internal_user_id=42)
        _patch_get_session(monkeypatch, session)
        _run(profile_module.upsert_user_profile(111, facts_delta={"age": 30}, notes=""))
        assert session.committed
        set_clause = str(session.executed[-1]).split("DO UPDATE SET", 1)[1]
        assert "notes = excluded.notes" in set_clause


class TestLoadUserProfile:
    def test_no_users_row_returns_none(self, monkeypatch):
        session = _FakeSession(internal_user_id=None)
        _patch_get_session(monkeypatch, session)
        result = _run(profile_module.load_user_profile(111))
        assert result is None

    def test_no_profile_row_returns_none(self, monkeypatch):
        class _Session(_FakeSession):
            async def execute(self, stmt):
                self.executed.append(stmt)
                result = MagicMock()
                if len(self.executed) == 1:
                    result.scalar_one_or_none.return_value = 42  # users.id
                else:
                    result.scalar_one_or_none.return_value = None  # no UserProfile row
                return result

        session = _Session(internal_user_id=42)
        _patch_get_session(monkeypatch, session)
        result = _run(profile_module.load_user_profile(111))
        assert result is None

    def test_existing_profile_returns_facts_and_notes(self, monkeypatch):
        fake_row = MagicMock()
        fake_row.facts = {"age": 30}
        fake_row.notes = "recap"
        fake_row.updated_at = None

        class _Session(_FakeSession):
            async def execute(self, stmt):
                self.executed.append(stmt)
                result = MagicMock()
                if len(self.executed) == 1:
                    result.scalar_one_or_none.return_value = 42
                else:
                    result.scalar_one_or_none.return_value = fake_row
                return result

        session = _Session(internal_user_id=42)
        _patch_get_session(monkeypatch, session)
        result = _run(profile_module.load_user_profile(111))
        assert result == {"facts": {"age": 30}, "notes": "recap", "updated_at": None}
