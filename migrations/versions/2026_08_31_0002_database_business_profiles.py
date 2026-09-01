"""Add published business profiles and telephone routing.

Revision ID: 202608310002
Revises: 202608310001
Create Date: 2026-08-31
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.database import NAMING_CONVENTION

revision: str = "202608310002"
down_revision: str | Sequence[str] | None = "202608310001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "business_profiles",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.Column("slug", sa.String(100), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("timezone", sa.String(100), nullable=False),
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
            ["organization_id"],
            ["organizations.id"],
            name="fk_business_profiles_organization_id_organizations",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_business_profiles"),
        sa.UniqueConstraint(
            "organization_id",
            "id",
            name="uq_business_profiles_organization_id_id",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "slug",
            name="uq_business_profiles_organization_id_slug",
        ),
    )
    op.create_table(
        "agent_versions",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.Column("business_profile_id", sa.String(36), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("config", sa.Text(), nullable=False),
        sa.Column("rendered_prompt", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('draft', 'published', 'archived')",
            name=op.f("ck_agent_versions_valid_status"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "business_profile_id"],
            ["business_profiles.organization_id", "business_profiles.id"],
            name=op.f(
                "fk_agent_versions_organization_id_business_profile_id_"
                "business_profiles"
            ),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_agent_versions"),
        sa.UniqueConstraint(
            "organization_id",
            "id",
            name="uq_agent_versions_organization_id_id",
        ),
        sa.UniqueConstraint(
            "business_profile_id",
            "version_number",
            name="uq_agent_versions_business_profile_id_version_number",
        ),
    )
    op.create_index(
        "ix_agent_versions_profile_status",
        "agent_versions",
        ["business_profile_id", "status"],
    )
    op.create_table(
        "phone_numbers",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.Column("business_profile_id", sa.String(36), nullable=False),
        sa.Column("e164", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
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
            "status IN ('active', 'inactive')",
            name=op.f("ck_phone_numbers_valid_status"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "business_profile_id"],
            ["business_profiles.organization_id", "business_profiles.id"],
            name=op.f(
                "fk_phone_numbers_organization_id_business_profile_id_business_profiles"
            ),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_phone_numbers"),
        sa.UniqueConstraint("e164", name="uq_phone_numbers_e164"),
        sa.UniqueConstraint(
            "organization_id", "id", name="uq_phone_numbers_organization_id_id"
        ),
    )
    op.create_index(
        "ix_phone_numbers_organization_profile",
        "phone_numbers",
        ["organization_id", "business_profile_id"],
    )

    op.add_column(
        "calls", sa.Column("agent_version_id", sa.String(36), nullable=True)
    )
    with op.batch_alter_table("calls", naming_convention=NAMING_CONVENTION) as batch:
        batch.create_foreign_key(
            "fk_calls_organization_id_agent_version_id_agent_versions",
            "agent_versions",
            ["organization_id", "agent_version_id"],
            ["organization_id", "id"],
            ondelete="RESTRICT",
        )
    op.create_index("ix_calls_agent_version_id", "calls", ["agent_version_id"])


def downgrade() -> None:
    op.drop_index("ix_calls_agent_version_id", table_name="calls")
    with op.batch_alter_table("calls", naming_convention=NAMING_CONVENTION) as batch:
        batch.drop_constraint(
            "fk_calls_organization_id_agent_version_id_agent_versions",
            type_="foreignkey",
        )
        batch.drop_column("agent_version_id")
    op.drop_index(
        "ix_phone_numbers_organization_profile", table_name="phone_numbers"
    )
    op.drop_table("phone_numbers")
    op.drop_index("ix_agent_versions_profile_status", table_name="agent_versions")
    op.drop_table("agent_versions")
    op.drop_table("business_profiles")
