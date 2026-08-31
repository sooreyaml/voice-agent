from __future__ import annotations

import json
import uuid
from calendar import monthrange
from datetime import UTC, datetime
from typing import Any

from app.domains.audit.models import AuditAction
from app.domains.auth.dependencies import CurrentUser, OrgContext
from app.store import Store

from ..exceptions import (
    ActiveSubscriptionConflict,
    BillingIdempotencyConflict,
    BillingPlanConflict,
    BillingPlanNotFound,
    BillingProviderNotConfigured,
    BillingProviderUnavailable,
    BillingSubscriptionNotFound,
    UsageEventNotFound,
)
from ..provider import StripeBillingError, StripeBillingService
from ..schemas import (
    CheckoutSessionRequest,
    CreateBillingPlanRequest,
    PortalSessionRequest,
    RecordUsageEventRequest,
    UpdateBillingPlanRequest,
)
from ..usage import insert_statement, usage_event


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _json(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def plan_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "code": str(row["code"]),
        "name": str(row["name"]),
        "status": str(row["status"]),
        "currency": str(row["currency"]),
        "monthly_amount_minor": int(row["monthly_amount_minor"]),
        "included_seconds": int(row["included_seconds"]),
        "overage_amount_micros_per_second": int(
            row["overage_amount_micros_per_second"]
        ),
        "stripe_price_id": row.get("stripe_price_id"),
        "stripe_meter_event_name": row.get("stripe_meter_event_name"),
        "entitlements": _json(row.get("entitlements")),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


def _plan_by_code(
    store: Store, code: str, *, active_only: bool = False
) -> dict[str, Any] | None:
    sql = "SELECT * FROM billing_plans WHERE code = ?"
    params: tuple[Any, ...] = (code,)
    if active_only:
        sql += " AND status = 'active'"
    rows = store.query(sql, params)
    return rows[0] if rows else None


def _plan_by_id(store: Store, plan_id: str) -> dict[str, Any] | None:
    rows = store.query("SELECT * FROM billing_plans WHERE id = ?", (plan_id,))
    return rows[0] if rows else None


def list_plans(store: Store, *, active_only: bool) -> list[dict[str, Any]]:
    sql = "SELECT * FROM billing_plans"
    if active_only:
        sql += " WHERE status = 'active'"
    sql += " ORDER BY monthly_amount_minor, code"
    return [plan_payload(row) for row in store.query(sql)]


def create_plan(
    store: Store,
    admin: CurrentUser,
    body: CreateBillingPlanRequest,
    *,
    ip: str | None,
) -> dict[str, Any]:
    conflict = store.query(
        "SELECT id FROM billing_plans WHERE code = ?"
        " OR (? IS NOT NULL AND stripe_price_id = ?)",
        (body.code, body.stripe_price_id, body.stripe_price_id),
    )
    if conflict:
        raise BillingPlanConflict()
    plan_id = str(uuid.uuid4())
    now = _utcnow()
    store.transaction(
        [
            (
                (
                    "INSERT INTO billing_plans"
                    " (id, code, name, status, currency, monthly_amount_minor,"
                    " included_seconds, overage_amount_micros_per_second,"
                    " stripe_price_id, stripe_meter_event_name, entitlements,"
                    " created_at, updated_at)"
                    " VALUES (?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                ),
                (
                    plan_id,
                    body.code,
                    body.name,
                    body.currency,
                    body.monthly_amount_minor,
                    body.included_seconds,
                    body.overage_amount_micros_per_second,
                    body.stripe_price_id,
                    body.stripe_meter_event_name,
                    json.dumps(body.entitlements, separators=(",", ":"), sort_keys=True),
                    now,
                    now,
                ),
            ),
            (
                (
                    "INSERT INTO audit_logs"
                    " (actor_user_id, action, target_type, target_id, metadata, ip,"
                    " created_at) VALUES (?, ?, ?, ?, ?, ?, ?)"
                ),
                (
                    admin.id,
                    AuditAction.BILLING_PLAN_CREATED.value,
                    "billing_plan",
                    plan_id,
                    json.dumps({"code": body.code}),
                    ip,
                    now,
                ),
            ),
        ]
    )
    row = _plan_by_id(store, plan_id)
    if row is None:
        raise RuntimeError("billing plan could not be reloaded")
    return plan_payload(row)


def update_plan(
    store: Store,
    admin: CurrentUser,
    plan_id: str,
    body: UpdateBillingPlanRequest,
    *,
    ip: str | None,
) -> dict[str, Any]:
    existing = _plan_by_id(store, plan_id)
    if existing is None:
        raise BillingPlanNotFound()
    values = body.model_dump(exclude_unset=True)
    if not values:
        return plan_payload(existing)
    if body.stripe_price_id:
        conflict = store.query(
            "SELECT id FROM billing_plans WHERE stripe_price_id = ? AND id <> ?",
            (body.stripe_price_id, plan_id),
        )
        if conflict:
            raise BillingPlanConflict()
    if "entitlements" in values:
        values["entitlements"] = json.dumps(
            values["entitlements"], separators=(",", ":"), sort_keys=True
        )
    allowed = {
        "name",
        "status",
        "monthly_amount_minor",
        "included_seconds",
        "overage_amount_micros_per_second",
        "stripe_price_id",
        "stripe_meter_event_name",
        "entitlements",
    }
    assignments = [f"{key} = ?" for key in values if key in allowed]
    params = [values[key] for key in values if key in allowed]
    if not assignments:
        return plan_payload(existing)
    now = _utcnow()
    assignments.append("updated_at = ?")
    params.extend([now, plan_id])
    store.transaction(
        [
            (
                f"UPDATE billing_plans SET {', '.join(assignments)} WHERE id = ?",
                tuple(params),
            ),
            (
                (
                    "INSERT INTO audit_logs"
                    " (actor_user_id, action, target_type, target_id, metadata, ip,"
                    " created_at) VALUES (?, ?, ?, ?, ?, ?, ?)"
                ),
                (
                    admin.id,
                    AuditAction.BILLING_PLAN_UPDATED.value,
                    "billing_plan",
                    plan_id,
                    json.dumps({"fields": sorted(values)}),
                    ip,
                    now,
                ),
            ),
        ]
    )
    row = _plan_by_id(store, plan_id)
    if row is None:
        raise RuntimeError("billing plan could not be reloaded")
    return plan_payload(row)


def _subscription_row(store: Store, organization_id: str) -> dict[str, Any] | None:
    rows = store.query(
        "SELECT subscriptions.*, billing_plans.code AS plan_code,"
        " billing_plans.name AS plan_name, billing_plans.status AS plan_status,"
        " billing_plans.currency AS plan_currency,"
        " billing_plans.monthly_amount_minor, billing_plans.included_seconds,"
        " billing_plans.overage_amount_micros_per_second,"
        " billing_plans.stripe_price_id, billing_plans.stripe_meter_event_name,"
        " billing_plans.entitlements AS plan_entitlements,"
        " billing_plans.created_at AS plan_created_at,"
        " billing_plans.updated_at AS plan_updated_at"
        " FROM subscriptions JOIN billing_plans"
        " ON billing_plans.id = subscriptions.billing_plan_id"
        " WHERE subscriptions.organization_id = ?",
        (organization_id,),
    )
    return rows[0] if rows else None


def subscription_payload(row: dict[str, Any]) -> dict[str, Any]:
    plan = {
        "id": str(row["billing_plan_id"]),
        "code": str(row["plan_code"]),
        "name": str(row["plan_name"]),
        "status": str(row["plan_status"]),
        "currency": str(row["plan_currency"]),
        "monthly_amount_minor": int(row["monthly_amount_minor"]),
        "included_seconds": int(row["included_seconds"]),
        "overage_amount_micros_per_second": int(
            row["overage_amount_micros_per_second"]
        ),
        "stripe_price_id": row.get("stripe_price_id"),
        "stripe_meter_event_name": row.get("stripe_meter_event_name"),
        "entitlements": _json(row.get("plan_entitlements")),
        "created_at": row.get("plan_created_at"),
        "updated_at": row.get("plan_updated_at"),
    }
    return {
        "id": str(row["id"]),
        "organization_id": str(row["organization_id"]),
        "plan": plan,
        "provider": str(row["provider"]),
        "status": str(row["status"]),
        "current_period_start": row.get("current_period_start"),
        "current_period_end": row.get("current_period_end"),
        "trial_end": row.get("trial_end"),
        "cancel_at_period_end": bool(row.get("cancel_at_period_end")),
        "last_invoice_status": row.get("last_invoice_status"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


def _calendar_period(now: datetime) -> tuple[datetime, datetime]:
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    days = monthrange(start.year, start.month)[1]
    end = start.replace(day=days, hour=23, minute=59, second=59)
    return start, end


def billing_overview(store: Store, organization_id: str) -> dict[str, Any]:
    now = _utcnow()
    subscription = _subscription_row(store, organization_id)
    period_start, period_end = _calendar_period(now)
    if subscription and subscription.get("current_period_start"):
        period_start = subscription["current_period_start"]
    if subscription and subscription.get("current_period_end"):
        period_end = subscription["current_period_end"]
    totals = store.query(
        "SELECT event_type, unit, SUM(quantity) AS quantity,"
        " SUM(provider_cost_micros) AS provider_cost_micros,"
        " SUM(customer_charge_micros) AS customer_charge_micros"
        " FROM usage_events WHERE organization_id = ?"
        " AND occurred_at >= ? AND occurred_at <= ?"
        " GROUP BY event_type, unit ORDER BY event_type, unit",
        (organization_id, period_start, period_end),
    )
    usage = [
        {
            "event_type": str(row["event_type"]),
            "unit": str(row["unit"]),
            "quantity": int(row["quantity"] or 0),
            "provider_cost_micros": int(row["provider_cost_micros"] or 0),
            "customer_charge_micros": int(row["customer_charge_micros"] or 0),
        }
        for row in totals
    ]
    return {
        "organization_id": organization_id,
        "subscription": subscription_payload(subscription) if subscription else None,
        "period_start": period_start,
        "period_end": period_end,
        "usage": usage,
        "provider_cost_micros": sum(row["provider_cost_micros"] for row in usage),
        "customer_charge_micros": sum(
            row["customer_charge_micros"] for row in usage
        ),
    }


def create_checkout(
    store: Store,
    provider: StripeBillingService,
    context: OrgContext,
    body: CheckoutSessionRequest,
    *,
    ip: str | None,
) -> dict[str, Any]:
    plan = _plan_by_code(store, body.plan_code, active_only=True)
    if plan is None or not plan.get("stripe_price_id"):
        raise BillingPlanNotFound()
    existing = _subscription_row(store, context.organization_id)
    if existing and existing["status"] in {
        "trialing",
        "active",
        "past_due",
        "paused",
        "unpaid",
    }:
        raise ActiveSubscriptionConflict()
    if not provider.secret_key:
        raise BillingProviderNotConfigured()
    try:
        hosted = provider.create_checkout_session(
            organization_id=context.organization_id,
            plan_id=str(plan["id"]),
            price_id=str(plan["stripe_price_id"]),
            customer_email=context.user.email,
            customer_id=(
                str(existing["provider_customer_id"])
                if existing and existing.get("provider_customer_id")
                else None
            ),
            success_url=str(body.success_url),
            cancel_url=str(body.cancel_url),
            idempotency_key=body.idempotency_key,
        )
    except StripeBillingError as exc:
        raise BillingProviderUnavailable() from exc
    now = _utcnow()
    subscription_id = str(existing["id"]) if existing else str(uuid.uuid4())
    store.transaction(
        [
            (
                (
                    "INSERT INTO subscriptions"
                    " (id, organization_id, billing_plan_id, provider,"
                    " provider_customer_id, status, cancel_at_period_end,"
                    " created_at, updated_at) VALUES (?, ?, ?, 'stripe', ?,"
                    " 'checkout_pending', ?, ?, ?)"
                    " ON CONFLICT (organization_id) DO UPDATE SET"
                    " billing_plan_id = excluded.billing_plan_id,"
                    " status = 'checkout_pending', updated_at = excluded.updated_at"
                ),
                (
                    subscription_id,
                    context.organization_id,
                    plan["id"],
                    existing.get("provider_customer_id") if existing else None,
                    False,
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
                    context.organization_id,
                    context.user.id,
                    AuditAction.SUBSCRIPTION_CHECKOUT_STARTED.value,
                    "subscription",
                    subscription_id,
                    json.dumps(
                        {"plan_code": body.plan_code, "checkout_session_id": hosted.id}
                    ),
                    ip,
                    now,
                ),
            ),
        ]
    )
    return {
        "id": hosted.id,
        "url": hosted.url,
        "expires_at": (
            datetime.fromtimestamp(hosted.expires_at, UTC)
            if hosted.expires_at
            else None
        ),
    }


def create_portal(
    store: Store,
    provider: StripeBillingService,
    context: OrgContext,
    body: PortalSessionRequest,
) -> dict[str, Any]:
    subscription = _subscription_row(store, context.organization_id)
    if subscription is None or not subscription.get("provider_customer_id"):
        raise BillingSubscriptionNotFound()
    if not provider.secret_key:
        raise BillingProviderNotConfigured()
    try:
        hosted = provider.create_portal_session(
            customer_id=str(subscription["provider_customer_id"]),
            return_url=str(body.return_url),
        )
    except StripeBillingError as exc:
        raise BillingProviderUnavailable() from exc
    return {"id": hosted.id, "url": hosted.url, "expires_at": None}


def usage_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "organization_id": str(row["organization_id"]),
        "call_id": row.get("call_id"),
        "event_type": str(row["event_type"]),
        "quantity": int(row["quantity"]),
        "unit": str(row["unit"]),
        "provider_cost_micros": int(row["provider_cost_micros"]),
        "customer_charge_micros": int(row["customer_charge_micros"]),
        "currency": str(row["currency"]),
        "source": str(row["source"]),
        "idempotency_key": str(row["idempotency_key"]),
        "provider_reference": row.get("provider_reference"),
        "reversal_of_event_id": row.get("reversal_of_event_id"),
        "metadata": _json(row.get("metadata")),
        "occurred_at": row["occurred_at"],
        "recorded_at": row.get("recorded_at"),
    }


def usage_page(
    store: Store,
    organization_id: str,
    limit: int,
    before_occurred_at: str | None,
    before_id: str | None,
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM usage_events WHERE organization_id = ?"
    params: list[Any] = [organization_id]
    if before_occurred_at is not None and before_id is not None:
        before: Any = before_occurred_at
        if store.dialect == "postgres":
            before = datetime.fromisoformat(before_occurred_at)
        sql += " AND (occurred_at < ? OR (occurred_at = ? AND id < ?))"
        params.extend([before, before, before_id])
    sql += " ORDER BY occurred_at DESC, id DESC LIMIT ?"
    params.append(limit)
    return [usage_payload(row) for row in store.query(sql, tuple(params))]


def record_usage(
    store: Store,
    admin: CurrentUser,
    body: RecordUsageEventRequest,
    *,
    ip: str | None,
) -> dict[str, Any]:
    if store.organization(body.organization_id) is None:
        raise UsageEventNotFound()
    existing = store.query(
        "SELECT * FROM usage_events WHERE organization_id = ? AND source = ?"
        " AND idempotency_key = ?",
        (body.organization_id, body.source, body.idempotency_key),
    )
    metadata_json = json.dumps(
        body.metadata, separators=(",", ":"), sort_keys=True
    )
    if existing:
        row = existing[0]
        same = (
            row["event_type"] == body.event_type
            and int(row["quantity"]) == body.quantity
            and row["unit"] == body.unit
            and int(row["provider_cost_micros"]) == body.provider_cost_micros
            and int(row["customer_charge_micros"]) == body.customer_charge_micros
            and (row.get("metadata") or "{}") == metadata_json
        )
        if not same:
            raise BillingIdempotencyConflict()
        return usage_payload(row)
    if body.call_id:
        call = store.query(
            "SELECT 1 FROM calls WHERE organization_id = ? AND call_id = ?",
            (body.organization_id, body.call_id),
        )
        if not call:
            raise UsageEventNotFound()
    if body.reversal_of_event_id:
        original = store.query(
            "SELECT * FROM usage_events WHERE organization_id = ? AND id = ?",
            (body.organization_id, body.reversal_of_event_id),
        )
        if not original:
            raise UsageEventNotFound()
        source = original[0]
        valid_reversal = (
            body.event_type == source["event_type"]
            and body.unit == source["unit"]
            and body.quantity == -int(source["quantity"])
            and body.provider_cost_micros == -int(source["provider_cost_micros"])
            and body.customer_charge_micros
            == -int(source["customer_charge_micros"])
        )
        if not valid_reversal:
            raise BillingIdempotencyConflict()
    event = usage_event(
        organization_id=body.organization_id,
        call_id=body.call_id,
        event_type=body.event_type,
        quantity=body.quantity,
        unit=body.unit,
        provider_cost_micros=body.provider_cost_micros,
        customer_charge_micros=body.customer_charge_micros,
        currency=body.currency,
        source=body.source,
        idempotency_key=body.idempotency_key,
        provider_reference=body.provider_reference,
        reversal_of_event_id=body.reversal_of_event_id,
        metadata=body.metadata,
        occurred_at=body.occurred_at,
    )
    now = _utcnow()
    store.transaction(
        [
            insert_statement(event),
            (
                (
                    "INSERT INTO audit_logs"
                    " (organization_id, actor_user_id, action, target_type, target_id,"
                    " metadata, ip, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
                ),
                (
                    body.organization_id,
                    admin.id,
                    AuditAction.USAGE_EVENT_RECORDED.value,
                    "usage_event",
                    event["id"],
                    json.dumps(
                        {
                            "event_type": body.event_type,
                            "reversal_of_event_id": body.reversal_of_event_id,
                        }
                    ),
                    ip,
                    now,
                ),
            ),
        ]
    )
    rows = store.query("SELECT * FROM usage_events WHERE id = ?", (event["id"],))
    if not rows:
        raise RuntimeError("usage event could not be reloaded")
    return usage_payload(rows[0])


def export_usage(
    store: Store,
    provider: StripeBillingService,
    *,
    limit: int,
) -> dict[str, int]:
    if not provider.secret_key:
        raise BillingProviderNotConfigured()
    rows = store.query(
        "SELECT usage_events.id, usage_events.quantity, usage_events.occurred_at,"
        " subscriptions.provider_customer_id,"
        " billing_plans.stripe_meter_event_name, usage_exports.status"
        " FROM usage_events"
        " JOIN subscriptions"
        " ON subscriptions.organization_id = usage_events.organization_id"
        " JOIN billing_plans ON billing_plans.id = subscriptions.billing_plan_id"
        " LEFT JOIN usage_exports ON usage_exports.usage_event_id = usage_events.id"
        " AND usage_exports.provider = 'stripe'"
        " WHERE usage_events.event_type = 'twilio.call.duration'"
        " AND usage_events.quantity > 0"
        " AND subscriptions.status IN ('trialing', 'active')"
        " AND subscriptions.provider_customer_id IS NOT NULL"
        " AND billing_plans.stripe_meter_event_name IS NOT NULL"
        " AND (usage_exports.status IS NULL OR usage_exports.status <> 'sent')"
        " ORDER BY usage_events.occurred_at, usage_events.id LIMIT ?",
        (limit,),
    )
    sent = failed = 0
    for row in rows:
        identifier = str(row["id"])
        occurred = row["occurred_at"]
        if isinstance(occurred, str):
            occurred = datetime.fromisoformat(occurred)
        try:
            provider.send_meter_event(
                event_name=str(row["stripe_meter_event_name"]),
                customer_id=str(row["provider_customer_id"]),
                quantity=int(row["quantity"]),
                identifier=identifier,
                timestamp=int(occurred.timestamp()),
            )
        except StripeBillingError as exc:
            failed += 1
            status = "failed"
            error = exc.message
            sent_at = None
        else:
            sent += 1
            status = "sent"
            error = None
            sent_at = _utcnow()
        now = _utcnow()
        store.execute(
            "INSERT INTO usage_exports"
            " (id, usage_event_id, provider, provider_event_identifier, status,"
            " attempts, last_error, created_at, updated_at, sent_at)"
            " VALUES (?, ?, 'stripe', ?, ?, 1, ?, ?, ?, ?)"
            " ON CONFLICT (usage_event_id, provider) DO UPDATE SET"
            " status = excluded.status, attempts = usage_exports.attempts + 1,"
            " last_error = excluded.last_error, updated_at = excluded.updated_at,"
            " sent_at = excluded.sent_at",
            (
                str(uuid.uuid4()),
                row["id"],
                identifier,
                status,
                error,
                now,
                now,
                sent_at,
            ),
        )
    remaining = store.query(
        "SELECT COUNT(*) AS count FROM usage_events"
        " JOIN subscriptions ON subscriptions.organization_id ="
        " usage_events.organization_id"
        " JOIN billing_plans ON billing_plans.id = subscriptions.billing_plan_id"
        " LEFT JOIN usage_exports ON usage_exports.usage_event_id = usage_events.id"
        " AND usage_exports.provider = 'stripe'"
        " WHERE usage_events.event_type = 'twilio.call.duration'"
        " AND usage_events.quantity > 0"
        " AND subscriptions.status IN ('trialing', 'active')"
        " AND subscriptions.provider_customer_id IS NOT NULL"
        " AND billing_plans.stripe_meter_event_name IS NOT NULL"
        " AND (usage_exports.status IS NULL OR usage_exports.status <> 'sent')"
    )[0]["count"]
    return {"sent": sent, "failed": failed, "remaining": int(remaining)}
