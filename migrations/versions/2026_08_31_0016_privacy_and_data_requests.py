"""Add transcript retention and account data-rights workflows.

Revision ID: 202608310016
Revises: 202608310015
Create Date: 2026-08-31
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "202608310016"
down_revision: str | Sequence[str] | None = "202608310015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("organizations") as batch_op:
        batch_op.add_column(
            sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True)
        )
    with op.batch_alter_table("calls") as batch_op:
        batch_op.add_column(
            sa.Column(
                "transcript_deleted_at", sa.DateTime(timezone=True), nullable=True
            )
        )

    op.create_table(
        "organization_privacy_settings",
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.Column(
            "transcript_retention_days",
            sa.Integer(),
            nullable=True,
            server_default="90",
        ),
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
            "transcript_retention_days IS NULL OR "
            "(transcript_retention_days >= 1 AND transcript_retention_days <= 3650)",
            name=op.f("ck_organization_privacy_settings_valid_retention"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name="fk_organization_privacy_settings_organization_id_organizations",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_user_id"],
            ["users.id"],
            name="fk_organization_privacy_settings_updated_by_user_id_users",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint(
            "organization_id", name="pk_organization_privacy_settings"
        ),
    )

    op.create_table(
        "data_requests",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.Column("requested_by_user_id", sa.String(36), nullable=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("execute_after", sa.DateTime(timezone=True), nullable=False),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result", sa.Text(), nullable=True),
        sa.Column("result_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(1000), nullable=True),
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
        sa.CheckConstraint(
            "kind IN ('export', 'deletion')",
            name=op.f("ck_data_requests_valid_kind"),
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'completed', 'failed', 'dead',"
            " 'cancelled')",
            name=op.f("ck_data_requests_valid_status"),
        ),
        sa.CheckConstraint(
            "attempts >= 0 AND max_attempts >= 1",
            name=op.f("ck_data_requests_valid_attempts"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name="fk_data_requests_organization_id_organizations",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["requested_by_user_id"],
            ["users.id"],
            name="fk_data_requests_requested_by_user_id_users",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_data_requests"),
        sa.UniqueConstraint(
            "organization_id",
            "kind",
            "idempotency_key",
            name="uq_data_requests_org_kind_idempotency",
        ),
    )
    op.create_index(
        "ix_data_requests_due", "data_requests", ["status", "execute_after"]
    )
    op.create_index(
        "ix_data_requests_org_created",
        "data_requests",
        ["organization_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_data_requests_org_created", table_name="data_requests")
    op.drop_index("ix_data_requests_due", table_name="data_requests")
    op.drop_table("data_requests")
    op.drop_table("organization_privacy_settings")
    with op.batch_alter_table("calls") as batch_op:
        batch_op.drop_column("transcript_deleted_at")
    with op.batch_alter_table("organizations") as batch_op:
        batch_op.drop_column("deleted_at")
