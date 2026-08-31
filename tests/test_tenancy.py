from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.store import Store, TenantScopeError

LEGACY_SCHEMA = """
CREATE TABLE calls (
    call_id TEXT PRIMARY KEY,
    business TEXT,
    from_number TEXT,
    to_number TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    outcome TEXT,
    summary TEXT,
    model_cost REAL DEFAULT 0
);
CREATE TABLE turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id TEXT NOT NULL,
    role TEXT NOT NULL,
    text TEXT NOT NULL,
    at TEXT NOT NULL
);
CREATE TABLE leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id TEXT NOT NULL,
    intent TEXT,
    caller_name TEXT,
    callback_number TEXT,
    urgency TEXT,
    preferred_time TEXT,
    details TEXT,
    at TEXT NOT NULL
);
"""


def _start_call(
    store: Store, organization_id: str, call_id: str, business: str
) -> None:
    store.start_call(organization_id, call_id, business, "+15550000001", "+15550000002")


def test_call_reads_are_isolated_by_organization(tmp_path: Path):
    store = Store(tmp_path / "calls.sqlite3")
    first = store.ensure_organization("first", "First Business")
    second = store.ensure_organization("second", "Second Business")
    _start_call(store, first, "call-first", "First Business")
    _start_call(store, second, "call-second", "Second Business")
    store.add_turn(first, "call-first", "caller", "first tenant transcript")
    store.add_turn(second, "call-second", "caller", "second tenant transcript")
    store.add_lead(first, "call-first", {"intent": "first tenant lead"})
    store.add_lead(second, "call-second", {"intent": "second tenant lead"})

    assert [call["call_id"] for call in store.recent_calls(first)] == ["call-first"]
    assert [call["call_id"] for call in store.recent_calls(second)] == ["call-second"]
    assert store.call_detail(first, "call-second") is None
    assert store.transcript(first, "call-second") == []
    assert store.recent_calls(first)[0]["leads"][0]["intent"] == "first tenant lead"


def test_cross_organization_writes_are_rejected_or_ignored(tmp_path: Path):
    store = Store(tmp_path / "calls.sqlite3")
    first = store.ensure_organization("first", "First Business")
    second = store.ensure_organization("second", "Second Business")
    _start_call(store, first, "shared-call-id", "First Business")

    with pytest.raises(TenantScopeError):
        _start_call(store, second, "shared-call-id", "Second Business")
    with pytest.raises(sqlite3.IntegrityError):
        store.add_turn(second, "shared-call-id", "caller", "wrong tenant")

    store.finish_call(second, "shared-call-id", "tampered", "", 0)
    detail = store.call_detail(first, "shared-call-id")
    assert detail is not None
    assert detail["outcome"] is None


def test_users_can_have_roles_in_multiple_organizations(tmp_path: Path):
    store = Store(tmp_path / "calls.sqlite3")
    first = store.ensure_organization("first", "First Business")
    second = store.ensure_organization("second", "Second Business")
    user_id = store.create_user("OWNER@EXAMPLE.COM")
    assert store.create_user("owner@example.com") == user_id
    store.add_membership(first, user_id, "owner")
    store.add_membership(second, user_id, "viewer")

    memberships = store.organizations_for_user(user_id)
    assert {(item["slug"], item["role"]) for item in memberships} == {
        ("first", "owner"),
        ("second", "viewer"),
    }
    with pytest.raises(ValueError, match="unknown membership role"):
        store.add_membership(first, user_id, "superuser")


def test_legacy_call_data_is_backfilled_without_losing_timestamps(tmp_path: Path):
    database_path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(LEGACY_SCHEMA)
        connection.execute(
            "INSERT INTO calls"
            " (call_id, business, from_number, to_number, started_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (
                "legacy-call",
                "Legacy Business",
                "+15550000001",
                "+15550000002",
                "2026-01-02T03:04:05+00:00",
            ),
        )
        connection.execute(
            "INSERT INTO turns (call_id, role, text, at) VALUES (?, ?, ?, ?)",
            (
                "legacy-call",
                "caller",
                "keep this transcript",
                "2026-01-02T03:04:06+00:00",
            ),
        )
        connection.execute(
            "INSERT INTO leads (call_id, intent, at) VALUES (?, ?, ?)",
            (
                "orphaned-call",
                "preserve orphaned lead",
                "2026-01-02T03:04:07+00:00",
            ),
        )

    store = Store(database_path)
    with sqlite3.connect(database_path) as connection:
        organization_id = connection.execute(
            "SELECT organization_id FROM calls WHERE call_id = ?", ("legacy-call",)
        ).fetchone()[0]

    assert store.organization_id_for_slug("legacy-business") == organization_id
    detail = store.call_detail(organization_id, "legacy-call")
    assert detail is not None
    assert detail["started_at"] == "2026-01-02T03:04:05+00:00"
    assert detail["transcript"] == [
        {
            "role": "caller",
            "text": "keep this transcript",
            "at": "2026-01-02T03:04:06+00:00",
        }
    ]

    with sqlite3.connect(database_path) as connection:
        orphaned_organization_id = connection.execute(
            "SELECT organization_id FROM calls WHERE call_id = ?", ("orphaned-call",)
        ).fetchone()[0]
    orphaned = store.call_detail(orphaned_organization_id, "orphaned-call")
    assert orphaned is not None
    assert orphaned["leads"][0]["intent"] == "preserve orphaned lead"
