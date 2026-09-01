"""Add signed outbound webhooks: endpoints, events, deliveries, attempts.

Revision ID: 202608310010
Revises: 202608310009
Create Date: 2026-08-31
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "202608310010"
down_revision: str | Sequence[str] | None = "202608310009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ID_TYPE = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def _timestamps() -> tuple[sa.Column, sa.Column]:
    return (
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
    )


def upgrade() -> None:
    op.create_table(
        "webhook_endpoints",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.Column("url", sa.String(2048), nullable=False),
        sa.Column("secret", sa.String(80), nullable=False),
        sa.Column("description", sa.String(200), nullable=True),
        sa.Column("event_types", sa.Text(), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name="fk_webhook_endpoints_organization_id_organizations",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_webhook_endpoints"),
    )
    op.create_index(
        "ix_webhook_endpoints_organization_id",
        "webhook_endpoints",
        ["organization_id"],
    )

    op.create_table(
        "webhook_events",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.Column("type", sa.String(64), nullable=False),
        sa.Column("dedupe_key", sa.String(200), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name="fk_webhook_events_organization_id_organizations",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_webhook_events"),
        sa.UniqueConstraint(
            "organization_id",
            "type",
            "dedupe_key",
            name="uq_webhook_events_organization_id_type_dedupe_key",
        ),
    )
    op.create_index(
        "ix_webhook_events_organization_id_id",
        "webhook_events",
        ["organization_id", "id"],
    )

    op.create_table(
        "webhook_deliveries",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.Column("webhook_event_id", sa.String(36), nullable=False),
        sa.Column("webhook_endpoint_id", sa.String(36), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_status_code", sa.Integer(), nullable=True),
        sa.Column("last_error", sa.String(500), nullable=True),
        sa.Column("response_snippet", sa.String(500), nullable=True),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "status IN ('pending', 'delivering', 'succeeded', 'failed', 'dead')",
            name=op.f("ck_webhook_deliveries_valid_status"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name="fk_webhook_deliveries_organization_id_organizations",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["webhook_event_id"],
            ["webhook_events.id"],
            name="fk_webhook_deliveries_webhook_event_id_webhook_events",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["webhook_endpoint_id"],
            ["webhook_endpoints.id"],
            name="fk_webhook_deliveries_webhook_endpoint_id_webhook_endpoints",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_webhook_deliveries"),
        sa.UniqueConstraint(
            "webhook_event_id",
            "webhook_endpoint_id",
            name="uq_webhook_deliveries_event_endpoint",
        ),
    )
    op.create_index(
        "ix_webhook_deliveries_due",
        "webhook_deliveries",
        ["status", "next_attempt_at"],
    )
    op.create_index(
        "ix_webhook_deliveries_endpoint_id_id",
        "webhook_deliveries",
        ["webhook_endpoint_id", "id"],
    )

    op.create_table(
        "webhook_delivery_attempts",
        sa.Column("id", _ID_TYPE, autoincrement=True, nullable=False),
        sa.Column("webhook_delivery_id", sa.String(36), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("attempted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=True),
        sa.Column("error", sa.String(500), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(
            ["webhook_delivery_id"],
            ["webhook_deliveries.id"],
            name=op.f(
                "fk_webhook_delivery_attempts_webhook_delivery_id_webhook_deliveries"
            ),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_webhook_delivery_attempts"),
    )
    op.create_index(
        "ix_webhook_delivery_attempts_delivery_id_id",
        "webhook_delivery_attempts",
        ["webhook_delivery_id", "id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_webhook_delivery_attempts_delivery_id_id",
        table_name="webhook_delivery_attempts",
    )
    op.drop_table("webhook_delivery_attempts")
    op.drop_index(
        "ix_webhook_deliveries_endpoint_id_id", table_name="webhook_deliveries"
    )
    op.drop_index("ix_webhook_deliveries_due", table_name="webhook_deliveries")
    op.drop_table("webhook_deliveries")
    op.drop_index(
        "ix_webhook_events_organization_id_id", table_name="webhook_events"
    )
    op.drop_table("webhook_events")
    op.drop_index(
        "ix_webhook_endpoints_organization_id", table_name="webhook_endpoints"
    )
    op.drop_table("webhook_endpoints")
