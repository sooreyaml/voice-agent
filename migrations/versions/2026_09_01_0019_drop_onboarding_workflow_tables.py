"""Drop the staff/self-service onboarding workflow tables.

Instant signup replaces the draft -> search -> provision -> publish ->
verify-test-call state machine, so ``onboarding_records`` and
``telephony_provisioning_requests`` have no writer left. They held only in-flight
workflow state; the organizations, profiles, agent versions, and phone numbers
they referenced are untouched.

Revision ID: 202609010019
Revises: 202609010018
Create Date: 2026-09-01
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "202609010019"
down_revision: str | Sequence[str] | None = "202609010018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index(
        "ix_telephony_provisioning_requests_organization_status",
        table_name="telephony_provisioning_requests",
    )
    op.drop_table("telephony_provisioning_requests")
    op.drop_index(
        "ix_onboarding_records_status_created_at",
        table_name="onboarding_records",
    )
    op.drop_table("onboarding_records")


def downgrade() -> None:
    op.create_table(
        "onboarding_records",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.Column("owner_email", sa.String(320), nullable=False),
        sa.Column(
            "status",
            sa.String(16),
            nullable=False,
            server_default=sa.text("'in_progress'"),
        ),
        sa.Column(
            "mode",
            sa.String(20),
            nullable=False,
            server_default=sa.text("'staff_led'"),
        ),
        sa.Column("created_by_user_id", sa.String(36), nullable=True),
        sa.Column("activated_by_user_id", sa.String(36), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('in_progress', 'active')",
            name=op.f("ck_onboarding_records_valid_status"),
        ),
        sa.CheckConstraint(
            "mode IN ('staff_led', 'self_service')",
            name=op.f("ck_onboarding_records_valid_mode"),
        ),
        sa.ForeignKeyConstraint(
            ["activated_by_user_id"],
            ["users.id"],
            name="fk_onboarding_records_activated_by_user_id_users",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            name="fk_onboarding_records_created_by_user_id_users",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name="fk_onboarding_records_organization_id_organizations",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_onboarding_records"),
        sa.UniqueConstraint(
            "organization_id",
            name="uq_onboarding_records_organization_id",
        ),
    )
    op.create_index(
        "ix_onboarding_records_status_created_at",
        "onboarding_records",
        ["status", "created_at"],
    )
    op.create_table(
        "telephony_provisioning_requests",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.Column("business_profile_id", sa.String(36), nullable=False),
        sa.Column("phone_number_id", sa.String(36), nullable=True),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("country_code", sa.String(2), nullable=False),
        sa.Column("number_type", sa.String(32), nullable=False),
        sa.Column("requested_phone_number", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("provider_phone_number_sid", sa.String(34), nullable=True),
        sa.Column("provider_trunk_sid", sa.String(34), nullable=True),
        sa.Column("phone_number_e164", sa.String(16), nullable=True),
        sa.Column("last_error_code", sa.String(64), nullable=True),
        sa.Column("last_error_message", sa.String(1000), nullable=True),
        sa.Column("created_by_user_id", sa.String(36), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("tested_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('provisioning', 'ready', 'failed', 'verified')",
            name=op.f("ck_telephony_provisioning_requests_valid_status"),
        ),
        sa.CheckConstraint(
            "number_type IN ('local', 'mobile', 'national', 'toll_free')",
            name=op.f("ck_telephony_provisioning_requests_valid_number_type"),
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            name="fk_telephony_provisioning_requests_created_by_user_id_users",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "business_profile_id"],
            ["business_profiles.organization_id", "business_profiles.id"],
            name=op.f(
                "fk_telephony_provisioning_requests_organization_id_"
                "business_profile_id_business_profiles"
            ),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "phone_number_id"],
            ["phone_numbers.organization_id", "phone_numbers.id"],
            name=op.f(
                "fk_telephony_provisioning_requests_organization_id_"
                "phone_number_id_phone_numbers"
            ),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "id", name="pk_telephony_provisioning_requests"
        ),
        sa.UniqueConstraint(
            "organization_id",
            "idempotency_key",
            name=op.f(
                "uq_telephony_provisioning_requests_organization_id_idempotency_key"
            ),
        ),
    )
    op.create_index(
        "ix_telephony_provisioning_requests_organization_status",
        "telephony_provisioning_requests",
        ["organization_id", "status"],
    )
