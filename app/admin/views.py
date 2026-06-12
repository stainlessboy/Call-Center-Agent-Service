from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone
from typing import Any

import httpx
import wtforms
from sqladmin import ModelView, action
from sqladmin.filters import BooleanFilter, AllUniqueStringValuesFilter, StaticValuesFilter
from sqlalchemy import String as SAString, cast, func as sa_func, or_, select
from sqlalchemy.orm import selectinload
from starlette.requests import Request
from starlette.responses import RedirectResponse

from app.config import get_settings
from app.db.models import (
    CardProductOffer,
    ChatSession,
    CreditProductOffer,
    CreditRateRule,
    DepositProductOffer,
    FaqItem,
    Filial,
    Lead,
    Message,
    SalesOffice,
    SalesPoint,
    User,
)
from app.db.session import AsyncSessionLocal

logger = logging.getLogger(__name__)

_DATETIME_FORMAT = "%d.%m.%Y %H:%M"


class _RuBooleanFilter(BooleanFilter):
    """BooleanFilter with Russian labels."""

    async def lookups(self, request, model, run_query):
        return [("all", "Все"), ("true", "Да"), ("false", "Нет")]


class _RuAllUniqueFilter(AllUniqueStringValuesFilter):
    """AllUniqueStringValuesFilter with Russian 'Все' instead of 'All'."""

    async def lookups(self, request, model, run_query):
        result = await super().lookups(request, model, run_query)
        if result and result[0] == ("", "All"):
            result[0] = ("", "Все")
        return result


class _UserNameFilter:
    """Filter sessions by user, displaying username/name instead of raw ID."""

    has_operator = False
    template = "sqladmin/filters/lookup_filter.html"

    def __init__(self, column, title="Пользователь", parameter_name="user_id"):
        self.column = column
        self.title = title
        self.parameter_name = parameter_name

    async def lookups(self, request, model, run_query):
        rows = await run_query(
            select(User.id, User.username, User.first_name, User.telegram_user_id)
            .join(ChatSession, ChatSession.user_id == User.id)
            .distinct()
            .order_by(User.username)
        )
        choices = [("", "Все")]
        for row in rows:
            uid, uname, fname, tg_id = row
            label = uname or fname or str(tg_id)
            choices.append((str(uid), label))
        return choices

    async def get_filtered_query(self, query, value, model):
        if value == "":
            return query
        return query.filter(ChatSession.user_id == int(value))


class _RuStaticValuesFilter(StaticValuesFilter):
    """StaticValuesFilter with Russian 'Все' instead of 'All'."""

    async def lookups(self, request, model, run_query):
        return [("", "Все")] + self.values


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _next_row_order(model_class: Any, **filters: Any) -> int:
    """Get next available source_row_order for a model, auto-incrementing."""
    async with AsyncSessionLocal() as session:
        q = select(sa_func.coalesce(sa_func.max(model_class.source_row_order), 0) + 1)
        for col, val in filters.items():
            if val and hasattr(model_class, col):
                q = q.where(getattr(model_class, col) == val)
        result = await session.execute(q)
        return result.scalar() or 1


async def _send_telegram_message_async(token: str, chat_id: int, text: str) -> tuple[bool, str | None]:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=get_settings().agent_timeout_seconds) as client:
            resp = await client.post(url, json={"chat_id": chat_id, "text": text})
            data = resp.json()
        if not data.get("ok"):
            return False, data.get("description") or "telegram api error"
        return True, None
    except Exception as exc:
        return False, str(exc)


def _fmt_dt(dt: Any) -> str:
    """Format datetime for display in admin list columns."""
    if isinstance(dt, datetime):
        return dt.strftime(_DATETIME_FORMAT)
    return str(dt) if dt else ""


# ---------------------------------------------------------------------------
# Value translation maps
# ---------------------------------------------------------------------------

_STATUS_MAP: dict[str, str] = {
    "active": "Активна",
    "ended": "Завершена",
}

_CLOSED_REASON_MAP: dict[str, str] = {
    "manual_end": "Закрыта вручную",
    "timeout": "Тайм-аут",
}

_ROLE_MAP: dict[str, str] = {
    "user": "Пользователь",
    "assistant": "Ассистент",
    "operator": "Оператор",
    "system": "Система",
}

_INCOME_TYPE_MAP: dict[str, str] = {
    "payroll": "Зарплатный проект",
    "official": "Официальный доход",
    "no_official": "Без офиц. дохода",
    "": "Для всех",
}

_PRODUCT_CATEGORY_MAP: dict[str, str] = {
    "mortgage": "Ипотека",
    "autoloan": "Автокредит",
    "microloan": "Микрозайм",
    "education_credit": "Образовательный кредит",
    "deposit": "Вклад",
    "debit_card": "Дебетовая карта",
    "fx_card": "Валютная карта",
}

_LEAD_STATUS_MAP: dict[str, str] = {
    "new": "Новый",
    "in_progress": "В работе",
    "done": "Завершён",
    "cancelled": "Отменён",
}


# ---------------------------------------------------------------------------
# User
# ---------------------------------------------------------------------------

class UserAdmin(ModelView, model=User):
    name = "Пользователь"
    name_plural = "Пользователи"
    icon = "fa-solid fa-user"

    column_list = [User.id, User.telegram_user_id, User.username, User.language, User.created_at, User.is_active]
    column_searchable_list = [User.telegram_user_id, User.username, User.first_name, User.last_name, User.phone]
    column_filters = [
        _RuAllUniqueFilter(User.language, title="Язык"),
        _RuBooleanFilter(User.is_active, title="Активен"),
    ]
    column_sortable_list = [User.id, User.telegram_user_id, User.created_at]
    column_default_sort = ("id", True)

    column_labels = {
        "id": "ID",
        "telegram_user_id": "Telegram ID",
        "username": "Имя пользователя",
        "first_name": "Имя",
        "last_name": "Фамилия",
        "phone": "Телефон",
        "language": "Язык",
        "created_at": "Дата регистрации",
        "is_active": "Активен",
    }

    column_formatters = {
        User.created_at: lambda m, _: _fmt_dt(m.created_at),
    }
    column_formatters_detail = {
        User.created_at: lambda m, _: _fmt_dt(m.created_at),
    }

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
# ChatSession
# ---------------------------------------------------------------------------

class ChatSessionAdmin(ModelView, model=ChatSession):
    name = "Сессия чата"
    name_plural = "Сессии чатов"
    icon = "fa-solid fa-comments"
    details_template = "sqladmin/chat_details.html"

    column_list = [
        ChatSession.id, ChatSession.user, "message_count", ChatSession.status,
        ChatSession.human_mode, ChatSession.assigned_operator_id,
        ChatSession.started_at, ChatSession.ended_at,
        ChatSession.last_activity_at, ChatSession.feedback_rating,
        ChatSession.closed_reason,
    ]
    column_filters = [
        _UserNameFilter(ChatSession.user_id, title="Пользователь"),
        _RuStaticValuesFilter(
            ChatSession.status, title="Статус",
            values=[("active", "Активна"), ("ended", "Завершена")],
        ),
        _RuStaticValuesFilter(
            ChatSession.closed_reason, title="Причина закрытия",
            values=[("manual_end", "Закрыта вручную"), ("timeout", "Тайм-аут")],
        ),
        _RuBooleanFilter(ChatSession.human_mode, title="Режим оператора"),
    ]
    column_searchable_list = [ChatSession.id]
    column_sortable_list = [ChatSession.id, ChatSession.started_at, ChatSession.last_activity_at]
    column_default_sort = ("started_at", True)

    column_details_list = [
        ChatSession.id, ChatSession.user, ChatSession.title,
        ChatSession.status, ChatSession.human_mode, ChatSession.human_mode_since,
        ChatSession.assigned_operator_id, ChatSession.started_at, ChatSession.ended_at,
        ChatSession.last_activity_at, ChatSession.feedback_rating, ChatSession.feedback_comment,
        ChatSession.closed_reason,
    ]

    form_excluded_columns = [ChatSession.messages]

    column_labels = {
        "id": "ID сессии",
        "user": "Пользователь",
        "title": "Заголовок",
        "status": "Статус",
        "human_mode": "Режим оператора",
        "human_mode_since": "В режиме оператора с",
        "assigned_operator_id": "ID оператора",
        "started_at": "Начало",
        "ended_at": "Завершение",
        "last_activity_at": "Последняя активность",
        "feedback_rating": "Оценка",
        "feedback_comment": "Комментарий",
        "closed_reason": "Причина закрытия",
        "messages": "Сообщения",
        "message_count": "Кол-во сообщений",
    }

    column_formatters = {
        ChatSession.started_at: lambda m, _: _fmt_dt(m.started_at),
        ChatSession.ended_at: lambda m, _: _fmt_dt(m.ended_at),
        ChatSession.last_activity_at: lambda m, _: _fmt_dt(m.last_activity_at),
        ChatSession.status: lambda m, _: _STATUS_MAP.get(m.status.value if hasattr(m.status, "value") else m.status, m.status),
        ChatSession.closed_reason: lambda m, _: _CLOSED_REASON_MAP.get(m.closed_reason, m.closed_reason or ""),
        "message_count": lambda m, _: m.message_count,
    }
    column_formatters_detail = {
        ChatSession.started_at: lambda m, _: _fmt_dt(m.started_at),
        ChatSession.ended_at: lambda m, _: _fmt_dt(m.ended_at),
        ChatSession.last_activity_at: lambda m, _: _fmt_dt(m.last_activity_at),
        ChatSession.human_mode_since: lambda m, _: _fmt_dt(m.human_mode_since),
        ChatSession.status: lambda m, _: _STATUS_MAP.get(m.status.value if hasattr(m.status, "value") else m.status, m.status),
        ChatSession.closed_reason: lambda m, _: _CLOSED_REASON_MAP.get(m.closed_reason, m.closed_reason or ""),
    }

    def list_query(self, request):
        return super().list_query(request).options(selectinload(ChatSession.messages))

    def search_placeholder(self):
        return "ID сессии или текст сообщений..."

    def search_query(self, stmt, term):
        return stmt.outerjoin(Message, Message.session_id == ChatSession.id).filter(
            or_(
                cast(ChatSession.id, SAString).ilike(f"%{term}%"),
                Message.text.ilike(f"%{term}%"),
            )
        ).distinct()

    def details_query(self, request):
        return super().details_query(request).options(selectinload(ChatSession.messages))

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
    name = "Сообщение"
    name_plural = "Сообщения"
    icon = "fa-solid fa-envelope"

    column_list = [Message.id, Message.session, Message.role, "text_preview", Message.created_at, Message.latency_ms, "llm_model", "llm_tokens", "llm_cost_fmt", Message.error_code]
    column_searchable_list = [Message.session_id, Message.text, Message.role]
    column_filters = [
        _RuStaticValuesFilter(
            Message.role, title="Роль",
            values=[("user", "Пользователь"), ("assistant", "Ассистент"), ("operator", "Оператор"), ("system", "Система")],
        ),
        _RuAllUniqueFilter(Message.error_code, title="Код ошибки"),
    ]
    column_sortable_list = [Message.id, Message.created_at]
    column_default_sort = ("created_at", True)

    column_labels = {
        "id": "ID",
        "session": "Сессия",
        "session_id": "ID сессии",
        "role": "Роль",
        "text": "Текст",
        "text_preview": "Текст",
        "telegram_message_id": "Telegram ID сообщения",
        "created_at": "Дата создания",
        "latency_ms": "Задержка (мс)",
        "error_code": "Код ошибки",
        "llm_usage": "LLM Usage",
        "llm_model": "Модель LLM",
        "llm_prompt_tokens": "Prompt токены",
        "llm_completion_tokens": "Completion токены",
        "llm_tokens": "Всего токенов",
        "llm_cost_fmt": "Стоимость ($)",
    }

    column_formatters = {
        Message.created_at: lambda m, _: _fmt_dt(m.created_at),
        Message.role: lambda m, _: _ROLE_MAP.get(m.role, m.role),
        "text_preview": lambda m, _: (m.text[:100] + "…") if m.text and len(m.text) > 100 else (m.text or ""),
        "llm_model": lambda m, _: (m.llm_usage or {}).get("model", "") if m.llm_usage else "",
        "llm_tokens": lambda m, _: str((m.llm_usage or {}).get("total_tokens", "")) if m.llm_usage else "",
        "llm_cost_fmt": lambda m, _: f"${m.llm_usage['cost']:.6f}" if m.llm_usage and m.llm_usage.get("cost") else "",
    }
    column_details_list = [
        Message.id, Message.session_id, Message.role, Message.text,
        Message.created_at, Message.latency_ms, Message.error_code,
        "llm_model", "llm_prompt_tokens", "llm_completion_tokens", "llm_tokens", "llm_cost_fmt",
    ]

    column_formatters_detail = {
        Message.created_at: lambda m, _: _fmt_dt(m.created_at),
        Message.role: lambda m, _: _ROLE_MAP.get(m.role, m.role),
        "llm_model": lambda m, _: (m.llm_usage or {}).get("model", "—") if m.llm_usage else "—",
        "llm_prompt_tokens": lambda m, _: str((m.llm_usage or {}).get("prompt_tokens", "—")) if m.llm_usage else "—",
        "llm_completion_tokens": lambda m, _: str((m.llm_usage or {}).get("completion_tokens", "—")) if m.llm_usage else "—",
        "llm_tokens": lambda m, _: str((m.llm_usage or {}).get("total_tokens", "—")) if m.llm_usage else "—",
        "llm_cost_fmt": lambda m, _: f"${m.llm_usage['cost']:.6f}" if m.llm_usage and m.llm_usage.get("cost") else "—",
    }


# ---------------------------------------------------------------------------
# Filial (ЦБУ), SalesOffice (мини-офис), SalesPoint (автосалон)
# ---------------------------------------------------------------------------

_OFFICE_COMMON_LABELS = {
    "id": "ID",
    "name_ru": "Название (RU)",
    "name_uz": "Название (UZ)",
    "address_ru": "Адрес (RU)",
    "address_uz": "Адрес (UZ)",
    "latitude": "Широта",
    "longitude": "Долгота",
    "phone": "Телефон",
    "hours": "Часы работы",
    "created_at": "Создан",
    "parent_filial_id": "Родительский филиал (ID)",
    "parent_filial": "Родительский филиал",
}


class FilialAdmin(ModelView, model=Filial):
    name = "Филиал (ЦБУ)"
    name_plural = "Филиалы (ЦБУ)"
    icon = "fa-solid fa-building-columns"
    category = "Офисы"

    column_list = [Filial.id, Filial.name_ru, Filial.address_ru, Filial.phone]
    column_searchable_list = [
        Filial.name_ru, Filial.name_uz, Filial.address_ru, Filial.address_uz
    ]
    column_sortable_list = [Filial.id, Filial.name_ru]
    column_default_sort = ("name_ru", False)

    column_labels = {
        **_OFFICE_COMMON_LABELS,
        "landmark_ru": "Ориентир (RU)",
        "landmark_uz": "Ориентир (UZ)",
        "location_url": "Ссылка на карту (Яндекс/Google)",
        "sales_offices": "Прикреплённые офисы продаж",
        "sales_points": "Прикреплённые точки продаж",
    }


class SalesOfficeAdmin(ModelView, model=SalesOffice):
    name = "Офис продаж"
    name_plural = "Офисы продаж (мини-офисы)"
    icon = "fa-solid fa-store"
    category = "Офисы"

    column_list = [
        SalesOffice.id, SalesOffice.name_ru, SalesOffice.region_ru,
        SalesOffice.parent_filial_id, SalesOffice.phone,
    ]
    column_searchable_list = [
        SalesOffice.name_ru, SalesOffice.name_uz,
        SalesOffice.region_ru, SalesOffice.region_uz,
        SalesOffice.address_ru, SalesOffice.address_uz,
    ]
    column_filters = [_RuAllUniqueFilter(SalesOffice.region_ru, title="Регион")]
    column_sortable_list = [SalesOffice.id, SalesOffice.name_ru, SalesOffice.region_ru]
    column_default_sort = ("region_ru", False)

    column_labels = {
        **_OFFICE_COMMON_LABELS,
        "region_ru": "Регион (RU)",
        "region_uz": "Регион (UZ)",
    }


class SalesPointAdmin(ModelView, model=SalesPoint):
    name = "Точка продаж"
    name_plural = "Точки продаж (автосалоны)"
    icon = "fa-solid fa-car"
    category = "Офисы"

    column_list = [
        SalesPoint.id, SalesPoint.name_ru, SalesPoint.address_ru,
        SalesPoint.parent_filial_id, SalesPoint.phone,
    ]
    column_searchable_list = [
        SalesPoint.name_ru, SalesPoint.name_uz,
        SalesPoint.address_ru, SalesPoint.address_uz,
    ]
    column_sortable_list = [SalesPoint.id, SalesPoint.name_ru]
    column_default_sort = ("name_ru", False)

    column_labels = dict(_OFFICE_COMMON_LABELS)


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

    # Built-in CSV export must contain answers too (column_list shows only
    # questions to keep the table readable).
    column_export_list = [
        FaqItem.id,
        FaqItem.question_ru, FaqItem.answer_ru,
        FaqItem.question_en, FaqItem.answer_en,
        FaqItem.question_uz, FaqItem.answer_uz,
    ]

    # 1536-dim numpy arrays — useless to render and break Jinja's `if obj`.
    column_details_exclude_list = ["embedding_ru", "embedding_en", "embedding_uz"]
    form_excluded_columns = ["embedding_ru", "embedding_en", "embedding_uz", "created_at"]

    column_labels = {
        "id": "ID",
        "question_ru": "Вопрос (RU)",
        "answer_ru": "Ответ (RU)",
        "question_en": "Вопрос (EN)",
        "answer_en": "Ответ (EN)",
        "question_uz": "Вопрос (UZ)",
        "answer_uz": "Ответ (UZ)",
        "created_at": "Дата создания",
    }

    column_formatters = {
        FaqItem.created_at: lambda m, _: _fmt_dt(m.created_at),
    }


# ---------------------------------------------------------------------------
# CreditProductOffer
# ---------------------------------------------------------------------------

class CreditProductOfferAdmin(ModelView, model=CreditProductOffer):
    name = "Кредитный оффер"
    name_plural = "Кредитные офферы"
    icon = "fa-solid fa-money-bill"

    column_list = [
        CreditProductOffer.id, CreditProductOffer.section_name,
        CreditProductOffer.service_name, CreditProductOffer.rate_condition_kind,
        CreditProductOffer.amount_min, CreditProductOffer.amount_max,
        CreditProductOffer.min_age, CreditProductOffer.is_active,
    ]
    column_searchable_list = [
        CreditProductOffer.service_name, CreditProductOffer.service_name_en,
        CreditProductOffer.service_name_uz, CreditProductOffer.section_name,
        CreditProductOffer.collateral_text,
    ]
    column_filters = [
        _RuAllUniqueFilter(CreditProductOffer.section_name, title="Раздел"),
        _RuBooleanFilter(CreditProductOffer.is_active, title="Активен"),
        _RuBooleanFilter(CreditProductOffer.for_brand_gm, title="Авто: марка GM"),
        _RuBooleanFilter(CreditProductOffer.for_brand_other, title="Авто: иная марка"),
        _RuBooleanFilter(CreditProductOffer.for_market_primary, title="Ипотека: первичный"),
        _RuBooleanFilter(CreditProductOffer.for_market_secondary, title="Ипотека: вторичный"),
        _RuBooleanFilter(CreditProductOffer.for_renovation, title="Ипотека: ремонт"),
        _RuBooleanFilter(CreditProductOffer.channel_cbu, title="Микрозайм: ЦБУ"),
        _RuBooleanFilter(CreditProductOffer.channel_online, title="Микрозайм: онлайн"),
    ]
    column_sortable_list = [
        CreditProductOffer.id, CreditProductOffer.section_name,
        CreditProductOffer.service_name,
    ]
    column_default_sort = [
        (CreditProductOffer.section_name, False),
        (CreditProductOffer.service_name, False),
        (CreditProductOffer.id, False),
    ]

    column_details_exclude_list = [CreditProductOffer.source_path]

    # Tariffs (rate/term/downpayment/income) live in CreditRateRule now — they
    # are managed in the separate "Тарифы (ставки)" view, not on the product form.
    form_excluded_columns = [
        CreditProductOffer.source_path,
        CreditProductOffer.created_at,
        CreditProductOffer.updated_at,
        "rate_rules",
    ]

    column_labels = {
        "id": "ID",
        "section_name": "Раздел", "service_name": "Название продукта",
        "service_name_en": "Название (EN)", "service_name_uz": "Название (UZ)",
        "amount_min": "Сумма мин.", "amount_max": "Сумма макс.",
        "min_age": "Мин. возраст",
        "rate_condition_kind": "Тип условия (тариф)",
        "purpose_text": "Цель кредита", "collateral_text": "Обеспечение",
        "for_brand_gm": "Авто: марка GM",
        "for_brand_other": "Авто: иная марка",
        "for_market_primary": "Ипотека: первичный рынок",
        "for_market_secondary": "Ипотека: вторичный рынок",
        "for_renovation": "Ипотека: ремонт",
        "channel_cbu": "Микрозайм: оформление в ЦБУ",
        "channel_online": "Микрозайм: оформление онлайн",
        "is_active": "Активен", "created_at": "Создано", "updated_at": "Обновлено",
    }

    _SECTION_CHOICES = [
        ("Ипотека", "Ипотека"),
        ("Автокредит", "Автокредит"),
        ("Микрозайм", "Микрозайм"),
        ("Образовательный", "Образовательный"),
    ]

    # The single condition axis the product's tariffs vary by. Drives which bound
    # fields show in the inline tariff editor (JS in _credit_rules_inline.html).
    _CONDITION_KIND_CHOICES = [
        ("flat", "Без условия (одна ставка)"),
        ("term", "Срок"),
        ("age", "Возраст"),
        ("amount", "Сумма"),
        ("downpayment", "Первоначальный взнос"),
    ]
    # axis key -> rule bound fields it controls
    _AXIS_FIELDS = {
        "term": ("term_min_months", "term_max_months"),
        "age": ("age_min", "age_max"),
        "amount": ("amount_min", "amount_max"),
        "downpayment": ("downpayment_min_pct", "downpayment_max_pct"),
    }

    # Qualification-flow tags. Rendered as 3-state selects (— / Да / Нет) and
    # shown/hidden by section via templates/sqladmin/_credit_tags_script.html.
    _TAG_CHOICES = [("", "— не задано —"), ("true", "Да"), ("false", "Нет")]
    _TAG_FIELDS = {
        "for_brand_gm": "Авто: марка GM",
        "for_brand_other": "Авто: иная марка",
        "for_market_primary": "Ипотека: первичный рынок",
        "for_market_secondary": "Ипотека: вторичный рынок",
        "for_renovation": "Ипотека: ремонт",
        "channel_cbu": "Микрозайм: оформление в ЦБУ",
        "channel_online": "Микрозайм: оформление онлайн",
    }

    @staticmethod
    def _tag_coerce(value):
        if value in (True, "true", "True"):
            return True
        if value in (False, "false", "False"):
            return False
        return None

    form_args = {
        "section_name": {
            "label": "Раздел",
            "description": "Категория кредитного продукта",
        },
        "service_name": {
            "description": "Например: «Ипотека на строительство жилья», «Автокредит Hyundai»",
        },
        "service_name_en": {
            "description": "Например: «Mortgage for housing construction»",
        },
        "service_name_uz": {
            "description": "Например: «Uy-joy qurilishi uchun ipoteka»",
        },
        "amount_min": {
            "description": "Минимальная сумма (число), например: 5000000",
        },
        "amount_max": {
            "description": "Максимальная сумма (число), например: 1500000000",
        },
        "min_age": {
            "description": "Минимальный возраст заёмщика, например: 21",
        },
        "purpose_text": {
            "description": "Например: «Покупка/строительство жилья»",
        },
        "collateral_text": {
            "description": "Например: «Залог приобретаемого жилья»",
        },
        "is_active": {
            "description": "Снимите, чтобы скрыть продукт от клиентов",
        },
    }

    form_overrides = {
        "section_name": wtforms.SelectField,
    }
    form_widget_args = {
        "section_name": {"coerce": str},
    }

    # Conditional tag fields are shown/hidden by section via JS in this template.
    create_template = "sqladmin/credit_offer_create.html"
    edit_template = "sqladmin/credit_offer_edit.html"

    async def scaffold_form(self, rules=None):
        form_class = await super().scaffold_form(rules)
        form_class.section_name = wtforms.SelectField(
            "Раздел",
            choices=self._SECTION_CHOICES,
            description="Категория кредитного продукта",
        )
        form_class.rate_condition_kind = wtforms.SelectField(
            "Тип условия (тариф)",
            choices=self._CONDITION_KIND_CHOICES,
            default="flat",
            description="От чего зависит ставка. Определяет, какие поля видны в тарифах ниже.",
        )
        for col, label in self._TAG_FIELDS.items():
            setattr(
                form_class,
                col,
                wtforms.SelectField(
                    label,
                    choices=self._TAG_CHOICES,
                    coerce=self._tag_coerce,
                    description="Показывается только для соответствующего раздела",
                ),
            )
        return form_class

    # --- Inline rate-rule editor --------------------------------------------
    # Tariffs are edited on the product page (not a separate view). The rule
    # inputs are rendered by the custom template as ``rule-{i}-{field}`` and are
    # NOT WTForms fields, so they arrive only via the raw request form, which we
    # parse here and sync into CreditRateRule children.

    _RULE_INT_FIELDS = (
        "age_min", "age_max", "amount_min", "amount_max",
        "term_min_months", "term_max_months", "priority",
    )
    _RULE_FLOAT_FIELDS = (
        "downpayment_min_pct", "downpayment_max_pct", "rate_min_pct", "rate_max_pct",
    )
    _RULE_STR_FIELDS = ("income_type", "currency_code", "condition_text")

    @staticmethod
    def _to_int(value):
        try:
            return int(str(value).strip()) if value not in (None, "") else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _to_float(value):
        try:
            return float(str(value).strip().replace(",", ".")) if value not in (None, "") else None
        except (TypeError, ValueError):
            return None

    async def get_object_for_edit(self, request: Request):
        obj = await super().get_object_for_edit(request)
        if obj is not None:
            async with self.session_maker() as session:
                rules = (
                    await session.execute(
                        select(CreditRateRule)
                        .where(CreditRateRule.credit_product_offer_id == obj.id)
                        .order_by(CreditRateRule.priority.desc(), CreditRateRule.id)
                    )
                ).scalars().all()
            # The shown axis is the product's rate_condition_kind (read by the
            # template's JS); rule rows just expose every bound field.
            obj._editor_rules = rules
        return obj

    def _parse_rule_rows(self, form) -> list[dict]:
        indices = set()
        for key in form.keys():
            m = re.match(r"rule-(\d+)-", key)
            if m:
                indices.add(int(m.group(1)))
        rows = []
        for i in sorted(indices):
            def g(name):
                return form.get(f"rule-{i}-{name}")
            row = {
                "_id": self._to_int(g("id")),
                "_delete": (g("delete") in ("on", "true", "1", "y")),
                "is_active": (g("is_active") in ("on", "true", "1", "y")),
            }
            for f in self._RULE_INT_FIELDS:
                row[f] = self._to_int(g(f))
            for f in self._RULE_FLOAT_FIELDS:
                row[f] = self._to_float(g(f))
            for f in self._RULE_STR_FIELDS:
                v = g(f)
                row[f] = v.strip() if isinstance(v, str) and v.strip() else None
            rows.append(row)
        return rows

    async def _sync_rules_from_request(self, request: Request, product_id: int) -> None:
        form = await request.form()
        rows = self._parse_rule_rows(form)
        if not rows:
            return
        managed = (
            "income_type", "currency_code", "condition_text",
            "age_min", "age_max", "amount_min", "amount_max",
            "term_min_months", "term_max_months",
            "downpayment_min_pct", "downpayment_max_pct",
            "rate_min_pct", "rate_max_pct", "priority", "is_active",
        )
        # Enforce the product's single condition axis: NULL out every bound that
        # is not on the chosen axis (currency_code/income_type stay as overlays).
        kind = (form.get("rate_condition_kind") or "").strip()
        keep = set(self._AXIS_FIELDS.get(kind, ()))
        off_axis = {c for cols in self._AXIS_FIELDS.values() for c in cols} - keep
        for row in rows:
            for c in off_axis:
                row[c] = None
        async with self.session_maker() as session:
            existing = {
                r.id: r
                for r in (
                    await session.execute(
                        select(CreditRateRule).where(
                            CreditRateRule.credit_product_offer_id == product_id
                        )
                    )
                ).scalars().all()
            }
            for row in rows:
                rid = row["_id"]
                if row["_delete"]:
                    if rid and rid in existing:
                        await session.delete(existing[rid])
                    continue
                # default rate_max to rate_min when omitted
                if row.get("rate_min_pct") is not None and row.get("rate_max_pct") is None:
                    row["rate_max_pct"] = row["rate_min_pct"]
                if rid and rid in existing:
                    r = existing[rid]
                    for f in managed:
                        setattr(r, f, row.get(f))
                else:
                    # new row — only persist if it carries a rate
                    if row.get("rate_min_pct") is None:
                        continue
                    session.add(
                        CreditRateRule(
                            credit_product_offer_id=product_id,
                            source="manual",
                            **{f: row.get(f) for f in managed},
                        )
                    )
            await session.commit()

    async def insert_model(self, request: Request, data: dict):
        obj = await super().insert_model(request, data)
        await self._sync_rules_from_request(request, obj.id)
        return obj

    async def update_model(self, request: Request, pk: str, data: dict):
        obj = await super().update_model(request, pk, data)
        await self._sync_rules_from_request(request, obj.id)
        return obj


# ---------------------------------------------------------------------------
# CreditRateRule
# ---------------------------------------------------------------------------

class CreditRateRuleAdmin(ModelView, model=CreditRateRule):
    name = "Тариф (ставка)"
    name_plural = "Тарифы (ставки)"
    icon = "fa-solid fa-percent"

    column_list = [
        CreditRateRule.id, CreditRateRule.credit_product_offer_id,
        CreditRateRule.income_type,
        CreditRateRule.rate_min_pct, CreditRateRule.rate_max_pct,
        CreditRateRule.term_min_months, CreditRateRule.term_max_months,
        CreditRateRule.age_min, CreditRateRule.age_max,
        CreditRateRule.source, CreditRateRule.is_active,
    ]
    column_searchable_list = [CreditRateRule.condition_text]
    column_filters = [
        _RuStaticValuesFilter(
            CreditRateRule.income_type, title="Тип дохода",
            values=[("payroll", "Зарплатный проект"), ("official", "Официальный доход"), ("no_official", "Без офиц. дохода")],
        ),
        _RuAllUniqueFilter(CreditRateRule.currency_code, title="Валюта"),
        _RuStaticValuesFilter(
            CreditRateRule.source, title="Источник",
            values=[("seed", "Из Excel"), ("manual", "Вручную")],
        ),
        _RuBooleanFilter(CreditRateRule.is_active, title="Активен"),
    ]
    column_sortable_list = [
        CreditRateRule.id, CreditRateRule.credit_product_offer_id,
        CreditRateRule.priority, CreditRateRule.rate_min_pct,
    ]
    column_default_sort = [
        (CreditRateRule.credit_product_offer_id, False),
        (CreditRateRule.priority, True),
        (CreditRateRule.id, False),
    ]

    column_formatters = {
        CreditRateRule.income_type: lambda m, _: _INCOME_TYPE_MAP.get(m.income_type or "", m.income_type or ""),
    }
    column_formatters_detail = {
        CreditRateRule.income_type: lambda m, _: _INCOME_TYPE_MAP.get(m.income_type or "", m.income_type or ""),
    }

    form_columns = [
        "product",
        "income_type",
        "age_min", "age_max",
        "amount_min", "amount_max",
        "term_min_months", "term_max_months",
        "downpayment_min_pct", "downpayment_max_pct",
        "currency_code",
        "rate_min_pct", "rate_max_pct",
        "condition_text", "priority", "source", "is_active",
    ]

    column_labels = {
        "id": "ID",
        "credit_product_offer_id": "Продукт (ID)",
        "product": "Продукт",
        "income_type": "Тип дохода",
        "age_min": "Возраст от", "age_max": "Возраст до",
        "amount_min": "Сумма от", "amount_max": "Сумма до",
        "term_min_months": "Срок от (мес.)", "term_max_months": "Срок до (мес.)",
        "downpayment_min_pct": "Взнос от %", "downpayment_max_pct": "Взнос до %",
        "currency_code": "Валюта",
        "rate_min_pct": "Ставка мин. %", "rate_max_pct": "Ставка макс. %",
        "condition_text": "Условие ставки",
        "priority": "Приоритет", "source": "Источник",
        "is_active": "Активен", "created_at": "Создано", "updated_at": "Обновлено",
    }

    _INCOME_TYPE_CHOICES = [
        ("", "— для всех —"),
        ("payroll", "payroll — зарплатный проект"),
        ("official", "official — официальный доход"),
        ("no_official", "no_official — без официального дохода"),
    ]
    _CURRENCY_CHOICES = [("", "— любая —"), ("UZS", "UZS"), ("USD", "USD"), ("EUR", "EUR")]
    _SOURCE_CHOICES = [("manual", "Вручную"), ("seed", "Из Excel")]

    @staticmethod
    def _blank_to_none(value):
        return value if value not in (None, "", "None") else None

    form_args = {
        "product": {"description": "Кредитный продукт, к которому относится тариф"},
        "rate_min_pct": {"description": "Ставка, например: 21.0. Без неё тариф не действует."},
        "rate_max_pct": {"description": "Верх диапазона ставки (если фиксированная — = мин.)"},
        "age_min": {"description": "Возраст от (включительно). Заполняйте только если ставка зависит от возраста."},
        "age_max": {"description": "Возраст до (включительно)."},
        "amount_min": {"description": "Сумма от (число). Пусто = без ограничения."},
        "amount_max": {"description": "Сумма до (число). Пусто = без ограничения."},
        "term_min_months": {"description": "Срок от (мес.). Пусто = без ограничения."},
        "term_max_months": {"description": "Срок до (мес.). Пусто = без ограничения."},
        "downpayment_min_pct": {"description": "Взнос от %. Пусто = без ограничения."},
        "downpayment_max_pct": {"description": "Взнос до %. Пусто = без ограничения."},
        "condition_text": {"description": "Текстовое описание условия (для показа клиенту)."},
        "priority": {"description": "При совпадении нескольких тарифов выбирается больший приоритет."},
        "is_active": {"description": "Снимите, чтобы временно отключить тариф."},
    }

    async def scaffold_form(self, rules=None):
        form_class = await super().scaffold_form(rules)
        form_class.income_type = wtforms.SelectField(
            "Тип дохода",
            choices=self._INCOME_TYPE_CHOICES,
            coerce=self._blank_to_none,
            description="Вариант дохода заёмщика для этой ставки",
        )
        form_class.currency_code = wtforms.SelectField(
            "Валюта",
            choices=self._CURRENCY_CHOICES,
            coerce=self._blank_to_none,
            description="Валюта, для которой действует ставка",
        )
        form_class.source = wtforms.SelectField(
            "Источник",
            choices=self._SOURCE_CHOICES,
            description="«Из Excel» перезаписывается при сидинге; «Вручную» сохраняется",
        )
        return form_class

# ---------------------------------------------------------------------------
# DepositProductOffer
# ---------------------------------------------------------------------------

class DepositProductOfferAdmin(ModelView, model=DepositProductOffer):
    name = "Оффер вклада"
    name_plural = "Офферы вкладов"
    icon = "fa-solid fa-piggy-bank"

    column_list = [
        DepositProductOffer.id, DepositProductOffer.service_name,
        DepositProductOffer.currency_code,
        DepositProductOffer.term_months, DepositProductOffer.rate_pct,
        DepositProductOffer.topup_allowed, DepositProductOffer.is_active,
    ]
    column_searchable_list = [
        DepositProductOffer.service_name, DepositProductOffer.service_name_en,
        DepositProductOffer.service_name_uz,
        DepositProductOffer.payout_text,
        DepositProductOffer.notes_text,
    ]
    column_filters = [
        _RuAllUniqueFilter(DepositProductOffer.currency_code, title="Валюта"),
        _RuBooleanFilter(DepositProductOffer.topup_allowed, title="Пополнение"),
        _RuBooleanFilter(DepositProductOffer.payout_monthly_available, title="Ежемесячная выплата"),
        _RuBooleanFilter(DepositProductOffer.payout_end_available, title="Выплата в конце"),
        _RuBooleanFilter(DepositProductOffer.is_active, title="Активен"),
    ]
    column_sortable_list = [
        DepositProductOffer.id, DepositProductOffer.service_name,
        DepositProductOffer.currency_code, DepositProductOffer.term_months,
    ]
    column_default_sort = [
        (DepositProductOffer.service_name, False),
        (DepositProductOffer.currency_code, False),
        (DepositProductOffer.term_months, False),
    ]

    column_details_exclude_list = [DepositProductOffer.source_path]

    form_excluded_columns = [
        DepositProductOffer.source_path,
        DepositProductOffer.created_at,
        DepositProductOffer.updated_at,
    ]

    column_labels = {
        "id": "ID",
        "service_name": "Название вклада",
        "service_name_en": "Название (EN)", "service_name_uz": "Название (UZ)",
        "currency_code": "Валюта",
        "term_months": "Срок (мес.)",
        "rate_pct": "Ставка %",
        "min_amount": "Мин. сумма",
        "open_channel_text": "Способ оформления",
        "payout_text": "Выплата процентов",
        "payout_monthly_available": "Ежемесячная выплата",
        "payout_end_available": "Выплата в конце",
        "topup_allowed": "Пополнение",
        "partial_withdrawal_allowed": "Частичное снятие",
        "notes_text": "Примечания",
        "is_active": "Активен",
        "created_at": "Создано", "updated_at": "Обновлено",
    }

    _CURRENCY_CHOICES = [
        ("UZS", "UZS — сум"),
        ("USD", "USD — доллар"),
        ("EUR", "EUR — евро"),
    ]

    form_args = {
        "service_name": {
            "description": "Например: «Жамгарма», «Омонат»",
        },
        "service_name_en": {
            "description": "Например: «Jamgarma», «Omonat»",
        },
        "service_name_uz": {
            "description": "Например: «Жамғарма», «Омонат»",
        },
        "currency_code": {
            "label": "Валюта",
            "description": "Валюта вклада",
        },
        "term_months": {
            "description": "Число месяцев, например: 6, 12, 24",
        },
        "rate_pct": {
            "description": "Число, например: 24.0",
        },
        "min_amount": {
            "description": "Число, например: 500000",
        },
        "open_channel_text": {
            "description": "Например: «Онлайн / В отделении»",
        },
        "payout_text": {
            "description": "Например: «Ежемесячно или в конце срока»",
        },
        "notes_text": {
            "description": "Дополнительные условия, например: «Досрочное расторжение — по ставке до востребования»",
        },
        "is_active": {
            "description": "Снимите, чтобы скрыть вклад от клиентов",
        },
    }

    form_overrides = {
        "currency_code": wtforms.SelectField,
    }

    async def scaffold_form(self, rules=None):
        form_class = await super().scaffold_form(rules)
        form_class.currency_code = wtforms.SelectField(
            "Валюта",
            choices=self._CURRENCY_CHOICES,
            description="Валюта вклада",
        )
        return form_class

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
        _RuStaticValuesFilter(
            Lead.status, title="Статус",
            values=[("new", "Новый"), ("in_progress", "В работе"), ("done", "Завершён"), ("cancelled", "Отменён")],
        ),
        _RuStaticValuesFilter(
            Lead.product_category, title="Категория продукта",
            values=[
                ("mortgage", "Ипотека"), ("autoloan", "Автокредит"),
                ("microloan", "Микрозайм"), ("education_credit", "Образовательный кредит"),
                ("deposit", "Вклад"), ("debit_card", "Дебетовая карта"), ("fx_card", "Валютная карта"),
            ],
        ),
    ]
    column_sortable_list = [Lead.id, Lead.created_at, Lead.status]
    column_default_sort = ("created_at", True)

    column_labels = {
        "id": "ID",
        "session_id": "ID сессии",
        "telegram_user_id": "Telegram ID",
        "product_category": "Категория продукта",
        "product_name": "Название продукта",
        "amount": "Сумма",
        "term_months": "Срок (мес.)",
        "rate_pct": "Ставка %",
        "contact_name": "Имя контакта",
        "contact_phone": "Телефон контакта",
        "status": "Статус",
        "created_at": "Дата создания",
    }

    column_formatters = {
        Lead.created_at: lambda m, _: _fmt_dt(m.created_at),
        Lead.status: lambda m, _: _LEAD_STATUS_MAP.get(m.status, m.status),
        Lead.product_category: lambda m, _: _PRODUCT_CATEGORY_MAP.get(m.product_category or "", m.product_category or ""),
    }
    column_formatters_detail = {
        Lead.created_at: lambda m, _: _fmt_dt(m.created_at),
        Lead.status: lambda m, _: _LEAD_STATUS_MAP.get(m.status, m.status),
        Lead.product_category: lambda m, _: _PRODUCT_CATEGORY_MAP.get(m.product_category or "", m.product_category or ""),
    }


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
        CardProductOffer.service_name, CardProductOffer.service_name_en,
        CardProductOffer.service_name_uz, CardProductOffer.issue_fee_text,
        CardProductOffer.annual_fee_text, CardProductOffer.issuance_time_text,
    ]
    column_filters = [
        _RuAllUniqueFilter(CardProductOffer.card_network, title="Платёжная сеть"),
        _RuAllUniqueFilter(CardProductOffer.currency_code, title="Валюта"),
        _RuBooleanFilter(CardProductOffer.is_fx_card, title="Валютная карта"),
        _RuBooleanFilter(CardProductOffer.payroll_supported, title="Зарплатная"),
        _RuBooleanFilter(CardProductOffer.issue_fee_free, title="Бесплатный выпуск"),
        _RuBooleanFilter(CardProductOffer.is_active, title="Активен"),
    ]
    column_sortable_list = [
        CardProductOffer.id, CardProductOffer.service_name, CardProductOffer.source_row_order,
    ]
    column_default_sort = [
        (CardProductOffer.source_row_order, False),
        (CardProductOffer.service_name, False),
    ]

    column_details_exclude_list = [CardProductOffer.source_path]

    form_excluded_columns = [
        CardProductOffer.source_path,
        CardProductOffer.created_at,
        CardProductOffer.updated_at,
    ]

    column_labels = {
        "id": "ID",
        "service_name": "Название карты",
        "service_name_en": "Название (EN)", "service_name_uz": "Название (UZ)",
        "card_network": "Платёжная сеть",
        "currency_code": "Валюта", "is_fx_card": "Валютная карта",
        "is_debit_card": "Дебетовая карта", "payroll_supported": "Зарплатная",
        "issue_fee_text": "Стоимость выпуска", "issue_fee_free": "Бесплатный выпуск",
        "reissue_fee_text": "Стоимость перевыпуска",
        "transfer_fee_text": "Комиссия за переводы",
        "cashback_pct": "Кэшбэк %",
        "validity_months": "Срок (мес.)",
        "issuance_time_text": "Время выпуска",
        "pin_setup_cbu_text": "Установка PIN (ЦБУ)",
        "sms_setup_cbu_text": "Установка SMS (ЦБУ)",
        "pin_setup_mobile_text": "Установка PIN (мобилка)",
        "sms_setup_mobile_text": "Установка SMS (мобилка)",
        "annual_fee_text": "Годовое обслуживание", "annual_fee_free": "Бесплатное обслуживание",
        "mobile_order_available": "Заказ через приложение",
        "delivery_available": "Доставка", "pickup_available": "Самовывоз",
        "source_row_order": "Порядок сортировки", "is_active": "Активен",
        "created_at": "Создано", "updated_at": "Обновлено",
    }

    _NETWORK_CHOICES = [
        ("", "— не указана —"),
        ("uzcard", "UzCard"),
        ("humo", "Humo"),
        ("visa", "Visa"),
        ("mastercard", "Mastercard"),
    ]

    _CARD_CURRENCY_CHOICES = [
        ("UZS", "UZS — сум"),
        ("USD", "USD — доллар"),
        ("EUR", "EUR — евро"),
        ("MULTI", "MULTI — мультивалютная"),
    ]

    form_args = {
        "service_name": {
            "description": "Например: «HUMO — Дебетовая карта», «Visa Classic»",
        },
        "service_name_en": {
            "description": "Например: «HUMO — Debit card», «Visa Classic»",
        },
        "service_name_uz": {
            "description": "Например: «HUMO — Debet karta», «Visa Classic»",
        },
        "card_network": {
            "label": "Платёжная сеть",
            "description": "Платёжная система карты",
        },
        "currency_code": {
            "label": "Валюта",
            "description": "Основная валюта карты",
        },
        "is_fx_card": {
            "description": "Отметьте, если это валютная карта (USD/EUR)",
        },
        "is_debit_card": {
            "description": "Отметьте для дебетовой карты (обычно — да)",
        },
        "payroll_supported": {
            "description": "Можно ли использовать как зарплатную",
        },
        "issue_fee_text": {
            "description": "Текст, например: «Бесплатно» или «15 000 сум»",
        },
        "reissue_fee_text": {
            "description": "Например: «10 000 сум» или «Бесплатно»",
        },
        "transfer_fee_text": {
            "description": "Например: «0.5% от суммы» или «Бесплатно до 5 млн»",
        },
        "cashback_pct": {
            "description": "Число, например: 1.0",
        },
        "validity_months": {
            "description": "Число месяцев, например: 36, 60",
        },
        "issuance_time_text": {
            "description": "Например: «1-3 рабочих дня», «Моментально»",
        },
        "annual_fee_text": {
            "description": "Например: «Бесплатно» или «30 000 сум/год»",
        },
        "pin_setup_cbu_text": {
            "description": "Например: «Бесплатно в ЦБУ»",
        },
        "sms_setup_cbu_text": {
            "description": "Например: «1 000 сум/мес.»",
        },
        "pin_setup_mobile_text": {
            "description": "Например: «Через приложение Asakabank»",
        },
        "sms_setup_mobile_text": {
            "description": "Например: «Через приложение — бесплатно»",
        },
        "source_row_order": {
            "description": "Порядок в списке (автоматически для новых)",
        },
        "is_active": {
            "description": "Снимите, чтобы скрыть карту от клиентов",
        },
    }

    form_overrides = {
        "card_network": wtforms.SelectField,
        "currency_code": wtforms.SelectField,
    }

    async def scaffold_form(self, rules=None):
        form_class = await super().scaffold_form(rules)
        form_class.card_network = wtforms.SelectField(
            "Платёжная сеть",
            choices=self._NETWORK_CHOICES,
            description="Платёжная система карты",
        )
        form_class.currency_code = wtforms.SelectField(
            "Валюта",
            choices=self._CARD_CURRENCY_CHOICES,
            description="Основная валюта карты",
        )
        return form_class

    async def on_model_change(self, data: dict, model: Any, is_created: bool, request: Request) -> None:
        if is_created and not model.source_row_order:
            model.source_row_order = await _next_row_order(
                CardProductOffer, service_name=model.service_name,
            )
