from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

import httpx
import wtforms
from sqladmin import ModelView, action
from sqladmin.filters import BooleanFilter, AllUniqueStringValuesFilter
from sqlalchemy import select
from starlette.requests import Request
from starlette.responses import RedirectResponse

from app.db.models import (
    Branch,
    CardProductOffer,
    ChatSession,
    CreditProductOffer,
    DepositProductOffer,
    FaqItem,
    Lead,
    Message,
    User,
)
from app.db.session import AsyncSessionLocal

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _send_telegram_message_async(token: str, chat_id: int, text: str) -> tuple[bool, str | None]:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        async with httpx.AsyncClient(verify=False, timeout=10) as client:
            resp = await client.post(url, json={"chat_id": chat_id, "text": text})
            data = resp.json()
        if not data.get("ok"):
            return False, data.get("description") or "telegram api error"
        return True, None
    except Exception as exc:
        return False, str(exc)


# ---------------------------------------------------------------------------
# User
# ---------------------------------------------------------------------------

class UserAdmin(ModelView, model=User):
    name = "User"
    name_plural = "Users"
    icon = "fa-solid fa-user"

    column_list = [User.id, User.telegram_user_id, User.username, User.language, User.created_at, User.is_active]
    column_searchable_list = [User.telegram_user_id, User.username, User.first_name, User.last_name, User.phone]
    column_filters = [
        AllUniqueStringValuesFilter(User.language, title="Language"),
        BooleanFilter(User.is_active, title="Active"),
    ]
    column_sortable_list = [User.id, User.telegram_user_id, User.created_at]
    column_default_sort = ("id", True)

    @action(
        name="delete_with_related",
        label="Удалить с сессиями и сообщениями",
        confirmation_message="Удалить выбранных пользователей вместе со всеми сессиями и сообщениями?",
    )
    async def delete_with_related(self, request: Request) -> RedirectResponse:
        pks = request.query_params.get("pks", "").split(",")
        if pks and pks[0]:
            async with AsyncSessionLocal() as session:
                async with session.begin():
                    for pk in pks:
                        user = await session.get(User, int(pk))
                        if user:
                            await session.delete(user)
        referer = request.headers.get("Referer")
        return RedirectResponse(referer or request.url_for("admin:list", identity=self.identity))


# ---------------------------------------------------------------------------
# ChatSession (with operator_reply feature)
# ---------------------------------------------------------------------------

class ChatSessionAdmin(ModelView, model=ChatSession):
    name = "Chat Session"
    name_plural = "Chat Sessions"
    icon = "fa-solid fa-comments"

    column_list = [
        ChatSession.id, ChatSession.user, ChatSession.status,
        ChatSession.human_mode, ChatSession.assigned_operator_id,
        ChatSession.started_at, ChatSession.ended_at,
        ChatSession.last_activity_at, ChatSession.feedback_rating,
        ChatSession.closed_reason,
    ]
    column_filters = [
        AllUniqueStringValuesFilter(ChatSession.status, title="Status"),
        AllUniqueStringValuesFilter(ChatSession.closed_reason, title="Closed Reason"),
        BooleanFilter(ChatSession.human_mode, title="Human Mode"),
    ]
    column_searchable_list = [ChatSession.id]
    column_sortable_list = [ChatSession.id, ChatSession.started_at, ChatSession.last_activity_at]
    column_default_sort = ("started_at", True)

    column_details_list = [
        ChatSession.id, ChatSession.user, ChatSession.title,
        ChatSession.status, ChatSession.human_mode, ChatSession.human_mode_since,
        ChatSession.assigned_operator_id, ChatSession.started_at, ChatSession.ended_at,
        ChatSession.last_activity_at, ChatSession.feedback_rating, ChatSession.feedback_comment,
        ChatSession.closed_reason, ChatSession.messages,
    ]

    form_excluded_columns = [ChatSession.messages]

    async def scaffold_form(self, rules=None):
        form_class = await super().scaffold_form(rules)
        form_class.operator_reply = wtforms.TextAreaField(
            "Ответ оператором",
            description="Отправится пользователю в Telegram и сохранится в истории сообщений.",
            render_kw={"rows": 4},
        )
        return form_class

    async def after_model_change(self, data: dict, model: Any, is_created: bool, request: Request) -> None:
        if is_created:
            return

        operator_reply = (data.get("operator_reply") or "").strip()
        if not operator_reply:
            return

        # Update session state
        async with AsyncSessionLocal() as session:
            async with session.begin():
                chat_session = await session.get(ChatSession, model.id)
                if chat_session is None:
                    return
                chat_session.human_mode = True
                chat_session.human_mode_since = chat_session.human_mode_since or datetime.now(timezone.utc)
                chat_session.last_activity_at = datetime.now(timezone.utc)

                # Find user
                user = await session.get(User, chat_session.user_id)
                if user is None:
                    logger.error("User not found for session %s", model.id)
                    return

                # Send Telegram message
                token = (os.getenv("BOT_TOKEN") or "").strip()
                if not token:
                    logger.error("BOT_TOKEN not configured, cannot send operator reply")
                    return

                label = "👤 Оператор"
                ok, error = await _send_telegram_message_async(
                    token, user.telegram_user_id, f"{label}: {operator_reply}",
                )
                if not ok:
                    logger.error("Failed to send Telegram message: %s", error)
                    return

                # Save operator message
                session.add(Message(
                    session_id=model.id,
                    role="operator",
                    text=operator_reply,
                    created_at=datetime.now(timezone.utc),
                ))

    @action(
        name="delete_with_messages",
        label="Удалить с сообщениями",
        confirmation_message="Удалить выбранные сессии вместе со всеми сообщениями?",
    )
    async def delete_with_messages(self, request: Request) -> RedirectResponse:
        pks = request.query_params.get("pks", "").split(",")
        if pks and pks[0]:
            async with AsyncSessionLocal() as session:
                async with session.begin():
                    for pk in pks:
                        chat_session = await session.get(ChatSession, pk)
                        if chat_session:
                            await session.delete(chat_session)
        referer = request.headers.get("Referer")
        return RedirectResponse(referer or request.url_for("admin:list", identity=self.identity))


# ---------------------------------------------------------------------------
# Message
# ---------------------------------------------------------------------------

class MessageAdmin(ModelView, model=Message):
    name = "Message"
    name_plural = "Messages"
    icon = "fa-solid fa-envelope"

    column_list = [Message.id, Message.session, Message.role, Message.created_at, Message.latency_ms, Message.error_code]
    column_searchable_list = [Message.session_id, Message.text, Message.role]
    column_filters = [
        AllUniqueStringValuesFilter(Message.role, title="Role"),
        AllUniqueStringValuesFilter(Message.error_code, title="Error Code"),
    ]
    column_sortable_list = [Message.id, Message.created_at]
    column_default_sort = ("created_at", True)


# ---------------------------------------------------------------------------
# Branch
# ---------------------------------------------------------------------------

class BranchAdmin(ModelView, model=Branch):
    name = "Отделение"
    name_plural = "Отделения"
    icon = "fa-solid fa-building"

    column_list = [Branch.id, Branch.name, Branch.region, Branch.district, Branch.phone, Branch.hours]
    column_details_exclude_list = []
    column_searchable_list = [Branch.name, Branch.region, Branch.district, Branch.address, Branch.phone]
    column_filters = [
        AllUniqueStringValuesFilter(Branch.region, title="Region"),
        AllUniqueStringValuesFilter(Branch.district, title="District"),
    ]
    column_sortable_list = [Branch.id, Branch.name, Branch.region]
    column_default_sort = ("id", True)


# ---------------------------------------------------------------------------
# FaqItem
# ---------------------------------------------------------------------------

class FaqItemAdmin(ModelView, model=FaqItem):
    name = "FAQ"
    name_plural = "FAQ"
    icon = "fa-solid fa-circle-question"

    column_list = [FaqItem.id, FaqItem.question_ru, FaqItem.question_en, FaqItem.question_uz, FaqItem.created_at]
    column_searchable_list = [
        FaqItem.question_ru, FaqItem.answer_ru,
        FaqItem.question_en, FaqItem.answer_en,
        FaqItem.question_uz, FaqItem.answer_uz,
    ]
    column_sortable_list = [FaqItem.id, FaqItem.created_at]
    column_default_sort = ("id", True)


# ---------------------------------------------------------------------------
# CreditProductOffer
# ---------------------------------------------------------------------------

class CreditProductOfferAdmin(ModelView, model=CreditProductOffer):
    name = "Кредитный оффер"
    name_plural = "Кредитные офферы"
    icon = "fa-solid fa-money-bill"

    column_list = [
        CreditProductOffer.id, CreditProductOffer.section_name,
        CreditProductOffer.service_name, CreditProductOffer.income_type,
        CreditProductOffer.rate_min_pct, CreditProductOffer.rate_max_pct,
        CreditProductOffer.term_min_months, CreditProductOffer.term_max_months,
        CreditProductOffer.downpayment_min_pct, CreditProductOffer.downpayment_max_pct,
        CreditProductOffer.is_active,
    ]
    column_searchable_list = [
        CreditProductOffer.service_name, CreditProductOffer.section_name,
        CreditProductOffer.rate_condition_text, CreditProductOffer.collateral_text,
    ]
    column_filters = [
        AllUniqueStringValuesFilter(CreditProductOffer.section_name, title="Section"),
        AllUniqueStringValuesFilter(CreditProductOffer.income_type, title="Income Type"),
        BooleanFilter(CreditProductOffer.is_active, title="Active"),
    ]
    column_sortable_list = [
        CreditProductOffer.id, CreditProductOffer.section_name,
        CreditProductOffer.source_row_order, CreditProductOffer.rate_order,
    ]
    column_default_sort = [
        (CreditProductOffer.section_name, False),
        (CreditProductOffer.source_row_order, False),
        (CreditProductOffer.rate_order, False),
    ]


# ---------------------------------------------------------------------------
# DepositProductOffer
# ---------------------------------------------------------------------------

class DepositProductOfferAdmin(ModelView, model=DepositProductOffer):
    name = "Оффер вклада"
    name_plural = "Офферы вкладов"
    icon = "fa-solid fa-piggy-bank"

    column_list = [
        DepositProductOffer.id, DepositProductOffer.service_name,
        DepositProductOffer.currency_code, DepositProductOffer.term_text,
        DepositProductOffer.term_months, DepositProductOffer.rate_pct,
        DepositProductOffer.topup_allowed, DepositProductOffer.is_active,
    ]
    column_searchable_list = [
        DepositProductOffer.service_name, DepositProductOffer.term_text,
        DepositProductOffer.payout_text, DepositProductOffer.topup_text,
        DepositProductOffer.notes_text,
    ]
    column_filters = [
        AllUniqueStringValuesFilter(DepositProductOffer.currency_code, title="Currency"),
        BooleanFilter(DepositProductOffer.topup_allowed, title="Topup Allowed"),
        BooleanFilter(DepositProductOffer.payout_monthly_available, title="Monthly Payout"),
        BooleanFilter(DepositProductOffer.payout_end_available, title="End Payout"),
        BooleanFilter(DepositProductOffer.is_active, title="Active"),
    ]
    column_sortable_list = [
        DepositProductOffer.id, DepositProductOffer.service_name,
        DepositProductOffer.currency_code, DepositProductOffer.source_row_order,
    ]
    column_default_sort = [
        (DepositProductOffer.service_name, False),
        (DepositProductOffer.currency_code, False),
        (DepositProductOffer.source_row_order, False),
    ]


# ---------------------------------------------------------------------------
# Lead
# ---------------------------------------------------------------------------

class LeadAdmin(ModelView, model=Lead):
    name = "Лид"
    name_plural = "Лиды"
    icon = "fa-solid fa-bullseye"

    column_list = [
        Lead.id, Lead.created_at, Lead.status,
        Lead.product_category, Lead.product_name,
        Lead.amount, Lead.term_months, Lead.rate_pct,
        Lead.contact_name, Lead.contact_phone, Lead.telegram_user_id,
    ]
    column_searchable_list = [
        Lead.session_id, Lead.telegram_user_id,
        Lead.product_category, Lead.product_name,
        Lead.contact_name, Lead.contact_phone,
    ]
    column_filters = [
        AllUniqueStringValuesFilter(Lead.status, title="Status"),
        AllUniqueStringValuesFilter(Lead.product_category, title="Product Category"),
    ]
    column_sortable_list = [Lead.id, Lead.created_at, Lead.status]
    column_default_sort = ("created_at", True)


# ---------------------------------------------------------------------------
# CardProductOffer
# ---------------------------------------------------------------------------

class CardProductOfferAdmin(ModelView, model=CardProductOffer):
    name = "Оффер карты"
    name_plural = "Офферы карт"
    icon = "fa-solid fa-credit-card"

    column_list = [
        CardProductOffer.id, CardProductOffer.service_name,
        CardProductOffer.card_network, CardProductOffer.currency_code,
        CardProductOffer.is_fx_card, CardProductOffer.payroll_supported,
        CardProductOffer.issue_fee_free, CardProductOffer.annual_fee_free,
        CardProductOffer.mobile_order_available, CardProductOffer.is_active,
    ]
    column_searchable_list = [
        CardProductOffer.service_name, CardProductOffer.issue_fee_text,
        CardProductOffer.annual_fee_text, CardProductOffer.issuance_time_text,
    ]
    column_filters = [
        AllUniqueStringValuesFilter(CardProductOffer.card_network, title="Network"),
        AllUniqueStringValuesFilter(CardProductOffer.currency_code, title="Currency"),
        BooleanFilter(CardProductOffer.is_fx_card, title="FX Card"),
        BooleanFilter(CardProductOffer.payroll_supported, title="Payroll"),
        BooleanFilter(CardProductOffer.issue_fee_free, title="Free Issue"),
        BooleanFilter(CardProductOffer.is_active, title="Active"),
    ]
    column_sortable_list = [
        CardProductOffer.id, CardProductOffer.service_name, CardProductOffer.source_row_order,
    ]
    column_default_sort = [
        (CardProductOffer.source_row_order, False),
        (CardProductOffer.service_name, False),
    ]
