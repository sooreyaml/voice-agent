"""Subscription lifecycle for instant signup.

Owns the single seeded billing plan and the checkout session created during
signup. Admin plan CRUD is gone; this module is what the signup path calls.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from app.store import Store

from ..provider import HostedSession, StripeBillingService
from .management import _plan_by_code

logger = logging.getLogger(__name__)

DEFAULT_PLAN_NAME = "Starter"
DEFAULT_CURRENCY = "USD"

# Stripe statuses that mean the tenant is paying and the number should stay live.
LIVE_SUBSCRIPTION_STATUSES = {"active", "past_due"}
# Statuses that should stop routing once the dunning grace has elapsed / Stripe
# has given up.
DEAD_SUBSCRIPTION_STATUSES = {"canceled", "unpaid", "incomplete_expired"}


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def ensure_default_plan(store: Store, settings: Any) -> dict[str, Any] | None:
    """Idempotently seed/refresh the one billing plan from env.

    Returns the active plan row, or ``None`` when instant-signup billing is not
    configured. Raises outside development if ``BILLING_ENABLED`` is on but no
    ``STRIPE_PRICE_ID`` is set — that is a broken deploy, not a soft state.
    """
    if not settings.billing_enabled:
        return None
    price_id = settings.stripe_price_id
    if not price_id:
        if settings.environment != "development":
            raise RuntimeError(
                "BILLING_ENABLED is on but STRIPE_PRICE_ID is unset; signup has "
                "no plan to attach a subscription to."
            )
        logger.warning(
            "no STRIPE_PRICE_ID configured; signup will not create subscriptions"
        )
        return None

    code = settings.default_billing_plan_code
    meter = settings.stripe_meter_event_name or None
    now = _utcnow()
    store.execute(
        "INSERT INTO billing_plans"
        " (id, code, name, status, currency, monthly_amount_minor,"
        " included_seconds, overage_amount_micros_per_second, stripe_price_id,"
        " stripe_meter_event_name, entitlements, created_at, updated_at)"
        " VALUES (?, ?, ?, 'active', ?, 0, 0, 0, ?, ?, '{}', ?, ?)"
        " ON CONFLICT (code) DO UPDATE SET"
        " stripe_price_id = excluded.stripe_price_id,"
        " stripe_meter_event_name = excluded.stripe_meter_event_name,"
        " status = 'active', updated_at = excluded.updated_at",
        (
            str(uuid.uuid4()),
            code,
            DEFAULT_PLAN_NAME,
            DEFAULT_CURRENCY,
            price_id,
            meter,
            now,
            now,
        ),
    )
    return _plan_by_code(store, code, active_only=True)


def create_incomplete_subscription(
    store: Store, organization_id: str, plan_id: str
) -> None:
    """Row that exists from the moment of signup so the reaper and the webhook
    have something to update. No provider ids yet.
    """
    now = _utcnow()
    store.execute(
        "INSERT INTO subscriptions"
        " (id, organization_id, billing_plan_id, provider, status,"
        " cancel_at_period_end, created_at, updated_at)"
        " VALUES (?, ?, ?, 'stripe', 'incomplete', ?, ?, ?)"
        " ON CONFLICT (organization_id) DO NOTHING",
        (str(uuid.uuid4()), organization_id, plan_id, False, now, now),
    )


def start_signup_checkout(
    store: Store,
    provider: StripeBillingService,
    settings: Any,
    *,
    organization_id: str,
    user_email: str,
    base_url: str | None = None,
) -> HostedSession:
    """Create the Stripe Checkout Session that captures the card and starts the
    paid subscription. Raises ``StripeBillingError`` if Stripe rejects it; the
    caller keeps the subscription 'incomplete' and lets the reaper clean up.

    ``base_url`` is the frontend to return the user to (the site they signed up
    on); it defaults to the primary ``APP_BASE_URL``.
    """
    plan = _plan_by_code(
        store, settings.default_billing_plan_code, active_only=True
    )
    if plan is None or not plan.get("stripe_price_id"):
        raise RuntimeError("default billing plan is not configured")
    base = (base_url or settings.app_base_url).rstrip("/")
    hosted = provider.create_checkout_session(
        organization_id=organization_id,
        plan_id=str(plan["id"]),
        price_id=str(plan["stripe_price_id"]),
        customer_email=user_email,
        customer_id=None,
        success_url=f"{base}/?checkout=success",
        cancel_url=f"{base}/?checkout=cancelled",
        idempotency_key=f"signup-checkout:{organization_id}",
    )
    now = _utcnow()
    store.execute(
        "UPDATE subscriptions SET status = 'checkout_pending', updated_at = ?"
        " WHERE organization_id = ? AND status = 'incomplete'",
        (now, organization_id),
    )
    return hosted
