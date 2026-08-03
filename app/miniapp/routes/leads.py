"""Lead capture — the "перезвоните мне" form (screens 18/19)."""
from __future__ import annotations

import logging
import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.agent.i18n import _localized_name
from app.db.models import Lead, User
from app.db.session import get_session
from app.miniapp.deps import get_chat_service, get_db_user, user_lang
from app.miniapp.routes.catalog import _find_product, _require_category
from app.services.chat_service import ChatService

logger = logging.getLogger(__name__)
router = APIRouter()

_PHONE_RE = re.compile(r"^\+?\d{9,15}$")


class LeadPayload(BaseModel):
    product_id: str
    name: str = Field(min_length=2, max_length=40)
    phone: str = Field(min_length=6, max_length=32)
    amount: int | None = None
    term_months: int | None = None
    rate_pct: float | None = None
    consent: bool = True
    client_request_id: str = Field(min_length=8, max_length=64)


def _lead_number(lead_id: int) -> str:
    return f"A-{lead_id:05d}"


@router.post("/leads")
async def create_lead(
    payload: LeadPayload,
    user: User = Depends(get_db_user),
    chat_service: ChatService = Depends(get_chat_service),
) -> dict:
    if not payload.consent:
        raise HTTPException(status_code=422, detail="Consent is required")

    phone = payload.phone.strip().replace(" ", "").replace("-", "")
    if not _PHONE_RE.match(phone):
        raise HTTPException(status_code=422, detail="Invalid phone format")

    category, _, _ = payload.product_id.rpartition("_")
    _require_category(category)
    product = await _find_product(category, payload.product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")

    active = await chat_service.get_active_session(user.id)
    product_name = _localized_name(product, user_lang(user))

    async with get_session() as session:
        existing = await session.execute(
            select(Lead).where(Lead.client_request_id == payload.client_request_id)
        )
        found = existing.scalar_one_or_none()
        if found is not None:
            # Idempotent replay — return the original lead untouched.
            return {
                "ok": True,
                "duplicate": True,
                "lead_id": found.id,
                "lead_number": _lead_number(found.id),
                "status": found.status,
            }

        lead = Lead(
            session_id=active.id if active else None,
            telegram_user_id=user.telegram_user_id,
            product_category=category,
            product_name=product_name,
            amount=payload.amount,
            term_months=payload.term_months,
            rate_pct=payload.rate_pct,
            contact_name=payload.name.strip(),
            contact_phone=phone,
            client_request_id=payload.client_request_id,
            source="miniapp",
        )
        session.add(lead)
        await session.commit()
        await session.refresh(lead)

    logger.info("Mini App lead #%s created for user %s", lead.id, user.telegram_user_id)
    return {
        "ok": True,
        "duplicate": False,
        "lead_id": lead.id,
        "lead_number": _lead_number(lead.id),
        "status": lead.status,
        "product_name": product_name,
    }
