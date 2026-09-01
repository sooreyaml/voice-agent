"""Add shared-account Twilio number provisioning and provider metadata.

Revision ID: 202608310008
Revises: 202608310007
Create Date: 2026-08-31
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "202608310008"
down_revision: str | Sequence[str] | None = "202608310007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("phone_numbers") as batch:
        batch.add_column(sa.Column("provider", sa.String(32), nullable=True))
        batch.add_column(
            sa.Column("provider_account_sid", sa.String(34), nullable=True)
        )
        batch.add_column(sa.Column("provider_number_sid", sa.String(34), nullable=True))
        batch.add_column(sa.Column("provider_trunk_sid", sa.String(34), nullable=True))
        batch.add_column(sa.Column("country_code", sa.String(2), nullable=True))
        batch.add_column(sa.Column("number_type", sa.String(32), nullable=True))
        batch.create_unique_constraint(
            "uq_phone_numbers_provider_number_sid",
            ["provider", "provider_number_sid"],
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
            name=("fk_telephony_provisioning_requests_created_by_user_id_users"),
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
        sa.PrimaryKeyConstraint("id", name="pk_telephony_provisioning_requests"),
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


def downgrade() -> None:
    op.drop_index(
        "ix_telephony_provisioning_requests_organization_status",
        table_name="telephony_provisioning_requests",
    )
    op.drop_table("telephony_provisioning_requests")
    with op.batch_alter_table("phone_numbers") as batch:
        batch.drop_constraint("uq_phone_numbers_provider_number_sid", type_="unique")
        batch.drop_column("number_type")
        batch.drop_column("country_code")
        batch.drop_column("provider_trunk_sid")
        batch.drop_column("provider_number_sid")
        batch.drop_column("provider_account_sid")
        batch.drop_column("provider")
