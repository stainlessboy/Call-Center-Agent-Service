"""Reliability hardening (roadmap П.10).

Two independent failure modes, both of which cost a real customer a real turn:

  * a transient blip (dropped DB connection, one provider 5xx) killing a turn
    that a single retry would have completed;
  * Telegram redelivering an update because a slow LLM turn did not answer in
    time, so the same message is processed twice.
"""
from __future__ import annotations

import pytest


class TestWebhookDeduplication:
    @pytest.fixture(autouse=True)
    def _clean_ring(self):
        from app.api import fastapi_app

        fastapi_app._SEEN_UPDATE_IDS.clear()
        yield
        fastapi_app._SEEN_UPDATE_IDS.clear()

    def test_first_delivery_is_accepted(self):
        from app.api.fastapi_app import _is_duplicate_update

        assert _is_duplicate_update(1001) is False

    def test_redelivery_is_rejected(self):
        """Telegram resends when it does not see a timely 200 — a slow turn is
        enough. Processing it twice means two answers and two lead rows."""
        from app.api.fastapi_app import _is_duplicate_update

        _is_duplicate_update(1001)

        assert _is_duplicate_update(1001) is True

    def test_distinct_updates_are_independent(self):
        from app.api.fastapi_app import _is_duplicate_update

        assert _is_duplicate_update(1) is False
        assert _is_duplicate_update(2) is False
        assert _is_duplicate_update(1) is True

    def test_missing_update_id_is_never_treated_as_duplicate(self):
        """A payload without an id must still be processed — dropping it would
        silently lose a customer message."""
        from app.api.fastapi_app import _is_duplicate_update

        assert _is_duplicate_update(None) is False
        assert _is_duplicate_update(None) is False

    def test_ring_stays_bounded(self):
        """The ring must not grow without limit in a long-running process."""
        from app.api import fastapi_app

        for update_id in range(fastapi_app._SEEN_UPDATE_LIMIT + 500):
            fastapi_app._is_duplicate_update(update_id)

        assert len(fastapi_app._SEEN_UPDATE_IDS) <= fastapi_app._SEEN_UPDATE_LIMIT

    def test_oldest_entries_are_evicted_first(self):
        """Eviction has to be FIFO: the newest ids are the ones a redelivery
        would repeat, so they are the ones worth remembering."""
        from app.api import fastapi_app

        for update_id in range(fastapi_app._SEEN_UPDATE_LIMIT + 10):
            fastapi_app._is_duplicate_update(update_id)

        # The very first ids fell out; the most recent ones are still known.
        assert fastapi_app._is_duplicate_update(0) is False
        assert fastapi_app._is_duplicate_update(fastapi_app._SEEN_UPDATE_LIMIT + 5) is True


class TestNodeRetryPolicy:
    def test_network_nodes_retry(self):
        """`faq`, `calc_flow` and `qualify_flow` all reach the provider or the
        database; a single retry is cheaper than a failed turn."""
        from app.agent.graph import _NETWORK_RETRY, build_graph

        assert _NETWORK_RETRY.max_attempts >= 2

        graph = build_graph()
        nodes = graph.get_graph().nodes
        assert {"faq", "calc_flow", "qualify_flow"} <= set(nodes)

    def test_deterministic_nodes_are_not_retried(self, monkeypatch):
        """The router and prepare_turn cannot fail transiently. Retrying them
        would hide a real bug rather than survive a blip."""
        import app.agent.graph as graph_module

        recorded: dict[str, object] = {}
        original = graph_module.StateGraph.add_node

        def _record(self, name, action=None, **kwargs):
            recorded[name] = kwargs.get("retry_policy")
            return original(self, name, action, **kwargs)

        monkeypatch.setattr(graph_module.StateGraph, "add_node", _record)
        graph_module.build_graph()

        assert recorded["router"] is None
        assert recorded["prepare_turn"] is None
        assert recorded["faq"] is not None
