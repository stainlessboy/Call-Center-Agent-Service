from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from app.bot.handlers import commands as command_handlers
from app.bot.keyboards.feedback import feedback_keyboard
from app.bot.middlewares.chat_service import ChatServiceMiddleware
from app.config import get_settings
from app.db.session import AsyncSessionLocal
from app.services.agent_client import AgentClient
from app.services.chat_service import ChatService


async def run_bot() -> None:
    settings = get_settings()
    if not settings.bot_token:
        raise RuntimeError("BOT_TOKEN is not set")

    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(levelname)s %(name)s: %(message)s",
    )

    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()

    agent_client = AgentClient()
    chat_service = ChatService(AsyncSessionLocal, agent_client, operator_ids=settings.operator_ids)

    chat_service_mw = ChatServiceMiddleware(chat_service)
    dp.message.middleware(chat_service_mw)
    dp.callback_query.middleware(chat_service_mw)

    dp.include_router(command_handlers.router)

    async def inactivity_watcher():
        while True:
            try:
                closed = await chat_service.close_inactive_sessions(timeout_minutes=5)
                for user, session in closed:
                    await bot.send_message(
                        chat_id=user.telegram_user_id,
                        text="Сессия закрыта из-за отсутствия активности. Оцените работу агента:",
                        reply_markup=feedback_keyboard(session.id),
                    )
            except Exception as exc:  # pragma: no cover
                logging.exception("Inactivity watcher error: %s", exc)
            await asyncio.sleep(60)

    watcher_task = asyncio.create_task(inactivity_watcher())

    try:
        await dp.start_polling(bot)
    finally:
        watcher_task.cancel()
        await agent_client.aclose()


def main() -> None:
    asyncio.run(run_bot())


if __name__ == "__main__":
    main()
