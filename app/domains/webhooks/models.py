from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

RECORD_ID_TYPE = BigInteger().with_variant(Integer(), "sqlite")


class WebhookEndpoint(Base):
    """A customer-registered HTTPS destination for one organization's events."""

    __tablename__ = "webhook_endpoints"
    __table_args__ = (
        Index("ix_webhook_endpoints_organization_id", "organization_id"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    organization_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="CASCADE")
    )
    url: Mapped[str] = mapped_column(String(2048))
    secret: Mapped[str] = mapped_column(String(80))
    description: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # JSON array of subscribed event types; NULL/empty means "every event type".
    event_types: Mapped[str | None] = mapped_column(Text, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class WebhookEvent(Base):
    """One thing that happened, fanned out to zero or more deliveries. The id is
    the stable event id sent to every endpoint.
    """

    __tablename__ = "webhook_events"
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "type",
            "dedupe_key",
            name="uq_webhook_events_organization_id_type_dedupe_key",
        ),
        Index("ix_webhook_events_organization_id_id", "organization_id", "id"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    organization_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="CASCADE")
    )
    type: Mapped[str] = mapped_column(String(64))
    dedupe_key: Mapped[str] = mapped_column(String(200))
    payload: Mapped[str] = mapped_column(Text)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class WebhookDelivery(Base):
    """The attempt to get one event to one endpoint, with retry bookkeeping."""

    __tablename__ = "webhook_deliveries"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'delivering', 'succeeded', 'failed', 'dead')",
            name="valid_status",
        ),
        UniqueConstraint(
            "webhook_event_id",
            "webhook_endpoint_id",
            name="uq_webhook_deliveries_event_endpoint",
        ),
        Index("ix_webhook_deliveries_due", "status", "next_attempt_at"),
        Index(
            "ix_webhook_deliveries_endpoint_id_id",
            "webhook_endpoint_id",
            "id",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    organization_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="CASCADE")
    )
    webhook_event_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("webhook_events.id", ondelete="CASCADE")
    )
    webhook_endpoint_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("webhook_endpoints.id", ondelete="CASCADE")
    )
    status: Mapped[str] = mapped_column(String(16))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    response_snippet: Mapped[str | None] = mapped_column(String(500), nullable=True)
    locked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class WebhookDeliveryAttempt(Base):
    """One HTTP attempt against an endpoint. Append-only history."""

    __tablename__ = "webhook_delivery_attempts"
    __table_args__ = (
        Index(
            "ix_webhook_delivery_attempts_delivery_id_id",
            "webhook_delivery_id",
            "id",
        ),
    )

    id: Mapped[int] = mapped_column(
        RECORD_ID_TYPE, primary_key=True, autoincrement=True
    )
    webhook_delivery_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("webhook_deliveries.id", ondelete="CASCADE")
    )
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    attempted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
