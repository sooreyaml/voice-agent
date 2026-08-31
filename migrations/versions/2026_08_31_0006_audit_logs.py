"""Add the append-only audit log.

Revision ID: 202608310006
Revises: 202608310005
Create Date: 2026-08-31
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "202608310006"
down_revision: str | Sequence[str] | None = "202608310005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ID_TYPE = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "audit_logs",
        sa.Column("id", _ID_TYPE, autoincrement=True, nullable=False),
        sa.Column("organization_id", sa.String(36), nullable=True),
        sa.Column("actor_user_id", sa.String(36), nullable=True),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("target_type", sa.String(64), nullable=True),
        sa.Column("target_id", sa.String(64), nullable=True),
        sa.Column("metadata", sa.Text(), nullable=True),
        sa.Column("ip", sa.String(64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["users.id"],
            name="fk_audit_logs_actor_user_id_users",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name="fk_audit_logs_organization_id_organizations",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_audit_logs"),
    )
    op.create_index(
        "ix_audit_logs_organization_id_id",
        "audit_logs",
        ["organization_id", "id"],
    )


def downgrade() -> None:
    op.drop_index("ix_audit_logs_organization_id_id", table_name="audit_logs")
    op.drop_table("audit_logs")
