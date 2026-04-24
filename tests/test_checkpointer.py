"""Tests for app.agent.checkpointer.

Three safety properties we must guarantee for the k8s / prod deploy:

1. ``backend="postgres"`` must NOT silently fall back to MemorySaver when
   the database is unreachable — it must raise. A silent fallback leaves
   a pod running without session persistence.

2. ``REQUIRE_PERSISTENT_CHECKPOINTER=true`` must cause any path that
   lands on MemorySaver to raise, regardless of which backend was
   requested. This is the belt-and-suspenders guarantee for k8s.

3. ``backend="auto"`` without ``REQUIRE_PERSISTENT_CHECKPOINTER`` must
   keep degrading to MemorySaver so local dev without Postgres still
   works.
"""
from __future__ import annotations

import pytest

from app.agent import checkpointer as checkpointer_module
from app.agent.checkpointer import _create_async_checkpointer


class _BoomCM:
    async def __aenter__(self):
        raise ConnectionError("boom — DB is down")

    async def __aexit__(self, *args):
        return False


def _patch_failing_postgres(monkeypatch: pytest.MonkeyPatch) -> None:
    """Monkey-patch the AsyncPostgresSaver import inside checkpointer.py
    so that every attempt to open a Postgres connection fails.

    The module imports AsyncPostgresSaver lazily inside the function,
    so we patch the import machinery via sys.modules.
    """
    import sys
    import types

    fake_mod = types.ModuleType("langgraph.checkpoint.postgres.aio")

    class _FakeAsyncPostgresSaver:
        @staticmethod
        def from_conn_string(url: str):
            return _BoomCM()

    fake_mod.AsyncPostgresSaver = _FakeAsyncPostgresSaver
    monkeypatch.setitem(sys.modules, "langgraph.checkpoint.postgres.aio", fake_mod)


class TestExplicitPostgresFailsFast:
    @pytest.mark.asyncio
    async def test_explicit_postgres_raises_on_db_outage(self, monkeypatch):
        _patch_failing_postgres(monkeypatch)
        monkeypatch.delenv("REQUIRE_PERSISTENT_CHECKPOINTER", raising=False)
        with pytest.raises(ConnectionError):
            await _create_async_checkpointer("postgres", "postgresql://x/y")

    @pytest.mark.asyncio
    async def test_explicit_postgres_without_url_raises(self, monkeypatch):
        # No URL provided and DATABASE_URL derivation returns None
        monkeypatch.setattr(
            checkpointer_module, "_derive_postgres_url", lambda _: None
        )
        monkeypatch.delenv("REQUIRE_PERSISTENT_CHECKPOINTER", raising=False)
        with pytest.raises(RuntimeError, match="no DATABASE_URL"):
            await _create_async_checkpointer("postgres", None)


class TestRequirePersistentGuardsAuto:
    @pytest.mark.asyncio
    async def test_auto_with_require_env_raises_when_pg_fails(self, monkeypatch):
        _patch_failing_postgres(monkeypatch)
        monkeypatch.setenv("REQUIRE_PERSISTENT_CHECKPOINTER", "true")
        with pytest.raises(RuntimeError, match="Refusing to start"):
            await _create_async_checkpointer("auto", "postgresql://x/y")

    @pytest.mark.asyncio
    async def test_auto_with_require_env_raises_when_no_url(self, monkeypatch):
        monkeypatch.setattr(
            checkpointer_module, "_derive_postgres_url", lambda _: None
        )
        monkeypatch.setenv("REQUIRE_PERSISTENT_CHECKPOINTER", "true")
        with pytest.raises(RuntimeError, match="Refusing to start"):
            await _create_async_checkpointer("auto", None)

    @pytest.mark.asyncio
    async def test_memory_backend_with_require_env_raises(self, monkeypatch):
        monkeypatch.setenv("REQUIRE_PERSISTENT_CHECKPOINTER", "true")
        with pytest.raises(RuntimeError, match="Refusing to start"):
            await _create_async_checkpointer("memory", None)


class TestAutoDegradesInDev:
    @pytest.mark.asyncio
    async def test_auto_without_require_env_returns_memorysaver_on_pg_failure(
        self, monkeypatch
    ):
        from langgraph.checkpoint.memory import MemorySaver

        _patch_failing_postgres(monkeypatch)
        monkeypatch.delenv("REQUIRE_PERSISTENT_CHECKPOINTER", raising=False)
        saver, cm = await _create_async_checkpointer("auto", "postgresql://x/y")
        assert isinstance(saver, MemorySaver)
        assert cm is None
