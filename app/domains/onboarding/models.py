from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class OnboardingRecord(Base):
    """Tracks organizations created through the staff-led MVP workflow."""

    __tablename__ = "onboarding_records"
    __table_args__ = (
        CheckConstraint(
            "status IN ('in_progress', 'active')",
            name="valid_status",
        ),
        CheckConstraint(
            "mode IN ('staff_led', 'self_service')",
            name="valid_mode",
        ),
        Index("ix_onboarding_records_status_created_at", "status", "created_at"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    organization_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        unique=True,
    )
    owner_email: Mapped[str] = mapped_column(String(320))
    status: Mapped[str] = mapped_column(String(16), default="in_progress")
    mode: Mapped[str] = mapped_column(String(20), default="staff_led")
    created_by_user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    activated_by_user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    activated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
