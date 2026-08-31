from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

RECORD_ID_TYPE = BigInteger().with_variant(Integer(), "sqlite")


class AuditAction(StrEnum):
    ORG_CREATED = "org.created"
    ORG_UPDATED = "org.updated"
    MEMBER_INVITED = "member.invited"
    MEMBER_INVITE_REVOKED = "member.invite_revoked"
    MEMBER_JOINED = "member.joined"
    MEMBER_ROLE_CHANGED = "member.role_changed"
    MEMBER_REMOVED = "member.removed"
    PLATFORM_ADMIN_GRANTED = "platform.admin_granted"
    ONBOARDING_STARTED = "onboarding.started"
    ONBOARDING_ACTIVATED = "onboarding.activated"
    PROFILE_DRAFT_SAVED = "profile.draft_saved"
    PROFILE_PUBLISHED = "profile.published"
    TELEPHONY_PROVISIONING_STARTED = "telephony.provisioning_started"
    TELEPHONY_PROVISIONING_READY = "telephony.provisioning_ready"
    TELEPHONY_PROVISIONING_FAILED = "telephony.provisioning_failed"
    TELEPHONY_TEST_CALL_VERIFIED = "telephony.test_call_verified"
    BILLING_PLAN_CREATED = "billing.plan_created"
    BILLING_PLAN_UPDATED = "billing.plan_updated"
    SUBSCRIPTION_CHECKOUT_STARTED = "billing.subscription_checkout_started"
    SUBSCRIPTION_UPDATED = "billing.subscription_updated"
    USAGE_EVENT_RECORDED = "billing.usage_event_recorded"
    WEBHOOK_ENDPOINT_CREATED = "webhook.endpoint_created"
    WEBHOOK_ENDPOINT_UPDATED = "webhook.endpoint_updated"
    WEBHOOK_ENDPOINT_DELETED = "webhook.endpoint_deleted"
    WEBHOOK_SECRET_ROTATED = "webhook.secret_rotated"
    WEBHOOK_DELIVERY_REPLAYED = "webhook.delivery_replayed"
    INTEGRATION_CONNECTED = "integration.connected"
    INTEGRATION_DISCONNECTED = "integration.disconnected"
    API_KEY_CREATED = "api_key.created"
    API_KEY_REVOKED = "api_key.revoked"
    API_KEY_ROTATED = "api_key.rotated"
    LEAD_STATUS_CHANGED = "lead.status_changed"
    PRIVACY_SETTINGS_UPDATED = "privacy.settings_updated"
    DATA_EXPORT_REQUESTED = "privacy.data_export_requested"
    DATA_DELETION_REQUESTED = "privacy.data_deletion_requested"
    DATA_REQUEST_CANCELLED = "privacy.data_request_cancelled"
    DATA_REQUEST_COMPLETED = "privacy.data_request_completed"
    SPEND_LIMIT_UPDATED = "billing.spend_limit_updated"
    SPEND_LIMIT_EXCEEDED = "billing.spend_limit_exceeded"


class AuditLog(Base):
    """One recorded change. Rows are never updated or deleted by the app."""

    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_logs_organization_id_id", "organization_id", "id"),
    )

    id: Mapped[int] = mapped_column(
        RECORD_ID_TYPE, primary_key=True, autoincrement=True
    )
    organization_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=True,
    )
    actor_user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    action: Mapped[str] = mapped_column(String(64))
    target_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    target_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Column name kept as "metadata"; the attribute is renamed because
    # ``Base.metadata`` is reserved by SQLAlchemy's declarative layer.
    event_metadata: Mapped[str | None] = mapped_column("metadata", Text, nullable=True)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
