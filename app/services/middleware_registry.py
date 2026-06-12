"""Process-wide handle to the ChatMiddlewareClient.

The client is created in the FastAPI lifespan, but it is also needed from the
service layer and aiogram handlers. Importing ``app.api.fastapi_app`` from
those modules creates an import cycle and silently yields ``None`` outside the
web process, so the instance is registered here instead.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from app.services.chat_middleware_client import ChatMiddlewareClient

_client: Optional["ChatMiddlewareClient"] = None


def set_middleware_client(client: Optional["ChatMiddlewareClient"]) -> None:
    global _client
    _client = client


def get_middleware_client() -> Optional["ChatMiddlewareClient"]:
    return _client
