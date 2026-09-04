from __future__ import annotations

import asyncio
import hmac
import logging
import os
from contextlib import asynccontextmanager, suppress
from typing import Optional

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import MenuButtonWebApp, Update, WebAppInfo
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import text

from app.bot.i18n import normalize_lang, t
from app.bot.handlers import commands as command_handlers
from app.bot.keyboards.feedback import feedback_keyboard
from app.bot.middlewares.chat_service import ChatServiceMiddleware
from app.bot.middlewares.rate_limit import RateLimitMiddleware
from app.config import get_settings
from app.db.events import register_faq_embedding_events
from app.utils import vector_store
from app.db.session import AsyncSessionLocal
from app.admin.setup import setup_admin
from app.miniapp.hub import hub as miniapp_hub
from app.miniapp.routes import router as miniapp_router
from app.miniapp.static import mount_miniapp_static
from app.services.agent_client import AgentClient
from app.services.chat_service import ChatService
from app.services.chat_middleware_client import ChatMiddlewareClient
from app.services.middleware_files import download_and_send_to_user
from app.services.middleware_registry import set_middleware_client

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

    register_faq_embedding_events()

    # FAQ-индекс. ensure_schema() никогда не роняет старт: если Weaviate
    # недоступен, поиск деградирует до отсутствия кандидатов, а не до
    # упавшего приложения.
    with suppress(Exception):
        if await vector_store.ensure_schema():
            logger.info("weaviate FAQ index ready")
        else:
            logger.warning("weaviate FAQ index unavailable — FAQ search degraded")

    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()

    agent_client = AgentClient()
    await agent_client.setup(settings)
    chat_service = ChatService(AsyncSessionLocal, agent_client)
    rate_limit_mw = RateLimitMiddleware(max_per_minute=int(settings.rate_limit_per_minute))
    dp.message.middleware(rate_limit_mw)
    dp.callback_query.middleware(rate_limit_mw)
    chat_service_mw = ChatServiceMiddleware(chat_service)
    dp.message.middleware(chat_service_mw)
    dp.callback_query.middleware(chat_service_mw)
    dp.include_router(command_handlers.router)

    # ── Chat Middleware (operator handoff via Socket.IO) ──
    middleware_client: ChatMiddlewareClient | None = None
    if settings.middleware_enabled and settings.middleware_url and settings.middleware_login and settings.middleware_password:

        async def _on_agent_joined(session_id: str, agent_name: str | None):
            data = await chat_service.get_session_with_user(session_id)
            if not data:
                return
            _, user = data
            lang = normalize_lang(user.language)
            await bot.send_message(
                chat_id=user.telegram_user_id,
                text=t("operator_connected", lang),
            )
            await miniapp_hub.publish(
                session_id, "operator_joined", operator_name=agent_name or "", mode="operator"
            )

        async def _on_agent_message(session_id: str, text: str):
            data = await chat_service.get_session_with_user(session_id)
            if not data:
                return
            _, user = data
            if not text:
                return
            await chat_service._save_message(session_id, role="operator", text=text)
            await bot.send_message(chat_id=user.telegram_user_id, text=text)
            await miniapp_hub.publish(session_id, "operator_message", text=text)
            try:
                await agent_client.resume_human_mode(session_id, text)
            except Exception:
                pass

        async def _on_agent_file(session_id: str, file_url: str):
            data = await chat_service.get_session_with_user(session_id)
            if not data:
                return
            _, user = data
            await chat_service._save_message(session_id, role="operator", text=f"[file] {file_url}")
            await download_and_send_to_user(bot, user.telegram_user_id, file_url)
            await miniapp_hub.publish(session_id, "operator_file", url=file_url)

        async def _on_chat_ended(session_id: str, reason: str):
            data = await chat_service.get_session_with_user(session_id)
            if not data:
                return
            _, user = data
            lang = normalize_lang(user.language)
            if reason == "operator_left":
                await chat_service.end_active_session(user.id, reason="operator_left")
                await bot.send_message(
                    chat_id=user.telegram_user_id,
                    text=t("operator_chat_ended_rate", lang),
                    reply_markup=feedback_keyboard(session_id),
                )
                await miniapp_hub.publish(
                    session_id, "operator_left", ask_rating=True, mode="bot"
                )
                return
            await chat_service.set_human_mode(session_id, False)
            if reason == "chat_finished_error":
                msg = t("chat_ended_try_again", lang)
            elif reason == "timeout":
                msg = t("operator_wait_timeout", lang)
            else:
                msg = t("chat_ended", lang)
            await bot.send_message(chat_id=user.telegram_user_id, text=msg)
            await miniapp_hub.publish(
                session_id, "chat_ended", reason=reason, message=msg, mode="bot"
            )

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
            elif error_code == "start_error":
                msg = t("chat_ended_try_again", lang)
            else:
                msg = t("all_operators_busy", lang)
            await bot.send_message(chat_id=user.telegram_user_id, text=msg)
            await miniapp_hub.publish(
                session_id, "operator_error", code=error_code, message=msg, mode="bot"
            )

        async def _on_inactivity_warning(session_id: str):
            data = await chat_service.get_session_with_user(session_id)
            if not data:
                return
            _, user = data
            lang = normalize_lang(user.language)
            await bot.send_message(
                chat_id=user.telegram_user_id,
                text=t("chat_inactivity_warning", lang),
            )
            await miniapp_hub.publish(
                session_id, "inactivity_warning", message=t("chat_inactivity_warning", lang)
            )

        middleware_client = ChatMiddlewareClient(
            middleware_url=settings.middleware_url,
            login=settings.middleware_login,
            password=settings.middleware_password,
            on_agent_message=_on_agent_message,
            on_agent_joined=_on_agent_joined,
            on_chat_ended=_on_chat_ended,
            on_error=_on_error,
            on_agent_file=_on_agent_file,
            on_inactivity_warning=_on_inactivity_warning,
            is_test_request=settings.middleware_is_test_request,
            nginx_ws_url=settings.middleware_nginx_ws_url,
            verify_ssl=settings.middleware_verify_ssl,
        )
        logger.info("Chat Middleware client initialized (url=%s)", settings.middleware_url)
    elif settings.middleware_enabled:
        logger.warning("MIDDLEWARE_ENABLED=true but missing URL/LOGIN/PASSWORD — middleware disabled")

    app.state.middleware_client = middleware_client
    set_middleware_client(middleware_client)
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

    # Telegram menu button → Mini App. Telegram only accepts https URLs, so a
    # localhost dev server is reached through the browser instead.
    if settings.miniapp_enabled and settings.miniapp_url:
        try:
            await bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(
                    text="Asakabank",
                    web_app=WebAppInfo(url=settings.miniapp_url),
                )
            )
            logger.info("Mini App menu button set: %s", settings.miniapp_url)
        except Exception as exc:
            logger.warning("Failed to set Mini App menu button: %s", exc)

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
        set_middleware_client(None)
        if middleware_client:
            with suppress(Exception):
                await middleware_client.close_all()
        with suppress(Exception):
            await vector_store.close()
        with suppress(Exception):
            await agent_client.aclose()
        with suppress(Exception):
            await bot.session.close()


app = FastAPI(title="Finance Bot API", lifespan=lifespan)
setup_admin(app)
WEBHOOK_PATH = get_settings().webhook_path

if get_settings().miniapp_enabled:
    app.include_router(miniapp_router)
    if get_settings().miniapp_dev_mode:
        # The Vite dev server runs on another origin, so browser testing needs
        # CORS. Never on in production — dev mode already bypasses initData.
        from fastapi.middleware.cors import CORSMiddleware

        app.add_middleware(
            CORSMiddleware,
            allow_origin_regex=r"http://(localhost|127\.0\.0\.1)(:\d+)?",
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )
        logger.warning("MINIAPP_DEV_MODE is on — Mini App API accepts unsigned requests")


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
    if settings.webhook_secret and not hmac.compare_digest(
        x_telegram_bot_api_secret_token or "", settings.webhook_secret
    ):
        raise HTTPException(status_code=403, detail="Invalid webhook secret")

    bot: Optional[Bot] = getattr(request.app.state, "bot", None)
    dp: Optional[Dispatcher] = getattr(request.app.state, "dp", None)
    if bot is None or dp is None:
        raise HTTPException(status_code=503, detail="Bot runtime is not ready")

    data = await request.json()
    try:
        update = Update.model_validate(data, context={"bot": bot})
    except Exception as exc:
        logger.warning("Invalid webhook update payload: %s", exc)
        raise HTTPException(status_code=400, detail="Invalid update payload") from exc
    try:
        await dp.feed_update(bot, update)
    except Exception as exc:
        logger.exception("Webhook handler error: %s", exc)
    return {"ok": True}




# Mounted last so the SPA fallback never shadows an API route.
if get_settings().miniapp_enabled:

    @app.get("/", include_in_schema=False)
    async def root_to_miniapp() -> RedirectResponse:
        """Telegram clients cache the menu-button URL, so a stale link can point
        at the bare domain. Send it to the app instead of a 404."""
        return RedirectResponse(url="/app/")

    mount_miniapp_static(app)
