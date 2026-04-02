from __future__ import annotations

import logging as _logging
from typing import Any, Dict, Optional, Sequence

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from app.agent.checkpointer import _create_async_checkpointer
from app.agent.constants import _REQUEST_LANGUAGE
from app.agent.i18n import at
from app.agent.graph import build_graph
from app.agent.state import AgentTurnResult, BotState, _default_dialog
from app.utils.data_loaders import _normalize_language_code

_agent_logger = _logging.getLogger(__name__)


class Agent:
    """Banking FAQ + product selection agent with LangGraph state persistence."""

    def __init__(self) -> None:
        from langgraph.store.memory import InMemoryStore
        self._store = InMemoryStore()
        self._graph = build_graph()
        self._checkpointer: Any = None
        self._checkpointer_cm: Any = None

    async def setup(self, backend: str = "auto", url: Optional[str] = None) -> None:
        """Initialize async checkpointer. Call once at startup."""
        checkpointer, cm = await _create_async_checkpointer(backend, url)
        self._checkpointer = checkpointer
        self._checkpointer_cm = cm
        self._graph = build_graph(checkpointer=checkpointer, store=self._store)

    def _build_config(self, session_id: str) -> Dict[str, Any]:
        return {"configurable": {"thread_id": session_id}}

    async def _aload_existing_state(self, config: Dict[str, Any]) -> dict:
        try:
            snapshot = await self._graph.aget_state(config)
            return dict(snapshot.values or {})
        except Exception as exc:
            _agent_logger.debug("Failed to load existing state: %s", exc)
            return {}

    def _save_user_preference(self, user_id: int, key: str, value: Any) -> None:
        try:
            self._store.put((str(user_id), "preferences"), key, {"value": value})
        except Exception as exc:
            _agent_logger.debug("Failed to save user preference: %s", exc)

    def _get_user_preference(self, user_id: int, key: str) -> Any:
        try:
            items = self._store.search((str(user_id), "preferences"), query=key, limit=1)
            if items:
                return items[0].value.get("value")
        except Exception as exc:
            _agent_logger.debug("Failed to get user preference: %s", exc)
        return None

    async def _ainvoke(
        self,
        session_id: str,
        user_text: str,
        language: Optional[str] = None,
        human_mode: bool = False,
        user_id: Optional[int] = None,
    ) -> AgentTurnResult:
        if user_id and language is None:
            language = self._get_user_preference(user_id, "language")
        lang_token = _REQUEST_LANGUAGE.set(_normalize_language_code(language))
        config = self._build_config(session_id)
        try:
            existing = await self._aload_existing_state(config)
            state_in: BotState = {
                "last_user_text": user_text,
                "messages": list(existing.get("messages") or [SystemMessage(content=at("system_policy", _REQUEST_LANGUAGE.get()))]),
                "answer": "",
                "human_mode": human_mode,
                "keyboard_options": None,
                "dialog": dict(existing.get("dialog") or _default_dialog()),
                "_route": "",
                "session_id": session_id,
                "user_id": user_id,
                "show_operator_button": False,
                "token_usage": None,
            }
            out = await self._graph.ainvoke(state_in, config=config)
            return AgentTurnResult(
                text=str(out.get("answer") or at("faq_fallback", _REQUEST_LANGUAGE.get())),
                keyboard_options=out.get("keyboard_options") or None,
                show_operator_button=bool(out.get("show_operator_button")),
                token_usage=out.get("token_usage") or None,
            )
        finally:
            _REQUEST_LANGUAGE.reset(lang_token)

    async def send_message(
        self,
        session_id: str,
        user_id: int,
        text: str,
        language: Optional[str] = None,
        human_mode: bool = False,
    ) -> AgentTurnResult:
        if user_id and language:
            self._save_user_preference(user_id, "language", language)
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

    async def ensure_language(self, text: str, language: Optional[str] = None) -> str:
        return text

    async def sync_history(self, session_id: str, events: Sequence[dict[str, str]]) -> None:
        if not events:
            return
        config = self._build_config(session_id)
        existing = await self._aload_existing_state(config)
        msgs = list(existing.get("messages") or [SystemMessage(content=at("system_policy", _REQUEST_LANGUAGE.get()))])
        for event in events:
            role = (event.get("role") or "").strip().lower()
            text = (event.get("text") or "").strip()
            if not text:
                continue
            if role in {"user", "human"}:
                msgs.append(HumanMessage(content=text))
            elif role in {"assistant", "agent", "operator", "bot", "ai"}:
                msgs.append(AIMessage(content=text))
        try:
            await self._graph.aupdate_state(config, {"messages": msgs})
        except Exception as exc:
            _agent_logger.warning("Failed to sync history for session %s: %s", session_id, exc)

    def close(self) -> None:
        return None

    async def aclose(self) -> None:
        if self._checkpointer_cm is not None:
            try:
                await self._checkpointer_cm.__aexit__(None, None, None)
            except Exception as exc:
                _agent_logger.warning("Failed to close checkpointer: %s", exc)
