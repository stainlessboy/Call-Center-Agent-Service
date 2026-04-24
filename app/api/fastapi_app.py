from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager, suppress
from typing import Optional

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import Update
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.bot.i18n import normalize_lang, t
from app.bot.handlers import commands as command_handlers
from app.bot.keyboards.feedback import feedback_keyboard
from app.bot.middlewares.chat_service import ChatServiceMiddleware
from app.config import get_settings
from app.db.session import AsyncSessionLocal
from app.admin.setup import setup_admin
from app.services.agent_client import AgentClient
from app.services.chat_service import ChatService
from app.services.chat_middleware_client import ChatMiddlewareClient

logger = logging.getLogger(__name__)



async def _inactivity_watcher(
    bot: Bot,
    chat_service: ChatService,
    session_timeout_minutes: int,
    human_mode_timeout_minutes: int,
) -> None:
    while True:
        try:
            if session_timeout_minutes > 0:
                closed = await chat_service.close_inactive_sessions(timeout_minutes=session_timeout_minutes)
                for user, session in closed:
                    lang = normalize_lang(user.language)
                    await bot.send_message(
                        chat_id=user.telegram_user_id,
                        text=t("session_closed_timeout", lang),
                        reply_markup=feedback_keyboard(session.id),
                    )
            if human_mode_timeout_minutes > 0:
                switched = await chat_service.return_stale_human_sessions_to_bot(
                    timeout_minutes=human_mode_timeout_minutes
                )
                for user, session in switched:
                    lang = normalize_lang(user.language)
                    await bot.send_message(
                        chat_id=user.telegram_user_id,
                        text=t("human_timeout_back_to_bot", lang, minutes=human_mode_timeout_minutes),
                    )
        except Exception as exc:  # pragma: no cover
            logger.exception("Inactivity watcher error: %s", exc)
        await asyncio.sleep(60)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    if not settings.bot_token:
        raise RuntimeError("BOT_TOKEN is not set")

    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()

    agent_client = AgentClient()
    await agent_client.setup(settings)
    chat_service = ChatService(AsyncSessionLocal, agent_client)
    chat_service_mw = ChatServiceMiddleware(chat_service)
    dp.message.middleware(chat_service_mw)
    dp.callback_query.middleware(chat_service_mw)
    dp.include_router(command_handlers.router)

    # ── Chat Middleware (operator handoff via Socket.IO) ──
    middleware_client: ChatMiddlewareClient | None = None
    if settings.middleware_enabled and settings.middleware_url and settings.middleware_login and settings.middleware_password:

        async def _on_agent_joined(session_id: str, agent_name: str):
            data = await chat_service.get_session_with_user(session_id)
            if not data:
                return
            _, user = data
            lang = normalize_lang(user.language)
            await bot.send_message(
                chat_id=user.telegram_user_id,
                text=t("operator_joined", lang, name=agent_name),
            )

        async def _on_agent_message(session_id: str, text: str):
            data = await chat_service.get_session_with_user(session_id)
            if not data:
                return
            _, user = data
            await chat_service._save_message(session_id, role="operator", text=text)
            name = middleware_client.get_agent_name(session_id) if middleware_client else "Оператор"
            formatted = f"👤 ({name or 'Оператор'}): {text}"
            await bot.send_message(chat_id=user.telegram_user_id, text=formatted)
            try:
                await agent_client.resume_human_mode(session_id, text)
            except Exception:
                pass

        async def _on_chat_ended(session_id: str, reason: str):
            data = await chat_service.get_session_with_user(session_id)
            if not data:
                return
            _, user = data
            lang = normalize_lang(user.language)
            await chat_service.set_human_mode(session_id, False)
            if reason == "timeout":
                msg = t("operator_wait_timeout", lang)
            else:
                msg = t("human_timeout_back_to_bot", lang, minutes=0)
            await bot.send_message(chat_id=user.telegram_user_id, text=msg)

        async def _on_error(session_id: str, error_code: str):
            data = await chat_service.get_session_with_user(session_id)
            if not data:
                return
            _, user = data
            lang = normalize_lang(user.language)
            await chat_service.set_human_mode(session_id, False)
            if error_code == "chat_request_rejected_by_agent":
                msg = t("all_operators_busy", lang)
            elif error_code == "chat_timedout_waiting_for_agent":
                msg = t("operator_wait_timeout", lang)
            else:
                msg = t("all_operators_busy", lang)
            await bot.send_message(chat_id=user.telegram_user_id, text=msg)

        middleware_client = ChatMiddlewareClient(
            middleware_url=settings.middleware_url,
            login=settings.middleware_login,
            password=settings.middleware_password,
            csq=settings.middleware_csq,
            on_agent_message=_on_agent_message,
            on_agent_joined=_on_agent_joined,
            on_chat_ended=_on_chat_ended,
            on_error=_on_error,
            nginx_ws_url=settings.middleware_nginx_ws_url,
        )
        logger.info("Chat Middleware client initialized (url=%s)", settings.middleware_url)
    elif settings.middleware_enabled:
        logger.warning("MIDDLEWARE_ENABLED=true but missing URL/LOGIN/PASSWORD — middleware disabled")

    app.state.middleware_client = middleware_client
    app.state.bot = bot
    app.state.dp = dp
    app.state.chat_service = chat_service
    app.state.agent_client = agent_client
    app.state.inactivity_watcher_task = asyncio.create_task(
        _inactivity_watcher(
            bot,
            chat_service,
            session_timeout_minutes=int(settings.session_inactivity_timeout_minutes),
            human_mode_timeout_minutes=int(settings.human_mode_operator_timeout_minutes),
        )
    )

    if settings.webhook_base_url:
        webhook_url = settings.webhook_base_url.rstrip("/") + settings.webhook_path
        await bot.set_webhook(
            url=webhook_url,
            secret_token=settings.webhook_secret or None,
            drop_pending_updates=True,
        )
        logger.info("Webhook configured: %s", webhook_url)
    else:
        logger.warning("WEBHOOK_BASE_URL is not set. Telegram webhook was not configured.")

    try:
        yield
    finally:
        if settings.webhook_base_url:
            with suppress(Exception):
                await bot.delete_webhook(drop_pending_updates=False)
        watcher = getattr(app.state, "inactivity_watcher_task", None)
        if watcher is not None:
            watcher.cancel()
            with suppress(asyncio.CancelledError):
                await watcher
        if middleware_client:
            with suppress(Exception):
                await middleware_client.close_all()
        with suppress(Exception):
            await agent_client.aclose()
        with suppress(Exception):
            await bot.session.close()


app = FastAPI(title="Finance Bot API", lifespan=lifespan)
setup_admin(app)
WEBHOOK_PATH = get_settings().webhook_path


@app.get("/health")
async def healthcheck() -> JSONResponse:
    status: dict = {"ok": True}
    # Check database connectivity
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(text("SELECT 1"))
        status["db"] = True
    except Exception as exc:
        status["ok"] = False
        status["db"] = False
        status["db_error"] = str(exc)

    # Checkpointer persistence probe. In environments that require a
    # non-in-memory checkpointer (k8s / prod), refuse readiness when the
    # agent ended up on MemorySaver — a pod in this state loses session
    # state on restart and should be rolled rather than kept in service.
    require_persistent = (os.getenv("REQUIRE_PERSISTENT_CHECKPOINTER") or "").strip().lower() in (
        "1", "true", "yes", "on",
    )
    agent_client = getattr(app.state, "agent_client", None)
    checkpointer = getattr(getattr(agent_client, "_agent", None), "_checkpointer", None)
    checkpointer_type = type(checkpointer).__name__ if checkpointer is not None else None
    status["checkpointer"] = checkpointer_type
    if require_persistent and checkpointer_type in (None, "MemorySaver"):
        status["ok"] = False
        status["checkpointer_error"] = (
            "REQUIRE_PERSISTENT_CHECKPOINTER=true but checkpointer is "
            f"{checkpointer_type or 'uninitialized'}"
        )

    http_status = 200 if status["ok"] else 503
    return JSONResponse(status_code=http_status, content=status)


@app.post(WEBHOOK_PATH)
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: Optional[str] = Header(default=None, alias="X-Telegram-Bot-Api-Secret-Token"),
) -> dict[str, bool]:
    settings = get_settings()
    if settings.webhook_secret and x_telegram_bot_api_secret_token != settings.webhook_secret:
        raise HTTPException(status_code=403, detail="Invalid webhook secret")

    bot: Optional[Bot] = getattr(request.app.state, "bot", None)
    dp: Optional[Dispatcher] = getattr(request.app.state, "dp", None)
    if bot is None or dp is None:
        raise HTTPException(status_code=503, detail="Bot runtime is not ready")

    data = await request.json()
    try:
        update = Update.model_validate(data, context={"bot": bot})
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid update payload: {exc}") from exc
    try:
        await dp.feed_update(bot, update)
    except Exception as exc:
        logger.exception("Webhook handler error: %s", exc)
    return {"ok": True}


