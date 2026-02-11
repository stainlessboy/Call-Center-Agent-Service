from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.config import get_settings
from app.db.models import ChatSession, Message, SessionStatus, User
from app.db.session import AsyncSessionLocal
from app.services.telegram_sender import send_telegram_message

app = FastAPI(title="Operator API")


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
    label = "👤 Оператор"
    if operator_name:
        label = f"{label} ({operator_name})"
    return f"{label}: {text}"


def _require_api_key(x_api_key: Optional[str]) -> None:
    api_key = (get_settings().operator_api_key or "").strip()
    if not api_key:
        return
    if not x_api_key or x_api_key != api_key:
        raise HTTPException(status_code=401, detail="Invalid API key")


@app.post("/operator/send", response_model=OperatorSendResponse)
async def send_operator(
    payload: OperatorSendRequest,
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

    ok, error = send_telegram_message(
        settings.bot_token,
        user_telegram_id,
        _format_operator_text(text, payload.operator_name),
    )
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
