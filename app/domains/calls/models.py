from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    Float,
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

RECORD_ID_TYPE = BigInteger().with_variant(Integer(), "sqlite")


class Call(Base):
    __tablename__ = "calls"
    __table_args__ = (
        UniqueConstraint("organization_id", "call_id"),
        Index("ix_calls_organization_started_at", "organization_id", "started_at"),
        ForeignKeyConstraint(
            ["organization_id", "agent_version_id"],
            ["agent_versions.organization_id", "agent_versions.id"],
            ondelete="RESTRICT",
        ),
        Index("ix_calls_agent_version_id", "agent_version_id"),
    )

    call_id: Mapped[str] = mapped_column(Text, primary_key=True)
    organization_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="RESTRICT")
    )
    agent_version_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    business: Mapped[str | None] = mapped_column(Text, nullable=True)
    from_number: Mapped[str | None] = mapped_column(Text, nullable=True)
    to_number: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    outcome: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    model_cost: Mapped[float] = mapped_column(Float, server_default="0")
    transcript_deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class Turn(Base):
    __tablename__ = "turns"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "call_id"],
            ["calls.organization_id", "calls.call_id"],
            ondelete="CASCADE",
        ),
        Index("ix_turns_organization_call", "organization_id", "call_id"),
    )

    id: Mapped[int] = mapped_column(
        RECORD_ID_TYPE, primary_key=True, autoincrement=True
    )
    organization_id: Mapped[str] = mapped_column(String(36))
    call_id: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(Text)
    text: Mapped[str] = mapped_column(Text)
    at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Lead(Base):
    __tablename__ = "leads"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "call_id"],
            ["calls.organization_id", "calls.call_id"],
            ondelete="CASCADE",
        ),
        Index("ix_leads_organization_call", "organization_id", "call_id"),
    )

    id: Mapped[int] = mapped_column(
        RECORD_ID_TYPE, primary_key=True, autoincrement=True
    )
    organization_id: Mapped[str] = mapped_column(String(36))
    call_id: Mapped[str] = mapped_column(Text)
    intent: Mapped[str | None] = mapped_column(Text, nullable=True)
    caller_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    callback_number: Mapped[str | None] = mapped_column(Text, nullable=True)
    urgency: Mapped[str | None] = mapped_column(Text, nullable=True)
    preferred_time: Mapped[str | None] = mapped_column(Text, nullable=True)
    details: Mapped[str | None] = mapped_column(Text, nullable=True)
    at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # Follow-up triage (Phase 9): new -> handled | dismissed.
    status: Mapped[str] = mapped_column(String(16), server_default="new")
    status_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    status_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    status_updated_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
