from __future__ import annotations

import json
import os
import ssl
import urllib.request
from typing import Optional, Tuple

from django import forms
from django.contrib import admin, messages
from django.db import transaction
from django.utils import timezone

from bank_admin.models import Branch, ChatSession, Message, User


def _send_telegram_message(token: str, chat_id: int, text: str) -> Tuple[bool, Optional[str]]:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({"chat_id": chat_id, "text": text}).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10, context=_build_ssl_context()) as response:
            data = json.loads(response.read().decode("utf-8"))
        if not data.get("ok"):
            return False, data.get("description") or "telegram api error"
        return True, None
    except Exception as exc:  # pragma: no cover - network issues
        return False, str(exc)


def _build_ssl_context() -> ssl.SSLContext:
    """
    Default SSL verification is disabled (to tolerate self-signed proxies).
    Set TELEGRAM_SSL_VERIFY=true to enable verification.
    """
    verify_raw = (os.getenv("TELEGRAM_SSL_VERIFY") or "").strip().lower()
    if verify_raw in {"1", "true", "yes", "on"}:
        return ssl.create_default_context()
    return ssl._create_unverified_context()  # pragma: no cover - opt-in only


class ChatSessionAdminForm(forms.ModelForm):
    operator_reply = forms.CharField(
        label="Ответ оператором",
        required=False,
        widget=forms.Textarea(attrs={"rows": 4}),
        help_text="Отправится пользователю в Telegram и сохранится в истории сообщений.",
    )

    class Meta:
        model = ChatSession
        fields = "__all__"


class MessageInline(admin.TabularInline):
    model = Message
    extra = 0
    can_delete = False
    fields = ("created_at", "role", "text", "telegram_message_id")
    readonly_fields = ("created_at", "role", "text", "telegram_message_id")
    ordering = ("created_at",)

    def has_add_permission(self, request, obj=None) -> bool:
        return False


@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    list_display = ("id", "telegram_user_id", "username", "language", "created_at", "is_active")
    search_fields = ("telegram_user_id", "username", "first_name", "last_name", "phone")
    list_filter = ("language", "is_active")
    actions = ["delete_with_related"]

    @admin.action(description="Удалить пользователей вместе с сессиями и сообщениями")
    def delete_with_related(self, request, queryset):
        with transaction.atomic():
            user_ids = list(queryset.values_list("id", flat=True))
            session_ids = list(ChatSession.objects.filter(user_id__in=user_ids).values_list("id", flat=True))
            if session_ids:
                Message.objects.filter(session_id__in=session_ids).delete()
                ChatSession.objects.filter(id__in=session_ids).delete()
            queryset.delete()


@admin.register(ChatSession)
class ChatSessionAdmin(admin.ModelAdmin):
    form = ChatSessionAdminForm
    inlines = [MessageInline]
    list_display = (
        "id",
        "user",
        "status",
        "human_mode",
        "assigned_operator_id",
        "started_at",
        "ended_at",
        "last_activity_at",
        "feedback_rating",
        "closed_reason",
    )
    list_filter = ("status", "closed_reason", "human_mode")
    search_fields = ("id", "user__username", "user__telegram_user_id")
    date_hierarchy = "started_at"
    actions = ["delete_with_messages"]

    def save_model(self, request, obj, form, change) -> None:
        operator_reply = (form.cleaned_data.get("operator_reply") or "").strip()
        if operator_reply:
            obj.human_mode = True
            obj.human_mode_since = obj.human_mode_since or timezone.now()
            obj.last_activity_at = timezone.now()
        super().save_model(request, obj, form, change)

        if not operator_reply:
            return

        token = os.getenv("BOT_TOKEN", "8482217460:AAHlXfkBDv1JEYNmqVzrwoslRqRr_pOSaok").strip()
        if not token:
            self.message_user(
                request,
                "BOT_TOKEN не настроен. Сообщение не отправлено.",
                level=messages.ERROR,
            )
            return

        user = User.objects.filter(id=obj.user_id).first()
        if user is None:
            self.message_user(request, "Пользователь не найден.", level=messages.ERROR)
            return

        operator_name = request.user.get_username() or None
        label = "👤 Оператор"
        if operator_name:
            label = f"{label} ({operator_name})"
        ok, error = _send_telegram_message(
            token,
            user.telegram_user_id,
            f"{label}: {operator_reply}",
        )
        if not ok:
            self.message_user(
                request,
                f"Не удалось отправить сообщение: {error}",
                level=messages.ERROR,
            )
            return

        Message.objects.create(
            session_id=obj.id,
            role="operator",
            text=operator_reply,
            created_at=timezone.now(),
        )
        self.message_user(request, "Сообщение отправлено пользователю.", level=messages.SUCCESS)

    @admin.action(description="Удалить сессии вместе с сообщениями")
    def delete_with_messages(self, request, queryset):
        with transaction.atomic():
            session_ids = list(queryset.values_list("id", flat=True))
            Message.objects.filter(session_id__in=session_ids).delete()
            queryset.delete()


@admin.register(Message)
class MessageAdmin(admin.ModelAdmin):
    list_display = ("id", "session", "role", "created_at", "latency_ms", "error_code")
    search_fields = ("session__id", "text", "role")
    list_filter = ("role", "error_code")
    date_hierarchy = "created_at"


@admin.register(Branch)
class BranchAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "region", "district", "phone", "hours")
    list_display_links = ("id", "name")
    search_fields = ("name", "region", "district", "address", "phone")
    list_filter = ("region", "district")
    fieldsets = (
        ("Основное", {"fields": ("name", "region", "district", "address", "landmarks", "metro")}),
        ("Контакты", {"fields": ("phone", "hours", "weekend")}),
        ("Реквизиты", {"fields": ("inn", "mfo", "postal_index")}),
        ("Счета", {"fields": ("uzcard_accounts", "humo_accounts")}),
        ("Гео", {"fields": ("latitude", "longitude")}),
    )
