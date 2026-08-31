"""Add per-tenant monthly spend limits.

Revision ID: 202608310017
Revises: 202608310016
Create Date: 2026-08-31
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "202608310017"
down_revision: str | Sequence[str] | None = "202608310016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "organization_spend_limits",
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.Column("monthly_limit_micros", sa.BigInteger(), nullable=True),
        sa.Column("hard_limit", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "warning_threshold_percent",
            sa.Integer(),
            nullable=False,
            server_default="80",
        ),
        sa.Column("blocked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_by_user_id", sa.String(36), nullable=True),
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
        sa.CheckConstraint(
            "monthly_limit_micros IS NULL OR monthly_limit_micros >= 1000000",
            name=op.f("ck_organization_spend_limits_valid_monthly_limit"),
        ),
        sa.CheckConstraint(
            "warning_threshold_percent >= 1 AND warning_threshold_percent <= 100",
            name=op.f("ck_organization_spend_limits_valid_warning_threshold"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name="fk_organization_spend_limits_organization_id_organizations",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_user_id"],
            ["users.id"],
            name="fk_organization_spend_limits_updated_by_user_id_users",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("organization_id", name="pk_organization_spend_limits"),
    )


def downgrade() -> None:
    op.drop_table("organization_spend_limits")
