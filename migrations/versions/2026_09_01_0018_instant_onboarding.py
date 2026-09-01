"""Instant onboarding: pre-warmed number pool and organization lifecycle.

Revision ID: 202609010018
Revises: 202608310017
Create Date: 2026-09-01
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "202609010018"
down_revision: str | Sequence[str] | None = "202608310017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "phone_number_pool",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("e164", sa.String(16), nullable=False),
        sa.Column("country_code", sa.String(2), nullable=False),
        sa.Column(
            "provider", sa.String(32), nullable=False, server_default="twilio"
        ),
        sa.Column("provider_number_sid", sa.String(34), nullable=True),
        sa.Column("provider_trunk_sid", sa.String(34), nullable=True),
        sa.Column(
            "status", sa.String(16), nullable=False, server_default="available"
        ),
        sa.Column("assigned_organization_id", sa.String(36), nullable=True),
        sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("quarantined_until", sa.DateTime(timezone=True), nullable=True),
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
        sa.ForeignKeyConstraint(
            ["assigned_organization_id"],
            ["organizations.id"],
            name="fk_phone_number_pool_assigned_organization_id_organizations",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_phone_number_pool")),
        sa.UniqueConstraint("e164", name="uq_phone_number_pool_e164"),
    )
    op.create_index(
        "ix_phone_number_pool_status", "phone_number_pool", ["status"]
    )
    # Existing organizations are already live, so they backfill to 'active';
    # signup sets 'provisioning' explicitly on the rows it creates.
    with op.batch_alter_table("organizations") as batch:
        batch.add_column(
            sa.Column(
                "lifecycle",
                sa.String(16),
                nullable=False,
                server_default="active",
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("organizations") as batch:
        batch.drop_column("lifecycle")
    op.drop_index("ix_phone_number_pool_status", table_name="phone_number_pool")
    op.drop_table("phone_number_pool")
