"""Distinguish staff-led and tenant self-service onboarding records.

Revision ID: 202608310015
Revises: 202608310014
Create Date: 2026-08-31
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "202608310015"
down_revision: str | Sequence[str] | None = "202608310014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("onboarding_records") as batch_op:
        batch_op.add_column(
            sa.Column(
                "mode",
                sa.String(20),
                nullable=False,
                server_default="staff_led",
            )
        )
        batch_op.create_check_constraint(
            op.f("ck_onboarding_records_valid_mode"),
            "mode IN ('staff_led', 'self_service')",
        )


def downgrade() -> None:
    with op.batch_alter_table("onboarding_records") as batch_op:
        batch_op.drop_constraint("ck_onboarding_records_valid_mode", type_="check")
        batch_op.drop_column("mode")
