from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class BillingPlan(Base):
    __tablename__ = "billing_plans"
    __table_args__ = (
        CheckConstraint("status IN ('active', 'archived')", name="valid_status"),
        CheckConstraint("monthly_amount_minor >= 0", name="monthly_amount_nonnegative"),
        CheckConstraint("included_seconds >= 0", name="included_seconds_nonnegative"),
        CheckConstraint(
            "overage_amount_micros_per_second >= 0",
            name="overage_amount_nonnegative",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    code: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(16), default="active")
    currency: Mapped[str] = mapped_column(String(3))
    monthly_amount_minor: Mapped[int] = mapped_column(BigInteger)
    included_seconds: Mapped[int] = mapped_column(BigInteger)
    overage_amount_micros_per_second: Mapped[int] = mapped_column(BigInteger)
    stripe_price_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True, unique=True
    )
    stripe_meter_event_name: Mapped[str | None] = mapped_column(
        String(100), nullable=True
    )
    entitlements: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Subscription(Base):
    __tablename__ = "subscriptions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('not_started', 'checkout_pending', 'trialing', 'active',"
            " 'past_due', 'paused', 'incomplete', 'incomplete_expired', 'unpaid',"
            " 'canceled')",
            name="valid_status",
        ),
        UniqueConstraint("organization_id"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    organization_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="CASCADE")
    )
    billing_plan_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("billing_plans.id", ondelete="RESTRICT")
    )
    provider: Mapped[str] = mapped_column(String(32), default="stripe")
    provider_customer_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True, unique=True
    )
    provider_subscription_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True, unique=True
    )
    status: Mapped[str] = mapped_column(String(32), default="not_started")
    current_period_start: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    current_period_end: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    trial_end: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cancel_at_period_end: Mapped[bool] = mapped_column(Boolean, default=False)
    last_invoice_status: Mapped[str | None] = mapped_column(
        String(32), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class UsageEvent(Base):
    """An append-only usage or billing adjustment.

    Database triggers reject UPDATE and DELETE statements. Corrections append a
    negative event linked through ``reversal_of_event_id``.
    """

    __tablename__ = "usage_events"
    __table_args__ = (
        CheckConstraint("quantity <> 0", name="quantity_nonzero"),
        CheckConstraint("id <> reversal_of_event_id", name="not_self_reversal"),
        UniqueConstraint("organization_id", "id"),
        UniqueConstraint("organization_id", "source", "idempotency_key"),
        ForeignKeyConstraint(
            ["organization_id", "call_id"],
            ["calls.organization_id", "calls.call_id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "reversal_of_event_id"],
            ["usage_events.organization_id", "usage_events.id"],
            ondelete="RESTRICT",
        ),
        Index("ix_usage_events_organization_occurred", "organization_id", "occurred_at"),
        Index("ix_usage_events_organization_call", "organization_id", "call_id"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    organization_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="RESTRICT")
    )
    call_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    event_type: Mapped[str] = mapped_column(String(64))
    quantity: Mapped[int] = mapped_column(BigInteger)
    unit: Mapped[str] = mapped_column(String(32))
    provider_cost_micros: Mapped[int] = mapped_column(BigInteger, default=0)
    customer_charge_micros: Mapped[int] = mapped_column(BigInteger, default=0)
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    source: Mapped[str] = mapped_column(String(32))
    idempotency_key: Mapped[str] = mapped_column(String(255))
    provider_reference: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    reversal_of_event_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True
    )
    event_metadata: Mapped[str | None] = mapped_column("metadata", Text, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class UsageExport(Base):
    __tablename__ = "usage_exports"
    __table_args__ = (
        CheckConstraint("status IN ('pending', 'sent', 'failed')", name="valid_status"),
        UniqueConstraint("usage_event_id", "provider"),
        UniqueConstraint("provider", "provider_event_identifier"),
        Index("ix_usage_exports_provider_status", "provider", "status"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    usage_event_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("usage_events.id", ondelete="RESTRICT")
    )
    provider: Mapped[str] = mapped_column(String(32))
    provider_event_identifier: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(16), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class BillingProviderEvent(Base):
    """The immutable receipt proving a signed provider event was handled."""

    __tablename__ = "billing_provider_events"
    __table_args__ = (
        UniqueConstraint("provider", "provider_event_id"),
        Index("ix_billing_provider_events_received_at", "received_at"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    provider: Mapped[str] = mapped_column(String(32))
    provider_event_id: Mapped[str] = mapped_column(String(255))
    event_type: Mapped[str] = mapped_column(String(100))
    organization_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("organizations.id", ondelete="SET NULL"),
        nullable=True,
    )
    payload_sha256: Mapped[str] = mapped_column(String(64))
    outcome: Mapped[str] = mapped_column(String(32))
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class OrganizationSpendLimit(Base):
    __tablename__ = "organization_spend_limits"
    __table_args__ = (
        CheckConstraint(
            "monthly_limit_micros IS NULL OR monthly_limit_micros >= 1000000",
            name="valid_monthly_limit",
        ),
        CheckConstraint(
            "warning_threshold_percent >= 1 AND warning_threshold_percent <= 100",
            name="valid_warning_threshold",
        ),
    )

    organization_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        primary_key=True,
    )
    monthly_limit_micros: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    hard_limit: Mapped[bool] = mapped_column(Boolean, server_default=true())
    warning_threshold_percent: Mapped[int] = mapped_column(
        Integer, server_default="80"
    )
    blocked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_by_user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
