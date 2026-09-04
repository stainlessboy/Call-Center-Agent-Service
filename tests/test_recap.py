"""Tests for the recap node + router's stale-flow detection (Phase 2:
"personal consultant" memory — RECAP_GAP_MINUTES)."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from langgraph.types import Command

from app.agent.constants import FLOW_CALC, FLOW_QUALIFY
from app.agent.i18n import at
from app.agent.nodes.recap import node_recap
from app.agent.nodes.router import _recap_gap_exceeded, node_router
from app.agent.state import _default_dialog


def _run(coro):
    return asyncio.run(coro)


def _iso(minutes_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()


def _state(user_text, dialog_overrides=None, **overrides):
    dialog = {**_default_dialog(), **(dialog_overrides or {})}
    base = {
        "last_user_text": user_text,
        "messages": [],
        "dialog": dialog,
        "human_mode": False,
        "keyboard_options": None,
        "lang": "ru",
        "session_id": "t",
        "user_id": 1,
        "answer": "",
    }
    base.update(overrides)
    return base


# ---- _recap_gap_exceeded ----------------------------------------------------

class TestRecapGapExceeded:
    def test_none_is_not_stale(self):
        assert _recap_gap_exceeded(None) is False

    def test_recent_timestamp_is_not_stale(self):
        assert _recap_gap_exceeded(_iso(5)) is False

    def test_old_timestamp_is_stale(self, monkeypatch):
        monkeypatch.delenv("RECAP_GAP_MINUTES", raising=False)
        assert _recap_gap_exceeded(_iso(31)) is True

    def test_respects_env_override(self, monkeypatch):
        monkeypatch.setenv("RECAP_GAP_MINUTES", "5")
        assert _recap_gap_exceeded(_iso(6)) is True
        assert _recap_gap_exceeded(_iso(4)) is False

    def test_malformed_timestamp_is_not_stale(self):
        assert _recap_gap_exceeded("not-a-timestamp") is False


# ---- node_router: recap routing --------------------------------------------

class TestRouterRecap:
    def test_fresh_calc_flow_goes_to_calc_flow_not_recap(self):
        state = _state("100", {"flow": FLOW_CALC, "calc_step": "amount", "last_turn_at": _iso(1)})
        result = _run(node_router(state))
        assert result.goto == "calc_flow"

    def test_stale_calc_flow_goes_to_recap(self, monkeypatch):
        monkeypatch.delenv("RECAP_GAP_MINUTES", raising=False)
        state = _state("100", {"flow": FLOW_CALC, "calc_step": "amount", "last_turn_at": _iso(45)})
        result = _run(node_router(state))
        assert result.goto == "recap"

    def test_stale_qualify_flow_goes_to_recap(self, monkeypatch):
        monkeypatch.delenv("RECAP_GAP_MINUTES", raising=False)
        state = _state(
            "да", {"flow": FLOW_QUALIFY, "qualify_category": "autoloan",
                   "qualify_node": "salary", "last_turn_at": _iso(45)},
        )
        result = _run(node_router(state))
        assert result.goto == "recap"

    def test_stale_lead_step_goes_to_recap(self, monkeypatch):
        """lead_step always carries flow=FLOW_CALC (see calc_flow.py), so the
        same flow-based gap check covers it without a separate lead_step check."""
        monkeypatch.delenv("RECAP_GAP_MINUTES", raising=False)
        state = _state(
            "да", {"flow": FLOW_CALC, "lead_step": "offer", "last_turn_at": _iso(45)},
        )
        result = _run(node_router(state))
        assert result.goto == "recap"

    def test_no_last_turn_at_never_triggers_recap(self):
        """A calc_flow that just started this session (no prior finalized
        turn yet) must not immediately bounce into a recap offer."""
        state = _state("100", {"flow": FLOW_CALC, "calc_step": "amount", "last_turn_at": None})
        result = _run(node_router(state))
        assert result.goto == "calc_flow"

    def test_recap_pending_routes_to_recap_regardless_of_gap(self):
        state = _state(
            "продолжить", {"flow": FLOW_CALC, "calc_step": "amount",
                            "recap_pending": True, "last_turn_at": _iso(1)},
        )
        result = _run(node_router(state))
        assert result.goto == "recap"

    def test_human_mode_takes_priority_over_recap(self):
        state = _state(
            "hi", {"flow": FLOW_CALC, "last_turn_at": _iso(60)}, human_mode=True,
        )
        result = _run(node_router(state))
        assert result.goto == "human_mode"

    def test_faq_flow_never_triggers_recap(self):
        """Only FLOW_CALC/FLOW_QUALIFY are recap-eligible — an old FAQ/browse
        session must route normally."""
        state = _state("привет", {"flow": None, "last_turn_at": _iso(120)})
        result = _run(node_router(state))
        assert result.goto == "faq"


# ---- node_recap: first entry (offer) ---------------------------------------

class TestNodeRecapOffer:
    def test_shows_offer_with_three_buttons(self):
        state = _state(
            "100000000",
            {"flow": FLOW_CALC, "category": "autoloan", "calc_step": "amount",
             "selected_product": {"name": "Автокредит Стандарт"}},
        )
        result = _run(node_recap(state))
        assert isinstance(result, dict)
        assert "Автокредит Стандарт" in result["answer"]
        assert result["keyboard_options"] == [
            at("btn_recap_continue", "ru"), at("btn_recap_restart", "ru"), at("btn_recap_other", "ru"),
        ]
        assert result["dialog"]["recap_pending"] is True
        # flow/category/selected_product must survive — offer doesn't reset progress
        assert result["dialog"]["flow"] == FLOW_CALC
        assert result["dialog"]["selected_product"]["name"] == "Автокредит Стандарт"

    def test_qualify_flow_description_uses_category_label(self):
        state = _state(
            "да", {"flow": FLOW_QUALIFY, "qualify_category": "deposit", "qualify_node": "goal"},
        )
        result = _run(node_recap(state))
        assert at("cat_deposit", "ru") in result["answer"]


# ---- node_recap: handling the answer ---------------------------------------

class TestNodeRecapAnswer:
    def test_continue_routes_back_to_calc_flow(self):
        state = _state(
            "Продолжить",
            {"flow": FLOW_CALC, "calc_step": "amount", "recap_pending": True},
        )
        result = _run(node_recap(state))
        assert isinstance(result, Command)
        assert result.goto == "calc_flow"
        assert result.update["dialog"]["recap_pending"] is False
        assert result.update["dialog"]["calc_step"] == "amount"  # untouched

    def test_continue_routes_back_to_qualify_flow(self):
        state = _state(
            "Davom etish",
            {"flow": FLOW_QUALIFY, "qualify_category": "autoloan",
             "qualify_node": "salary", "recap_pending": True},
        )
        result = _run(node_recap(state))
        assert isinstance(result, Command)
        assert result.goto == "qualify_flow"

    def test_restart_resets_dialog_and_goes_to_faq(self):
        state = _state(
            "Начать заново",
            {"flow": FLOW_CALC, "calc_step": "amount", "calc_slots": {"amount": 100},
             "recap_pending": True},
        )
        result = _run(node_recap(state))
        assert isinstance(result, Command)
        assert result.goto == "faq"
        assert result.update["dialog"]["flow"] is None
        assert result.update["dialog"]["calc_slots"] == {}

    def test_different_question_clears_flag_and_goes_to_faq_preserving_context(self):
        state = _state(
            "а какая у вас процентная ставка по вкладам?",
            {"flow": FLOW_CALC, "category": "autoloan", "calc_step": "amount",
             "recap_pending": True},
        )
        result = _run(node_recap(state))
        assert isinstance(result, Command)
        assert result.goto == "faq"
        assert result.update["dialog"]["recap_pending"] is False
        # unlike "restart", dialog context (flow/category) is left as-is
        assert result.update["dialog"]["flow"] == FLOW_CALC
        assert result.update["dialog"]["category"] == "autoloan"
