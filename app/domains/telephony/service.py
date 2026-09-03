"""On-demand phone-number assignment for newly created organizations."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from app.domains.businesses.defaults import build_default_profile
from app.domains.businesses.repository import BusinessRepository
from app.store import Store

from .provider import TelephonyProviderError, TwilioProvisioningService

logger = logging.getLogger(__name__)

SEARCH_CANDIDATE_LIMIT = 10


@dataclass
class PendingProvisioningResult:
    attempted: int = 0
    provisioned: list[tuple[str, str]] = field(default_factory=list)
    failures: list[tuple[str, str]] = field(default_factory=list)


def _buy_and_assign_number(
    store: Store,
    provider: TwilioProvisioningService,
    *,
    organization_id: str,
    country: str,
    number_type: str,
    sms_enabled: bool,
    bundle_sid: str | None,
    address_sid: str | None,
) -> dict:
    try:
        candidates = provider.search_available_numbers(
            country,
            number_type,
            exclude_address_required=not bundle_sid,
            sms_enabled=sms_enabled,
            limit=SEARCH_CANDIDATE_LIMIT,
        )
    except ValueError as exc:
        raise TelephonyProviderError("invalid_number_search", str(exc)) from exc

    if not candidates:
        raise TelephonyProviderError(
            "number_unavailable",
            f"No matching {country.upper()} {number_type} number is available.",
            retryable=True,
        )

    last_error: TelephonyProviderError | None = None
    for candidate in candidates:
        phone_number = str(candidate["phone_number"])
        try:
            result = provider.provision_number(
                phone_number,
                bundle_sid=bundle_sid,
                address_sid=address_sid,
            )
        except TelephonyProviderError as exc:
            last_error = exc
            logger.warning(
                "could not provision signup number %s: %s", phone_number, exc.code
            )
            continue

        inserted = store.add_assigned_pool_number(
            result.phone_number,
            country,
            organization_id=organization_id,
            provider_number_sid=result.phone_number_sid,
            provider_trunk_sid=result.trunk_sid,
        )
        if inserted:
            assigned = store.pool_number_for_org(organization_id)
            assert assigned is not None
            return assigned

        # Another request won a race for this number. Continue through the
        # remaining live candidates rather than leaving this signup pending.
        logger.warning("signup number %s was concurrently assigned", phone_number)

    if last_error is not None:
        raise last_error
    raise TelephonyProviderError(
        "number_assignment_conflict",
        "Available numbers were claimed by another signup.",
        retryable=True,
    )


def provision_organization_number(
    store: Store,
    provider: TwilioProvisioningService | None,
    *,
    organization_id: str,
    default_profile_template: Path,
    default_timezone: str,
    country: str,
    number_type: str,
    sms_enabled: bool,
    bundle_sid: str | None,
    address_sid: str | None,
) -> str:
    """Give an unprovisioned organization a live number and default agent.

    An already-owned recycled number is used first. If none exists, a matching
    number is searched and purchased from Twilio immediately; signup never has
    to wait for a background pool refill.
    """
    organization = store.organization(organization_id)
    if organization is None:
        raise ValueError("organization does not exist")

    repository = BusinessRepository(store)
    overview = repository.agent_overview(organization_id)
    if overview is not None:
        active_numbers = overview["active_phone_numbers"]
        if active_numbers:
            return str(active_numbers[0])
        raise TelephonyProviderError(
            "agent_already_exists",
            "The organization already has an agent without an active number.",
        )

    assigned = store.pool_number_for_org(organization_id)
    if assigned is None:
        assigned = store.claim_pool_number(organization_id, country_code=country)
    if assigned is None:
        if provider is None:
            raise TelephonyProviderError(
                "provider_not_configured",
                "Twilio provisioning is not configured on this deployment.",
            )
        assigned = _buy_and_assign_number(
            store,
            provider,
            organization_id=organization_id,
            country=country,
            number_type=number_type,
            sms_enabled=sms_enabled,
            bundle_sid=bundle_sid,
            address_sid=address_sid,
        )

    phone_number = str(assigned["e164"])
    try:
        profile = build_default_profile(
            template_path=default_profile_template,
            business_name=str(organization["name"]),
            slug=str(organization["slug"]),
            phone_number=phone_number,
            timezone=default_timezone,
        )
        repository.publish(profile, organization_id=organization_id)
    except Exception as exc:
        # Keep the already-purchased number under platform ownership and make it
        # available for a later attempt; do not silently destroy a paid resource.
        store.release_pool_number(phone_number, quarantine_until=None)
        raise TelephonyProviderError(
            "agent_publish_failed",
            "The number was acquired but the default agent could not be published.",
            retryable=True,
        ) from exc

    try:
        store.set_phone_number_provider_metadata(
            organization_id,
            phone_number,
            provider=str(assigned.get("provider") or "twilio"),
            provider_account_sid=(provider.account_sid or None) if provider else None,
            provider_number_sid=assigned.get("provider_number_sid"),
            provider_trunk_sid=assigned.get("provider_trunk_sid"),
            country_code=country,
            number_type=number_type,
        )
    except Exception:
        # Routing is already live. Missing provider metadata must not turn a
        # successful purchase into a false pending state or free the live number.
        logger.exception(
            "could not persist provider metadata for %s (%s)",
            organization_id,
            phone_number,
        )

    return phone_number


def provision_pending_organizations(
    store: Store,
    provider: TwilioProvisioningService,
    *,
    default_profile_template: Path,
    default_timezone: str,
    country: str,
    number_type: str,
    sms_enabled: bool,
    bundle_sid: str | None,
    address_sid: str | None,
    limit: int,
) -> PendingProvisioningResult:
    """Provision older accounts that were created before on-demand signup."""
    result = PendingProvisioningResult()
    for organization in store.organizations_pending_number(limit):
        organization_id = str(organization["id"])
        result.attempted += 1
        try:
            phone_number = provision_organization_number(
                store,
                provider,
                organization_id=organization_id,
                default_profile_template=default_profile_template,
                default_timezone=default_timezone,
                country=country,
                number_type=number_type,
                sms_enabled=sms_enabled,
                bundle_sid=bundle_sid,
                address_sid=address_sid,
            )
        except TelephonyProviderError as exc:
            logger.warning(
                "pending organization %s was not provisioned: %s",
                organization_id,
                exc.code,
            )
            result.failures.append((organization_id, exc.code))
            # A failed search/provider configuration affects the deployment,
            # not this tenant. Avoid repeating the same paid-provider request
            # for every remaining organization in the batch.
            break
        result.provisioned.append((organization_id, phone_number))
    return result
