"""Per-tenant calendar-month spend limits."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from app.domains.audit.models import AuditAction
from app.domains.auth.dependencies import CurrentUser
from app.store import Store

from ..exceptions import SpendLimitExceeded


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _period(now: datetime | None = None) -> tuple[datetime, datetime]:
    now = now or _utcnow()
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    return start, end


def _current_spend(
    store: Store, organization_id: str, start: datetime, end: datetime
) -> int:
    rows = store.query(
        "SELECT COALESCE(SUM(CASE WHEN customer_charge_micros <> 0"
        " THEN customer_charge_micros ELSE provider_cost_micros END), 0) AS total"
        " FROM usage_events WHERE organization_id = ?"
        " AND occurred_at >= ? AND occurred_at < ?",
        (organization_id, start, end),
    )
    return int(rows[0]["total"] or 0)


def _row(store: Store, organization_id: str) -> dict[str, Any] | None:
    rows = store.query(
        "SELECT * FROM organization_spend_limits WHERE organization_id = ?",
        (organization_id,),
    )
    return rows[0] if rows else None


def spend_limit_status(
    store: Store,
    organization_id: str,
    *,
    record_crossing: bool = False,
) -> dict[str, Any]:
    start, end = _period()
    row = _row(store, organization_id) or {
        "organization_id": organization_id,
        "monthly_limit_micros": None,
        "hard_limit": True,
        "warning_threshold_percent": 80,
        "blocked_at": None,
        "updated_by_user_id": None,
        "created_at": None,
        "updated_at": None,
    }
    limit = row.get("monthly_limit_micros")
    limit = int(limit) if limit is not None else None
    spent = _current_spend(store, organization_id, start, end)
    percent = round((spent / limit) * 100, 2) if limit else None
    warning_threshold = int(row.get("warning_threshold_percent") or 80)
    warning = bool(limit is not None and spent >= limit * warning_threshold / 100)
    blocked = bool(limit is not None and spent >= limit and row.get("hard_limit"))
    blocked_at = row.get("blocked_at")
    if isinstance(blocked_at, str):
        blocked_at = datetime.fromisoformat(blocked_at)
    if blocked_at is not None and blocked_at.tzinfo is None:
        blocked_at = blocked_at.replace(tzinfo=UTC)
    if blocked_at is not None and blocked_at < start:
        blocked_at = None

    if record_crossing and blocked and blocked_at is None:
        blocked_at = _utcnow()
        store.execute(
            "UPDATE organization_spend_limits SET blocked_at = ?, updated_at = ?"
            " WHERE organization_id = ?",
            (blocked_at, blocked_at, organization_id),
        )
        store.record_audit(
            AuditAction.SPEND_LIMIT_EXCEEDED.value,
            organization_id=organization_id,
            target_type="organization_spend_limit",
            target_id=organization_id,
            metadata={"spent_micros": spent, "monthly_limit_micros": limit},
        )

    return {
        "organization_id": organization_id,
        "monthly_limit_micros": limit,
        "hard_limit": bool(row.get("hard_limit")),
        "warning_threshold_percent": warning_threshold,
        "period_start": start,
        "period_end": end,
        "spent_micros": spent,
        "percent_used": percent,
        "warning": warning,
        "blocked": blocked,
        "blocked_at": blocked_at,
        "updated_by_user_id": row.get("updated_by_user_id"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


def update_spend_limit(
    store: Store,
    user: CurrentUser,
    organization_id: str,
    *,
    monthly_limit_micros: int | None,
    hard_limit: bool,
    warning_threshold_percent: int,
    ip: str | None,
) -> dict[str, Any]:
    now = _utcnow()
    store.transaction(
        [
            (
                (
                    "INSERT INTO organization_spend_limits"
                    " (organization_id, monthly_limit_micros, hard_limit,"
                    " warning_threshold_percent, updated_by_user_id,"
                    " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)"
                    " ON CONFLICT (organization_id) DO UPDATE SET"
                    " monthly_limit_micros = excluded.monthly_limit_micros,"
                    " hard_limit = excluded.hard_limit,"
                    " warning_threshold_percent = excluded.warning_threshold_percent,"
                    " blocked_at = NULL,"
                    " updated_by_user_id = excluded.updated_by_user_id,"
                    " updated_at = excluded.updated_at"
                ),
                (
                    organization_id,
                    monthly_limit_micros,
                    hard_limit,
                    warning_threshold_percent,
                    user.id,
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
                    AuditAction.SPEND_LIMIT_UPDATED.value,
                    "organization_spend_limit",
                    organization_id,
                    json.dumps(
                        {
                            "monthly_limit_micros": monthly_limit_micros,
                            "hard_limit": hard_limit,
                            "warning_threshold_percent": warning_threshold_percent,
                        }
                    ),
                    ip,
                    now,
                ),
            ),
        ]
    )
    return spend_limit_status(store, organization_id)


def call_is_allowed(
    store: Store, organization_id: str, *, billing_enabled: bool = True
) -> bool:
    if not billing_enabled:
        return True
    return not spend_limit_status(store, organization_id, record_crossing=True)[
        "blocked"
    ]


def require_spend_available(
    store: Store, organization_id: str, *, billing_enabled: bool = True
) -> None:
    if not call_is_allowed(
        store, organization_id, billing_enabled=billing_enabled
    ):
        raise SpendLimitExceeded()
