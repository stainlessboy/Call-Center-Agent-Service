from __future__ import annotations

import time
from collections import defaultdict, deque
from typing import Any, Awaitable, Callable, Dict

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from app.bot.i18n import normalize_lang, t


class RateLimitMiddleware(BaseMiddleware):
    """Per-user sliding-window rate limiter.

    In-memory deque[timestamp] per telegram user. We run before
    ChatServiceMiddleware so blocked requests never touch the DB or agent.
    On exceed: short reply, no downstream handler call.
    """

    def __init__(self, max_per_minute: int):
        super().__init__()
        self.max = max(int(max_per_minute), 0)
        self.window = 60.0
        self._hits: dict[int, deque[float]] = defaultdict(deque)

    def _allow(self, user_id: int) -> bool:
        if self.max == 0:
            return True
        now = time.monotonic()
        bucket = self._hits[user_id]
        cutoff = now - self.window
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) >= self.max:
            return False
        bucket.append(now)
        return True

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        from_user = getattr(event, "from_user", None)
        if from_user is None or not getattr(from_user, "id", None):
            return await handler(event, data)

        if self._allow(from_user.id):
            return await handler(event, data)

        lang = normalize_lang(getattr(from_user, "language_code", None))
        warn = t("rate_limit_exceeded", lang)
        if isinstance(event, Message):
            try:
                await event.answer(warn)
            except Exception:
                pass
        elif isinstance(event, CallbackQuery):
            try:
                await event.answer(warn, show_alert=False)
            except Exception:
                pass
        return None
