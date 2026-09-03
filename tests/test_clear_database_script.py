from __future__ import annotations

from importlib import import_module
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

from app.database import Base
from scripts.clear_database import (
    APPLICATION_TABLES,
    EXPECTED_ALEMBIC_REVISION,
    ClearRefused,
    confirmation_phrase,
    validate_execution_safety,
    validate_target,
)

ROOT = Path(__file__).resolve().parent.parent
MODEL_MODULES = (
    "app.domains.api_keys.models",
    "app.domains.audit.models",
    "app.domains.auth.models",
    "app.domains.billing.models",
    "app.domains.businesses.models",
    "app.domains.calls.models",
    "app.domains.integrations.models",
    "app.domains.organizations.models",
    "app.domains.privacy.models",
    "app.domains.telephony.models",
    "app.domains.tenancy.models",
    "app.domains.webhooks.models",
)


def test_validate_target_accepts_named_remote_postgres_database() -> None:
    target = validate_target(
        "postgresql://callagent:secret@db:5432/callagent", "callagent"
    )

    assert target.host == "db"
    assert target.database == "callagent"


def test_safety_allowlist_matches_current_schema() -> None:
    for module in MODEL_MODULES:
        import_module(module)

    config = Config(str(ROOT / "alembic.ini"))
    scripts = ScriptDirectory.from_config(config)

    assert scripts.get_current_head() == EXPECTED_ALEMBIC_REVISION
    assert set(Base.metadata.tables) == APPLICATION_TABLES


@pytest.mark.parametrize(
    ("database_url", "expected_database"),
    [
        ("sqlite:///callagent.db", "callagent"),
        ("postgresql://user:secret@localhost/callagent", "callagent"),
        ("postgresql://user:secret@db/postgres", "postgres"),
        ("postgresql://user:secret@db/not-production", "callagent"),
    ],
)
def test_validate_target_rejects_unsafe_targets(
    database_url: str, expected_database: str
) -> None:
    with pytest.raises(ClearRefused):
        validate_target(database_url, expected_database)


def test_preview_does_not_require_destructive_confirmation() -> None:
    validate_execution_safety(
        execute=False,
        database="callagent",
        confirmation="",
        backup_reference="",
        acknowledge_external_resources=False,
    )


@pytest.mark.parametrize(
    ("confirmation", "backup_reference", "acknowledged"),
    [
        ("wrong", "backup-123", True),
        (confirmation_phrase("callagent"), "", True),
        (confirmation_phrase("callagent"), "backup-123", False),
    ],
)
def test_execution_requires_every_safety_gate(
    confirmation: str, backup_reference: str, acknowledged: bool
) -> None:
    with pytest.raises(ClearRefused):
        validate_execution_safety(
            execute=True,
            database="callagent",
            confirmation=confirmation,
            backup_reference=backup_reference,
            acknowledge_external_resources=acknowledged,
        )


def test_execution_accepts_exact_confirmation_and_backup() -> None:
    validate_execution_safety(
        execute=True,
        database="callagent",
        confirmation=confirmation_phrase("callagent"),
        backup_reference="coolify-backup-2026-09-03",
        acknowledge_external_resources=True,
    )


def test_staging_execution_can_explicitly_skip_backup() -> None:
    validate_execution_safety(
        execute=True,
        database="callagent",
        confirmation=confirmation_phrase("callagent", "staging"),
        backup_reference="",
        acknowledge_external_resources=True,
        environment="staging",
        skip_backup=True,
    )


def test_production_execution_cannot_skip_backup() -> None:
    with pytest.raises(ClearRefused):
        validate_execution_safety(
            execute=True,
            database="callagent",
            confirmation=confirmation_phrase("callagent"),
            backup_reference="",
            acknowledge_external_resources=True,
            environment="production",
            skip_backup=True,
        )
