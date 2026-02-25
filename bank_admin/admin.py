from __future__ import annotations

import os

from django import forms
from django.contrib import admin, messages
from django.db import transaction
from django.utils import timezone

from app.services.telegram_sender import send_telegram_message
from bank_admin.models import (
    Branch,
    CardProductOffer,
    ChatSession,
    CreditProductOffer,
    DepositProductOffer,
    FaqItem,
    Message,
    User,
)


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
        ok, error = send_telegram_message(
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


@admin.register(FaqItem)
class FaqItemAdmin(admin.ModelAdmin):
    list_display = ("id", "question_ru", "question_en", "question_uz", "created_at")
    list_display_links = ("id", "question_ru")
    search_fields = (
        "question_ru",
        "answer_ru",
        "question_en",
        "answer_en",
        "question_uz",
        "answer_uz",
    )
    ordering = ("-id",)


@admin.register(CreditProductOffer)
class CreditProductOfferAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "section_name",
        "service_name",
        "income_type",
        "rate_min_pct",
        "rate_max_pct",
        "term_min_months",
        "term_max_months",
        "downpayment_min_pct",
        "downpayment_max_pct",
        "is_active",
    )
    list_display_links = ("id", "service_name")
    search_fields = ("service_name", "section_name", "rate_condition_text", "collateral_text")
    list_filter = ("section_name", "income_type", "is_active")
    ordering = ("section_name", "source_row_order", "rate_order")


@admin.register(DepositProductOffer)
class DepositProductOfferAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "service_name",
        "currency_code",
        "term_text",
        "term_months",
        "rate_pct",
        "topup_allowed",
        "is_active",
    )
    list_display_links = ("id", "service_name")
    search_fields = ("service_name", "term_text", "payout_text", "topup_text", "notes_text")
    list_filter = ("currency_code", "topup_allowed", "payout_monthly_available", "payout_end_available", "is_active")
    ordering = ("service_name", "currency_code", "source_row_order")


@admin.register(CardProductOffer)
class CardProductOfferAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "service_name",
        "card_network",
        "currency_code",
        "is_fx_card",
        "payroll_supported",
        "issue_fee_free",
        "annual_fee_free",
        "mobile_order_available",
        "is_active",
    )
    list_display_links = ("id", "service_name")
    search_fields = ("service_name", "issue_fee_text", "annual_fee_text", "issuance_time_text")
    list_filter = ("card_network", "currency_code", "is_fx_card", "payroll_supported", "issue_fee_free", "is_active")
    ordering = ("source_row_order", "service_name")
