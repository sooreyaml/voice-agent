"""Add the post-call CRM sync queue (Phase 9, HubSpot integration).

Revision ID: 202608310014
Revises: 202608310013
Create Date: 2026-08-31
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "202608310014"
down_revision: str | Sequence[str] | None = "202608310013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "crm_sync_jobs",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.Column("provider", sa.String(40), nullable=False),
        sa.Column("call_id", sa.Text(), nullable=False),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(1000), nullable=True),
        sa.Column("result", sa.Text(), nullable=True),
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
            "status IN ('pending', 'syncing', 'succeeded', 'failed', 'dead')",
            name=op.f("ck_crm_sync_jobs_valid_status"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name="fk_crm_sync_jobs_organization_id_organizations",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_crm_sync_jobs"),
        sa.UniqueConstraint(
            "organization_id",
            "provider",
            "call_id",
            "kind",
            name="uq_crm_sync_jobs_org_provider_call_kind",
        ),
    )
    op.create_index(
        "ix_crm_sync_jobs_due", "crm_sync_jobs", ["status", "next_attempt_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_crm_sync_jobs_due", table_name="crm_sync_jobs")
    op.drop_table("crm_sync_jobs")
