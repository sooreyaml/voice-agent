"""Privacy settings, retention, export, and delayed account deletion."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from app.domains.audit.models import AuditAction
from app.domains.auth.dependencies import CurrentUser
from app.store import Store

from .constants import (
    DEFAULT_RETENTION_DAYS,
    DELETION_GRACE_PERIOD,
    EXPORT_TTL,
    JOB_LOCK_TIMEOUT,
)
from .exceptions import (
    DataRequestConflict,
    DataRequestNotCancellable,
    DataRequestNotFound,
    DeletionConfirmationMismatch,
)


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _parse_json(raw: Any, fallback: Any = None) -> Any:
    if not isinstance(raw, str):
        return raw if raw is not None else fallback
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return fallback


def _privacy_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "organization_id": str(row["organization_id"]),
        "transcript_retention_days": row.get("transcript_retention_days"),
        "updated_by_user_id": row.get("updated_by_user_id"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


def _request_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "organization_id": str(row["organization_id"]),
        "requested_by_user_id": row.get("requested_by_user_id"),
        "kind": str(row["kind"]),
        "status": str(row["status"]),
        "attempts": int(row.get("attempts") or 0),
        "max_attempts": int(row.get("max_attempts") or 5),
        "execute_after": row["execute_after"],
        "result": _parse_json(row.get("result")),
        "result_expires_at": row.get("result_expires_at"),
        "last_error": row.get("last_error"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "completed_at": row.get("completed_at"),
    }


def get_privacy_settings(store: Store, organization_id: str) -> dict[str, Any]:
    store.execute(
        "INSERT INTO organization_privacy_settings"
        " (organization_id, transcript_retention_days) VALUES (?, ?)"
        " ON CONFLICT (organization_id) DO NOTHING",
        (organization_id, DEFAULT_RETENTION_DAYS),
    )
    rows = store.query(
        "SELECT organization_id, transcript_retention_days, updated_by_user_id,"
        " created_at, updated_at FROM organization_privacy_settings"
        " WHERE organization_id = ?",
        (organization_id,),
    )
    if not rows:  # pragma: no cover - organization dependency guarantees the FK
        raise RuntimeError("privacy settings could not be loaded")
    return _privacy_payload(rows[0])


def update_privacy_settings(
    store: Store,
    user: CurrentUser,
    organization_id: str,
    *,
    transcript_retention_days: int | None,
    ip: str | None,
) -> dict[str, Any]:
    now = _utcnow()
    store.transaction(
        [
            (
                (
                    "INSERT INTO organization_privacy_settings"
                    " (organization_id, transcript_retention_days,"
                    " updated_by_user_id, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?)"
                    " ON CONFLICT (organization_id) DO UPDATE SET"
                    " transcript_retention_days = excluded.transcript_retention_days,"
                    " updated_by_user_id = excluded.updated_by_user_id,"
                    " updated_at = excluded.updated_at"
                ),
                (organization_id, transcript_retention_days, user.id, now, now),
            ),
            (
                (
                    "INSERT INTO audit_logs"
                    " (organization_id, actor_user_id, action, target_type, target_id,"
                    " metadata, ip, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
                ),
                (
                    organization_id,
                    user.id,
                    AuditAction.PRIVACY_SETTINGS_UPDATED.value,
                    "organization_privacy_settings",
                    organization_id,
                    json.dumps(
                        {"transcript_retention_days": transcript_retention_days}
                    ),
                    ip,
                    now,
                ),
            ),
        ]
    )
    return get_privacy_settings(store, organization_id)


def _data_request(
    store: Store, organization_id: str, request_id: str
) -> dict[str, Any] | None:
    rows = store.query(
        "SELECT * FROM data_requests WHERE organization_id = ? AND id = ?",
        (organization_id, request_id),
    )
    return rows[0] if rows else None


def create_export_request(
    store: Store,
    user: CurrentUser,
    organization_id: str,
    *,
    idempotency_key: str,
    ip: str | None,
) -> dict[str, Any]:
    return _create_request(
        store,
        user,
        organization_id,
        kind="export",
        idempotency_key=idempotency_key,
        execute_after=_utcnow(),
        ip=ip,
    )


def create_deletion_request(
    store: Store,
    user: CurrentUser,
    organization_id: str,
    *,
    idempotency_key: str,
    confirm_organization_slug: str,
    ip: str | None,
) -> dict[str, Any]:
    organization = store.organization(organization_id)
    if organization is None or confirm_organization_slug != organization["slug"]:
        raise DeletionConfirmationMismatch()
    active = store.query(
        "SELECT * FROM data_requests WHERE organization_id = ?"
        " AND kind = 'deletion' AND status IN ('pending', 'processing')"
        " ORDER BY created_at DESC LIMIT 1",
        (organization_id,),
    )
    if active:
        if active[0]["idempotency_key"] == idempotency_key:
            return _request_payload(active[0])
        raise DataRequestConflict()
    return _create_request(
        store,
        user,
        organization_id,
        kind="deletion",
        idempotency_key=idempotency_key,
        execute_after=_utcnow() + DELETION_GRACE_PERIOD,
        ip=ip,
    )


def _create_request(
    store: Store,
    user: CurrentUser,
    organization_id: str,
    *,
    kind: str,
    idempotency_key: str,
    execute_after: datetime,
    ip: str | None,
) -> dict[str, Any]:
    existing = store.query(
        "SELECT * FROM data_requests WHERE organization_id = ? AND kind = ?"
        " AND idempotency_key = ?",
        (organization_id, kind, idempotency_key),
    )
    if existing:
        return _request_payload(existing[0])
    request_id = str(uuid.uuid4())
    now = _utcnow()
    action = (
        AuditAction.DATA_EXPORT_REQUESTED
        if kind == "export"
        else AuditAction.DATA_DELETION_REQUESTED
    )
    store.transaction(
        [
            (
                (
                    "INSERT INTO data_requests"
                    " (id, organization_id, requested_by_user_id, kind, status,"
                    " idempotency_key, execute_after, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?)"
                ),
                (
                    request_id,
                    organization_id,
                    user.id,
                    kind,
                    idempotency_key,
                    execute_after,
                    now,
                    now,
                ),
            ),
            (
                (
                    "INSERT INTO audit_logs"
                    " (organization_id, actor_user_id, action, target_type, target_id,"
                    " metadata, ip, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
                ),
                (
                    organization_id,
                    user.id,
                    action.value,
                    "data_request",
                    request_id,
                    json.dumps({"kind": kind, "execute_after": str(execute_after)}),
                    ip,
                    now,
                ),
            ),
        ]
    )
    row = _data_request(store, organization_id, request_id)
    if row is None:  # pragma: no cover
        raise RuntimeError("data request could not be reloaded")
    return _request_payload(row)


def list_data_requests(store: Store, organization_id: str) -> list[dict[str, Any]]:
    return [
        _request_payload(row)
        for row in store.query(
            "SELECT * FROM data_requests WHERE organization_id = ?"
            " ORDER BY created_at DESC, id DESC",
            (organization_id,),
        )
    ]


def get_data_request(
    store: Store, organization_id: str, request_id: str
) -> dict[str, Any]:
    row = _data_request(store, organization_id, request_id)
    if row is None:
        raise DataRequestNotFound()
    expires_at = row.get("result_expires_at")
    if isinstance(expires_at, str):
        expires_at = datetime.fromisoformat(expires_at)
    if expires_at is not None and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if expires_at is not None and expires_at <= _utcnow():
        row["result"] = None
    return _request_payload(row)


def cancel_data_request(
    store: Store,
    user: CurrentUser,
    organization_id: str,
    request_id: str,
    *,
    ip: str | None,
) -> dict[str, Any]:
    now = _utcnow()
    rows = store.execute_returning(
        "UPDATE data_requests SET status = 'cancelled', updated_at = ?"
        " WHERE organization_id = ? AND id = ? AND status = 'pending'"
        " RETURNING id",
        (now, organization_id, request_id),
    )
    if not rows:
        existing = _data_request(store, organization_id, request_id)
        if existing is None:
            raise DataRequestNotFound()
        raise DataRequestNotCancellable()
    store.record_audit(
        AuditAction.DATA_REQUEST_CANCELLED.value,
        organization_id=organization_id,
        actor_user_id=user.id,
        target_type="data_request",
        target_id=request_id,
        ip=ip,
    )
    row = _data_request(store, organization_id, request_id)
    assert row is not None
    return _request_payload(row)


def purge_expired_transcripts(store: Store, *, limit: int = 100) -> int:
    """Erase transcript turns and summaries once each tenant's policy expires."""
    now = _utcnow()
    candidates = store.query(
        "SELECT calls.organization_id, calls.call_id, calls.ended_at,"
        " CASE WHEN organization_privacy_settings.organization_id IS NULL THEN ?"
        " ELSE organization_privacy_settings.transcript_retention_days END"
        " AS retention_days FROM calls"
        " LEFT JOIN organization_privacy_settings"
        " ON organization_privacy_settings.organization_id = calls.organization_id"
        " WHERE calls.ended_at IS NOT NULL"
        " AND calls.transcript_deleted_at IS NULL"
        " ORDER BY calls.ended_at LIMIT ?",
        (DEFAULT_RETENTION_DAYS, limit * 5),
    )
    expired: list[tuple[str, str]] = []
    for row in candidates:
        days = row.get("retention_days")
        if days is None:
            continue
        ended_at = row["ended_at"]
        if isinstance(ended_at, str):
            ended_at = datetime.fromisoformat(ended_at)
        if ended_at.tzinfo is None:
            ended_at = ended_at.replace(tzinfo=UTC)
        if ended_at + timedelta(days=int(days)) <= now:
            expired.append((str(row["organization_id"]), str(row["call_id"])))
        if len(expired) >= limit:
            break
    statements: list[tuple[str, tuple[Any, ...]]] = []
    for organization_id, call_id in expired:
        statements.extend(
            [
                (
                    "DELETE FROM turns WHERE organization_id = ? AND call_id = ?",
                    (organization_id, call_id),
                ),
                (
                    (
                        "UPDATE calls SET summary = NULL, transcript_deleted_at = ?"
                        " WHERE organization_id = ? AND call_id = ?"
                    ),
                    (now, organization_id, call_id),
                ),
            ]
        )
    if statements:
        store.transaction(statements)
    return len(expired)


def _export_rows(store: Store, sql: str, params: tuple[Any, ...]) -> list[dict]:
    rows = store.query(sql, params)
    for row in rows:
        for key in ("config", "metadata", "settings", "scopes", "event_types"):
            if key in row:
                row[key] = _parse_json(row[key], row[key])
    return rows


def build_account_export(store: Store, organization_id: str) -> dict[str, Any]:
    """Build a portable snapshot without credential hashes or encrypted secrets."""
    params = (organization_id,)
    organization = store.query(
        "SELECT id, slug, name, created_at, updated_at FROM organizations WHERE id = ?",
        params,
    )
    return {
        "version": 1,
        "generated_at": _utcnow(),
        "organization": organization[0] if organization else None,
        "members": _export_rows(
            store,
            "SELECT users.email, memberships.role, memberships.created_at"
            " FROM memberships JOIN users ON users.id = memberships.user_id"
            " WHERE memberships.organization_id = ? ORDER BY users.email",
            params,
        ),
        "business_profiles": _export_rows(
            store,
            "SELECT id, slug, name, timezone, created_at, updated_at"
            " FROM business_profiles WHERE organization_id = ?",
            params,
        ),
        "agent_versions": _export_rows(
            store,
            "SELECT id, business_profile_id, version_number, status, config,"
            " rendered_prompt, created_at, published_at FROM agent_versions"
            " WHERE organization_id = ? ORDER BY version_number",
            params,
        ),
        "phone_numbers": _export_rows(
            store, "SELECT * FROM phone_numbers WHERE organization_id = ?", params
        ),
        "calls": _export_rows(
            store, "SELECT * FROM calls WHERE organization_id = ?", params
        ),
        "turns": _export_rows(
            store,
            "SELECT call_id, role, text, at FROM turns WHERE organization_id = ?"
            " ORDER BY id",
            params,
        ),
        "leads": _export_rows(
            store, "SELECT * FROM leads WHERE organization_id = ? ORDER BY id", params
        ),
        "integrations": _export_rows(
            store,
            "SELECT provider, status, display_name, external_account_id, scopes,"
            " settings, last_error, last_verified_at, created_at, updated_at"
            " FROM integration_connections WHERE organization_id = ?",
            params,
        ),
        "webhook_endpoints": _export_rows(
            store,
            "SELECT id, url, description, event_types, active, created_at, updated_at"
            " FROM webhook_endpoints WHERE organization_id = ?",
            params,
        ),
        "api_keys": _export_rows(
            store,
            "SELECT id, name, prefix, scopes, last_used_at, revoked_at, created_at"
            " FROM api_keys WHERE organization_id = ?",
            params,
        ),
        "subscriptions": _export_rows(
            store, "SELECT * FROM subscriptions WHERE organization_id = ?", params
        ),
        "usage_events": _export_rows(
            store,
            "SELECT * FROM usage_events WHERE organization_id = ? ORDER BY occurred_at",
            params,
        ),
        "audit_log": _export_rows(
            store,
            "SELECT action, target_type, target_id, metadata, ip, created_at"
            " FROM audit_logs WHERE organization_id = ? ORDER BY id",
            params,
        ),
    }


def _claim_data_requests(store: Store, limit: int) -> list[dict[str, Any]]:
    now = _utcnow()
    stale = now - JOB_LOCK_TIMEOUT
    due = (
        "SELECT id FROM data_requests WHERE"
        " (status = 'pending' AND execute_after <= ?)"
        " OR (status = 'failed' AND attempts < max_attempts AND execute_after <= ?)"
        " OR (status = 'processing' AND locked_at IS NOT NULL AND locked_at < ?)"
        " ORDER BY execute_after LIMIT ?"
    )
    if store.dialect == "postgres":
        due += " FOR UPDATE SKIP LOCKED"
    claimed = store.execute_returning(
        "UPDATE data_requests SET status = 'processing', locked_at = ?, updated_at = ?"
        f" WHERE id IN ({due}) RETURNING id",
        (now, now, now, now, stale, limit),
    )
    ids = [str(row["id"]) for row in claimed]
    if not ids:
        return []
    placeholders = ", ".join("?" for _ in ids)
    return store.query(
        f"SELECT * FROM data_requests WHERE id IN ({placeholders})", tuple(ids)
    )


def process_due_data_requests(store: Store, *, limit: int = 10) -> int:
    now = _utcnow()
    # Export artifacts are deliberately short-lived even though request metadata
    # remains available for audit.
    store.execute(
        "UPDATE data_requests SET result = NULL, updated_at = ?"
        " WHERE result IS NOT NULL AND result_expires_at <= ?",
        (now, now),
    )
    jobs = _claim_data_requests(store, limit)
    for job in jobs:
        request_id = str(job["id"])
        organization_id = str(job["organization_id"])
        try:
            if job["kind"] == "export":
                result = json.dumps(
                    build_account_export(store, organization_id),
                    ensure_ascii=False,
                    default=str,
                )
                completed_at = _utcnow()
                store.execute(
                    "UPDATE data_requests SET status = 'completed', result = ?,"
                    " result_expires_at = ?, completed_at = ?, last_error = NULL,"
                    " locked_at = NULL, updated_at = ? WHERE id = ?",
                    (
                        result,
                        completed_at + EXPORT_TTL,
                        completed_at,
                        completed_at,
                        request_id,
                    ),
                )
            else:
                execute_account_deletion(store, organization_id)
                completed_at = _utcnow()
                store.execute(
                    "UPDATE data_requests SET status = 'completed', completed_at = ?,"
                    " requested_by_user_id = NULL, locked_at = NULL, updated_at = ?"
                    " WHERE id = ?",
                    (completed_at, completed_at, request_id),
                )
            store.record_audit(
                AuditAction.DATA_REQUEST_COMPLETED.value,
                organization_id=organization_id,
                target_type="data_request",
                target_id=request_id,
                metadata={"kind": job["kind"]},
            )
        except Exception as exc:  # noqa: BLE001 - one failed job must not stop the queue
            failed_at = _utcnow()
            attempts = int(job.get("attempts") or 0) + 1
            dead = attempts >= int(job.get("max_attempts") or 5)
            store.execute(
                "UPDATE data_requests SET status = ?, attempts = ?, last_error = ?,"
                " execute_after = ?, locked_at = NULL, updated_at = ? WHERE id = ?",
                (
                    "dead" if dead else "failed",
                    attempts,
                    str(exc)[:1000],
                    failed_at + timedelta(seconds=min(2**attempts * 30, 3600)),
                    failed_at,
                    request_id,
                ),
            )
    return len(jobs)


def execute_account_deletion(store: Store, organization_id: str) -> None:
    """Pseudonymize the tenant while retaining immutable financial evidence.

    Calls and the usage ledger remain as anonymous accounting records because
    foreign keys and ledger immutability intentionally prevent erasing them.
    Transcripts, leads, credentials, delivery payloads, and membership access are
    removed. External number/subscription cancellation remains an operational
    provider step; their identifiers are removed locally and routing is disabled.
    """
    now = _utcnow()
    redacted_slug = f"deleted-{organization_id}"
    store.transaction(
        [
            ("DELETE FROM crm_sync_jobs WHERE organization_id = ?", (organization_id,)),
            (
                "DELETE FROM webhook_events WHERE organization_id = ?",
                (organization_id,),
            ),
            (
                "DELETE FROM webhook_endpoints WHERE organization_id = ?",
                (organization_id,),
            ),
            (
                "DELETE FROM integration_connections WHERE organization_id = ?",
                (organization_id,),
            ),
            ("DELETE FROM invitations WHERE organization_id = ?", (organization_id,)),
            ("DELETE FROM api_keys WHERE organization_id = ?", (organization_id,)),
            ("DELETE FROM turns WHERE organization_id = ?", (organization_id,)),
            ("DELETE FROM leads WHERE organization_id = ?", (organization_id,)),
            (
                (
                    "UPDATE calls SET business = 'Deleted organization',"
                    " from_number = NULL, to_number = NULL, summary = NULL,"
                    " transcript_deleted_at = ? WHERE organization_id = ?"
                ),
                (now, organization_id),
            ),
            (
                (
                    # The E.164 on the phone_numbers row is scrambled just below,
                    # so the pooled number can safely go back into circulation
                    # after a quarantine window.
                    "UPDATE phone_number_pool SET status = 'quarantined',"
                    " assigned_organization_id = NULL, assigned_at = NULL,"
                    " quarantined_until = ?, updated_at = ?"
                    " WHERE assigned_organization_id = ?"
                ),
                (now + timedelta(days=30), now, organization_id),
            ),
            (
                (
                    "UPDATE phone_numbers SET e164 = 'x' || substr(replace(id, '-', ''), 1, 15),"
                    " status = 'inactive', provider_account_sid = NULL,"
                    " provider_number_sid = NULL, provider_trunk_sid = NULL, updated_at = ?"
                    " WHERE organization_id = ?"
                ),
                (now, organization_id),
            ),
            (
                (
                    "UPDATE agent_versions SET status = 'archived', config = '{}',"
                    " rendered_prompt = '[redacted]' WHERE organization_id = ?"
                ),
                (organization_id,),
            ),
            (
                (
                    "UPDATE business_profiles SET"
                    " slug = 'deleted-' || substr(replace(id, '-', ''), 1, 32),"
                    " name = 'Deleted organization',"
                    " timezone = 'UTC', updated_at = ? WHERE organization_id = ?"
                ),
                (now, organization_id),
            ),
            (
                (
                    "UPDATE subscriptions SET provider_customer_id = NULL,"
                    " provider_subscription_id = NULL, status = 'canceled',"
                    " last_invoice_status = NULL, updated_at = ? WHERE organization_id = ?"
                ),
                (now, organization_id),
            ),
            (
                (
                    "UPDATE billing_provider_events SET organization_id = NULL"
                    " WHERE organization_id = ?"
                ),
                (organization_id,),
            ),
            (
                (
                    "UPDATE audit_logs SET actor_user_id = NULL, metadata = NULL, ip = NULL"
                    " WHERE organization_id = ?"
                ),
                (organization_id,),
            ),
            (
                (
                    "UPDATE data_requests SET requested_by_user_id = NULL, result = NULL,"
                    " result_expires_at = NULL, updated_at = ? WHERE organization_id = ?"
                ),
                (now, organization_id),
            ),
            ("DELETE FROM memberships WHERE organization_id = ?", (organization_id,)),
            (
                (
                    "UPDATE organizations SET slug = ?, name = 'Deleted organization',"
                    " deleted_at = ?, updated_at = ? WHERE id = ?"
                ),
                (redacted_slug, now, now, organization_id),
            ),
        ]
    )
