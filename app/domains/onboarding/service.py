"""Post-signup onboarding: persist the business profile, then — once the owner's
email is verified and the profile is complete — provision the phone number
(billing off) or start the signup checkout (billing on).

FastAPI-free so the worker sweep and tests can call it directly.
"""

from __future__ import annotations

import logging
from typing import Any

from app.domains.telephony.provider import (
    TelephonyProviderError,
    TwilioProvisioningService,
)
from app.settings import Settings
from app.store import Store

from .constants import ACTIVATABLE_LIFECYCLES
from .exceptions import (
    BusinessProfileIncomplete,
    EmailNotVerified,
    NumberProvisioningFailed,
)

logger = logging.getLogger("callagent.onboarding")

# Lifecycle states an organization can be moved *out of* on the way to a live
# number. Mirrors ``ACTIVATABLE_LIFECYCLES``.
_PRE_NUMBER_LIFECYCLES = ("registered", "profile_pending", "eligible", "provisioning")

_PROFILE_FIELDS = (
    "legal_name",
    "address_line1",
    "address_line2",
    "city",
    "region",
    "postal_code",
    "country",
    "contact_email",
    "contact_phone",
    "business_name",
    "timezone",
    "industry",
    "what_you_do",
)


def billing_active(settings: Settings) -> bool:
    return bool(settings.billing_enabled and settings.stripe_price_id)


def _owner_email_verified(store: Store, organization_id: str) -> bool:
    """True when at least one owner of the organization has a verified email.

    Onboarding is always driven by the signup owner, so this is effectively
    "has the owner confirmed their address".
    """
    rows = store.query(
        "SELECT 1 FROM memberships m JOIN users u ON u.id = m.user_id"
        " WHERE m.organization_id = ? AND m.role = 'owner'"
        " AND u.email_verified_at IS NOT NULL LIMIT 1",
        (organization_id,),
    )
    return bool(rows)


def _profile_complete(intake: dict[str, Any] | None) -> bool:
    return bool(intake and intake.get("completed_at"))


def _has_live_subscription(store: Store, organization_id: str) -> bool:
    rows = store.query(
        "SELECT 1 FROM subscriptions WHERE organization_id = ?"
        " AND status IN ('active', 'trialing', 'past_due') LIMIT 1",
        (organization_id,),
    )
    return bool(rows)


def _profile_view(intake: dict[str, Any] | None) -> dict[str, Any] | None:
    if intake is None:
        return None
    view = {name: intake.get(name) for name in _PROFILE_FIELDS}
    view["completed"] = bool(intake.get("completed_at"))
    return view


def onboarding_state(
    store: Store,
    settings: Settings,
    organization_id: str,
    *,
    checkout_url: str | None = None,
) -> dict[str, Any]:
    """Read model: where this organization is between signup and a live number."""
    organization = store.organization(organization_id)
    lifecycle = str((organization or {}).get("lifecycle") or "active")
    intake = store.organization_intake(organization_id)
    email_verified = _owner_email_verified(store, organization_id)
    profile_complete = _profile_complete(intake)
    phone_number = store.active_phone_number(organization_id)

    reasons: list[str] = []
    if settings.require_email_verification and not email_verified:
        reasons.append("email_not_verified")
    if not profile_complete:
        reasons.append("business_profile_incomplete")
    if (
        phone_number is None
        and not reasons
        and billing_active(settings)
        and not _has_live_subscription(store, organization_id)
    ):
        reasons.append("awaiting_payment")

    return {
        "lifecycle": lifecycle,
        "email_verified": email_verified,
        "profile_complete": profile_complete,
        "number_provisioned": phone_number is not None,
        "phone_number": phone_number,
        "checkout_url": checkout_url,
        "blocking_reasons": reasons,
        "business_profile": _profile_view(intake),
    }


def _require_email_verified(
    store: Store, settings: Settings, organization_id: str
) -> None:
    if settings.require_email_verification and not _owner_email_verified(
        store, organization_id
    ):
        raise EmailNotVerified()


def _require_gates_open(store: Store, settings: Settings, organization_id: str) -> dict:
    """Raise the right 4xx unless the org is ready to be handed a number."""
    _require_email_verified(store, settings, organization_id)
    intake = store.organization_intake(organization_id)
    if not _profile_complete(intake):
        raise BusinessProfileIncomplete()
    return intake


def save_business_profile(
    store: Store,
    settings: Settings,
    organization_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Persist the intake as complete. Every field is validated upstream by the
    request schema, so reaching here means the profile is finished.

    The email gate is enforced here too so a rejected request never leaves a
    completed profile behind.
    """
    _require_email_verified(store, settings, organization_id)
    fields = {name: payload.get(name) for name in _PROFILE_FIELDS}
    return store.upsert_organization_intake(organization_id, fields, completed=True)


def activate(
    store: Store,
    settings: Settings,
    provider: TwilioProvisioningService | None,
    organization_id: str,
    *,
    owner_email: str,
    base_url: str | None,
    stripe_provider: Any | None = None,
) -> dict[str, Any]:
    """Move a profile-complete organization towards a live number.

    Billing off: provision the number now and go ``active``.
    Billing on: create the subscription, go ``eligible``, and return a
    ``checkout_url``; the number is provisioned once payment lands (webhook +
    provisioning sweep). ``stripe_provider`` is injected by the router so the
    checkout call is testable; the worker leaves it ``None``.

    Idempotent: an organization that already has a number is returned as-is.
    """
    if store.active_phone_number(organization_id) is not None:
        return onboarding_state(store, settings, organization_id)

    intake = _require_gates_open(store, settings, organization_id)
    organization = store.organization(organization_id)
    if organization is None:
        raise BusinessProfileIncomplete()
    lifecycle = str(organization.get("lifecycle") or "active")
    if lifecycle not in ACTIVATABLE_LIFECYCLES:
        # suspended / closed / already active without a number falling through.
        return onboarding_state(store, settings, organization_id)

    if billing_active(settings):
        return _start_checkout(
            store,
            settings,
            organization_id,
            owner_email=owner_email,
            base_url=base_url,
            stripe_provider=stripe_provider,
        )
    return _provision_now(store, settings, provider, organization_id, intake)


def _provision_now(
    store: Store,
    settings: Settings,
    provider: TwilioProvisioningService | None,
    organization_id: str,
    intake: dict[str, Any],
) -> dict[str, Any]:
    from app.domains.telephony.service import provision_organization_number

    try:
        provision_organization_number(
            store,
            provider,
            organization_id=organization_id,
            default_profile_template=settings.businesses_dir / "_default.yaml",
            default_timezone=settings.default_timezone,
            country=settings.number_pool_country,
            number_type=settings.number_pool_number_type,
            sms_enabled=settings.number_pool_sms_enabled,
            bundle_sid=settings.number_pool_bundle_sid or None,
            address_sid=settings.number_pool_address_sid or None,
            intake=intake,
        )
    except TelephonyProviderError as exc:
        logger.warning(
            "number provisioning failed for %s: %s", organization_id, exc.code
        )
        raise NumberProvisioningFailed() from exc

    store.advance_organization_lifecycle(
        organization_id, "active", _PRE_NUMBER_LIFECYCLES
    )
    return onboarding_state(store, settings, organization_id)


def _start_checkout(
    store: Store,
    settings: Settings,
    organization_id: str,
    *,
    owner_email: str,
    base_url: str | None,
    stripe_provider: Any | None = None,
) -> dict[str, Any]:
    from app.domains.billing.provider import StripeBillingError, StripeBillingService
    from app.domains.billing.services.subscriptions import (
        create_incomplete_subscription,
        start_signup_checkout,
    )

    plans = store.query(
        "SELECT id FROM billing_plans WHERE code = ? AND status = 'active'",
        (settings.default_billing_plan_code,),
    )
    if not plans:
        # BILLING_ENABLED with no seeded plan is a broken deploy, not a live
        # state — fall back to provisioning now rather than stranding the owner.
        logger.warning(
            "no active '%s' plan; provisioning %s without a subscription",
            settings.default_billing_plan_code,
            organization_id,
        )
        intake = store.organization_intake(organization_id) or {}
        return _provision_now(store, settings, None, organization_id, intake)

    create_incomplete_subscription(store, organization_id, str(plans[0]["id"]))
    store.advance_organization_lifecycle(
        organization_id, "eligible", ("registered", "profile_pending", "provisioning")
    )

    checkout_url: str | None = None
    provider = stripe_provider or StripeBillingService(
        settings.stripe_secret_key, settings.stripe_webhook_secret
    )
    try:
        hosted = start_signup_checkout(
            store,
            provider,
            settings,
            organization_id=organization_id,
            user_email=owner_email,
            base_url=base_url,
        )
        checkout_url = hosted.url
    except (StripeBillingError, RuntimeError):
        logger.warning(
            "signup checkout could not be created for %s; the owner can retry",
            organization_id,
        )
    return onboarding_state(
        store, settings, organization_id, checkout_url=checkout_url
    )


def provision_after_payment(
    store: Store,
    provider: TwilioProvisioningService | None,
    settings: Settings,
    organization_id: str,
) -> bool:
    """Best-effort: give a paid, profile-complete organization its number.

    Called from the Stripe webhook path and the worker sweep. Never raises —
    a transient provider failure just leaves the org for the next sweep.
    Returns whether a number was provisioned this call.
    """
    if store.active_phone_number(organization_id) is not None:
        return False
    intake = store.organization_intake(organization_id)
    if not _profile_complete(intake):
        return False
    if billing_active(settings) and not _has_live_subscription(store, organization_id):
        return False
    try:
        from app.domains.telephony.service import provision_organization_number

        provision_organization_number(
            store,
            provider,
            organization_id=organization_id,
            default_profile_template=settings.businesses_dir / "_default.yaml",
            default_timezone=settings.default_timezone,
            country=settings.number_pool_country,
            number_type=settings.number_pool_number_type,
            sms_enabled=settings.number_pool_sms_enabled,
            bundle_sid=settings.number_pool_bundle_sid or None,
            address_sid=settings.number_pool_address_sid or None,
            intake=intake,
        )
    except TelephonyProviderError as exc:
        logger.warning(
            "post-payment provisioning deferred for %s: %s", organization_id, exc.code
        )
        return False
    store.advance_organization_lifecycle(
        organization_id, "active", _PRE_NUMBER_LIFECYCLES
    )
    logger.info("provisioned number for paid organization %s", organization_id)
    return True


def sweep_awaiting_number(
    store: Store,
    provider: TwilioProvisioningService,
    settings: Settings,
    *,
    limit: int,
) -> int:
    """Provision every profile-complete organization still waiting on a number.

    The backstop for billing-on signups (the webhook's inline attempt may have
    hit a Twilio hiccup) and for any org left ``provisioning``. Stops on the
    first provider failure so one outage does not burn the whole batch.
    """
    provisioned = 0
    for row in store.organizations_awaiting_number(
        limit, require_paid_subscription=billing_active(settings)
    ):
        organization_id = str(row["id"])
        if provision_after_payment(store, provider, settings, organization_id):
            provisioned += 1
            continue
        if store.active_phone_number(organization_id) is None:
            # Nothing was provisioned and the org still has no number: the
            # provider (or a missing sub) is the blocker. Stop rather than
            # repeating a paid API call for every remaining row this tick.
            break
    return provisioned
