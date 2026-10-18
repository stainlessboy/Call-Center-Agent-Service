from __future__ import annotations

import asyncio
import logging as _logging
import os
from typing import Any, Dict, Optional, Sequence

from langchain_core.messages import AIMessage, HumanMessage

from app.agent.checkpointer import _create_async_checkpointer
from app.agent.i18n import at
from app.agent.graph import build_graph
from app.agent.memory_extract import extract_and_store
from app.agent.llm import extract_text_content
from app.agent.pii_masker import mask_pii
from app.agent.profile import load_user_profile
from app.agent.state import AgentTurnResult
from app.agent.streaming import get_on_token_callback
from app.agent.store import create_store
from app.agent.tracing import build_run_config
from app.utils.faq_tools import reset_faq_turn_cache

_agent_logger = _logging.getLogger(__name__)


def _memory_extract_enabled() -> bool:
    """MEMORY_EXTRACT_ENABLED env flag (default true) — see CLAUDE.md."""
    return (os.getenv("MEMORY_EXTRACT_ENABLED") or "true").strip().lower() in (
        "1", "true", "yes", "on",
    )

# Backoff schedule (seconds) between retries of a failed checkpoint read in
# `_aload_existing_state`. A transient DB blip should not silently look like
# "no prior session" — see that method's docstring.
_STATE_READ_RETRY_BACKOFFS: tuple[float, ...] = (0.2, 0.5)

# Graph nodes whose model output is the customer's answer, and may therefore
# be streamed token by token. "faq" is the legacy node; "model" is the model
# step inside the `create_agent` consultant subgraph. Everything else that
# calls an LLM in this graph is internal (language detection, FAQ reranking,
# answer rephrasing) and must never trickle into the chat window.
_STREAMED_NODES = frozenset({"faq", "model"})


class Agent:
    """Banking FAQ + product selection agent with LangGraph state persistence."""

    def __init__(self) -> None:
        self._graph = build_graph()
        self._checkpointer: Any = None
        self._checkpointer_cm: Any = None
        # Long-term (cross-thread) memory — see app/agent/store.py.
        self._store: Any = None
        self._store_cm: Any = None
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
        # Fire-and-forget background tasks (currently: the memory-extractor
        # kicked off after each turn — see _launch_background_task). A plain
        # `asyncio.create_task(...)` with the result discarded is a known
        # footgun: nothing holds a strong reference to the Task, so the event
        # loop is free to garbage-collect it mid-flight (silently dropping
        # the extraction). Keeping a strong reference here until the task's
        # own done-callback removes it prevents that.
        self._background_tasks: set[asyncio.Task] = set()

    def _launch_background_task(self, coro, *, name: str) -> asyncio.Task:
        task = asyncio.create_task(coro, name=name)
        self._background_tasks.add(task)

        def _on_done(t: asyncio.Task) -> None:
            self._background_tasks.discard(t)
            if t.cancelled():
                return
            exc = t.exception()
            if exc is not None:
                _agent_logger.warning(
                    "Background task %s failed: %s", t.get_name(), exc, exc_info=exc,
                )

        task.add_done_callback(_on_done)
        return task

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
        """Initialize the async checkpointer and the long-term store. Call
        once at startup.

        The checkpointer holds one thread's conversation; the store holds what
        we know about the PERSON across all their threads (roadmap П.4). A
        store failure is not fatal — `create_store` degrades to in-memory,
        because losing long-term memory is a quality regression, not an outage.
        """
        checkpointer, cm = await _create_async_checkpointer(backend, url)
        self._checkpointer = checkpointer
        self._checkpointer_cm = cm
        store, store_cm = await create_store(backend, url)
        self._store = store
        self._store_cm = store_cm
        self._graph = build_graph(checkpointer=checkpointer, store=store)

    def _build_config(self, session_id: str, **kwargs: Any) -> Dict[str, Any]:
        """Per-run LangGraph config: checkpointer thread_id plus observability
        metadata/tags and (when enabled) the PII-masking LangSmith tracer.
        See app/agent/tracing.py."""
        return build_run_config(session_id, **kwargs)

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
        channel: Optional[str] = None,
    ) -> AgentTurnResult:
        # Serialize concurrent turns for the same session — see
        # `_session_locks` docstring in __init__ for why this is needed.
        lock = self._get_session_lock(session_id)
        async with lock:
            result = await self._ainvoke_locked(
                session_id, user_text, language, human_mode, user_id, channel
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
        channel: Optional[str] = None,
    ) -> AgentTurnResult:
        # Only genuinely NEW input goes in. Everything derived from the
        # session — `dialog`, `messages`, the resolved language, the client
        # profile — is read from the checkpoint by the graph itself:
        #   * `messages` carries the `add_messages` reducer, so passing the
        #     prior history back in would append a second copy of it.
        #   * `dialog` is left untouched here so `node_prepare_turn` (the entry
        #     node) can read it from the checkpoint, which is what removed the
        #     second Postgres round-trip this method used to make.
        # `node_faq` rebuilds its SystemMessage(policy[lang]) every turn, so no
        # system message is baked into the history either.
        state_in: dict = {
            "last_user_text": user_text,
            "answer": "",
            "human_mode": human_mode,
            "keyboard_options": None,
            "requested_lang": language,
            "suggested_lang": None,
            "_route": "",
            "session_id": session_id,
            "user_id": user_id,
            "show_operator_button": False,
            "token_usage": None,
            "ui_blocks": None,
        }
        # Fresh per-turn faq_search memoization scope: node_faq's strict
        # pre-check and the faq_lookup tool (invoked later in the same turn's
        # ToolNode loop, same asyncio task) then share one search result
        # instead of paying for the retrieval + LLM rerank twice.
        reset_faq_turn_cache()
        # `lang` here is the language the CALLER knew (User.language). The
        # turn's finally-resolved language lives on node_prepare_turn's output
        # inside the trace; run metadata is fixed when the run starts, so this
        # is the best value available at this point and it matches the final
        # one on every turn where the user has picked a language.
        run_config = self._build_config(
            session_id,
            user_id=user_id,
            lang=language,
            channel=channel,
            run_name="agent_turn",
            extra_tags=("human_mode",) if human_mode else (),
        )
        on_token = get_on_token_callback()
        if on_token is None:
            out = await self._graph.ainvoke(state_in, config=run_config)
        else:
            out = await self._astream_turn(state_in, run_config, on_token)
        answer_text = str(out.get("answer") or "")
        # Resolved by node_prepare_turn during the run.
        detected_lang = out.get("lang") or language or "ru"
        suggested_lang = out.get("suggested_lang")
        user_profile = out.get("user_profile")

        # Fire-and-forget memory extraction: NOT awaited, must not add to turn
        # latency. Skipped for human_mode turns (nothing useful to extract from
        # an operator handoff turn — the operator's reply isn't LLM-analyzable
        # customer content the same way) and empty answers (nothing happened).
        if (
            user_id is not None
            and not human_mode
            and answer_text
            and _memory_extract_enabled()
        ):
            self._launch_background_task(
                extract_and_store(user_id, user_text, answer_text, user_profile),
                name=f"memory_extract:{session_id}",
            )

        return AgentTurnResult(
            text=answer_text or at("faq_fallback", out.get("lang") or detected_lang),
            keyboard_options=out.get("keyboard_options") or None,
            show_operator_button=bool(out.get("show_operator_button")),
            token_usage=out.get("token_usage") or None,
            suggested_language=suggested_lang,
            ui_blocks=out.get("ui_blocks") or None,
        )

    async def _astream_turn(
        self, state_in: dict, config: Dict[str, Any], on_token,
    ) -> dict:
        """Run the turn with `astream` so live tokens can be forwarded.

        Tokens come from LangGraph's own `messages` stream rather than from a
        hand-rolled `.astream()` inside a node (roadmap П.5). That matters
        because the model call moved: under `CONSULTANT_AGENT_V2` it happens
        inside the `create_agent` subgraph, where a node-local streaming branch
        could not see it. Sourcing from the graph covers both paths and every
        future node for free.

        Filtering is deliberately strict — the graph makes several LLM calls
        per turn that the customer must NOT see trickling in:
          * the language detector in `node_prepare_turn`,
          * the FAQ reranker and the answer rephraser,
          * tool-call rounds, which produce arguments, not prose.
        So only assistant chunks carrying visible text, from a customer-facing
        node, with no tool-call delta, are forwarded.
        """
        from langchain_core.messages import AIMessageChunk

        final_state: dict = {}
        async for event in self._graph.astream(
            state_in, config=config, stream_mode=["messages", "values"], subgraphs=True,
        ):
            # `subgraphs=True` prefixes each event with the namespace tuple of
            # the (sub)graph it came from, so events are 3-tuples here and
            # 2-tuples at the top level. Both shapes have to be accepted: the
            # consultant's model call arrives namespaced, the legacy node's
            # does not.
            if len(event) == 3:
                namespace, mode, chunk = event
            else:
                namespace, mode, chunk = (), *event
            if mode == "values":
                # ONLY the top-level graph's state is the turn's result. With
                # `subgraphs=True` the consultant subgraph also emits `values`,
                # and its state has no `answer`/`dialog` of the shape the
                # caller expects — taking the last event regardless of
                # namespace would hand back the subgraph's inner state and
                # silently blank the reply.
                if not namespace and isinstance(chunk, dict):
                    final_state = chunk
                continue
            if mode != "messages":
                continue
            try:
                message, metadata = chunk
            except (TypeError, ValueError):
                continue
            if not isinstance(message, AIMessageChunk):
                continue
            if getattr(message, "tool_call_chunks", None):
                # This round is producing a tool call, not an answer.
                continue
            if (metadata or {}).get("langgraph_node") not in _STREAMED_NODES:
                continue
            text = extract_text_content(message)
            if not text:
                continue
            try:
                await on_token(text)
            except Exception:
                _agent_logger.debug("on_token callback failed", exc_info=True)

        return final_state

    async def send_message(
        self,
        session_id: str,
        user_id: int,
        text: str,
        language: Optional[str] = None,
        human_mode: bool = False,
        channel: Optional[str] = None,
    ) -> AgentTurnResult:
        return await self._ainvoke(
            session_id, text, language, human_mode=human_mode, user_id=user_id, channel=channel,
        )

    async def get_handoff_context(self, session_id: str) -> Dict[str, Any]:
        """Best-effort, read-only snapshot of this session's current dialog,
        client profile, and message history — for building the operator
        handoff summary (see app/agent/handoff.py). Deliberately does NOT
        take the per-session lock (`_session_locks`): this only ever runs
        right after a human_mode handoff already committed, and a summary is
        a nice-to-have that must never block on / contend with an in-flight
        turn. Never raises — a read failure just means a thinner summary.
        """
        try:
            config = self._build_config(session_id, run_name="handoff_context")
            existing = await self._aload_existing_state(config)
        except Exception as exc:
            _agent_logger.warning(
                "get_handoff_context: failed to load state for session %s: %s", session_id, exc,
            )
            return {}

        dialog = existing.get("dialog") or {}
        messages = existing.get("messages") or []
        user_id = existing.get("user_id")
        profile: Optional[dict] = None
        if user_id is not None:
            try:
                profile = await load_user_profile(user_id)
            except Exception as exc:
                _agent_logger.warning(
                    "get_handoff_context: failed to load profile for user %s: %s", user_id, exc,
                )
        return {"dialog": dialog, "messages": messages, "user_profile": profile}

    async def get_pending_interrupt(self, session_id: str) -> Optional[dict]:
        """The interrupt this session is paused on, or None (roadmap П.6).

        Two different things can pause a session, and the caller needs to tell
        them apart:
          * `node_human_mode_turn` waiting for an operator's reply;
          * `HumanInTheLoopMiddleware` waiting for someone to approve a write
            tool the model wants to call.

        Reading the graph's own `.interrupts` is authoritative — it reflects
        what the checkpoint actually holds. `ChatSession.human_mode` is a
        separate DB flag that can drift from it (a crash between the two
        writes, an operator timeout that flipped the flag back while the graph
        stayed parked), and acting on the flag alone is what makes a session
        look responsive while the graph is still waiting.

        Never raises: a read failure means "we don't know", and the caller
        falls back to the flag it already had.
        """
        try:
            snapshot = await self._graph.aget_state(self._build_config(session_id))
        except Exception as exc:
            _agent_logger.warning(
                "get_pending_interrupt: state read failed for %s: %s", session_id, exc,
            )
            return None

        interrupts = list(getattr(snapshot, "interrupts", None) or [])
        if not interrupts:
            return None

        first = interrupts[0]
        value = getattr(first, "value", None)
        payload = dict(value) if isinstance(value, dict) else {"value": value}
        payload["interrupt_id"] = getattr(first, "id", None)
        # `kind` lets the caller route without re-parsing the payload shape.
        payload["kind"] = (
            "operator" if payload.get("reason") == "human_mode_active" else "approval"
        )
        return payload

    async def resume_pending_interrupt(self, session_id: str, decision: Any) -> Optional[dict]:
        """Resume a paused graph with `decision` (roadmap П.6).

        `decision` is passed to `Command(resume=...)` verbatim, so it carries
        whatever the waiting interrupt expects: an operator's reply text for
        `node_human_mode_turn`, or `HumanInTheLoopMiddleware`'s decision dict
        (`{"decisions": [{"type": "approve"}]}`, an `edit` with new arguments,
        or a `reject` with a message) for an approval gate.

        Returns the resulting state, or None when the session was not paused
        or the resume failed — a failed approval must not take the bot down.
        """
        from langgraph.types import Command

        lock = self._get_session_lock(session_id)
        async with lock:
            try:
                config = self._build_config(session_id, run_name="interrupt_resume")
                out = await self._graph.ainvoke(Command(resume=decision), config=config)
            except Exception as exc:
                _agent_logger.warning(
                    "resume_pending_interrupt failed for %s: %s", session_id, exc,
                )
                out = None
        self._release_session_lock(session_id, lock)
        return out

    async def resume_human_mode(self, session_id: str, operator_reply: str) -> str:
        """Resume a graph interrupted in human_mode node, injecting operator reply."""
        # Same per-session serialization as _ainvoke — a resume is also a
        # read-process-write cycle against the same checkpointer thread_id.
        lock = self._get_session_lock(session_id)
        async with lock:
            try:
                from langgraph.types import Command
                config = self._build_config(session_id, run_name="operator_resume", extra_tags=("human_mode",))
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
        config = self._build_config(session_id, run_name="sync_history")
        # Only the NEW events are written: `BotState.messages` carries the
        # `add_messages` reducer, which merges this delta into the checkpointed
        # history. (Before the reducer this method had to read the whole
        # history and write it back, which is also why it no longer needs a
        # checkpoint read at all.)
        msgs: list = []
        for event in events:
            role = (event.get("role") or "").strip().lower()
            text = (event.get("text") or "").strip()
            if not text:
                continue
            if role in {"user", "human"}:
                msgs.append(HumanMessage(content=mask_pii(text)))
            elif role in {"assistant", "agent", "operator", "bot", "ai"}:
                msgs.append(AIMessage(content=text))
        if not msgs:
            return
        try:
            # `as_node` is REQUIRED here. Without it LangGraph rejects the write
            # with "Ambiguous update, specify as_node" on any session that
            # already has a checkpoint — which silently broke every sync after
            # the very first one (the failure was caught and logged below, so
            # operator history simply stopped reaching the graph).
            #
            # "human_mode" is the honest choice: this method is only ever called
            # when a session LEAVES operator mode (see
            # ChatService.sync_human_mode_history_to_agent's two call sites), so
            # attributing the operator's messages to that node is accurate, and
            # it clears the `interrupt()` that node may still be parked on —
            # exactly what returning to bot mode should do.
            await self._graph.aupdate_state(config, {"messages": msgs}, as_node="human_mode")
        except Exception as exc:
            _agent_logger.warning("Failed to sync history for session %s: %s", session_id, exc)

    async def aclose(self) -> None:
        if self._store_cm is not None:
            try:
                await self._store_cm.__aexit__(None, None, None)
            except Exception as exc:
                _agent_logger.warning("Failed to close store: %s", exc)
        if self._checkpointer_cm is not None:
            try:
                await self._checkpointer_cm.__aexit__(None, None, None)
            except Exception as exc:
                _agent_logger.warning("Failed to close checkpointer: %s", exc)
