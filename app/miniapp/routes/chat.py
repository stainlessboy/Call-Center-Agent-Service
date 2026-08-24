"""Chat with the assistant, operator handoff and the live event socket."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from app.agent.handoff import send_operator_handoff_summary
from app.agent.streaming import reset_on_token_callback, set_on_token_callback
from app.bot.i18n import t
from app.config import get_settings
from app.db.models import User
from app.miniapp.auth import InitDataError, authenticate
from app.miniapp.deps import get_chat_service, get_db_user, user_lang
from app.miniapp.hub import hub
from app.services.chat_service import ChatService
from app.services.middleware_registry import get_middleware_client
from app.utils.working_hours import is_within_working_hours

logger = logging.getLogger(__name__)
router = APIRouter()


def _make_on_token(session_id: str):
    """Build an `on_token` callback (see app/agent/streaming.py) that
    publishes each chunk to the Mini App WS hub as `{"event":
    "assistant_token", "text": ...}`. A publish failure (no listener, dead
    socket) must never fail the turn — `hub.publish` already swallows
    per-socket errors, so this only guards against something unexpected in
    the publish call itself.
    """
    async def _on_token(text: str) -> None:
        try:
            await hub.publish(session_id, "assistant_token", text=text)
        except Exception:
            logger.debug("assistant_token publish failed for session=%s", session_id, exc_info=True)

    return _on_token


class MessagePayload(BaseModel):
    text: str = Field(min_length=1)
    # Set when the client is viewing a specific conversation. Writing to a
    # closed one answers 410 so the archive screen can offer a new dialog
    # instead of silently starting one behind the user's back.
    session_id: str | None = None


@router.get("/chat/history")
async def history(
    limit: int = 40,
    user: User = Depends(get_db_user),
    chat_service: ChatService = Depends(get_chat_service),
) -> dict:
    session = await chat_service.get_active_session(user.id)
    if session is None:
        return {"session_id": None, "human_mode": False, "messages": []}
    messages = await chat_service.get_recent_messages(
        session.id, limit=max(1, min(limit, 200)), roles=("user", "agent", "operator")
    )
    return {
        "session_id": session.id,
        "human_mode": bool(session.human_mode),
        "messages": [
            {
                "id": m.id,
                "role": m.role,
                "text": m.text,
                "created_at": m.created_at.isoformat() if m.created_at else None,
                # Structured cards for this turn, or [] — see docs/MINIAPP.md
                # "UI blocks". Only ever populated on role="agent" rows.
                "ui_blocks": m.ui_blocks or [],
            }
            for m in messages
        ],
    }


@router.post("/chat/message")
async def send_message(
    payload: MessagePayload,
    user: User = Depends(get_db_user),
    chat_service: ChatService = Depends(get_chat_service),
) -> dict:
    settings = get_settings()
    lang = user_lang(user)
    text = payload.text.strip()
    if not text:
        raise HTTPException(status_code=422, detail="Empty message")
    if len(text) > settings.max_message_length:
        text = text[: settings.max_message_length]

    session = await chat_service.get_active_session(user.id)

    if payload.session_id and (session is None or session.id != payload.session_id):
        raise HTTPException(
            status_code=410,
            detail="Session is closed — start a new dialog to continue",
        )

    in_human_mode = bool(session and session.human_mode)

    # Daily limit applies to bot turns only — a conversation with a live
    # operator is never cut off (same rule as the Telegram handler).
    if not in_human_mode and settings.daily_message_limit > 0:
        used = await chat_service.count_user_messages_today(user.id)
        if used >= settings.daily_message_limit:
            return {
                "blocked": "daily_limit",
                "text": t("daily_limit_reached", lang),
                "quick_replies": [],
                "show_operator_button": True,
                "human_mode": in_human_mode,
            }

    # Live token streaming (Phase 3, node_faq only — see app/agent/streaming.py):
    # armed only for a genuine bot turn on an already-known session — human_mode
    # turns never call the LLM, and a brand-new session's id isn't known until
    # `handle_user_message` creates it below, so there is nothing to publish to
    # yet (the POST response still carries the full text + ui_blocks either
    # way; only the live token trickle is skipped for that one first message).
    stream_session_id = session.id if (session and not in_human_mode and settings.miniapp_streaming_enabled) else None
    stream_token = None
    if stream_session_id:
        stream_token = set_on_token_callback(_make_on_token(stream_session_id))
    try:
        reply = await chat_service.handle_user_message(user=user, text=text)
    finally:
        if stream_token is not None:
            reset_on_token_callback(stream_token)
            try:
                await hub.publish(stream_session_id, "assistant_done")
            except Exception:
                logger.debug("assistant_done publish failed for session=%s", stream_session_id, exc_info=True)

    return {
        "blocked": None,
        "text": reply.text,
        "quick_replies": reply.keyboard_options or [],
        "show_operator_button": bool(reply.show_operator_button),
        "human_mode": bool(reply.human_mode),
        "session_id": reply.session_id,
        "has_pdf": bool(reply.pdf_path),
        "ui_blocks": reply.ui_blocks or [],
        "suggested_language": reply.suggested_language,
    }


class OperatorPayload(BaseModel):
    enabled: bool


@router.post("/chat/operator")
async def toggle_operator(
    payload: OperatorPayload,
    user: User = Depends(get_db_user),
    chat_service: ChatService = Depends(get_chat_service),
) -> dict:
    lang = user_lang(user)
    session = await chat_service.ensure_active_session(user.id)
    middleware_client = get_middleware_client()

    if not payload.enabled:
        if middleware_client is not None:
            await middleware_client.end_chat(session.id)
        await chat_service.set_human_mode(session.id, False)
        await hub.publish(session.id, "mode_changed", mode="bot")
        return {"ok": True, "mode": "bot"}

    if session.human_mode:
        return {"ok": True, "mode": "queue", "already": True}

    if not is_within_working_hours():
        return {"ok": False, "reason": "out_of_hours", "message": t("working_hours", lang)}

    if middleware_client is None:
        return {
            "ok": False,
            "reason": "unavailable",
            "message": t("middleware_unavailable", lang),
        }

    # The middleware uses the phone as the request identity.
    if not user.phone:
        return {
            "ok": False,
            "reason": "phone_required",
            "message": t("phone_required_for_operator", lang),
        }

    await chat_service.set_human_mode(session.id, True)
    customer_name = user.first_name or (f"@{user.username}" if user.username else str(user.telegram_user_id))
    started = await middleware_client.start_chat(
        session_id=session.id,
        phone=user.phone,
        user_name=customer_name,
        lang=lang,
        telegram_id=user.telegram_user_id,
    )
    if not started:
        await chat_service.set_human_mode(session.id, False)
        return {
            "ok": False,
            "reason": "unavailable",
            "message": t("middleware_unavailable", lang),
        }

    # Phase 4 "Экспертиза": brief the operator with a short context summary
    # as their first message. Never blocks/breaks the handoff — see
    # send_operator_handoff_summary's own try/except.
    await send_operator_handoff_summary(chat_service, middleware_client, session.id)

    await hub.publish(session.id, "mode_changed", mode="queue")
    return {"ok": True, "mode": "queue", "message": t("searching_operator", lang)}


class RatingPayload(BaseModel):
    rating: int = Field(ge=1, le=5)
    comment: str | None = Field(default=None, max_length=1000)


@router.post("/chat/rating")
async def rate_operator(
    payload: RatingPayload,
    user: User = Depends(get_db_user),
    chat_service: ChatService = Depends(get_chat_service),
) -> dict:
    session = await chat_service.get_active_session(user.id)
    if session is None:
        raise HTTPException(status_code=404, detail="No active session")
    saved = await chat_service.record_feedback(session.id, payload.rating, payload.comment)
    return {"ok": bool(saved)}


@router.websocket("/ws")
async def events_socket(
    websocket: WebSocket,
    init_data: str | None = None,
) -> None:
    """Live operator/session events for the currently active chat session."""
    try:
        tg_user = authenticate(init_data)
    except InitDataError as exc:
        await websocket.close(code=4401, reason=str(exc)[:120])
        return

    chat_service: ChatService | None = getattr(websocket.app.state, "chat_service", None)
    if chat_service is None:
        await websocket.close(code=1013, reason="Service not ready")
        return

    user = await chat_service.get_or_create_user(
        telegram_user_id=tg_user.telegram_user_id,
        username=tg_user.username or None,
        first_name=tg_user.first_name or None,
        last_name=tg_user.last_name or None,
    )
    session = await chat_service.ensure_active_session(user.id)

    await websocket.accept()
    await hub.connect(session.id, websocket)
    await websocket.send_json(
        {
            "event": "connected",
            "session_id": session.id,
            "mode": "operator" if session.human_mode else "bot",
        }
    )
    try:
        while True:
            # The socket is server→client only; reading keeps it alive and
            # surfaces disconnects promptly.
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.debug("Mini App socket closed with an error", exc_info=True)
    finally:
        await hub.disconnect(session.id, websocket)
