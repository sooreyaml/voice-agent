from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class PoolNumberStatus(StrEnum):
    AVAILABLE = "available"
    ASSIGNED = "assigned"
    QUARANTINED = "quarantined"
    RETIRED = "retired"


class PhoneNumberPool(Base):
    """Numbers bought and attached to the shared trunk ahead of demand.

    Signup claims one with a single conditional UPDATE so a new tenant gets a
    live number with no Twilio round-trip on the request path. Refilled and
    recycled by ``app/worker.py``; not an admin-facing resource.
    """

    __tablename__ = "phone_number_pool"
    __table_args__ = (
        UniqueConstraint("e164", name="uq_phone_number_pool_e164"),
        Index("ix_phone_number_pool_status", "status"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    e164: Mapped[str] = mapped_column(String(16))
    country_code: Mapped[str] = mapped_column(String(2))
    provider: Mapped[str] = mapped_column(String(32), default="twilio")
    provider_number_sid: Mapped[str | None] = mapped_column(String(34), nullable=True)
    provider_trunk_sid: Mapped[str | None] = mapped_column(String(34), nullable=True)
    # available | assigned | quarantined | retired (see PoolNumberStatus).
    status: Mapped[str] = mapped_column(String(16), default="available")
    assigned_organization_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("organizations.id", ondelete="SET NULL"),
        nullable=True,
    )
    assigned_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    quarantined_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
