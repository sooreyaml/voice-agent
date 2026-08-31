from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class TelephonyProvisioningRequest(Base):
    __tablename__ = "telephony_provisioning_requests"
    __table_args__ = (
        CheckConstraint(
            "status IN ('provisioning', 'ready', 'failed', 'verified')",
            name="valid_status",
        ),
        CheckConstraint(
            "number_type IN ('local', 'mobile', 'national', 'toll_free')",
            name="valid_number_type",
        ),
        ForeignKeyConstraint(
            ["organization_id", "business_profile_id"],
            ["business_profiles.organization_id", "business_profiles.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["organization_id", "phone_number_id"],
            ["phone_numbers.organization_id", "phone_numbers.id"],
            ondelete="RESTRICT",
        ),
        UniqueConstraint("organization_id", "idempotency_key"),
        Index(
            "ix_telephony_provisioning_requests_organization_status",
            "organization_id",
            "status",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    organization_id: Mapped[str] = mapped_column(String(36))
    business_profile_id: Mapped[str] = mapped_column(String(36))
    phone_number_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(128))
    country_code: Mapped[str] = mapped_column(String(2))
    number_type: Mapped[str] = mapped_column(String(32))
    requested_phone_number: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), default="provisioning")
    attempts: Mapped[int] = mapped_column(Integer, default=1)
    provider_phone_number_sid: Mapped[str | None] = mapped_column(
        String(34), nullable=True
    )
    provider_trunk_sid: Mapped[str | None] = mapped_column(String(34), nullable=True)
    phone_number_e164: Mapped[str | None] = mapped_column(String(16), nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_error_message: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    created_by_user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    tested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
