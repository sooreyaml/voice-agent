"""Add subscription billing and the immutable usage ledger.

Revision ID: 202608310009
Revises: 202608310008
Create Date: 2026-08-31
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "202608310009"
down_revision: str | Sequence[str] | None = "202608310008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


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


def _create_immutability_triggers() -> None:
    if op.get_bind().dialect.name == "sqlite":
        op.execute(
            "CREATE TRIGGER usage_events_no_update BEFORE UPDATE ON usage_events "
            "BEGIN SELECT RAISE(ABORT, 'usage_events are immutable'); END"
        )
        op.execute(
            "CREATE TRIGGER usage_events_no_delete BEFORE DELETE ON usage_events "
            "BEGIN SELECT RAISE(ABORT, 'usage_events are immutable'); END"
        )
        return
    op.execute(
        "CREATE FUNCTION prevent_usage_events_mutation() RETURNS trigger AS $$ "
        "BEGIN RAISE EXCEPTION 'usage_events are immutable'; END; "
        "$$ LANGUAGE plpgsql"
    )
    op.execute(
        "CREATE TRIGGER usage_events_no_update BEFORE UPDATE ON usage_events "
        "FOR EACH ROW EXECUTE FUNCTION prevent_usage_events_mutation()"
    )
    op.execute(
        "CREATE TRIGGER usage_events_no_delete BEFORE DELETE ON usage_events "
        "FOR EACH ROW EXECUTE FUNCTION prevent_usage_events_mutation()"
    )


def _drop_immutability_triggers() -> None:
    if op.get_bind().dialect.name == "sqlite":
        op.execute("DROP TRIGGER IF EXISTS usage_events_no_update")
        op.execute("DROP TRIGGER IF EXISTS usage_events_no_delete")
        return
    op.execute("DROP TRIGGER IF EXISTS usage_events_no_update ON usage_events")
    op.execute("DROP TRIGGER IF EXISTS usage_events_no_delete ON usage_events")
    op.execute("DROP FUNCTION IF EXISTS prevent_usage_events_mutation()")


def upgrade() -> None:
    op.create_table(
        "billing_plans",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("code", sa.String(64), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("monthly_amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("included_seconds", sa.BigInteger(), nullable=False),
        sa.Column(
            "overage_amount_micros_per_second", sa.BigInteger(), nullable=False
        ),
        sa.Column("stripe_price_id", sa.String(255), nullable=True),
        sa.Column("stripe_meter_event_name", sa.String(100), nullable=True),
        sa.Column("entitlements", sa.Text(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "status IN ('active', 'archived')",
            name=op.f("ck_billing_plans_valid_status"),
        ),
        sa.CheckConstraint(
            "monthly_amount_minor >= 0",
            name=op.f("ck_billing_plans_monthly_amount_nonnegative"),
        ),
        sa.CheckConstraint(
            "included_seconds >= 0",
            name=op.f("ck_billing_plans_included_seconds_nonnegative"),
        ),
        sa.CheckConstraint(
            "overage_amount_micros_per_second >= 0",
            name=op.f("ck_billing_plans_overage_amount_nonnegative"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_billing_plans")),
        sa.UniqueConstraint("code", name=op.f("uq_billing_plans_code")),
        sa.UniqueConstraint(
            "stripe_price_id", name=op.f("uq_billing_plans_stripe_price_id")
        ),
    )

    op.create_table(
        "subscriptions",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.Column("billing_plan_id", sa.String(36), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("provider_customer_id", sa.String(255), nullable=True),
        sa.Column("provider_subscription_id", sa.String(255), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("current_period_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("current_period_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("trial_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "cancel_at_period_end",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("last_invoice_status", sa.String(32), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "status IN ('not_started', 'checkout_pending', 'trialing', 'active',"
            " 'past_due', 'paused', 'incomplete', 'incomplete_expired', 'unpaid',"
            " 'canceled')",
            name=op.f("ck_subscriptions_valid_status"),
        ),
        sa.ForeignKeyConstraint(
            ["billing_plan_id"],
            ["billing_plans.id"],
            name=op.f("fk_subscriptions_billing_plan_id_billing_plans"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_subscriptions_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_subscriptions")),
        sa.UniqueConstraint(
            "organization_id", name=op.f("uq_subscriptions_organization_id")
        ),
        sa.UniqueConstraint(
            "provider_customer_id",
            name=op.f("uq_subscriptions_provider_customer_id"),
        ),
        sa.UniqueConstraint(
            "provider_subscription_id",
            name=op.f("uq_subscriptions_provider_subscription_id"),
        ),
    )

    op.create_table(
        "usage_events",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.Column("call_id", sa.Text(), nullable=True),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("quantity", sa.BigInteger(), nullable=False),
        sa.Column("unit", sa.String(32), nullable=False),
        sa.Column(
            "provider_cost_micros", sa.BigInteger(), nullable=False, server_default="0"
        ),
        sa.Column(
            "customer_charge_micros",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("provider_reference", sa.String(255), nullable=True),
        sa.Column("reversal_of_event_id", sa.String(36), nullable=True),
        sa.Column("metadata", sa.Text(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "quantity <> 0", name=op.f("ck_usage_events_quantity_nonzero")
        ),
        sa.CheckConstraint(
            "id <> reversal_of_event_id",
            name=op.f("ck_usage_events_not_self_reversal"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_usage_events_organization_id_organizations"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "call_id"],
            ["calls.organization_id", "calls.call_id"],
            name=op.f("fk_usage_events_organization_id_call_id_calls"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "reversal_of_event_id"],
            ["usage_events.organization_id", "usage_events.id"],
            name=op.f(
                "fk_usage_events_organization_id_reversal_of_event_id_usage_events"
            ),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_usage_events")),
        sa.UniqueConstraint(
            "organization_id", "id", name=op.f("uq_usage_events_organization_id_id")
        ),
        sa.UniqueConstraint(
            "organization_id",
            "source",
            "idempotency_key",
            name=op.f(
                "uq_usage_events_organization_id_source_idempotency_key"
            ),
        ),
    )
    op.create_index(
        "ix_usage_events_organization_occurred",
        "usage_events",
        ["organization_id", "occurred_at"],
    )
    op.create_index(
        "ix_usage_events_organization_call",
        "usage_events",
        ["organization_id", "call_id"],
    )

    op.create_table(
        "usage_exports",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("usage_event_id", sa.String(36), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("provider_event_identifier", sa.String(100), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.String(1000), nullable=True),
        *_timestamps(),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'sent', 'failed')",
            name=op.f("ck_usage_exports_valid_status"),
        ),
        sa.ForeignKeyConstraint(
            ["usage_event_id"],
            ["usage_events.id"],
            name=op.f("fk_usage_exports_usage_event_id_usage_events"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_usage_exports")),
        sa.UniqueConstraint(
            "usage_event_id",
            "provider",
            name=op.f("uq_usage_exports_usage_event_id_provider"),
        ),
        sa.UniqueConstraint(
            "provider",
            "provider_event_identifier",
            name=op.f("uq_usage_exports_provider_provider_event_identifier"),
        ),
    )
    op.create_index(
        "ix_usage_exports_provider_status",
        "usage_exports",
        ["provider", "status"],
    )

    op.create_table(
        "billing_provider_events",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("provider_event_id", sa.String(255), nullable=False),
        sa.Column("event_type", sa.String(100), nullable=False),
        sa.Column("organization_id", sa.String(36), nullable=True),
        sa.Column("payload_sha256", sa.String(64), nullable=False),
        sa.Column("outcome", sa.String(32), nullable=False),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_billing_provider_events_organization_id_organizations"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_billing_provider_events")),
        sa.UniqueConstraint(
            "provider",
            "provider_event_id",
            name=op.f("uq_billing_provider_events_provider_provider_event_id"),
        ),
    )
    op.create_index(
        "ix_billing_provider_events_received_at",
        "billing_provider_events",
        ["received_at"],
    )
    _create_immutability_triggers()


def downgrade() -> None:
    _drop_immutability_triggers()
    op.drop_index(
        "ix_billing_provider_events_received_at",
        table_name="billing_provider_events",
    )
    op.drop_table("billing_provider_events")
    op.drop_index(
        "ix_usage_exports_provider_status", table_name="usage_exports"
    )
    op.drop_table("usage_exports")
    op.drop_index(
        "ix_usage_events_organization_call", table_name="usage_events"
    )
    op.drop_index(
        "ix_usage_events_organization_occurred", table_name="usage_events"
    )
    op.drop_table("usage_events")
    op.drop_table("subscriptions")
    op.drop_table("billing_plans")
