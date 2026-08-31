from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
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


class IntegrationConnection(Base):
    """One organization's connection to one third-party provider.

    ``encrypted_credentials`` is a Fernet token wrapping a JSON object (API key,
    OAuth tokens, etc.) and is never returned to a client. ``settings`` holds
    non-secret configuration such as the calendar/event id and timezone.
    """

    __tablename__ = "integration_connections"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'error', 'revoked')", name="valid_status"
        ),
        UniqueConstraint(
            "organization_id",
            "provider",
            name="uq_integration_connections_organization_id_provider",
        ),
        Index("ix_integration_connections_organization_id", "organization_id"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    organization_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="CASCADE")
    )
    provider: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(16))
    display_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    encrypted_credentials: Mapped[str] = mapped_column(Text)
    external_account_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    scopes: Mapped[str | None] = mapped_column(Text, nullable=True)
    settings: Mapped[str | None] = mapped_column(Text, nullable=True)
    token_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    last_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class CrmSyncJob(Base):
    """One queued post-call push to a CRM. Drained by the background worker with
    the same backoff/dead-letter shape as webhook deliveries.
    """

    __tablename__ = "crm_sync_jobs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'syncing', 'succeeded', 'failed', 'dead')",
            name="valid_status",
        ),
        UniqueConstraint(
            "organization_id",
            "provider",
            "call_id",
            "kind",
            name="uq_crm_sync_jobs_org_provider_call_kind",
        ),
        Index("ix_crm_sync_jobs_due", "status", "next_attempt_at"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    organization_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="CASCADE")
    )
    provider: Mapped[str] = mapped_column(String(40))
    call_id: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(String(24))
    status: Mapped[str] = mapped_column(String(16))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    locked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    result: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
