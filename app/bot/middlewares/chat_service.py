from __future__ import annotations

from typing import Any, Awaitable, Callable, Dict

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

from app.services.chat_service import ChatService


class ChatServiceMiddleware(BaseMiddleware):
    """
    Injects ChatService into handler data, so handlers can declare chat_service: ChatService.
    """

    def __init__(self, chat_service: ChatService):
        super().__init__()
        self.chat_service = chat_service

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        data["chat_service"] = self.chat_service
        return await handler(event, data)
