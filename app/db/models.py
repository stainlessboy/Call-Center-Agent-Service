from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import List, Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base


class SessionStatus(str, enum.Enum):
    ACTIVE = "active"
    ENDED = "ended"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[Optional[str]] = mapped_column(String(255))
    first_name: Mapped[Optional[str]] = mapped_column(String(255))
    last_name: Mapped[Optional[str]] = mapped_column(String(255))
    phone: Mapped[Optional[str]] = mapped_column(String(32))
    language: Mapped[Optional[str]] = mapped_column(String(8))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    is_active: Mapped[bool] = mapped_column(Boolean, server_default="true", default=True)

    sessions: Mapped[List["ChatSession"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        order_by="ChatSession.started_at",
    )

    def __str__(self) -> str:
        return self.username or str(self.telegram_user_id)


class ChatSession(Base):
    __tablename__ = "chat_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True, nullable=False)
    title: Mapped[Optional[str]] = mapped_column(String(255))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    status: Mapped[SessionStatus] = mapped_column(
        Enum(SessionStatus, native_enum=False),
        default=SessionStatus.ACTIVE,
        server_default=SessionStatus.ACTIVE.value,
    )
    human_mode: Mapped[bool] = mapped_column(Boolean, server_default="false", default=False)
    human_mode_since: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    assigned_operator_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    last_activity_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    feedback_rating: Mapped[Optional[int]] = mapped_column(Integer)
    feedback_comment: Mapped[Optional[str]] = mapped_column(Text)
    closed_reason: Mapped[Optional[str]] = mapped_column(String(64))

    user: Mapped[User] = relationship(back_populates="sessions")
    messages: Mapped[List["Message"]] = relationship(
        back_populates="session",
        cascade="all, delete-orphan",
        order_by="Message.created_at",
    )

    @property
    def message_count(self) -> int:
        if self.messages:
            return len(self.messages)
        return 0

    def __str__(self) -> str:
        return f"{self.id} ({self.status})"


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("chat_sessions.id"), index=True, nullable=False)
    role: Mapped[str] = mapped_column(String(32))
    text: Mapped[str] = mapped_column(Text)
    telegram_message_id: Mapped[Optional[str]] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    latency_ms: Mapped[Optional[int]] = mapped_column(Integer)
    error_code: Mapped[Optional[str]] = mapped_column(String(64))
    llm_usage: Mapped[Optional[dict]] = mapped_column(JSONB)  # {model, prompt_tokens, completion_tokens, total_tokens, cost}

    session: Mapped[ChatSession] = relationship(back_populates="messages")


class Filial(Base):
    """Центр банковских услуг (ЦБУ) — главный офис с полным спектром услуг."""
    __tablename__ = "filials"

    # Used by agent/branches.py helpers to tag objects polymorphically
    OFFICE_TYPE_CODE = "filial"

    id: Mapped[int] = mapped_column(primary_key=True)

    name_ru: Mapped[str] = mapped_column(String(255), index=True)
    name_uz: Mapped[Optional[str]] = mapped_column(String(255))

    address_ru: Mapped[str] = mapped_column(Text)
    address_uz: Mapped[Optional[str]] = mapped_column(Text)

    # Only filials have landmarks and map URLs in the source data
    landmark_ru: Mapped[Optional[str]] = mapped_column(Text)
    landmark_uz: Mapped[Optional[str]] = mapped_column(Text)
    location_url: Mapped[Optional[str]] = mapped_column(String(512))

    latitude: Mapped[Optional[float]] = mapped_column(Float)
    longitude: Mapped[Optional[float]] = mapped_column(Float)
    phone: Mapped[Optional[str]] = mapped_column(String(64))
    hours: Mapped[Optional[str]] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    sales_offices: Mapped[List["SalesOffice"]] = relationship(
        back_populates="parent_filial", cascade="all, delete-orphan"
    )
    sales_points: Mapped[List["SalesPoint"]] = relationship(
        back_populates="parent_filial", cascade="all, delete-orphan"
    )


class SalesOffice(Base):
    """Офис продаж (мини-офис) — полный спектр услуг для физлиц, без юрлиц."""
    __tablename__ = "sales_offices"

    OFFICE_TYPE_CODE = "sales_office"

    id: Mapped[int] = mapped_column(primary_key=True)

    name_ru: Mapped[str] = mapped_column(String(255), index=True)
    name_uz: Mapped[Optional[str]] = mapped_column(String(255))

    region_ru: Mapped[Optional[str]] = mapped_column(String(255), index=True)
    region_uz: Mapped[Optional[str]] = mapped_column(String(255))

    address_ru: Mapped[str] = mapped_column(Text)
    address_uz: Mapped[Optional[str]] = mapped_column(Text)

    parent_filial_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("filials.id", ondelete="SET NULL"), index=True
    )
    parent_filial: Mapped[Optional["Filial"]] = relationship(back_populates="sales_offices")

    latitude: Mapped[Optional[float]] = mapped_column(Float)
    longitude: Mapped[Optional[float]] = mapped_column(Float)
    phone: Mapped[Optional[str]] = mapped_column(String(64))
    hours: Mapped[Optional[str]] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SalesPoint(Base):
    """Точка продаж (в автосалонах) — только автокредиты + консультации + ATM."""
    __tablename__ = "sales_points"

    OFFICE_TYPE_CODE = "sales_point"

    id: Mapped[int] = mapped_column(primary_key=True)

    name_ru: Mapped[str] = mapped_column(String(255), index=True)
    name_uz: Mapped[Optional[str]] = mapped_column(String(255))

    address_ru: Mapped[str] = mapped_column(Text)
    address_uz: Mapped[Optional[str]] = mapped_column(Text)

    parent_filial_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("filials.id", ondelete="SET NULL"), index=True
    )
    parent_filial: Mapped[Optional["Filial"]] = relationship(back_populates="sales_points")

    latitude: Mapped[Optional[float]] = mapped_column(Float)
    longitude: Mapped[Optional[float]] = mapped_column(Float)
    phone: Mapped[Optional[str]] = mapped_column(String(64))
    hours: Mapped[Optional[str]] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class FaqItem(Base):
    __tablename__ = "faq"

    id: Mapped[int] = mapped_column(primary_key=True)
    question_ru: Mapped[str] = mapped_column(Text)
    answer_ru: Mapped[str] = mapped_column(Text)
    question_en: Mapped[Optional[str]] = mapped_column(Text)
    answer_en: Mapped[Optional[str]] = mapped_column(Text)
    question_uz: Mapped[Optional[str]] = mapped_column(Text)
    answer_uz: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CreditProductOffer(Base):
    __tablename__ = "credit_product_offers"
    __table_args__ = (
        UniqueConstraint(
            "section_name",
            "source_row_order",
            "rate_order",
            "income_type",
            name="uq_credit_product_offers_row_rate",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    section_name: Mapped[str] = mapped_column(String(128), index=True)
    service_name: Mapped[str] = mapped_column(String(512), index=True)
    service_name_en: Mapped[Optional[str]] = mapped_column(String(512))
    service_name_uz: Mapped[Optional[str]] = mapped_column(String(512))
    min_age: Mapped[Optional[int]] = mapped_column(Integer)
    min_age_text: Mapped[Optional[str]] = mapped_column(String(128))
    purpose_text: Mapped[Optional[str]] = mapped_column(Text)
    amount_text: Mapped[Optional[str]] = mapped_column(Text)
    amount_min: Mapped[Optional[int]] = mapped_column(BigInteger)
    amount_max: Mapped[Optional[int]] = mapped_column(BigInteger)
    term_text: Mapped[Optional[str]] = mapped_column(String(255))
    term_min_months: Mapped[Optional[int]] = mapped_column(Integer)
    term_max_months: Mapped[Optional[int]] = mapped_column(Integer)
    downpayment_text: Mapped[Optional[str]] = mapped_column(String(255))
    downpayment_min_pct: Mapped[Optional[float]] = mapped_column(Float)
    downpayment_max_pct: Mapped[Optional[float]] = mapped_column(Float)
    income_type: Mapped[Optional[str]] = mapped_column(String(32), index=True)
    rate_text: Mapped[Optional[str]] = mapped_column(Text)
    rate_condition_text: Mapped[Optional[str]] = mapped_column(Text)
    rate_min_pct: Mapped[Optional[float]] = mapped_column(Float)
    rate_max_pct: Mapped[Optional[float]] = mapped_column(Float)
    collateral_text: Mapped[Optional[str]] = mapped_column(Text)
    source_path: Mapped[Optional[str]] = mapped_column(String(255))
    source_row_order: Mapped[int] = mapped_column(Integer)
    rate_order: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    is_active: Mapped[bool] = mapped_column(Boolean, server_default="true", default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )


class DepositProductOffer(Base):
    __tablename__ = "deposit_product_offers"
    __table_args__ = (
        UniqueConstraint(
            "service_name",
            "currency_code",
            "source_row_order",
            name="uq_deposit_product_offers_row_currency",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    service_name: Mapped[str] = mapped_column(String(512), index=True)
    service_name_en: Mapped[Optional[str]] = mapped_column(String(512))
    service_name_uz: Mapped[Optional[str]] = mapped_column(String(512))
    currency_code: Mapped[str] = mapped_column(String(8), index=True)  # UZS / USD / EUR
    min_amount_text: Mapped[Optional[str]] = mapped_column(Text)
    min_amount: Mapped[Optional[int]] = mapped_column(BigInteger)
    term_text: Mapped[Optional[str]] = mapped_column(String(255))
    term_months: Mapped[Optional[int]] = mapped_column(Integer, index=True)
    rate_text: Mapped[Optional[str]] = mapped_column(String(128))
    rate_pct: Mapped[Optional[float]] = mapped_column(Float, index=True)
    open_channel_text: Mapped[Optional[str]] = mapped_column(Text)
    payout_text: Mapped[Optional[str]] = mapped_column(Text)
    payout_monthly_available: Mapped[Optional[bool]] = mapped_column(Boolean)
    payout_end_available: Mapped[Optional[bool]] = mapped_column(Boolean)
    topup_text: Mapped[Optional[str]] = mapped_column(Text)
    topup_allowed: Mapped[Optional[bool]] = mapped_column(Boolean, index=True)
    partial_withdrawal_allowed: Mapped[Optional[bool]] = mapped_column(Boolean)
    notes_text: Mapped[Optional[str]] = mapped_column(Text)
    source_path: Mapped[Optional[str]] = mapped_column(String(255))
    source_row_order: Mapped[int] = mapped_column(Integer)
    is_active: Mapped[bool] = mapped_column(Boolean, server_default="true", default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )


class Lead(Base):
    __tablename__ = "leads"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[Optional[str]] = mapped_column(String(36), index=True)
    telegram_user_id: Mapped[Optional[int]] = mapped_column(BigInteger, index=True)
    product_category: Mapped[Optional[str]] = mapped_column(String(64))
    product_name: Mapped[Optional[str]] = mapped_column(String(512))
    amount: Mapped[Optional[int]] = mapped_column(BigInteger)
    term_months: Mapped[Optional[int]] = mapped_column(Integer)
    rate_pct: Mapped[Optional[float]] = mapped_column(Float)
    contact_name: Mapped[Optional[str]] = mapped_column(String(255))
    contact_phone: Mapped[Optional[str]] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), server_default="new", default="new")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CardProductOffer(Base):
    __tablename__ = "card_product_offers"
    __table_args__ = (
        UniqueConstraint("service_name", "source_row_order", name="uq_card_product_offers_row"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    service_name: Mapped[str] = mapped_column(String(512), index=True)
    service_name_en: Mapped[Optional[str]] = mapped_column(String(512))
    service_name_uz: Mapped[Optional[str]] = mapped_column(String(512))
    card_network: Mapped[Optional[str]] = mapped_column(String(32), index=True)  # uzcard/humo/visa/mastercard
    currency_code: Mapped[Optional[str]] = mapped_column(String(16), index=True)  # UZS / USD / EUR / MULTI / UNKNOWN
    is_fx_card: Mapped[bool] = mapped_column(Boolean, server_default="false", default=False, index=True)
    is_debit_card: Mapped[bool] = mapped_column(Boolean, server_default="true", default=True, index=True)
    payroll_supported: Mapped[Optional[bool]] = mapped_column(Boolean, index=True)
    issue_fee_text: Mapped[Optional[str]] = mapped_column(Text)
    issue_fee_free: Mapped[Optional[bool]] = mapped_column(Boolean, index=True)
    reissue_fee_text: Mapped[Optional[str]] = mapped_column(Text)
    transfer_fee_text: Mapped[Optional[str]] = mapped_column(Text)
    cashback_text: Mapped[Optional[str]] = mapped_column(String(128))
    cashback_pct: Mapped[Optional[float]] = mapped_column(Float)
    validity_text: Mapped[Optional[str]] = mapped_column(String(255))
    validity_months: Mapped[Optional[int]] = mapped_column(Integer)
    issuance_time_text: Mapped[Optional[str]] = mapped_column(Text)
    pin_setup_cbu_text: Mapped[Optional[str]] = mapped_column(Text)
    sms_setup_cbu_text: Mapped[Optional[str]] = mapped_column(Text)
    pin_setup_mobile_text: Mapped[Optional[str]] = mapped_column(Text)
    sms_setup_mobile_text: Mapped[Optional[str]] = mapped_column(Text)
    annual_fee_text: Mapped[Optional[str]] = mapped_column(Text)
    annual_fee_free: Mapped[Optional[bool]] = mapped_column(Boolean)
    mobile_order_available: Mapped[Optional[bool]] = mapped_column(Boolean, index=True)
    delivery_available: Mapped[Optional[bool]] = mapped_column(Boolean, index=True)
    pickup_available: Mapped[Optional[bool]] = mapped_column(Boolean, index=True)
    source_path: Mapped[Optional[str]] = mapped_column(String(255))
    source_row_order: Mapped[int] = mapped_column(Integer)
    is_active: Mapped[bool] = mapped_column(Boolean, server_default="true", default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )
