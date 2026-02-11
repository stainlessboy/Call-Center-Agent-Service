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
    question = models.TextField()
    answer = models.TextField()
    created_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        managed = False
        db_table = "faq"
        verbose_name = "FAQ"
        verbose_name_plural = "FAQ"

    def __str__(self) -> str:
        return self.question[:80]
