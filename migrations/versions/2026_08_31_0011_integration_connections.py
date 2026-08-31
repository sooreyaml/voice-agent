"""Add third-party integration connections (roadmap section 7).

One row per (organization, provider). Provider credentials are stored as a single
Fernet-encrypted JSON blob; non-secret configuration lives in ``settings``.

Revision ID: 202608310011
Revises: 202608310010
Create Date: 2026-08-31
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "202608310011"
down_revision: str | Sequence[str] | None = "202608310010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "integration_connections",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.Column("provider", sa.String(40), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("display_name", sa.String(200), nullable=True),
        sa.Column("encrypted_credentials", sa.Text(), nullable=False),
        sa.Column("external_account_id", sa.String(255), nullable=True),
        sa.Column("scopes", sa.Text(), nullable=True),
        sa.Column("settings", sa.Text(), nullable=True),
        sa.Column("token_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(1000), nullable=True),
        sa.Column("last_verified_at", sa.DateTime(timezone=True), nullable=True),
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
            "status IN ('active', 'error', 'revoked')",
            name=op.f("ck_integration_connections_valid_status"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name="fk_integration_connections_organization_id_organizations",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_integration_connections"),
        sa.UniqueConstraint(
            "organization_id",
            "provider",
            name="uq_integration_connections_organization_id_provider",
        ),
    )
    op.create_index(
        "ix_integration_connections_organization_id",
        "integration_connections",
        ["organization_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_integration_connections_organization_id",
        table_name="integration_connections",
    )
    op.drop_table("integration_connections")
