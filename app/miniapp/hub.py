"""Process-wide fan-out of session events to connected Mini App clients.

Operator replies arrive from the Asaka chat-middleware over Socket.IO and are
delivered to Telegram by the callbacks in ``app/api/fastapi_app.py``. The same
callbacks publish here so a user sitting in the Mini App sees the reply without
a Telegram message.

Sockets are held in memory, which is correct for the current single-process
deployment. Running multiple uvicorn workers would need a shared broker
(Redis pub/sub) behind the same ``publish`` signature.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class MiniAppHub:
    def __init__(self) -> None:
        self._connections: dict[str, set[WebSocket]] = {}
        self._lock = asyncio.Lock()

    async def connect(self, session_id: str, websocket: WebSocket) -> None:
        async with self._lock:
            self._connections.setdefault(session_id, set()).add(websocket)
        logger.debug("Mini App socket attached to session %s", session_id)

    async def disconnect(self, session_id: str, websocket: WebSocket) -> None:
        async with self._lock:
            sockets = self._connections.get(session_id)
            if not sockets:
                return
            sockets.discard(websocket)
            if not sockets:
                self._connections.pop(session_id, None)

    def has_listeners(self, session_id: str) -> bool:
        return bool(self._connections.get(session_id))

    async def publish(self, session_id: str, event: str, **data: Any) -> int:
        """Send ``{event, ...data}`` to every socket of *session_id*.

        Returns how many sockets received it. Dead sockets are dropped rather
        than raising — the caller is a middleware callback that must not fail
        because a browser tab went away.
        """
        async with self._lock:
            sockets = list(self._connections.get(session_id) or ())
        if not sockets:
            return 0

        payload = {"event": event, **data}
        delivered = 0
        stale: list[WebSocket] = []
        for socket in sockets:
            try:
                await socket.send_json(payload)
                delivered += 1
            except Exception:
                stale.append(socket)
        for socket in stale:
            await self.disconnect(session_id, socket)
        return delivered


hub = MiniAppHub()
