"""Add staff-led organization onboarding records.

Revision ID: 202608310007
Revises: 202608310006
Create Date: 2026-08-31
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "202608310007"
down_revision: str | Sequence[str] | None = "202608310006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
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


def downgrade() -> None:
    op.drop_index(
        "ix_onboarding_records_status_created_at",
        table_name="onboarding_records",
    )
    op.drop_table("onboarding_records")
