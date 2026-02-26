from __future__ import annotations

from typing import TYPE_CHECKING, Sequence

from app.services.agent import Agent, AgentTurnResult

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
    ) -> AgentTurnResult:
        return await self._agent.send_message(
            session_id=session_id,
            user_id=user_id,
            text=text,
            language=language,
            human_mode=human_mode,
        )

    async def resume_human_mode(self, session_id: str, operator_reply: str) -> str:
        """Resume a graph interrupted in human_mode, injecting operator reply."""
        return await self._agent.resume_human_mode(
            session_id=session_id,
            operator_reply=operator_reply,
        )

    async def ensure_language(self, text: str, language: str | None = None) -> str:
        return await self._agent.ensure_language(text=text, language=language)

    async def sync_history(self, session_id: str, events: Sequence[dict[str, str]]) -> None:
        await self._agent.sync_history(session_id=session_id, events=events)

    async def aclose(self) -> None:
        await self._agent.aclose()
