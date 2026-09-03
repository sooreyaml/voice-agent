from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class OrganizationIntake(Base):
    """The business profile an owner fills in after verifying their email and
    before a phone number is provisioned.

    One row per organization. ``completed_at`` is set the first time every
    required field is present; it gates number provisioning together with the
    owner's verified email. The runtime reads/writes this table through raw SQL
    in :class:`app.store.Store`; this model exists so Alembic and the schema
    docs stay in sync.
    """

    __tablename__ = "organization_intake"

    organization_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        primary_key=True,
    )
    # Legal / compliance identity (also feeds a future Twilio regulatory bundle).
    legal_name: Mapped[str] = mapped_column(String(200))
    address_line1: Mapped[str] = mapped_column(String(200))
    address_line2: Mapped[str | None] = mapped_column(String(200), nullable=True)
    city: Mapped[str] = mapped_column(String(120))
    region: Mapped[str | None] = mapped_column(String(120), nullable=True)
    postal_code: Mapped[str] = mapped_column(String(32))
    country: Mapped[str] = mapped_column(String(2))
    contact_email: Mapped[str] = mapped_column(String(320))
    contact_phone: Mapped[str] = mapped_column(String(32))
    # Enough to render a real agent instead of the bare placeholder.
    business_name: Mapped[str] = mapped_column(String(200))
    timezone: Mapped[str] = mapped_column(String(100))
    industry: Mapped[str] = mapped_column(String(120))
    what_you_do: Mapped[str] = mapped_column(Text)
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
