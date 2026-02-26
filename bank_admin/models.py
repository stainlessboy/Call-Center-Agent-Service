from __future__ import annotations

from django.db import models


class User(models.Model):
    id = models.AutoField(primary_key=True)
    telegram_user_id = models.BigIntegerField(unique=True)
    username = models.CharField(max_length=255, blank=True, null=True)
    first_name = models.CharField(max_length=255, blank=True, null=True)
    last_name = models.CharField(max_length=255, blank=True, null=True)
    phone = models.CharField(max_length=32, blank=True, null=True)
    language = models.CharField(max_length=8, blank=True, null=True)
    created_at = models.DateTimeField(blank=True, null=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        managed = False
        db_table = "users"
        verbose_name = "User"
        verbose_name_plural = "Users"

    def __str__(self) -> str:
        return f"{self.username or self.telegram_user_id}"


class ChatSession(models.Model):
    id = models.CharField(primary_key=True, max_length=36)
    user = models.ForeignKey(User, on_delete=models.DO_NOTHING, db_column="user_id", related_name="sessions")
    title = models.CharField(max_length=255, blank=True, null=True)
    started_at = models.DateTimeField(blank=True, null=True)
    ended_at = models.DateTimeField(blank=True, null=True)
    status = models.CharField(max_length=32, blank=True, null=True)
    human_mode = models.BooleanField(default=False)
    human_mode_since = models.DateTimeField(blank=True, null=True)
    assigned_operator_id = models.BigIntegerField(blank=True, null=True)
    last_activity_at = models.DateTimeField(blank=True, null=True)
    feedback_rating = models.IntegerField(blank=True, null=True)
    feedback_comment = models.TextField(blank=True, null=True)
    closed_reason = models.CharField(max_length=64, blank=True, null=True)

    class Meta:
        managed = False
        db_table = "chat_sessions"
        verbose_name = "Chat Session"
        verbose_name_plural = "Chat Sessions"

    def __str__(self) -> str:
        return f"{self.id} ({self.status})"


class Message(models.Model):
    id = models.AutoField(primary_key=True)
    session = models.ForeignKey(ChatSession, on_delete=models.DO_NOTHING, db_column="session_id", related_name="messages")
    role = models.CharField(max_length=32)
    text = models.TextField()
    telegram_message_id = models.CharField(max_length=64, blank=True, null=True)
    created_at = models.DateTimeField(blank=True, null=True)
    latency_ms = models.IntegerField(blank=True, null=True)
    agent_model = models.CharField(max_length=128, blank=True, null=True)
    error_code = models.CharField(max_length=64, blank=True, null=True)

    class Meta:
        managed = False
        db_table = "messages"
        verbose_name = "Message"
        verbose_name_plural = "Messages"

    def __str__(self) -> str:
        return f"{self.role}: {self.text[:40]}..."



class Branch(models.Model):
    id = models.AutoField(primary_key=True)
    name = models.CharField(max_length=255)
    region = models.CharField(max_length=255)
    district = models.CharField(max_length=255, blank=True, null=True)
    address = models.TextField(blank=True, null=True)
    landmarks = models.TextField(blank=True, null=True)
    metro = models.CharField(max_length=255, blank=True, null=True)
    phone = models.CharField(max_length=64, blank=True, null=True)
    hours = models.CharField(max_length=255, blank=True, null=True)
    weekend = models.CharField(max_length=255, blank=True, null=True)
    inn = models.CharField(max_length=64, blank=True, null=True)
    mfo = models.CharField(max_length=64, blank=True, null=True)
    postal_index = models.CharField(max_length=32, blank=True, null=True)
    uzcard_accounts = models.TextField(blank=True, null=True)
    humo_accounts = models.TextField(blank=True, null=True)
    latitude = models.FloatField(blank=True, null=True)
    longitude = models.FloatField(blank=True, null=True)
    created_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        managed = False
        db_table = "branches"
        verbose_name = "Отделение"
        verbose_name_plural = "Отделения"

    def __str__(self) -> str:
        return f"{self.name} ({self.region})"


class FaqItem(models.Model):
    id = models.AutoField(primary_key=True)
    question_ru = models.TextField()
    answer_ru = models.TextField()
    question_en = models.TextField(blank=True, null=True)
    answer_en = models.TextField(blank=True, null=True)
    question_uz = models.TextField(blank=True, null=True)
    answer_uz = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        managed = False
        db_table = "faq"
        verbose_name = "FAQ"
        verbose_name_plural = "FAQ"

    def __str__(self) -> str:
        return self.question_ru[:80]


class CreditProductOffer(models.Model):
    id = models.AutoField(primary_key=True)
    section_name = models.CharField(max_length=128)
    service_name = models.CharField(max_length=512)
    min_age = models.IntegerField(blank=True, null=True)
    min_age_text = models.CharField(max_length=128, blank=True, null=True)
    purpose_text = models.TextField(blank=True, null=True)
    amount_text = models.TextField(blank=True, null=True)
    amount_min = models.BigIntegerField(blank=True, null=True)
    amount_max = models.BigIntegerField(blank=True, null=True)
    term_text = models.CharField(max_length=255, blank=True, null=True)
    term_min_months = models.IntegerField(blank=True, null=True)
    term_max_months = models.IntegerField(blank=True, null=True)
    downpayment_text = models.CharField(max_length=255, blank=True, null=True)
    downpayment_min_pct = models.FloatField(blank=True, null=True)
    downpayment_max_pct = models.FloatField(blank=True, null=True)
    income_type = models.CharField(max_length=32, blank=True, null=True)
    rate_text = models.TextField(blank=True, null=True)
    rate_condition_text = models.TextField(blank=True, null=True)
    rate_min_pct = models.FloatField(blank=True, null=True)
    rate_max_pct = models.FloatField(blank=True, null=True)
    collateral_text = models.TextField(blank=True, null=True)
    source_path = models.CharField(max_length=255, blank=True, null=True)
    source_row_order = models.IntegerField()
    rate_order = models.IntegerField()
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(blank=True, null=True)
    updated_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        managed = False
        db_table = "credit_product_offers"
        verbose_name = "Кредитный оффер"
        verbose_name_plural = "Кредитные офферы"

    def __str__(self) -> str:
        return f"{self.section_name}: {self.service_name} [{self.income_type or 'any'}]"


class DepositProductOffer(models.Model):
    id = models.AutoField(primary_key=True)
    service_name = models.CharField(max_length=512)
    currency_code = models.CharField(max_length=8)
    min_amount_text = models.TextField(blank=True, null=True)
    min_amount = models.BigIntegerField(blank=True, null=True)
    term_text = models.CharField(max_length=255, blank=True, null=True)
    term_months = models.IntegerField(blank=True, null=True)
    rate_text = models.CharField(max_length=128, blank=True, null=True)
    rate_pct = models.FloatField(blank=True, null=True)
    open_channel_text = models.TextField(blank=True, null=True)
    payout_text = models.TextField(blank=True, null=True)
    payout_monthly_available = models.BooleanField(blank=True, null=True)
    payout_end_available = models.BooleanField(blank=True, null=True)
    topup_text = models.TextField(blank=True, null=True)
    topup_allowed = models.BooleanField(blank=True, null=True)
    partial_withdrawal_allowed = models.BooleanField(blank=True, null=True)
    notes_text = models.TextField(blank=True, null=True)
    source_path = models.CharField(max_length=255, blank=True, null=True)
    source_row_order = models.IntegerField()
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(blank=True, null=True)
    updated_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        managed = False
        db_table = "deposit_product_offers"
        verbose_name = "Оффер вклада"
        verbose_name_plural = "Офферы вкладов"

    def __str__(self) -> str:
        return f"{self.service_name} [{self.currency_code}] {self.term_text or ''}".strip()


class Lead(models.Model):
    id = models.AutoField(primary_key=True)
    session_id = models.CharField(max_length=36, blank=True, null=True)
    telegram_user_id = models.BigIntegerField(blank=True, null=True)
    product_category = models.CharField(max_length=64, blank=True, null=True)
    product_name = models.CharField(max_length=512, blank=True, null=True)
    amount = models.BigIntegerField(blank=True, null=True)
    term_months = models.IntegerField(blank=True, null=True)
    rate_pct = models.FloatField(blank=True, null=True)
    contact_name = models.CharField(max_length=255, blank=True, null=True)
    contact_phone = models.CharField(max_length=64, blank=True, null=True)
    status = models.CharField(max_length=32, blank=True, null=True)
    created_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        managed = False
        db_table = "leads"
        verbose_name = "Лид"
        verbose_name_plural = "Лиды"

    def __str__(self) -> str:
        base = self.product_name or self.product_category or "Lead"
        return f"{base} ({self.status or 'new'})"


class CardProductOffer(models.Model):
    id = models.AutoField(primary_key=True)
    service_name = models.CharField(max_length=512)
    card_network = models.CharField(max_length=32, blank=True, null=True)
    currency_code = models.CharField(max_length=16, blank=True, null=True)
    is_fx_card = models.BooleanField(default=False)
    is_debit_card = models.BooleanField(default=True)
    payroll_supported = models.BooleanField(blank=True, null=True)
    issue_fee_text = models.TextField(blank=True, null=True)
    issue_fee_free = models.BooleanField(blank=True, null=True)
    reissue_fee_text = models.TextField(blank=True, null=True)
    transfer_fee_text = models.TextField(blank=True, null=True)
    cashback_text = models.CharField(max_length=128, blank=True, null=True)
    cashback_pct = models.FloatField(blank=True, null=True)
    validity_text = models.CharField(max_length=255, blank=True, null=True)
    validity_months = models.IntegerField(blank=True, null=True)
    issuance_time_text = models.TextField(blank=True, null=True)
    pin_setup_cbu_text = models.TextField(blank=True, null=True)
    sms_setup_cbu_text = models.TextField(blank=True, null=True)
    pin_setup_mobile_text = models.TextField(blank=True, null=True)
    sms_setup_mobile_text = models.TextField(blank=True, null=True)
    annual_fee_text = models.TextField(blank=True, null=True)
    annual_fee_free = models.BooleanField(blank=True, null=True)
    mobile_order_available = models.BooleanField(blank=True, null=True)
    delivery_available = models.BooleanField(blank=True, null=True)
    pickup_available = models.BooleanField(blank=True, null=True)
    source_path = models.CharField(max_length=255, blank=True, null=True)
    source_row_order = models.IntegerField()
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(blank=True, null=True)
    updated_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        managed = False
        db_table = "card_product_offers"
        verbose_name = "Оффер карты"
        verbose_name_plural = "Офферы карт"

    def __str__(self) -> str:
        kind = "FX" if self.is_fx_card else "Debit"
        return f"{self.service_name} [{kind}]"
