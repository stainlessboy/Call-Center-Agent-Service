from __future__ import annotations

import logging as _logging
from typing import Any, Dict, Optional, Sequence

from langchain_core.messages import AIMessage, HumanMessage

from app.agent.checkpointer import _create_async_checkpointer
from app.agent.constants import VALID_LANGS, resolve_language
from app.agent.i18n import at
from app.agent.graph import build_graph
from app.agent.lang_detect import detect_language
from app.agent.lang_heuristic import check_lang_mismatch
from app.agent.pii_masker import mask_pii
from app.agent.state import AgentTurnResult, BotState, _default_dialog

_agent_logger = _logging.getLogger(__name__)


class Agent:
    """Banking FAQ + product selection agent with LangGraph state persistence."""

    def __init__(self) -> None:
        self._graph = build_graph()
        self._checkpointer: Any = None
        self._checkpointer_cm: Any = None

    async def setup(self, backend: str = "auto", url: Optional[str] = None) -> None:
        """Initialize async checkpointer. Call once at startup."""
        checkpointer, cm = await _create_async_checkpointer(backend, url)
        self._checkpointer = checkpointer
        self._checkpointer_cm = cm
        self._graph = build_graph(checkpointer=checkpointer)

    def _build_config(self, session_id: str) -> Dict[str, Any]:
        return {"configurable": {"thread_id": session_id}}

    async def _aload_existing_state(self, config: Dict[str, Any]) -> dict:
        try:
            snapshot = await self._graph.aget_state(config)
            return dict(snapshot.values or {})
        except Exception as exc:
            _agent_logger.debug("Failed to load existing state: %s", exc)
            return {}

    async def _ainvoke(
        self,
        session_id: str,
        user_text: str,
        language: Optional[str] = None,
        human_mode: bool = False,
        user_id: Optional[int] = None,
    ) -> AgentTurnResult:
        config = self._build_config(session_id)
        existing = await self._aload_existing_state(config)
        dialog = dict(existing.get("dialog") or _default_dialog())

        # Language resolution (hybrid):
        # 1. User.language (passed via `language` arg) is authoritative — it's
        #    what the user explicitly chose in /start. Costs zero tokens.
        # 2. If User.language is unset (first contact), fall back to the
        #    dedicated LLM detector — one small call, cached.
        # 3. Run a cheap regex heuristic on top: if the message clearly looks
        #    like a different language, surface a switch suggestion so the
        #    bot layer can ask the user to confirm. We never switch silently.
        primary_lang = language if language in VALID_LANGS else None
        if primary_lang is None:
            primary_lang = await detect_language(
                user_text, fallback=resolve_language(dialog)
            )
        suggested_lang: Optional[str] = check_lang_mismatch(user_text, primary_lang)
        # Don't re-prompt every turn — once we've offered a switch for a given
        # target language and the user answered, we record the answer in
        # `dialog["lang_switch_offered"]`. While that flag matches the same
        # target, suppress the offer.
        if suggested_lang and dialog.get("lang_switch_offered") == suggested_lang:
            suggested_lang = None
        dialog["last_lang"] = primary_lang
        if suggested_lang:
            # Mark that we've offered a switch toward this target so the next
            # turn doesn't re-prompt. The flag is cleared whenever a tool call
            # resets dialog to defaults (greeting, get_products, find_office).
            dialog["lang_switch_offered"] = suggested_lang
        detected_lang = primary_lang

        # node_faq builds a fresh SystemMessage(policy[lang]) every turn, so we
        # don't bake one in here. Any stale SystemMessage left at messages[0]
        # from older code is silently dropped by node_faq's history-tail logic.
        prior = list(existing.get("messages") or [])

        state_in: BotState = {
            "last_user_text": user_text,
            "messages": prior,
            "answer": "",
            "human_mode": human_mode,
            "keyboard_options": None,
            "dialog": dialog,
            "lang": detected_lang,
            "_route": "",
            "session_id": session_id,
            "user_id": user_id,
            "show_operator_button": False,
            "token_usage": None,
        }
        out = await self._graph.ainvoke(state_in, config=config)
        return AgentTurnResult(
            text=str(out.get("answer") or at("faq_fallback", out.get("lang") or detected_lang)),
            keyboard_options=out.get("keyboard_options") or None,
            show_operator_button=bool(out.get("show_operator_button")),
            token_usage=out.get("token_usage") or None,
            suggested_language=suggested_lang,
        )

    async def send_message(
        self,
        session_id: str,
        user_id: int,
        text: str,
        language: Optional[str] = None,
        human_mode: bool = False,
    ) -> AgentTurnResult:
        return await self._ainvoke(session_id, text, language, human_mode=human_mode, user_id=user_id)

    async def resume_human_mode(self, session_id: str, operator_reply: str) -> str:
        """Resume a graph interrupted in human_mode node, injecting operator reply."""
        try:
            from langgraph.types import Command
            config = self._build_config(session_id)
            out = await self._graph.ainvoke(Command(resume=operator_reply), config=config)
            return str(out.get("answer") or operator_reply)
        except Exception as e:
            _agent_logger.warning("resume_human_mode error for %s: %s", session_id, e)
            return operator_reply

    async def sync_history(self, session_id: str, events: Sequence[dict[str, str]]) -> None:
        if not events:
            return
        config = self._build_config(session_id)
        existing = await self._aload_existing_state(config)
        msgs = list(existing.get("messages") or [])
        for event in events:
            role = (event.get("role") or "").strip().lower()
            text = (event.get("text") or "").strip()
            if not text:
                continue
            if role in {"user", "human"}:
                msgs.append(HumanMessage(content=mask_pii(text)))
            elif role in {"assistant", "agent", "operator", "bot", "ai"}:
                msgs.append(AIMessage(content=text))
        try:
            await self._graph.aupdate_state(config, {"messages": msgs})
        except Exception as exc:
            _agent_logger.warning("Failed to sync history for session %s: %s", session_id, exc)

    async def aclose(self) -> None:
        if self._checkpointer_cm is not None:
            try:
                await self._checkpointer_cm.__aexit__(None, None, None)
            except Exception as exc:
                _agent_logger.warning("Failed to close checkpointer: %s", exc)
