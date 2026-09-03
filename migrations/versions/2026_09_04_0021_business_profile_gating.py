"""Gate number provisioning behind a completed business profile.

Signup no longer hands over a phone number. A new organization is created
``registered``; verifying the owner's email moves it to ``profile_pending``;
completing the ``organization_intake`` business profile provisions the number
(``active``), or — when billing is on — moves it to ``eligible`` until the
signup checkout is paid.

* ``organization_intake`` holds the business profile the owner fills in.
* ``organizations.lifecycle`` gains ``registered`` / ``profile_pending`` /
  ``eligible``; it is free text guarded in the app, so no constraint changes.
  Existing organizations keep their ``active`` default and are untouched.

Revision ID: 202609040021
Revises: 202609030020
Create Date: 2026-09-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "202609040021"
down_revision: str | Sequence[str] | None = "202609030020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "organization_intake",
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.Column("legal_name", sa.String(200), nullable=False),
        sa.Column("address_line1", sa.String(200), nullable=False),
        sa.Column("address_line2", sa.String(200), nullable=True),
        sa.Column("city", sa.String(120), nullable=False),
        sa.Column("region", sa.String(120), nullable=True),
        sa.Column("postal_code", sa.String(32), nullable=False),
        sa.Column("country", sa.String(2), nullable=False),
        sa.Column("contact_email", sa.String(320), nullable=False),
        sa.Column("contact_phone", sa.String(32), nullable=False),
        sa.Column("business_name", sa.String(200), nullable=False),
        sa.Column("timezone", sa.String(100), nullable=False),
        sa.Column("industry", sa.String(120), nullable=False),
        sa.Column("what_you_do", sa.Text(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
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
            name=op.f("fk_organization_intake_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "organization_id", name=op.f("pk_organization_intake")
        ),
    )


def downgrade() -> None:
    op.drop_table("organization_intake")
