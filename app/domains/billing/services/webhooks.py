from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import Any

from app.domains.audit.models import AuditAction
from app.store import Store

from ..provider import StripeBillingService
from ..usage import insert_statement, usage_event


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _id(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and value.get("id"):
        return str(value["id"])
    return None


def _metadata(obj: dict[str, Any]) -> dict[str, str]:
    raw = obj.get("metadata") or {}
    return {str(key): str(value) for key, value in raw.items()}


def _subscription_lookup(
    store: Store, subscription_id: str | None, customer_id: str | None
) -> dict[str, Any] | None:
    if subscription_id:
        rows = store.query(
            "SELECT * FROM subscriptions WHERE provider_subscription_id = ?",
            (subscription_id,),
        )
        if rows:
            return rows[0]
    if customer_id:
        rows = store.query(
            "SELECT * FROM subscriptions WHERE provider_customer_id = ?",
            (customer_id,),
        )
        if rows:
            return rows[0]
    return None


def _item_data(subscription: dict[str, Any]) -> list[dict[str, Any]]:
    items = subscription.get("items") or {}
    rows = items.get("data") if isinstance(items, dict) else None
    return [row for row in (rows or []) if isinstance(row, dict)]


def _period(subscription: dict[str, Any]) -> tuple[datetime | None, datetime | None]:
    items = _item_data(subscription)
    starts = [int(row["current_period_start"]) for row in items if row.get("current_period_start")]
    ends = [int(row["current_period_end"]) for row in items if row.get("current_period_end")]
    return (
        datetime.fromtimestamp(min(starts), UTC) if starts else None,
        datetime.fromtimestamp(max(ends), UTC) if ends else None,
    )


def _plan_id(store: Store, obj: dict[str, Any], existing: dict[str, Any] | None) -> str | None:
    metadata = _metadata(obj)
    if metadata.get("billing_plan_id"):
        rows = store.query(
            "SELECT id FROM billing_plans WHERE id = ?",
            (metadata["billing_plan_id"],),
        )
        if rows:
            return str(rows[0]["id"])
    for item in _item_data(obj):
        price_id = _id(item.get("price"))
        if not price_id:
            continue
        rows = store.query(
            "SELECT id FROM billing_plans WHERE stripe_price_id = ?", (price_id,)
        )
        if rows:
            return str(rows[0]["id"])
    return str(existing["billing_plan_id"]) if existing else None


def _organization_id(
    store: Store, obj: dict[str, Any], existing: dict[str, Any] | None
) -> str | None:
    metadata = _metadata(obj)
    candidate = metadata.get("organization_id") or obj.get("client_reference_id")
    if candidate and store.organization(str(candidate)):
        return str(candidate)
    return str(existing["organization_id"]) if existing else None


def _subscription_statement(
    *,
    organization_id: str,
    plan_id: str,
    obj: dict[str, Any],
    existing: dict[str, Any] | None,
    now: datetime,
) -> tuple[str, tuple[Any, ...]]:
    period_start, period_end = _period(obj)
    trial_end = (
        datetime.fromtimestamp(int(obj["trial_end"]), UTC)
        if obj.get("trial_end")
        else None
    )
    customer_id = _id(obj.get("customer"))
    subscription_id = _id(obj.get("id"))
    status = str(obj.get("status") or "incomplete")
    internal_id = str(existing["id"]) if existing else str(uuid.uuid4())
    return (
        (
            "INSERT INTO subscriptions"
            " (id, organization_id, billing_plan_id, provider, provider_customer_id,"
            " provider_subscription_id, status, current_period_start,"
            " current_period_end, trial_end, cancel_at_period_end, created_at,"
            " updated_at) VALUES (?, ?, ?, 'stripe', ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (organization_id) DO UPDATE SET"
            " billing_plan_id = excluded.billing_plan_id,"
            " provider_customer_id = COALESCE(excluded.provider_customer_id,"
            " subscriptions.provider_customer_id),"
            " provider_subscription_id = excluded.provider_subscription_id,"
            " status = excluded.status,"
            " current_period_start = excluded.current_period_start,"
            " current_period_end = excluded.current_period_end,"
            " trial_end = excluded.trial_end,"
            " cancel_at_period_end = excluded.cancel_at_period_end,"
            " updated_at = excluded.updated_at"
        ),
        (
            internal_id,
            organization_id,
            plan_id,
            customer_id,
            subscription_id,
            status,
            period_start,
            period_end,
            trial_end,
            bool(obj.get("cancel_at_period_end")),
            now,
            now,
        ),
    )


def process_webhook(
    store: Store,
    provider: StripeBillingService,
    payload: bytes,
    signature: str | None,
) -> dict[str, Any]:
    event = provider.construct_event(payload, signature)
    event_id = str(event["id"])
    event_type = str(event["type"])
    duplicate = store.query(
        "SELECT outcome FROM billing_provider_events"
        " WHERE provider = 'stripe' AND provider_event_id = ?",
        (event_id,),
    )
    if duplicate:
        return {"received": True, "duplicate": True, "outcome": duplicate[0]["outcome"]}

    obj = ((event.get("data") or {}).get("object") or {})
    if not isinstance(obj, dict):
        obj = {}
    now = _utcnow()
    statements: list[tuple[str, tuple[Any, ...]]] = []
    organization_id: str | None = None
    outcome = "ignored"

    if event_type == "checkout.session.completed":
        customer_id = _id(obj.get("customer"))
        provider_subscription_id = _id(obj.get("subscription"))
        existing = _subscription_lookup(store, provider_subscription_id, customer_id)
        organization_id = _organization_id(store, obj, existing)
        plan_id = _plan_id(store, obj, existing)
        if organization_id and plan_id:
            statements.append(
                (
                    (
                        "UPDATE subscriptions SET billing_plan_id = ?,"
                        " provider_customer_id = COALESCE(?, provider_customer_id),"
                        " provider_subscription_id = COALESCE(?,"
                        " provider_subscription_id), updated_at = ?"
                        " WHERE organization_id = ?"
                    ),
                    (
                        plan_id,
                        customer_id,
                        provider_subscription_id,
                        now,
                        organization_id,
                    ),
                )
            )
            outcome = "processed"

    elif event_type.startswith("customer.subscription."):
        customer_id = _id(obj.get("customer"))
        provider_subscription_id = _id(obj.get("id"))
        existing = _subscription_lookup(store, provider_subscription_id, customer_id)
        organization_id = _organization_id(store, obj, existing)
        plan_id = _plan_id(store, obj, existing)
        if organization_id and plan_id:
            statements.append(
                _subscription_statement(
                    organization_id=organization_id,
                    plan_id=plan_id,
                    obj=obj,
                    existing=existing,
                    now=now,
                )
            )
            outcome = "processed"

    elif event_type in {"invoice.paid", "invoice.payment_failed"}:
        customer_id = _id(obj.get("customer"))
        parent = obj.get("parent") or {}
        details = parent.get("subscription_details") if isinstance(parent, dict) else {}
        provider_subscription_id = _id(
            (details or {}).get("subscription") if isinstance(details, dict) else None
        ) or _id(obj.get("subscription"))
        existing = _subscription_lookup(store, provider_subscription_id, customer_id)
        if existing:
            organization_id = str(existing["organization_id"])
            invoice_status = "paid" if event_type == "invoice.paid" else "payment_failed"
            statements.append(
                (
                    (
                        "UPDATE subscriptions SET last_invoice_status = ?,"
                        " updated_at = ? WHERE id = ?"
                    ),
                    (invoice_status, now, existing["id"]),
                )
            )
            invoice_event = usage_event(
                organization_id=organization_id,
                event_type=f"stripe.{event_type}",
                quantity=1,
                unit="invoice",
                source="stripe",
                idempotency_key=event_id,
                provider_reference=_id(obj.get("id")),
                occurred_at=now,
                metadata={
                    "amount_due_minor": int(obj.get("amount_due") or 0),
                    "amount_paid_minor": int(obj.get("amount_paid") or 0),
                    "currency": str(obj.get("currency") or "").upper(),
                    "status": obj.get("status"),
                },
            )
            statements.append(insert_statement(invoice_event))
            outcome = "processed"

    if organization_id and outcome == "processed":
        statements.append(
            (
                (
                    "INSERT INTO audit_logs"
                    " (organization_id, action, target_type, target_id, metadata,"
                    " created_at) VALUES (?, ?, ?, ?, ?, ?)"
                ),
                (
                    organization_id,
                    AuditAction.SUBSCRIPTION_UPDATED.value,
                    "stripe_event",
                    event_id,
                    json.dumps({"event_type": event_type}),
                    now,
                ),
            )
        )
    statements.append(
        (
            (
                "INSERT INTO billing_provider_events"
                " (id, provider, provider_event_id, event_type, organization_id,"
                " payload_sha256, outcome, received_at)"
                " VALUES (?, 'stripe', ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (provider, provider_event_id) DO NOTHING"
            ),
            (
                str(uuid.uuid4()),
                event_id,
                event_type,
                organization_id,
                hashlib.sha256(payload).hexdigest(),
                outcome,
                now,
            ),
        )
    )
    store.transaction(statements)
    return {"received": True, "duplicate": False, "outcome": outcome}
