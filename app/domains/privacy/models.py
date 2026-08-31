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


class OrganizationPrivacySettings(Base):
    __tablename__ = "organization_privacy_settings"
    __table_args__ = (
        CheckConstraint(
            "transcript_retention_days IS NULL OR "
            "(transcript_retention_days >= 1 AND transcript_retention_days <= 3650)",
            name="valid_retention",
        ),
    )

    organization_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        primary_key=True,
    )
    transcript_retention_days: Mapped[int | None] = mapped_column(
        Integer, nullable=True, server_default="90"
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


class DataRequest(Base):
    __tablename__ = "data_requests"
    __table_args__ = (
        CheckConstraint("kind IN ('export', 'deletion')", name="valid_kind"),
        CheckConstraint(
            "status IN ('pending', 'processing', 'completed', 'failed', 'dead',"
            " 'cancelled')",
            name="valid_status",
        ),
        CheckConstraint("attempts >= 0 AND max_attempts >= 1", name="valid_attempts"),
        UniqueConstraint(
            "organization_id",
            "kind",
            "idempotency_key",
            name="uq_data_requests_org_kind_idempotency",
        ),
        Index("ix_data_requests_due", "status", "execute_after"),
        Index("ix_data_requests_org_created", "organization_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    organization_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="CASCADE")
    )
    requested_by_user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    kind: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16))
    attempts: Mapped[int] = mapped_column(Integer, server_default="0")
    max_attempts: Mapped[int] = mapped_column(Integer, server_default="5")
    idempotency_key: Mapped[str] = mapped_column(String(128))
    execute_after: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    locked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    result: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
