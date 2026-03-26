from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone
from typing import Optional

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import Update
from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select, text

from app.bot.i18n import normalize_lang, t
from app.bot.handlers import commands as command_handlers
from app.bot.keyboards.feedback import feedback_keyboard
from app.bot.middlewares.chat_service import ChatServiceMiddleware
from app.config import get_settings
from app.db.models import ChatSession, Message, SessionStatus, User
from app.db.session import AsyncSessionLocal
from app.admin.setup import setup_admin
from app.services.agent_client import AgentClient
from app.services.chat_service import ChatService
from app.services.telegram_sender import send_telegram_message

logger = logging.getLogger(__name__)


class OperatorSendRequest(BaseModel):
    session_id: str = Field(..., min_length=1)
    text: str = Field(..., min_length=1)
    operator_name: Optional[str] = None
    operator_id: Optional[int] = None


class OperatorSendResponse(BaseModel):
    ok: bool
    session_id: str
    user_telegram_id: int


def _format_operator_text(text: str, operator_name: Optional[str]) -> str:
    label = "👤"
    if operator_name:
        label = f"{label} ({operator_name})"
    return f"{label}: {text}"


def _require_api_key(x_api_key: Optional[str]) -> None:
    api_key = (get_settings().operator_api_key or "").strip()
    if not api_key:
        raise HTTPException(status_code=403, detail="OPERATOR_API_KEY not configured")
    if not x_api_key or x_api_key != api_key:
        raise HTTPException(status_code=401, detail="Invalid API key")


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
    chat_service = ChatService(AsyncSessionLocal, agent_client, operator_ids=settings.operator_ids)
    chat_service_mw = ChatServiceMiddleware(chat_service)
    dp.message.middleware(chat_service_mw)
    dp.callback_query.middleware(chat_service_mw)
    dp.include_router(command_handlers.router)

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
        with suppress(Exception):
            await agent_client.aclose()
        with suppress(Exception):
            await bot.session.close()


app = FastAPI(title="Finance Bot API", lifespan=lifespan)
setup_admin(app)
WEBHOOK_PATH = get_settings().webhook_path


@app.get("/health")
async def healthcheck() -> dict:
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
    return status


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


@app.post("/operator/send", response_model=OperatorSendResponse)
async def send_operator(
    payload: OperatorSendRequest,
    request: Request,
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
) -> OperatorSendResponse:
    _require_api_key(x_api_key)

    settings = get_settings()
    if not settings.bot_token:
        raise HTTPException(status_code=500, detail="BOT_TOKEN is not set")

    text = payload.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Text is required")

    async with AsyncSessionLocal() as session:
        async with session.begin():
            result = await session.execute(
                select(ChatSession, User)
                .join(User, ChatSession.user_id == User.id)
                .where(ChatSession.id == payload.session_id)
            )
            row = result.one_or_none()
            if row is None:
                raise HTTPException(status_code=404, detail="Session not found")
            chat_session, user = row
            if chat_session.status != SessionStatus.ACTIVE:
                raise HTTPException(status_code=409, detail="Session is closed")

            chat_session.human_mode = True
            chat_session.human_mode_since = chat_session.human_mode_since or datetime.now(timezone.utc)
            if payload.operator_id is not None:
                chat_session.assigned_operator_id = payload.operator_id
            chat_session.last_activity_at = datetime.now(timezone.utc)
            user_telegram_id = user.telegram_user_id

    text_for_user = _format_operator_text(text, payload.operator_name)

    bot: Optional[Bot] = getattr(request.app.state, "bot", None)
    if bot is not None:
        try:
            await bot.send_message(chat_id=user_telegram_id, text=text_for_user)
        except Exception as exc:  # pragma: no cover
            raise HTTPException(status_code=502, detail=f"Telegram send failed: {exc}") from exc
    else:
        ok, error = send_telegram_message(settings.bot_token, user_telegram_id, text_for_user)
        if not ok:
            raise HTTPException(status_code=502, detail=f"Telegram send failed: {error}")

    async with AsyncSessionLocal() as session:
        async with session.begin():
            session.add(
                Message(
                    session_id=payload.session_id,
                    role="operator",
                    text=text,
                    created_at=datetime.now(timezone.utc),
                )
            )

    return OperatorSendResponse(
        ok=True,
        session_id=payload.session_id,
        user_telegram_id=user_telegram_id,
    )
