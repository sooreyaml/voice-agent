"""Add follow-up triage status to captured leads (Phase 9, public-API writes).

Revision ID: 202608310013
Revises: 202608310012
Create Date: 2026-08-31
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "202608310013"
down_revision: str | Sequence[str] | None = "202608310012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "leads",
        sa.Column(
            "status", sa.String(16), nullable=False, server_default="new"
        ),
    )
    op.add_column("leads", sa.Column("status_note", sa.Text(), nullable=True))
    op.add_column(
        "leads",
        sa.Column("status_updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "leads", sa.Column("status_updated_by", sa.String(64), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("leads", "status_updated_by")
    op.drop_column("leads", "status_updated_at")
    op.drop_column("leads", "status_note")
    op.drop_column("leads", "status")
