from __future__ import annotations

import asyncio
import logging as _logging
from typing import Any, Dict, Optional, Sequence

from langchain_core.messages import AIMessage, HumanMessage

from app.agent.checkpointer import _create_async_checkpointer
from app.agent.constants import VALID_LANGS, resolve_language
from app.agent.i18n import at
from app.agent.graph import build_graph
from app.agent.lang_detect import detect_language
from app.agent.lang_heuristic import check_lang_mismatch, looks_worth_llm_recheck
from app.agent.pii_masker import mask_pii
from app.agent.state import AgentTurnResult, BotState, _default_dialog
from app.utils.faq_tools import reset_faq_turn_cache

_agent_logger = _logging.getLogger(__name__)

# Backoff schedule (seconds) between retries of a failed checkpoint read in
# `_aload_existing_state`. A transient DB blip should not silently look like
# "no prior session" — see that method's docstring.
_STATE_READ_RETRY_BACKOFFS: tuple[float, ...] = (0.2, 0.5)


class Agent:
    """Banking FAQ + product selection agent with LangGraph state persistence."""

    def __init__(self) -> None:
        self._graph = build_graph()
        self._checkpointer: Any = None
        self._checkpointer_cm: Any = None
        # Per-session_id (== checkpointer thread_id) lock serializing the
        # read-process-write cycle in _ainvoke / resume_human_mode. Without
        # this, two concurrent turns for the same session (double-tap,
        # webhook redelivery — the webhook handler has no per-chat
        # serialization or update_id dedup) race on the checkpointer: both
        # read the same prior state, both write back — last write wins, the
        # other turn's dialog update is silently lost.
        #
        # CONSTRAINT: this is an in-process dict of asyncio.Lock, so it only
        # serializes turns within a single uvicorn worker process. It is
        # sufficient for the current single-process deployment. If this app
        # is ever scaled to multiple processes/workers, this must be replaced
        # with a distributed lock (e.g. Redis) or per-session affinity
        # routing at the load balancer — an in-memory lock cannot coordinate
        # across processes.
        self._session_locks: Dict[str, asyncio.Lock] = {}

    def _get_session_lock(self, session_id: str) -> asyncio.Lock:
        lock = self._session_locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._session_locks[session_id] = lock
        return lock

    def _release_session_lock(self, session_id: str, lock: asyncio.Lock) -> None:
        """Best-effort cleanup so `_session_locks` doesn't grow unboundedly.

        Only drops the entry when nobody else holds or is waiting on this
        exact lock instance — otherwise a concurrent waiter would silently
        start waiting on a lock nobody else knows about anymore.
        """
        if lock.locked():
            return
        if getattr(lock, "_waiters", None):
            return
        if self._session_locks.get(session_id) is lock:
            del self._session_locks[session_id]

    async def setup(self, backend: str = "auto", url: Optional[str] = None) -> None:
        """Initialize async checkpointer. Call once at startup."""
        checkpointer, cm = await _create_async_checkpointer(backend, url)
        self._checkpointer = checkpointer
        self._checkpointer_cm = cm
        self._graph = build_graph(checkpointer=checkpointer)

    def _build_config(self, session_id: str) -> Dict[str, Any]:
        return {"configurable": {"thread_id": session_id}}

    async def _aload_existing_state(self, config: Dict[str, Any]) -> dict:
        """Load the last checkpointed state for this thread_id.

        Retries a failed read a couple of times (transient DB blips) before
        giving up. On persistent failure this RAISES rather than returning
        `{}`: `_ainvoke` supplies an explicit value for every `BotState` key
        on every turn (there are no reducers on `dialog`/`messages`), so
        silently returning `{}` here would make the following
        `graph.ainvoke` call overwrite the real checkpointed dialog/messages
        with fresh defaults — i.e. silently reset the user's whole session.
        Letting the exception propagate instead lets
        `chat_service.handle_user_message`'s existing `except Exception`
        path show "agent temporarily unavailable" — a far better failure
        mode than a silent session wipe.

        A missing checkpoint (brand new session — `snapshot.values` is
        empty/None) is NOT an error: no exception is raised, so this returns
        `{}` immediately on the first attempt, same as before.
        """
        session_id = (config.get("configurable") or {}).get("thread_id", "unknown")
        last_exc: Optional[BaseException] = None
        attempts = len(_STATE_READ_RETRY_BACKOFFS) + 1
        for attempt in range(attempts):
            try:
                snapshot = await self._graph.aget_state(config)
                return dict(snapshot.values or {})
            except Exception as exc:
                last_exc = exc
                if attempt < attempts - 1:
                    _agent_logger.warning(
                        "Failed to load existing state for session %s (attempt %d/%d): %s",
                        session_id, attempt + 1, attempts, exc,
                    )
                    await asyncio.sleep(_STATE_READ_RETRY_BACKOFFS[attempt])
        _agent_logger.error(
            "Failed to load existing state for session %s after %d attempts: %s",
            session_id, attempts, last_exc,
        )
        raise last_exc

    async def _ainvoke(
        self,
        session_id: str,
        user_text: str,
        language: Optional[str] = None,
        human_mode: bool = False,
        user_id: Optional[int] = None,
    ) -> AgentTurnResult:
        # Serialize concurrent turns for the same session — see
        # `_session_locks` docstring in __init__ for why this is needed.
        lock = self._get_session_lock(session_id)
        async with lock:
            result = await self._ainvoke_locked(
                session_id, user_text, language, human_mode, user_id
            )
        self._release_session_lock(session_id, lock)
        return result

    async def _ainvoke_locked(
        self,
        session_id: str,
        user_text: str,
        language: Optional[str],
        human_mode: bool,
        user_id: Optional[int],
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
        # 4. If the regex stayed silent on a "suspicious" message (Uzbek typos,
        #    short transliterated text), fall back to the LLM detector — same
        #    one as step 2, with PII masking baked in. This catches cases
        #    where the regex marker list is too narrow.
        primary_lang = language if language in VALID_LANGS else None
        llm_detected: Optional[str] = None
        if primary_lang is None:
            llm_detected = await detect_language(
                user_text, fallback=resolve_language(dialog)
            )
            primary_lang = llm_detected
        suggested_lang: Optional[str] = check_lang_mismatch(user_text, primary_lang)
        if (
            suggested_lang is None
            and primary_lang in VALID_LANGS
            and looks_worth_llm_recheck(user_text)
        ):
            # Reuse the step-2 result if we already paid for it this turn;
            # otherwise pay for one extra detector call. mask_pii is applied
            # inside detect_language, so raw PII never reaches the LLM.
            recheck = llm_detected if llm_detected is not None else await detect_language(
                user_text, fallback=primary_lang
            )
            if recheck in VALID_LANGS and recheck != primary_lang:
                suggested_lang = recheck
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
        # Fresh per-turn faq_search memoization scope: node_faq's strict
        # pre-check and the faq_lookup tool (invoked later in the same turn's
        # ToolNode loop, same asyncio task) then share one hybrid-search
        # result instead of paying for the embedding call + lexical scan twice.
        reset_faq_turn_cache()
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
        # Same per-session serialization as _ainvoke — a resume is also a
        # read-process-write cycle against the same checkpointer thread_id.
        lock = self._get_session_lock(session_id)
        async with lock:
            try:
                from langgraph.types import Command
                config = self._build_config(session_id)
                out = await self._graph.ainvoke(Command(resume=operator_reply), config=config)
                result = str(out.get("answer") or operator_reply)
            except Exception as e:
                _agent_logger.warning("resume_human_mode error for %s: %s", session_id, e)
                result = operator_reply
        self._release_session_lock(session_id, lock)
        return result

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
