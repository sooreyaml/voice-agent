"""Add the multi-tenant foundation and scope call data.

Revision ID: 202608310001
Revises:
Create Date: 2026-08-31
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

from app.database import NAMING_CONVENTION

revision: str = "202608310001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ROLE_CHECK = "role IN ('owner', 'admin', 'member', 'viewer')"


def _id_type() -> sa.TypeEngine:
    return sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def _table_names() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _column_names(table_name: str) -> set[str]:
    return {
        column["name"] for column in sa.inspect(op.get_bind()).get_columns(table_name)
    }


def _create_tenancy_tables(existing: set[str]) -> None:
    if "organizations" not in existing:
        op.create_table(
            "organizations",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("slug", sa.String(100), nullable=False),
            sa.Column("name", sa.String(200), nullable=False),
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
            sa.UniqueConstraint("slug", name="uq_organizations_slug"),
        )
    if "users" not in existing:
        op.create_table(
            "users",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("email", sa.String(320), nullable=False),
            sa.Column("password_hash", sa.String(255), nullable=True),
            sa.Column("email_verified_at", sa.DateTime(timezone=True), nullable=True),
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
            sa.UniqueConstraint("email", name="uq_users_email"),
        )
    if "memberships" not in existing:
        op.create_table(
            "memberships",
            sa.Column("organization_id", sa.String(36), nullable=False),
            sa.Column("user_id", sa.String(36), nullable=False),
            sa.Column("role", sa.String(16), nullable=False),
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
            sa.CheckConstraint(ROLE_CHECK, name=op.f("ck_memberships_valid_role")),
            sa.ForeignKeyConstraint(
                ["organization_id"],
                ["organizations.id"],
                name="fk_memberships_organization_id_organizations",
                ondelete="CASCADE",
            ),
            sa.ForeignKeyConstraint(
                ["user_id"],
                ["users.id"],
                name="fk_memberships_user_id_users",
                ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint(
                "organization_id", "user_id", name="pk_memberships"
            ),
        )


def _create_calls_table() -> None:
    op.create_table(
        "calls",
        sa.Column("call_id", sa.Text(), nullable=False),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.Column("business", sa.Text(), nullable=True),
        sa.Column("from_number", sa.Text(), nullable=True),
        sa.Column("to_number", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("outcome", sa.Text(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column(
            "model_cost", sa.Float(), nullable=False, server_default=sa.text("0")
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name="fk_calls_organization_id_organizations",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("call_id", name="pk_calls"),
        sa.UniqueConstraint(
            "organization_id", "call_id", name="uq_calls_organization_id_call_id"
        ),
    )
    op.create_index(
        "ix_calls_organization_started_at",
        "calls",
        ["organization_id", "started_at"],
    )


def _create_turns_table() -> None:
    op.create_table(
        "turns",
        sa.Column("id", _id_type(), autoincrement=True, nullable=False),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.Column("call_id", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column(
            "at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "call_id"],
            ["calls.organization_id", "calls.call_id"],
            name="fk_turns_organization_id_call_id_calls",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_turns"),
    )
    op.create_index(
        "ix_turns_organization_call", "turns", ["organization_id", "call_id"]
    )


def _create_leads_table() -> None:
    op.create_table(
        "leads",
        sa.Column("id", _id_type(), autoincrement=True, nullable=False),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.Column("call_id", sa.Text(), nullable=False),
        sa.Column("intent", sa.Text(), nullable=True),
        sa.Column("caller_name", sa.Text(), nullable=True),
        sa.Column("callback_number", sa.Text(), nullable=True),
        sa.Column("urgency", sa.Text(), nullable=True),
        sa.Column("preferred_time", sa.Text(), nullable=True),
        sa.Column("details", sa.Text(), nullable=True),
        sa.Column(
            "at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "call_id"],
            ["calls.organization_id", "calls.call_id"],
            name="fk_leads_organization_id_call_id_calls",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_leads"),
    )
    op.create_index(
        "ix_leads_organization_call", "leads", ["organization_id", "call_id"]
    )


def _legacy_slug(name: str, used: set[str]) -> str:
    # Existing YAML filenames are normally the kebab-case business name. Using
    # the same shape lets startup reuse the backfilled organization instead of
    # splitting historical and future calls across two tenants.
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "legacy"
    base = base[:100].rstrip("-")
    slug = base
    suffix = 2
    while slug in used:
        ending = f"-{suffix}"
        slug = base[: 100 - len(ending)].rstrip("-") + ending
        suffix += 1
    used.add(slug)
    return slug


def _insert_legacy_organization(name: str, used_slugs: set[str]) -> str:
    organization_id = str(uuid.uuid4())
    op.get_bind().execute(
        sa.text(
            "INSERT INTO organizations (id, slug, name) VALUES (:id, :slug, :name)"
        ),
        {
            "id": organization_id,
            "slug": _legacy_slug(name, used_slugs),
            "name": name,
        },
    )
    return organization_id


def _add_organization_column(table_name: str) -> None:
    if "organization_id" not in _column_names(table_name):
        op.add_column(
            table_name, sa.Column("organization_id", sa.String(36), nullable=True)
        )


def _backfill_legacy_organizations(existing: set[str]) -> None:
    bind = op.get_bind()
    for table_name in ("calls", "turns", "leads"):
        if table_name in existing:
            _add_organization_column(table_name)

    used_slugs = set(bind.execute(sa.text("SELECT slug FROM organizations")).scalars())
    businesses = bind.execute(
        sa.text("SELECT DISTINCT business FROM calls ORDER BY business")
    ).scalars()
    for raw_business in businesses:
        business = str(raw_business or "").strip()
        name = business or "Legacy organization"
        organization_id = _insert_legacy_organization(name, used_slugs)
        if business:
            bind.execute(
                sa.text(
                    "UPDATE calls SET organization_id = :organization_id "
                    "WHERE business = :business"
                ),
                {"organization_id": organization_id, "business": raw_business},
            )
        else:
            bind.execute(
                sa.text(
                    "UPDATE calls SET organization_id = :organization_id "
                    "WHERE business IS NULL OR TRIM(business) = ''"
                ),
                {"organization_id": organization_id},
            )

    orphan_call_ids: set[str] = set()
    for table_name in ("turns", "leads"):
        if table_name not in existing:
            continue
        orphan_call_ids.update(
            bind.execute(
                sa.text(
                    f"SELECT DISTINCT child.call_id FROM {table_name} AS child "
                    "LEFT JOIN calls ON calls.call_id = child.call_id "
                    "WHERE calls.call_id IS NULL"
                )
            ).scalars()
        )

    if orphan_call_ids:
        organization_id = _insert_legacy_organization(
            "Legacy orphaned records", used_slugs
        )
        for call_id in sorted(orphan_call_ids):
            bind.execute(
                sa.text(
                    "INSERT INTO calls "
                    "(call_id, organization_id, business, started_at, model_cost) "
                    "VALUES (:call_id, :organization_id, :business, :started_at, 0)"
                ),
                {
                    "call_id": call_id,
                    "organization_id": organization_id,
                    "business": "Legacy orphaned records",
                    "started_at": datetime.now(UTC).isoformat(),
                },
            )

    for table_name in ("turns", "leads"):
        if table_name in existing:
            bind.execute(
                sa.text(
                    f"UPDATE {table_name} SET organization_id = "
                    "(SELECT organization_id FROM calls "
                    f"WHERE calls.call_id = {table_name}.call_id)"
                )
            )


def _alter_timestamp(
    batch: object, table_name: str, column_name: str, *, nullable: bool
) -> None:
    if op.get_bind().dialect.name != "postgresql":
        # SQLite's CAST(... AS DATETIME) reduces an ISO timestamp to its year.
        # Its dynamic typing already accepts datetime values in legacy TEXT
        # columns, so preserving the declaration also preserves the data.
        return
    batch.alter_column(
        column_name,
        existing_type=sa.Text(),
        type_=sa.DateTime(timezone=True),
        existing_nullable=nullable,
        postgresql_using=f"{column_name}::timestamptz",
    )


def _constrain_legacy_tables(existing: set[str]) -> None:
    if "calls" in existing:
        op.get_bind().execute(
            sa.text("UPDATE calls SET model_cost = 0 WHERE model_cost IS NULL")
        )
        with op.batch_alter_table(
            "calls", naming_convention=NAMING_CONVENTION
        ) as batch:
            batch.alter_column(
                "organization_id", existing_type=sa.String(36), nullable=False
            )
            _alter_timestamp(batch, "calls", "started_at", nullable=False)
            _alter_timestamp(batch, "calls", "ended_at", nullable=True)
            batch.alter_column(
                "model_cost",
                existing_type=sa.Float(),
                nullable=False,
                server_default=sa.text("0"),
            )
            batch.create_foreign_key(
                "fk_calls_organization_id_organizations",
                "organizations",
                ["organization_id"],
                ["id"],
                ondelete="RESTRICT",
            )
            batch.create_unique_constraint(
                "uq_calls_organization_id_call_id", ["organization_id", "call_id"]
            )
        op.create_index(
            "ix_calls_organization_started_at",
            "calls",
            ["organization_id", "started_at"],
        )

    if "turns" in existing:
        with op.batch_alter_table(
            "turns", naming_convention=NAMING_CONVENTION
        ) as batch:
            batch.alter_column(
                "organization_id", existing_type=sa.String(36), nullable=False
            )
            _alter_timestamp(batch, "turns", "at", nullable=False)
            batch.create_foreign_key(
                "fk_turns_organization_id_call_id_calls",
                "calls",
                ["organization_id", "call_id"],
                ["organization_id", "call_id"],
                ondelete="CASCADE",
            )
        op.create_index(
            "ix_turns_organization_call", "turns", ["organization_id", "call_id"]
        )

    if "leads" in existing:
        with op.batch_alter_table(
            "leads", naming_convention=NAMING_CONVENTION
        ) as batch:
            batch.alter_column(
                "organization_id", existing_type=sa.String(36), nullable=False
            )
            _alter_timestamp(batch, "leads", "at", nullable=False)
            batch.create_foreign_key(
                "fk_leads_organization_id_call_id_calls",
                "calls",
                ["organization_id", "call_id"],
                ["organization_id", "call_id"],
                ondelete="CASCADE",
            )
        op.create_index(
            "ix_leads_organization_call", "leads", ["organization_id", "call_id"]
        )


def upgrade() -> None:
    existing = _table_names()
    _create_tenancy_tables(existing)

    if "calls" in existing:
        _backfill_legacy_organizations(existing)
        _constrain_legacy_tables(existing)
        if "turns" not in existing:
            _create_turns_table()
        if "leads" not in existing:
            _create_leads_table()
    else:
        _create_calls_table()
        _create_turns_table()
        _create_leads_table()


def downgrade() -> None:
    op.drop_index("ix_leads_organization_call", table_name="leads")
    with op.batch_alter_table("leads", naming_convention=NAMING_CONVENTION) as batch:
        batch.drop_constraint(
            "fk_leads_organization_id_call_id_calls", type_="foreignkey"
        )
        batch.alter_column(
            "at",
            existing_type=sa.DateTime(timezone=True),
            type_=sa.Text(),
            existing_nullable=False,
            postgresql_using="at::text",
        )
        batch.drop_column("organization_id")
    if "idx_leads_call" not in {
        index["name"] for index in sa.inspect(op.get_bind()).get_indexes("leads")
    }:
        op.create_index("idx_leads_call", "leads", ["call_id"])

    op.drop_index("ix_turns_organization_call", table_name="turns")
    with op.batch_alter_table("turns", naming_convention=NAMING_CONVENTION) as batch:
        batch.drop_constraint(
            "fk_turns_organization_id_call_id_calls", type_="foreignkey"
        )
        batch.alter_column(
            "at",
            existing_type=sa.DateTime(timezone=True),
            type_=sa.Text(),
            existing_nullable=False,
            postgresql_using="at::text",
        )
        batch.drop_column("organization_id")
    if "idx_turns_call" not in {
        index["name"] for index in sa.inspect(op.get_bind()).get_indexes("turns")
    }:
        op.create_index("idx_turns_call", "turns", ["call_id"])

    op.drop_index("ix_calls_organization_started_at", table_name="calls")
    with op.batch_alter_table("calls", naming_convention=NAMING_CONVENTION) as batch:
        batch.drop_constraint(
            "fk_calls_organization_id_organizations", type_="foreignkey"
        )
        batch.drop_constraint("uq_calls_organization_id_call_id", type_="unique")
        batch.alter_column(
            "started_at",
            existing_type=sa.DateTime(timezone=True),
            type_=sa.Text(),
            existing_nullable=False,
            postgresql_using="started_at::text",
        )
        batch.alter_column(
            "ended_at",
            existing_type=sa.DateTime(timezone=True),
            type_=sa.Text(),
            existing_nullable=True,
            postgresql_using="ended_at::text",
        )
        batch.alter_column(
            "model_cost",
            existing_type=sa.Float(),
            nullable=True,
            server_default=sa.text("0"),
        )
        batch.drop_column("organization_id")
    op.drop_table("memberships")
    op.drop_table("users")
    op.drop_table("organizations")
