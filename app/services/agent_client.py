from __future__ import annotations

from typing import TYPE_CHECKING, Sequence

from app.agent import Agent, AgentTurnResult

if TYPE_CHECKING:
    from app.config import Settings


class AgentClient:
    """
    Always uses the built-in local agent (langgraph + OpenAI).
    """

    def __init__(self) -> None:
        self._agent = Agent()

    async def setup(self, settings: Settings) -> None:
        """Initialize async checkpointer. Call once at startup."""
        await self._agent.setup(
            backend=settings.langgraph_checkpoint_backend,
            url=settings.langgraph_checkpoint_url,
        )

    async def send_message(
        self,
        session_id: str,
        user_id: int,
        text: str,
        language: str | None = None,
        human_mode: bool = False,
        channel: str | None = None,
    ) -> AgentTurnResult:
        """`channel` is observability-only ("telegram" | "miniapp"): it lands
        in the run's LangSmith metadata/tags (app/agent/tracing.py) and never
        changes how a turn is processed."""
        return await self._agent.send_message(
            session_id=session_id,
            user_id=user_id,
            text=text,
            language=language,
            human_mode=human_mode,
            channel=channel,
        )

    async def get_handoff_context(self, session_id: str) -> dict:
        """Read-only dialog/profile/messages snapshot for the operator
        handoff summary (app/agent/handoff.py). See Agent.get_handoff_context
        for the never-raises contract."""
        return await self._agent.get_handoff_context(session_id=session_id)

    async def resume_human_mode(self, session_id: str, operator_reply: str) -> str:
        """Resume a graph interrupted in human_mode, injecting operator reply."""
        return await self._agent.resume_human_mode(
            session_id=session_id,
            operator_reply=operator_reply,
        )

    async def sync_history(self, session_id: str, events: Sequence[dict[str, str]]) -> None:
        await self._agent.sync_history(session_id=session_id, events=events)

    async def aclose(self) -> None:
        await self._agent.aclose()

    async def get_pending_interrupt(self, session_id: str) -> dict | None:
        """What this session's graph is paused on, if anything — see
        `Agent.get_pending_interrupt` (roadmap П.6)."""
        return await self._agent.get_pending_interrupt(session_id=session_id)

    async def resume_pending_interrupt(self, session_id: str, decision) -> dict | None:
        """Resume a paused graph with an operator reply or an approval
        decision — see `Agent.resume_pending_interrupt`."""
        return await self._agent.resume_pending_interrupt(
            session_id=session_id, decision=decision,
        )
