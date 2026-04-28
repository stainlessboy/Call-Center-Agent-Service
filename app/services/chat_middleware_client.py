"""
Chat Middleware client (Asaka chat-middleware).

Реализация 1:1 с эталонным `call_center_bot/services/chat.py`:

- POST /api/users/login → access_token (кэш на 1 час, под asyncio.Lock).
- Socket.IO: токен передаётся через query-string `?token=...`,
  `socketio_path="/api/ws/chats"`, transport=websocket, verify_ssl=False,
  reconnection=False.
- emit "start-chat" {userPhone, userName, lang, requestId, telegramId, isTestRequest}
- emit "send-message" {requestId, message}
- emit "send-leave"   {requestId}
- on "chat-event": eventData.{type,status,from,body}
    * MessageEvent (from != "me")             → on_agent_message
        - body начинается с "file_path:/"     → on_agent_file(url)
        - body содержит "get.asakabank.uz/"   → on_agent_file(url)
    * PresenceEvent / status=joined           → on_agent_joined + chat_active=True + start inactivity
    * PresenceEvent / status=left             → on_chat_ended("operator_left") + cleanup
    * StatusEvent  / status=chat_finished_error → on_chat_ended("chat_finished_error") + cleanup
    * type=start-error                        → on_error("start_error") + cleanup
    * status=chat_timedout_waiting_for_agent  → on_error(...) + cleanup
    * status=chat_request_rejected_by_agent   → on_error(...) + cleanup

Поддерживаемые расширения нашей стороны:
- per-session inactivity warning через 240 сек (как в эталоне).
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional

import aiohttp
import socketio

logger = logging.getLogger(__name__)


INACTIVITY_WARNING_TIMEOUT = 240  # секунды, как в call_center_bot


AgentMessageCb = Callable[[str, str], Awaitable[None]]                  # (session_id, text)
AgentFileCb    = Callable[[str, str], Awaitable[None]]                  # (session_id, file_url)
AgentJoinedCb  = Callable[[str, Optional[str]], Awaitable[None]]        # (session_id, agent_name|None)
ChatEndedCb    = Callable[[str, str], Awaitable[None]]                  # (session_id, reason)
ErrorCb        = Callable[[str, str], Awaitable[None]]                  # (session_id, code)
InactivityCb   = Callable[[str], Awaitable[None]]                       # (session_id)


@dataclass
class ChatConnection:
    """Одно активное Socket.IO-соединение = один чат с оператором."""
    session_id: str
    request_id: str          # = userPhone, нужен в каждом emit
    sio: socketio.AsyncClient
    chat_active: bool = False
    agent_name: Optional[str] = None
    inactivity_task: Optional[asyncio.Task] = None
    connected_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class ChatMiddlewareClient:
    """
    Один экземпляр на приложение, держит dict[session_id -> ChatConnection].

    JWT-токен общий, кэшируется на 1 час под asyncio.Lock.
    """

    _TOKEN_TTL_SECONDS = 3600

    def __init__(
        self,
        middleware_url: str,
        login: str,
        password: str,
        on_agent_message: AgentMessageCb,
        on_agent_joined: AgentJoinedCb,
        on_chat_ended: ChatEndedCb,
        on_error: ErrorCb,
        on_agent_file: Optional[AgentFileCb] = None,
        on_inactivity_warning: Optional[InactivityCb] = None,
        is_test_request: bool = False,
        nginx_ws_url: Optional[str] = None,
        verify_ssl: bool = False,
    ):
        self.middleware_url = middleware_url.rstrip("/")
        self.ws_url = (nginx_ws_url or middleware_url).rstrip("/")
        self.login = login
        self.password = password
        self.is_test_request = is_test_request
        self.verify_ssl = verify_ssl

        self.on_agent_message = on_agent_message
        self.on_agent_joined = on_agent_joined
        self.on_chat_ended = on_chat_ended
        self.on_error = on_error
        self.on_agent_file = on_agent_file
        self.on_inactivity_warning = on_inactivity_warning

        self._connections: dict[str, ChatConnection] = {}

        self._cached_token: Optional[str] = None
        self._token_expires_at: float = 0.0
        self._token_lock = asyncio.Lock()

    # ─── JWT ────────────────────────────────────────────────────

    async def _fetch_token(self) -> str:
        connector = aiohttp.TCPConnector(verify_ssl=self.verify_ssl)
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.post(
                f"{self.middleware_url}/api/users/login",
                json={"login": self.login, "password": self.password},
            ) as res:
                if res.status != 200:
                    raise Exception(f"Chat middleware auth error: HTTP {res.status}")
                data = await res.json()
                token = data.get("access_token")
                if not token:
                    raise Exception("Chat middleware auth: 'access_token' missing in response")
                return token

    async def get_token(self) -> str:
        """Кэшируем токен на 1 час, при ошибке — fallback на просроченный."""
        async with self._token_lock:
            now = time.time()
            if self._cached_token and now < self._token_expires_at:
                return self._cached_token
            try:
                self._cached_token = await self._fetch_token()
                self._token_expires_at = now + self._TOKEN_TTL_SECONDS
                logger.info("New middleware JWT obtained, expires in %ss", self._TOKEN_TTL_SECONDS)
                return self._cached_token
            except Exception:
                if self._cached_token:
                    logger.warning("Failed to refresh middleware JWT — using stale cached token")
                    return self._cached_token
                raise

    async def clear_token_cache(self) -> None:
        async with self._token_lock:
            self._cached_token = None
            self._token_expires_at = 0.0

    # ─── Inactivity timer ──────────────────────────────────────

    async def _inactivity_task(self, session_id: str) -> None:
        try:
            await asyncio.sleep(INACTIVITY_WARNING_TIMEOUT)
            if self.on_inactivity_warning is not None:
                try:
                    await self.on_inactivity_warning(session_id)
                except Exception:
                    logger.exception("Inactivity warning callback failed for %s", session_id)
        except asyncio.CancelledError:
            raise

    def _start_inactivity_timer(self, session_id: str) -> None:
        conn = self._connections.get(session_id)
        if conn is None:
            return
        self._cancel_inactivity_timer(session_id)
        conn.inactivity_task = asyncio.create_task(self._inactivity_task(session_id))

    def _cancel_inactivity_timer(self, session_id: str) -> None:
        conn = self._connections.get(session_id)
        if conn is None or conn.inactivity_task is None:
            return
        conn.inactivity_task.cancel()
        conn.inactivity_task = None

    # ─── Start chat ─────────────────────────────────────────────

    async def start_chat(
        self,
        session_id: str,
        phone: str,
        user_name: str,
        lang: str,
        telegram_id: int,
    ) -> bool:
        """
        Поднимает Socket.IO к middleware и шлёт `start-chat`.
        Возвращает True если сокет подключён (оператор может ещё не подсоединиться).
        """
        if session_id in self._connections:
            return False

        try:
            token = await self.get_token()
            socket_url = f"{self.ws_url}/api/ws/chats?token={token}"

            connector = aiohttp.TCPConnector(verify_ssl=self.verify_ssl)
            http_session = aiohttp.ClientSession(connector=connector)

            sio = socketio.AsyncClient(http_session=http_session, reconnection=False)

            conn = ChatConnection(session_id=session_id, request_id=phone, sio=sio)
            self._connections[session_id] = conn
            self._register_handlers(sio, session_id)

            await asyncio.wait_for(
                sio.connect(
                    socket_url,
                    transports=["websocket"],
                    socketio_path="/api/ws/chats",
                ),
                timeout=30,
            )

            await sio.emit("start-chat", {
                "userPhone":     phone,
                "userName":      user_name,
                "lang":          lang,
                "requestId":     phone,
                "telegramId":    str(telegram_id),
                "isTestRequest": self.is_test_request,
            })

            logger.info("Middleware chat started: session=%s phone=%s", session_id, phone)
            return True

        except asyncio.TimeoutError:
            logger.error("Middleware connect timeout for session %s", session_id)
            self._connections.pop(session_id, None)
            return False
        except Exception as exc:
            logger.exception("Failed to start middleware chat for session %s: %s", session_id, exc)
            self._connections.pop(session_id, None)
            return False

    # ─── Send / leave ───────────────────────────────────────────

    async def send_message(self, session_id: str, text: str) -> bool:
        conn = self._connections.get(session_id)
        if not conn or not conn.sio.connected:
            return False
        try:
            await conn.sio.emit("send-message", {
                "requestId": conn.request_id,
                "message":   text,
            })
            self._start_inactivity_timer(session_id)
            return True
        except Exception as exc:
            logger.exception("Failed to send-message to middleware: %s", exc)
            return False

    async def end_chat(self, session_id: str) -> None:
        conn = self._connections.pop(session_id, None)
        if conn is None:
            return
        if conn.inactivity_task is not None:
            conn.inactivity_task.cancel()
        try:
            if conn.sio.connected:
                try:
                    await conn.sio.emit("send-leave", {"requestId": conn.request_id})
                except Exception:
                    pass
                await conn.sio.disconnect()
        except Exception as exc:
            logger.warning("Error ending middleware chat: %s", exc)

    # ─── Socket.IO handlers ─────────────────────────────────────

    def _register_handlers(self, sio: socketio.AsyncClient, session_id: str) -> None:

        @sio.on("chat-event")
        async def on_chat_event(event):
            try:
                event_data   = event.get("eventData", {}) or {}
                event_type   = event_data.get("type")
                event_status = event_data.get("status")
                event_from   = event_data.get("from")
                body         = event_data.get("body", "") or ""

                # Сообщение от оператора
                if event_type == "MessageEvent" and event_from != "me":
                    self._start_inactivity_timer(session_id)
                    if isinstance(body, str) and body.startswith("file_path:/"):
                        url = body[10:].strip()
                        if url and self.on_agent_file is not None:
                            await self.on_agent_file(session_id, url)
                        elif url:
                            await self.on_agent_message(session_id, url)
                    elif isinstance(body, str) and "get.asakabank.uz/" in body:
                        if self.on_agent_file is not None:
                            await self.on_agent_file(session_id, body.strip())
                        else:
                            await self.on_agent_message(session_id, body.strip())
                    else:
                        await self.on_agent_message(session_id, body)

                # Оператор присоединился
                elif event_type == "PresenceEvent" and event_status == "joined":
                    conn = self._connections.get(session_id)
                    if conn:
                        conn.chat_active = True
                    self._start_inactivity_timer(session_id)
                    await self.on_agent_joined(session_id, None)

                # Оператор отключился
                elif event_type == "PresenceEvent" and event_status == "left":
                    self._cancel_inactivity_timer(session_id)
                    await self.on_chat_ended(session_id, "operator_left")
                    await self._cleanup(session_id)

                # Чат завершён с ошибкой
                elif event_type == "StatusEvent" and event_status == "chat_finished_error":
                    self._cancel_inactivity_timer(session_id)
                    await self.on_chat_ended(session_id, "chat_finished_error")
                    await self._cleanup(session_id)

                # Ошибка старта
                elif event_type == "start-error":
                    self._cancel_inactivity_timer(session_id)
                    await self.on_error(session_id, "start_error")
                    await self._cleanup(session_id)

                # Все операторы заняты / отказ
                elif event_status in (
                    "chat_timedout_waiting_for_agent",
                    "chat_request_rejected_by_agent",
                ):
                    self._cancel_inactivity_timer(session_id)
                    await self.on_error(session_id, event_status)
                    await self._cleanup(session_id)

                else:
                    logger.debug("Unhandled chat-event for %s: %s", session_id, event)
            except Exception:
                logger.exception("Error handling chat-event for %s", session_id)

        @sio.on("connect")
        async def on_connect():
            logger.info("Middleware socket connected for %s", session_id)

        @sio.on("disconnect")
        async def on_disconnect():
            logger.info("Middleware socket disconnected for %s", session_id)

        @sio.on("connect_error")
        async def on_connect_error(data):
            logger.error("Middleware connection error for %s: %s", session_id, data)

    # ─── Internal cleanup ───────────────────────────────────────

    async def _cleanup(self, session_id: str) -> None:
        conn = self._connections.pop(session_id, None)
        if conn is None:
            return
        if conn.inactivity_task is not None:
            conn.inactivity_task.cancel()
        try:
            if conn.sio.connected:
                await conn.sio.disconnect()
        except Exception:
            pass

    # ─── Lifecycle ──────────────────────────────────────────────

    async def close_all(self) -> None:
        for sid in list(self._connections.keys()):
            await self.end_chat(sid)

    def has_active_chat(self, session_id: str) -> bool:
        conn = self._connections.get(session_id)
        return conn is not None and conn.chat_active

    def get_agent_name(self, session_id: str) -> Optional[str]:
        conn = self._connections.get(session_id)
        return conn.agent_name if conn else None

    def get_request_id(self, session_id: str) -> Optional[str]:
        conn = self._connections.get(session_id)
        return conn.request_id if conn else None
