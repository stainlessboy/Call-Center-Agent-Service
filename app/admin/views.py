from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

import httpx
import wtforms
from sqladmin import ModelView, action
from sqladmin.filters import BooleanFilter, AllUniqueStringValuesFilter, StaticValuesFilter
from sqlalchemy import func as sa_func, select
from sqlalchemy.orm import selectinload
from starlette.requests import Request
from starlette.responses import RedirectResponse

from app.config import get_settings
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
# ChatSession (with operator_reply feature)
# ---------------------------------------------------------------------------

class ChatSessionAdmin(ModelView, model=ChatSession):
    name = "Сессия чата"
    name_plural = "Сессии чатов"
    icon = "fa-solid fa-comments"
    details_template = "sqladmin/chat_details.html"

    column_list = [
        ChatSession.id, ChatSession.user, ChatSession.status,
        ChatSession.human_mode, ChatSession.assigned_operator_id,
        ChatSession.started_at, ChatSession.ended_at,
        ChatSession.last_activity_at, ChatSession.feedback_rating,
        ChatSession.closed_reason,
    ]
    column_filters = [
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
        ChatSession.closed_reason, ChatSession.messages,
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
    }

    column_formatters = {
        ChatSession.started_at: lambda m, _: _fmt_dt(m.started_at),
        ChatSession.ended_at: lambda m, _: _fmt_dt(m.ended_at),
        ChatSession.last_activity_at: lambda m, _: _fmt_dt(m.last_activity_at),
        ChatSession.status: lambda m, _: _STATUS_MAP.get(m.status.value if hasattr(m.status, "value") else m.status, m.status),
        ChatSession.closed_reason: lambda m, _: _CLOSED_REASON_MAP.get(m.closed_reason, m.closed_reason or ""),
    }
    column_formatters_detail = {
        ChatSession.started_at: lambda m, _: _fmt_dt(m.started_at),
        ChatSession.ended_at: lambda m, _: _fmt_dt(m.ended_at),
        ChatSession.last_activity_at: lambda m, _: _fmt_dt(m.last_activity_at),
        ChatSession.human_mode_since: lambda m, _: _fmt_dt(m.human_mode_since),
        ChatSession.status: lambda m, _: _STATUS_MAP.get(m.status.value if hasattr(m.status, "value") else m.status, m.status),
        ChatSession.closed_reason: lambda m, _: _CLOSED_REASON_MAP.get(m.closed_reason, m.closed_reason or ""),
    }

    def details_query(self, request):
        return select(ChatSession).options(selectinload(ChatSession.messages))

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
    name = "Сообщение"
    name_plural = "Сообщения"
    icon = "fa-solid fa-envelope"

    column_list = [Message.id, Message.session, Message.role, Message.created_at, Message.latency_ms, Message.error_code]
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
        "telegram_message_id": "Telegram ID сообщения",
        "created_at": "Дата создания",
        "latency_ms": "Задержка (мс)",
        "agent_model": "Модель агента",
        "error_code": "Код ошибки",
    }

    column_formatters = {
        Message.created_at: lambda m, _: _fmt_dt(m.created_at),
        Message.role: lambda m, _: _ROLE_MAP.get(m.role, m.role),
    }
    column_formatters_detail = {
        Message.created_at: lambda m, _: _fmt_dt(m.created_at),
        Message.role: lambda m, _: _ROLE_MAP.get(m.role, m.role),
    }


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
        _RuAllUniqueFilter(Branch.region, title="Регион"),
        _RuAllUniqueFilter(Branch.district, title="Район"),
    ]
    column_sortable_list = [Branch.id, Branch.name, Branch.region]
    column_default_sort = ("id", True)

    column_labels = {
        "id": "ID",
        "name": "Название",
        "region": "Регион",
        "district": "Район",
        "address": "Адрес",
        "landmarks": "Ориентиры",
        "metro": "Метро",
        "phone": "Телефон",
        "hours": "Время работы",
        "weekend": "Выходные",
        "inn": "ИНН",
        "mfo": "МФО",
        "postal_index": "Почтовый индекс",
        "uzcard_accounts": "Счета Uzcard",
        "humo_accounts": "Счета HUMO",
        "latitude": "Широта",
        "longitude": "Долгота",
        "created_at": "Дата создания",
    }

    _REGION_CHOICES = [
        ("Ташкент", "Ташкент"),
        ("Ташкентская область", "Ташкентская область"),
        ("Самарканд", "Самарканд"),
        ("Бухара", "Бухара"),
        ("Андижан", "Андижан"),
        ("Фергана", "Фергана"),
        ("Наманган", "Наманган"),
        ("Хорезм", "Хорезм"),
        ("Навои", "Навои"),
        ("Кашкадарья", "Кашкадарья"),
        ("Сурхандарья", "Сурхандарья"),
        ("Сырдарья", "Сырдарья"),
        ("Джизак", "Джизак"),
        ("Каракалпакстан", "Каракалпакстан"),
    ]

    form_overrides = {
        "region": wtforms.SelectField,
    }

    async def scaffold_form(self, rules=None):
        form_class = await super().scaffold_form(rules)
        form_class.region = wtforms.SelectField(
            "Регион",
            choices=self._REGION_CHOICES,
            description="Область / город республиканского значения",
        )
        return form_class


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
        CreditProductOffer.service_name, CreditProductOffer.income_type,
        CreditProductOffer.rate_min_pct, CreditProductOffer.rate_max_pct,
        CreditProductOffer.term_min_months, CreditProductOffer.term_max_months,
        CreditProductOffer.downpayment_min_pct, CreditProductOffer.downpayment_max_pct,
        CreditProductOffer.is_active,
    ]
    column_searchable_list = [
        CreditProductOffer.service_name, CreditProductOffer.service_name_en,
        CreditProductOffer.service_name_uz, CreditProductOffer.section_name,
        CreditProductOffer.rate_condition_text, CreditProductOffer.collateral_text,
    ]
    column_filters = [
        _RuAllUniqueFilter(CreditProductOffer.section_name, title="Раздел"),
        _RuStaticValuesFilter(
            CreditProductOffer.income_type, title="Тип дохода",
            values=[("payroll", "Зарплатный проект"), ("official", "Официальный доход"), ("no_official", "Без офиц. дохода")],
        ),
        _RuBooleanFilter(CreditProductOffer.is_active, title="Активен"),
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

    column_details_exclude_list = [CreditProductOffer.source_path]

    column_formatters = {
        CreditProductOffer.income_type: lambda m, _: _INCOME_TYPE_MAP.get(m.income_type or "", m.income_type or ""),
    }
    column_formatters_detail = {
        CreditProductOffer.income_type: lambda m, _: _INCOME_TYPE_MAP.get(m.income_type or "", m.income_type or ""),
    }

    form_excluded_columns = [
        CreditProductOffer.source_path,
        CreditProductOffer.created_at,
        CreditProductOffer.updated_at,
    ]

    column_labels = {
        "id": "ID",
        "section_name": "Раздел", "service_name": "Название продукта",
        "service_name_en": "Название (EN)", "service_name_uz": "Название (UZ)",
        "income_type": "Тип дохода", "rate_min_pct": "Ставка мин. %",
        "rate_max_pct": "Ставка макс. %", "rate_text": "Ставка (текст)",
        "rate_condition_text": "Условие ставки",
        "term_min_months": "Срок мин. (мес.)", "term_max_months": "Срок макс. (мес.)",
        "term_text": "Срок (текст)",
        "downpayment_min_pct": "Взнос мин. %", "downpayment_max_pct": "Взнос макс. %",
        "downpayment_text": "Взнос (текст)",
        "amount_text": "Сумма (текст)", "amount_min": "Сумма мин.", "amount_max": "Сумма макс.",
        "min_age": "Мин. возраст", "min_age_text": "Возраст (текст)",
        "purpose_text": "Цель кредита", "collateral_text": "Обеспечение",
        "source_row_order": "Порядок сортировки", "rate_order": "Вариант ставки",
        "is_active": "Активен", "created_at": "Создано", "updated_at": "Обновлено",
    }

    _SECTION_CHOICES = [
        ("Ипотека", "Ипотека"),
        ("Автокредит", "Автокредит"),
        ("Микрозайм", "Микрозайм"),
        ("Образовательный", "Образовательный"),
    ]

    _INCOME_TYPE_CHOICES = [
        ("", "— для всех —"),
        ("payroll", "payroll — зарплатный проект"),
        ("official", "official — официальный доход"),
        ("no_official", "no_official — без официального дохода"),
    ]

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
        "income_type": {
            "label": "Тип дохода",
            "description": "Вариант дохода заёмщика для этой ставки",
        },
        "rate_min_pct": {
            "description": "Число, например: 21.0",
        },
        "rate_max_pct": {
            "description": "Число, например: 24.0 (если ставка фиксированная — совпадает с мин.)",
        },
        "rate_text": {
            "description": "Текст для отображения, например: «от 21% годовых»",
        },
        "rate_condition_text": {
            "description": "Условие, при котором действует ставка. Например: «При зарплатном проекте банка»",
        },
        "term_min_months": {
            "description": "Минимальный срок в месяцах, например: 12",
        },
        "term_max_months": {
            "description": "Максимальный срок в месяцах, например: 240 (= 20 лет)",
        },
        "term_text": {
            "description": "Текст для отображения, например: «от 1 года до 20 лет»",
        },
        "downpayment_min_pct": {
            "description": "Минимальный % первоначального взноса, например: 20",
        },
        "downpayment_max_pct": {
            "description": "Максимальный %, например: 80 (обычно не нужен)",
        },
        "downpayment_text": {
            "description": "Текст для клиента, например: «от 20%»",
        },
        "amount_text": {
            "description": "Сумма кредита (текст), например: «до 1 500 000 000 сум»",
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
        "min_age_text": {
            "description": "Текст, например: «от 21 года»",
        },
        "purpose_text": {
            "description": "Например: «Покупка/строительство жилья»",
        },
        "collateral_text": {
            "description": "Например: «Залог приобретаемого жилья»",
        },
        "source_row_order": {
            "description": "Порядок в списке (автоматически для новых). Например: 1, 2, 3",
        },
        "rate_order": {
            "description": "Номер варианта ставки внутри продукта (авто = 1). Если у продукта несколько строк с разным income_type — увеличьте",
        },
        "is_active": {
            "description": "Снимите, чтобы скрыть продукт от клиентов",
        },
    }

    form_overrides = {
        "section_name": wtforms.SelectField,
        "income_type": wtforms.SelectField,
    }
    form_widget_args = {
        "section_name": {"coerce": str},
        "income_type": {"coerce": str},
    }

    async def scaffold_form(self, rules=None):
        form_class = await super().scaffold_form(rules)
        form_class.section_name = wtforms.SelectField(
            "Раздел",
            choices=self._SECTION_CHOICES,
            description="Категория кредитного продукта",
        )
        form_class.income_type = wtforms.SelectField(
            "Тип дохода",
            choices=self._INCOME_TYPE_CHOICES,
            description="Вариант дохода заёмщика для этой ставки",
        )
        return form_class

    async def on_model_change(self, data: dict, model: Any, is_created: bool, request: Request) -> None:
        if is_created:
            if not model.source_row_order:
                model.source_row_order = await _next_row_order(
                    CreditProductOffer, section_name=model.section_name,
                )
            if not model.rate_order:
                model.rate_order = 1


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
        DepositProductOffer.service_name, DepositProductOffer.service_name_en,
        DepositProductOffer.service_name_uz, DepositProductOffer.term_text,
        DepositProductOffer.payout_text, DepositProductOffer.topup_text,
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
        DepositProductOffer.currency_code, DepositProductOffer.source_row_order,
    ]
    column_default_sort = [
        (DepositProductOffer.service_name, False),
        (DepositProductOffer.currency_code, False),
        (DepositProductOffer.source_row_order, False),
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
        "term_text": "Срок (текст)", "term_months": "Срок (мес.)",
        "rate_text": "Ставка (текст)", "rate_pct": "Ставка %",
        "min_amount_text": "Мин. сумма (текст)", "min_amount": "Мин. сумма",
        "open_channel_text": "Способ оформления",
        "payout_text": "Выплата процентов",
        "payout_monthly_available": "Ежемесячная выплата",
        "payout_end_available": "Выплата в конце",
        "topup_text": "Пополнение (текст)", "topup_allowed": "Пополнение",
        "partial_withdrawal_allowed": "Частичное снятие",
        "notes_text": "Примечания",
        "source_row_order": "Порядок сортировки", "is_active": "Активен",
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
        "term_text": {
            "description": "Текст для клиента, например: «6 месяцев», «1 год»",
        },
        "term_months": {
            "description": "Число месяцев, например: 6, 12, 24",
        },
        "rate_text": {
            "description": "Текст ставки, например: «24% годовых»",
        },
        "rate_pct": {
            "description": "Число, например: 24.0",
        },
        "min_amount_text": {
            "description": "Текст, например: «от 500 000 сум»",
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
        "topup_text": {
            "description": "Например: «Пополнение доступно без ограничений»",
        },
        "notes_text": {
            "description": "Дополнительные условия, например: «Досрочное расторжение — по ставке до востребования»",
        },
        "source_row_order": {
            "description": "Порядок в списке (автоматически для новых)",
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

    async def on_model_change(self, data: dict, model: Any, is_created: bool, request: Request) -> None:
        if is_created and not model.source_row_order:
            model.source_row_order = await _next_row_order(
                DepositProductOffer,
                service_name=model.service_name,
                currency_code=model.currency_code,
            )


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
        "cashback_text": "Кэшбэк (текст)", "cashback_pct": "Кэшбэк %",
        "validity_text": "Срок действия", "validity_months": "Срок (мес.)",
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
        "cashback_text": {
            "description": "Например: «1% на все покупки», «до 3% на АЗС»",
        },
        "cashback_pct": {
            "description": "Число, например: 1.0",
        },
        "validity_text": {
            "description": "Например: «3 года», «5 лет»",
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
            "description": "Например: «Через приложение AsakaBank»",
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
