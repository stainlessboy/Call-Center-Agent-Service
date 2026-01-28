from __future__ import annotations

from app.services.local_agent import LocalAgent


class AgentClient:
    """
    Always uses the built-in local agent (langgraph + OpenAI).
    """

    def __init__(self) -> None:
        self._local_agent = LocalAgent()

    async def send_message(self, session_id: str, user_id: int, text: str) -> str:
        return await self._local_agent.send_message(session_id=session_id, user_id=user_id, text=text)

    async def aclose(self) -> None:
        # Nothing to close for the in-process agent.
        return None
