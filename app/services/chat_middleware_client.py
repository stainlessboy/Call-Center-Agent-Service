"""
Client for Chat Middleware System (Cisco UCCX).
Manages Socket.IO connections for each session in human_mode.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional

import httpx
import socketio

logger = logging.getLogger(__name__)


@dataclass
class ChatConnection:
    """One active Socket.IO connection = one operator chat."""
    session_id: str
    sio: socketio.AsyncClient
    jwt_token: str
    chat_active: bool = False
    agent_name: Optional[str] = None
    connected_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class ChatMiddlewareClient:
    """
    Manages connections to Chat Middleware System.
    One instance per application, holds dict[session_id -> ChatConnection].
    """

    def __init__(
        self,
        middleware_url: str,
        login: str,
        password: str,
        csq: str,
        on_agent_message: Callable[..., Awaitable],
        on_agent_joined: Callable[..., Awaitable],
        on_chat_ended: Callable[..., Awaitable],
        on_error: Callable[..., Awaitable],
        nginx_ws_url: Optional[str] = None,
    ):
        self.middleware_url = middleware_url.rstrip("/")
        self.login = login
        self.password = password
        self.csq = csq
        self.on_agent_message = on_agent_message
        self.on_agent_joined = on_agent_joined
        self.on_chat_ended = on_chat_ended
        self.on_error = on_error
        self.ws_url = nginx_ws_url or middleware_url

        self._connections: dict[str, ChatConnection] = {}
        self._http = httpx.AsyncClient(timeout=10)

    # ─── JWT ──────────────────────────────────────────────

    async def _get_jwt_token(self) -> str:
        resp = await self._http.post(
            f"{self.middleware_url}/api/users/login",
            json={"login": self.login, "password": self.password},
        )
        resp.raise_for_status()
        return resp.json()["token"]

    # ─── Start chat ───────────────────────────────────────

    async def start_chat(
        self,
        session_id: str,
        user_name: str,
        initial_message: str,
    ) -> bool:
        if session_id in self._connections:
            return False

        try:
            token = await self._get_jwt_token()

            sio = socketio.AsyncClient(
                reconnection=True,
                reconnection_attempts=3,
                reconnection_delay=2,
            )

            conn = ChatConnection(
                session_id=session_id,
                sio=sio,
                jwt_token=token,
            )
            self._connections[session_id] = conn
            self._register_handlers(sio, session_id)

            await sio.connect(
                self.ws_url,
                socketio_path="/api/ws/chats/",
                transports=["websocket"],
                auth={"token": token},
            )

            await sio.emit("start-chat", {
                "csq": self.csq,
                "message": initial_message,
                "customerName": user_name,
            })

            logger.info("Chat started for session %s", session_id)
            return True

        except Exception as exc:
            logger.exception("Failed to start middleware chat: %s", exc)
            self._connections.pop(session_id, None)
            return False

    # ─── Send message ─────────────────────────────────────

    async def send_message(self, session_id: str, text: str) -> bool:
        conn = self._connections.get(session_id)
        if not conn or not conn.chat_active:
            return False
        try:
            await conn.sio.emit("send-message", {"message": text})
            return True
        except Exception as exc:
            logger.exception("Failed to send message to middleware: %s", exc)
            return False

    # ─── End chat ─────────────────────────────────────────

    async def end_chat(self, session_id: str) -> None:
        conn = self._connections.pop(session_id, None)
        if conn is None:
            return
        try:
            if conn.chat_active:
                await conn.sio.emit("send-leave", {})
                await asyncio.sleep(0.5)
            await conn.sio.disconnect()
        except Exception as exc:
            logger.warning("Error ending middleware chat: %s", exc)

    # ─── Socket.IO handlers ──────────────────────────────

    def _register_handlers(self, sio: socketio.AsyncClient, session_id: str):

        @sio.on("chat-event")
        async def on_chat_event(data):
            event_type = data.get("type", "")

            if event_type == "chat_initialized":
                conn = self._connections.get(session_id)
                if conn:
                    conn.chat_active = True

            elif event_type == "chat_agent_joined":
                agent_name = data.get("agent", "Оператор")
                conn = self._connections.get(session_id)
                if conn:
                    conn.agent_name = agent_name
                await self.on_agent_joined(session_id, agent_name)

            elif event_type == "chat_message":
                if data.get("from") == "agent":
                    await self.on_agent_message(session_id, data.get("text", ""))

            elif event_type in ("chat_left", "chat_closed", "chat_ended"):
                reason = data.get("reason", event_type)
                await self.on_chat_ended(session_id, reason)
                await self._cleanup(session_id)

        @sio.on("channel-errors")
        async def on_error(data):
            code = data.get("code", "unknown")
            await self.on_error(session_id, code)
            await self._cleanup(session_id)

        @sio.on("disconnect")
        async def on_disconnect():
            logger.info("Middleware disconnected for session %s", session_id)

    async def _cleanup(self, session_id: str) -> None:
        conn = self._connections.pop(session_id, None)
        if conn:
            conn.chat_active = False
            try:
                await conn.sio.disconnect()
            except Exception:
                pass

    # ─── Lifecycle ────────────────────────────────────────

    async def close_all(self) -> None:
        for session_id in list(self._connections.keys()):
            await self.end_chat(session_id)
        await self._http.aclose()

    def has_active_chat(self, session_id: str) -> bool:
        conn = self._connections.get(session_id)
        return conn is not None and conn.chat_active

    def get_agent_name(self, session_id: str) -> Optional[str]:
        conn = self._connections.get(session_id)
        return conn.agent_name if conn else None
