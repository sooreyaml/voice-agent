#!/usr/bin/env python3
"""Preview or clear all application data from a hosted PostgreSQL database.

The database schema and Alembic revision are preserved. Execution is deliberately
guarded: the target database, current migration, table set, backup reference, and
an exact confirmation phrase must all match before any rows are truncated.

This does not release or cancel resources held by external providers such as
Twilio, Stripe, OpenAI, or connected CRMs.
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

import psycopg
from psycopg import sql

SCHEMA = "public"
ALEMBIC_TABLE = "alembic_version"
EXPECTED_ALEMBIC_REVISION = "202609030020"
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
SYSTEM_DATABASES = {"postgres", "template0", "template1"}

# Keep this fail-closed allowlist aligned with the tables at the expected Alembic
# revision. An unknown table may belong to another application, so it must never
# be included automatically in a production clear.
APPLICATION_TABLES = {
    "agent_versions",
    "api_keys",
    "audit_logs",
    "billing_plans",
    "billing_provider_events",
    "business_profiles",
    "calls",
    "crm_sync_jobs",
    "data_requests",
    "email_tokens",
    "integration_connections",
    "invitations",
    "leads",
    "memberships",
    "organization_privacy_settings",
    "organization_spend_limits",
    "organizations",
    "phone_number_pool",
    "phone_numbers",
    "subscriptions",
    "turns",
    "usage_events",
    "usage_exports",
    "user_sessions",
    "users",
    "webhook_deliveries",
    "webhook_delivery_attempts",
    "webhook_endpoints",
    "webhook_events",
}


class ClearRefused(ValueError):
    """The requested clear did not pass a safety check."""


@dataclass(frozen=True)
class DatabaseTarget:
    host: str
    database: str


def confirmation_phrase(database: str, environment: str = "production") -> str:
    return f"CLEAR {environment.upper()} DATABASE {database}"


def validate_target(database_url: str, expected_database: str) -> DatabaseTarget:
    expected_database = expected_database.strip()
    if not expected_database:
        raise ClearRefused("--expected-database cannot be empty")
    if expected_database.lower() in SYSTEM_DATABASES:
        raise ClearRefused("PostgreSQL system databases cannot be cleared")

    parsed = urlsplit(database_url)
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise ClearRefused("DATABASE_URL must be a PostgreSQL URL")
    if not parsed.hostname:
        raise ClearRefused("DATABASE_URL must include a hostname")
    if parsed.hostname.lower() in LOCAL_HOSTS:
        raise ClearRefused("localhost database targets are forbidden by this script")

    database = unquote(parsed.path.removeprefix("/"))
    if not database or "/" in database:
        raise ClearRefused("DATABASE_URL must identify exactly one database")
    if database != expected_database:
        raise ClearRefused(
            f"target database is {database!r}, not {expected_database!r}"
        )
    return DatabaseTarget(host=parsed.hostname, database=database)


def validate_execution_safety(
    *,
    execute: bool,
    database: str,
    confirmation: str,
    backup_reference: str,
    acknowledge_external_resources: bool,
    environment: str = "production",
    skip_backup: bool = False,
) -> None:
    if not execute:
        return
    if environment not in {"staging", "production"}:
        raise ClearRefused("environment must be staging or production")
    if skip_backup and environment != "staging":
        raise ClearRefused("only staging may explicitly skip its backup")

    expected = confirmation_phrase(database, environment)
    if confirmation != expected:
        raise ClearRefused(f"confirmation must be exactly {expected!r}")
    if not backup_reference.strip() and not skip_backup:
        raise ClearRefused("a verified backup reference is required")
    if not acknowledge_external_resources:
        raise ClearRefused(
            "acknowledge that external provider resources will not be deleted"
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--expected-database",
        required=True,
        help="database name that must match both DATABASE_URL and PostgreSQL",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="perform the clear; without this flag the command is read-only",
    )
    parser.add_argument(
        "--environment",
        choices=("staging", "production"),
        default="production",
        help="target environment used by the confirmation and backup guard",
    )
    parser.add_argument(
        "--skip-backup",
        action="store_true",
        help="explicitly proceed without a backup; accepted for staging only",
    )
    parser.add_argument(
        "--acknowledge-external-resources",
        action="store_true",
        help="confirm that provider-side resources are intentionally untouched",
    )
    return parser


def _external_reference_counts(
    cursor: psycopg.Cursor[tuple[object, ...]],
) -> dict[str, int]:
    cursor.execute(
        "SELECT COUNT(DISTINCT provider_number_sid) FROM ("
        " SELECT provider_number_sid FROM phone_numbers"
        " WHERE provider_number_sid IS NOT NULL"
        " UNION ALL"
        " SELECT provider_number_sid FROM phone_number_pool"
        " WHERE provider_number_sid IS NOT NULL"
        ") AS provider_numbers"
    )
    twilio_numbers = int(cursor.fetchone()[0])
    cursor.execute(
        "SELECT COUNT(*) FROM subscriptions"
        " WHERE provider_customer_id IS NOT NULL"
        " OR provider_subscription_id IS NOT NULL"
    )
    billing_accounts = int(cursor.fetchone()[0])
    cursor.execute("SELECT COUNT(*) FROM integration_connections")
    integrations = int(cursor.fetchone()[0])
    return {
        "billing_accounts": billing_accounts,
        "integration_connections": integrations,
        "twilio_numbers": twilio_numbers,
    }


def clear_database(
    database_url: str,
    target: DatabaseTarget,
    *,
    execute: bool,
    backup_reference: str,
    environment: str,
    backup_skipped: bool,
) -> dict[str, object]:
    with (
        psycopg.connect(database_url, connect_timeout=10) as connection,
        connection.cursor() as cursor,
    ):
        cursor.execute("SET LOCAL lock_timeout = '15s'")
        cursor.execute("SET LOCAL statement_timeout = '120s'")
        cursor.execute("SELECT current_database()")
        actual_database = str(cursor.fetchone()[0])
        if actual_database != target.database:
            raise ClearRefused(
                f"PostgreSQL connected to {actual_database!r}, not {target.database!r}"
            )

        cursor.execute(
            "SELECT tablename FROM pg_catalog.pg_tables"
            " WHERE schemaname = %s ORDER BY tablename",
            (SCHEMA,),
        )
        all_tables = {str(row[0]) for row in cursor.fetchall()}
        if ALEMBIC_TABLE not in all_tables:
            raise ClearRefused("alembic_version table is missing")

        cursor.execute("SELECT version_num FROM alembic_version")
        revisions = {str(row[0]) for row in cursor.fetchall()}
        if revisions != {EXPECTED_ALEMBIC_REVISION}:
            raise ClearRefused(
                "database migration revision does not match this reset script: "
                f"expected {EXPECTED_ALEMBIC_REVISION!r}, found {sorted(revisions)!r}"
            )

        data_tables = all_tables - {ALEMBIC_TABLE}
        missing_tables = APPLICATION_TABLES - data_tables
        unexpected_tables = data_tables - APPLICATION_TABLES
        if missing_tables or unexpected_tables:
            raise ClearRefused(
                "database table set does not match this reset script: "
                f"missing={sorted(missing_tables)!r}, "
                f"unexpected={sorted(unexpected_tables)!r}"
            )

        external_references = _external_reference_counts(cursor)
        result: dict[str, object] = {
            "backup_reference": backup_reference or None,
            "backup_skipped": backup_skipped,
            "database": actual_database,
            "environment": environment,
            "external_references": external_references,
            "host": target.host,
            "mode": "execute" if execute else "preview",
            "schema": SCHEMA,
            "table_count": len(data_tables),
            "tables": sorted(data_tables),
        }
        print(json.dumps(result, sort_keys=True))

        if execute:
            identifiers = sql.SQL(", ").join(
                sql.Identifier(SCHEMA, table) for table in sorted(data_tables)
            )
            cursor.execute(
                sql.SQL("TRUNCATE TABLE {} RESTART IDENTITY").format(identifiers)
            )
            result["status"] = "cleared"
        else:
            result["status"] = "preview_only"
        return result


def main() -> None:
    args = _parser().parse_args()
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        raise SystemExit("refusing database clear: DATABASE_URL is not configured")

    try:
        target = validate_target(database_url, args.expected_database)
        backup_reference = os.environ.get("DATABASE_BACKUP_REFERENCE", "").strip()
        validate_execution_safety(
            execute=args.execute,
            database=target.database,
            confirmation=os.environ.get("DATABASE_CLEAR_CONFIRMATION", ""),
            backup_reference=backup_reference,
            acknowledge_external_resources=args.acknowledge_external_resources,
            environment=args.environment,
            skip_backup=args.skip_backup,
        )
        result = clear_database(
            database_url,
            target,
            execute=args.execute,
            backup_reference=backup_reference,
            environment=args.environment,
            backup_skipped=args.skip_backup,
        )
    except ClearRefused as exc:
        raise SystemExit(f"refusing database clear: {exc}") from exc

    print(json.dumps({"result": result["status"]}, sort_keys=True))


if __name__ == "__main__":
    main()
