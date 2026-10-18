"""Store-backed long-term memory (roadmap П.4).

`PROFILE_BACKEND` decides who owns the "personal consultant" profile: the
existing `user_profiles` table or LangGraph's `BaseStore`. Both must behave
identically from the agent's point of view, because the switch is meant to be
one env var — these tests pin that equivalence on the store side (the table
side is covered by tests/test_profile.py).
"""
from __future__ import annotations

import pytest
from langgraph.store.memory import InMemoryStore

from app.agent import profile as profile_module
from app.agent.store import PROFILE_KEY, memories_namespace, profile_namespace


@pytest.fixture
def store():
    return InMemoryStore()


@pytest.fixture(autouse=True)
def _store_backend(monkeypatch):
    monkeypatch.setenv("PROFILE_BACKEND", "store")


class TestNamespaces:
    def test_profile_and_memories_are_separate(self):
        assert profile_namespace(42) == ("users", "42")
        assert memories_namespace(42) == ("users", "42", "memories")

    def test_namespace_is_per_user(self):
        assert profile_namespace(1) != profile_namespace(2)


class TestRoundTrip:
    @pytest.mark.asyncio
    async def test_unknown_user_has_no_profile(self, store):
        assert await profile_module.load_user_profile(42, store=store) is None

    @pytest.mark.asyncio
    async def test_write_then_read(self, store):
        await profile_module.upsert_user_profile(
            42, {"income_monthly": 5_000_000}, "Постоянный клиент.", store=store,
        )
        loaded = await profile_module.load_user_profile(42, store=store)

        assert loaded["facts"] == {"income_monthly": 5_000_000}
        assert loaded["notes"] == "Постоянный клиент."


class TestMergeSemantics:
    """Same contract as the table backend's JSONB `||` merge."""

    @pytest.mark.asyncio
    async def test_delta_touches_only_named_keys(self, store):
        await profile_module.upsert_user_profile(
            42, {"income_monthly": 5_000_000, "age": 30}, store=store,
        )
        await profile_module.upsert_user_profile(42, {"age": 31}, store=store)

        facts = (await profile_module.load_user_profile(42, store=store))["facts"]
        assert facts == {"income_monthly": 5_000_000, "age": 31}

    @pytest.mark.asyncio
    async def test_notes_none_leaves_stored_notes_alone(self, store):
        await profile_module.upsert_user_profile(42, {}, "Первая заметка.", store=store)
        await profile_module.upsert_user_profile(42, {"age": 30}, None, store=store)

        assert (await profile_module.load_user_profile(42, store=store))["notes"] == "Первая заметка."

    @pytest.mark.asyncio
    async def test_notes_are_replaced_wholesale_not_appended(self, store):
        await profile_module.upsert_user_profile(42, {}, "Первая.", store=store)
        await profile_module.upsert_user_profile(42, {}, "Вторая.", store=store)

        assert (await profile_module.load_user_profile(42, store=store))["notes"] == "Вторая."

    @pytest.mark.asyncio
    async def test_empty_delta_and_no_notes_is_a_no_op(self, store):
        """The background extractor produces exactly this on most turns — it
        must cost no I/O and must not create an empty profile."""
        await profile_module.upsert_user_profile(42, {}, None, store=store)

        assert await profile_module.load_user_profile(42, store=store) is None


class TestBackendSelection:
    @pytest.mark.asyncio
    async def test_table_backend_ignores_the_store(self, monkeypatch, store):
        """With the default backend, a passed store must not be consulted —
        otherwise a half-migrated deployment would read from both."""
        monkeypatch.setenv("PROFILE_BACKEND", "table")
        called = {}

        async def fake_table_load(user_id):
            called["yes"] = user_id
            return {"facts": {"age": 1}, "notes": "", "updated_at": None}

        monkeypatch.setattr(profile_module, "_load_from_table", fake_table_load)

        loaded = await profile_module.load_user_profile(42, store=store)

        assert called == {"yes": 42}
        assert loaded["facts"] == {"age": 1}

    @pytest.mark.asyncio
    async def test_missing_store_degrades_to_no_profile(self, monkeypatch):
        """Outside a graph run there is no bound store. That must read as
        "nothing known yet", never as an exception in the middle of a turn."""
        monkeypatch.setattr(profile_module, "_get_store", lambda: None)

        assert await profile_module.load_user_profile(42) is None
        # And a write is a silent no-op rather than a crash.
        await profile_module.upsert_user_profile(42, {"age": 30})
