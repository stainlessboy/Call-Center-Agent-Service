"""LangSmith tracing plumbing (roadmap П.7а — app/agent/tracing.py).

Two things matter here and nothing else:
  1. `build_run_config` always produces a usable checkpointer config, with
     observability metadata/tags that never change turn behavior.
  2. Nothing leaves the process unmasked. The graph state carries raw PII on
     purpose (lead capture parses the real phone out of `last_user_text`),
     so the masking hook is the only thing standing between that state and
     LangSmith.
"""
from __future__ import annotations

import pytest

from app.agent import tracing


@pytest.fixture(autouse=True)
def _clear_tracer_cache():
    tracing.get_tracer.cache_clear()
    yield
    tracing.get_tracer.cache_clear()


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in (
        "AGENT_TRACING_ENABLED", "LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2",
        "LANGSMITH_API_KEY", "LANGSMITH_PROJECT", "LANGSMITH_ENDPOINT",
    ):
        monkeypatch.delenv(var, raising=False)


# ---------------------------------------------------------------------------
# build_run_config
# ---------------------------------------------------------------------------

class TestBuildRunConfig:
    def test_thread_id_always_present(self):
        config = tracing.build_run_config("sess-1")
        assert config["configurable"]["thread_id"] == "sess-1"

    def test_metadata_and_tags(self):
        config = tracing.build_run_config(
            "sess-1", user_id=42, lang="uz", channel="miniapp", flow="calc_flow",
        )
        assert config["metadata"] == {
            "session_id": "sess-1", "user_id": 42, "lang": "uz",
            "channel": "miniapp", "flow": "calc_flow",
        }
        assert config["tags"] == ["miniapp", "uz", "calc_flow"]

    def test_absent_fields_are_omitted_not_nulled(self):
        """A None lang must not become a `lang: None` tag/metadata entry —
        LangSmith would render it as a real (useless) facet value."""
        config = tracing.build_run_config("sess-1")
        assert config["metadata"] == {"session_id": "sess-1"}
        assert config["tags"] == []

    def test_extra_tags_and_run_name(self):
        config = tracing.build_run_config(
            "sess-1", lang="ru", run_name="agent_turn", extra_tags=("human_mode",),
        )
        assert config["run_name"] == "agent_turn"
        assert "human_mode" in config["tags"]

    def test_no_callbacks_when_tracing_off(self):
        assert "callbacks" not in tracing.build_run_config("sess-1")


# ---------------------------------------------------------------------------
# get_tracer gating
# ---------------------------------------------------------------------------

class TestTracerGating:
    def test_off_by_default(self):
        assert tracing.get_tracer() is None

    def test_enabled_without_api_key_is_off(self, monkeypatch):
        monkeypatch.setenv("AGENT_TRACING_ENABLED", "true")
        assert tracing.get_tracer() is None

    def test_refuses_when_stock_langsmith_tracing_also_on(self, monkeypatch):
        """Both switches on would attach LangChain's own unmasked global
        tracer alongside ours: duplicate traces AND leaked PII."""
        monkeypatch.setenv("AGENT_TRACING_ENABLED", "true")
        monkeypatch.setenv("LANGSMITH_API_KEY", "test-key")
        monkeypatch.setenv("LANGSMITH_TRACING", "true")
        assert tracing.get_tracer() is None

    def test_builds_tracer_with_masking_client(self, monkeypatch):
        monkeypatch.setenv("AGENT_TRACING_ENABLED", "true")
        monkeypatch.setenv("LANGSMITH_API_KEY", "test-key")
        monkeypatch.setenv("LANGSMITH_PROJECT", "test-project")
        tracer = tracing.get_tracer()
        assert tracer is not None
        assert tracer.project_name == "test-project"
        # The client must carry OUR masking hooks, not the defaults.
        assert tracer.client._hide_inputs is tracing._mask_payload
        assert tracer.client._hide_outputs is tracing._mask_payload

    def test_config_carries_tracer_when_enabled(self, monkeypatch):
        monkeypatch.setenv("AGENT_TRACING_ENABLED", "true")
        monkeypatch.setenv("LANGSMITH_API_KEY", "test-key")
        config = tracing.build_run_config("sess-1")
        assert config["callbacks"] == [tracing.get_tracer()]


# ---------------------------------------------------------------------------
# PII masking of uploaded payloads
# ---------------------------------------------------------------------------

class TestMaskPayload:
    def test_masks_raw_user_text(self):
        masked = tracing._mask_payload({"last_user_text": "мой номер +998901234567"})
        assert "998901234567" not in masked["last_user_text"]

    def test_masks_nested_lead_slots(self):
        """`dialog.lead_slots` holds the captured phone by design — it must
        never be uploaded."""
        masked = tracing._mask_payload(
            {"dialog": {"lead_slots": {"phone": "+998901234567", "name": "Иван"}}}
        )
        assert "998901234567" not in masked["dialog"]["lead_slots"]["phone"]

    def test_masks_inside_lists_and_messages(self):
        from langchain_core.messages import HumanMessage

        masked = tracing._mask_payload(
            {"messages": [HumanMessage(content="карта 8600123412341234")]}
        )
        assert "8600123412341234" not in str(masked)

    def test_preserves_non_pii_scalars(self):
        masked = tracing._mask_payload(
            {"user_id": 42, "human_mode": False, "dti": 0.45, "answer": None}
        )
        assert masked == {"user_id": 42, "human_mode": False, "dti": 0.45, "answer": None}

    def test_unknown_objects_become_type_markers(self):
        """An object we can't introspect must not be stringified blind — its
        repr could contain anything."""
        class Opaque:
            def __repr__(self):
                return "Opaque(+998901234567)"

        masked = tracing._mask_payload({"thing": Opaque()})
        assert masked["thing"] == "<Opaque>"

    def test_deep_nesting_is_capped(self):
        payload: dict = {"level": "+998901234567"}
        for _ in range(20):
            payload = {"nested": payload}
        masked = tracing._mask_payload(payload)
        assert "998901234567" not in str(masked)

    def test_never_raises(self, monkeypatch):
        """A masking bug must not kill the turn it is tracing."""
        monkeypatch.setattr(tracing, "_mask_value", lambda *a, **k: (_ for _ in ()).throw(ValueError("boom")))
        assert tracing._mask_payload({"x": "y"}) == {"masked": "<masking failed>"}
