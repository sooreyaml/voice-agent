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
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class BusinessProfileRecord(Base):
    __tablename__ = "business_profiles"
    __table_args__ = (
        UniqueConstraint("organization_id", "id"),
        UniqueConstraint("organization_id", "slug"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    organization_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="CASCADE")
    )
    slug: Mapped[str] = mapped_column(String(100))
    name: Mapped[str] = mapped_column(String(200))
    timezone: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class AgentVersion(Base):
    __tablename__ = "agent_versions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('draft', 'published', 'archived')", name="valid_status"
        ),
        ForeignKeyConstraint(
            ["organization_id", "business_profile_id"],
            ["business_profiles.organization_id", "business_profiles.id"],
            ondelete="CASCADE",
        ),
        UniqueConstraint("organization_id", "id"),
        UniqueConstraint("business_profile_id", "version_number"),
        Index(
            "ix_agent_versions_profile_status",
            "business_profile_id",
            "status",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    organization_id: Mapped[str] = mapped_column(String(36))
    business_profile_id: Mapped[str] = mapped_column(String(36))
    version_number: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16))
    config: Mapped[str] = mapped_column(Text)
    rendered_prompt: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class PhoneNumber(Base):
    __tablename__ = "phone_numbers"
    __table_args__ = (
        CheckConstraint("status IN ('active', 'inactive')", name="valid_status"),
        ForeignKeyConstraint(
            ["organization_id", "business_profile_id"],
            ["business_profiles.organization_id", "business_profiles.id"],
            ondelete="CASCADE",
        ),
        UniqueConstraint("organization_id", "id"),
        UniqueConstraint(
            "provider",
            "provider_number_sid",
            name="uq_phone_numbers_provider_number_sid",
        ),
        Index(
            "ix_phone_numbers_organization_profile",
            "organization_id",
            "business_profile_id",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    organization_id: Mapped[str] = mapped_column(String(36))
    business_profile_id: Mapped[str] = mapped_column(String(36))
    e164: Mapped[str] = mapped_column(String(16), unique=True)
    status: Mapped[str] = mapped_column(String(16))
    provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    provider_account_sid: Mapped[str | None] = mapped_column(String(34), nullable=True)
    provider_number_sid: Mapped[str | None] = mapped_column(String(34), nullable=True)
    provider_trunk_sid: Mapped[str | None] = mapped_column(String(34), nullable=True)
    country_code: Mapped[str | None] = mapped_column(String(2), nullable=True)
    number_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
