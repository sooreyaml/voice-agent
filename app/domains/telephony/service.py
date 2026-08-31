"""Persistence and business rules around billable Twilio operations."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

from app.domains.audit.models import AuditAction
from app.domains.auth.dependencies import CurrentUser
from app.domains.auth.exceptions import APIError
from app.domains.billing.services.spend import require_spend_available
from app.domains.billing.usage import insert_statement, usage_event
from app.domains.businesses.normalization import normalize_e164
from app.domains.businesses.repository import BusinessRepository
from app.domains.onboarding.exceptions import OnboardingNotFound
from app.store import Store

from .exceptions import (
    IdempotencyConflict,
    NumberNotSelected,
    PhoneNumberConflict,
    ProfileRequired,
    ProviderNotConfigured,
    ProviderUnavailable,
    ProvisioningNotFound,
    ProvisioningNotReady,
    TestCallNotFound,
)
from .provider import TelephonyProviderError, TwilioProvisioningService
from .schemas import ProvisionPhoneNumberRequest, VerifyTestCallRequest


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _phone_id(e164: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"call-agent:phone:{e164}"))


def _onboarding(store: Store, organization_id: str) -> dict[str, Any]:
    row = store.onboarding_record(organization_id)
    if row is None:
        raise OnboardingNotFound()
    return row


def _profile(repository: BusinessRepository, organization_id: str) -> dict[str, Any]:
    profile = repository.profile_state(organization_id)
    if profile is None or profile.get("configuration") is None:
        raise ProfileRequired()
    return profile


def _provider_error(exc: TelephonyProviderError) -> APIError:
    if exc.code == "provider_not_configured":
        return ProviderNotConfigured()
    return ProviderUnavailable("Twilio could not complete the request. Retry shortly.")


def regulatory_requirements(
    store: Store,
    provider: TwilioProvisioningService,
    organization_id: str,
    country_code: str,
    number_type: str,
    end_user_type: str,
) -> list[dict[str, Any]]:
    _onboarding(store, organization_id)
    try:
        return provider.regulatory_requirements(
            country_code, number_type, end_user_type
        )
    except TelephonyProviderError as exc:
        raise _provider_error(exc) from exc


def available_numbers(
    store: Store,
    provider: TwilioProvisioningService,
    organization_id: str,
    country_code: str,
    number_type: str,
    *,
    area_code: int | None,
    contains: str | None,
    exclude_address_required: bool,
    limit: int,
) -> list[dict[str, Any]]:
    _onboarding(store, organization_id)
    try:
        return provider.search_available_numbers(
            country_code,
            number_type,
            area_code=area_code,
            contains=contains,
            exclude_address_required=exclude_address_required,
            limit=limit,
        )
    except ValueError as exc:
        raise APIError(
            str(exc),
            code="validation_failed",
            status_code=422,
            field_errors={"area_code": str(exc)},
        ) from exc
    except TelephonyProviderError as exc:
        raise _provider_error(exc) from exc


def provisioning_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "organization_id": str(row["organization_id"]),
        "business_profile_id": str(row["business_profile_id"]),
        "idempotency_key": str(row["idempotency_key"]),
        "country_code": str(row["country_code"]),
        "number_type": str(row["number_type"]),
        "requested_phone_number": str(row["requested_phone_number"]),
        "status": str(row["status"]),
        "attempts": int(row["attempts"]),
        "provider_phone_number_sid": row.get("provider_phone_number_sid"),
        "provider_trunk_sid": row.get("provider_trunk_sid"),
        "phone_number": row.get("phone_number_e164"),
        "last_error_code": row.get("last_error_code"),
        "last_error_message": row.get("last_error_message"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "completed_at": row.get("completed_at"),
        "tested_at": row.get("tested_at"),
    }


def latest_provisioning(store: Store, organization_id: str) -> dict[str, Any]:
    _onboarding(store, organization_id)
    row = store.latest_provisioning(organization_id)
    if row is None:
        raise ProvisioningNotFound()
    return provisioning_payload(row)


def provision_number(
    store: Store,
    repository: BusinessRepository,
    provider: TwilioProvisioningService,
    admin: CurrentUser,
    organization_id: str,
    body: ProvisionPhoneNumberRequest,
    *,
    ip: str | None,
) -> dict[str, Any]:
    _onboarding(store, organization_id)
    require_spend_available(store, organization_id)
    profile = _profile(repository, organization_id)
    number = normalize_e164(body.phone_number)
    selected_numbers = {
        normalize_e164(value)
        for value in (
            (profile["configuration"].get("business") or {}).get("phone_numbers") or []
        )
    }
    if number not in selected_numbers:
        raise NumberNotSelected()

    assigned = store.query(
        "SELECT id, organization_id, business_profile_id FROM phone_numbers"
        " WHERE e164 = ?",
        (number,),
    )
    if assigned and (
        assigned[0]["organization_id"] != organization_id
        or assigned[0]["business_profile_id"] != profile["id"]
    ):
        raise PhoneNumberConflict()

    existing = store.provisioning_by_idempotency_key(
        organization_id, body.idempotency_key
    )
    if existing is not None:
        same_request = (
            existing["business_profile_id"] == profile["id"]
            and existing["requested_phone_number"] == number
            and existing["country_code"] == body.country_code
            and existing["number_type"] == body.number_type
        )
        if not same_request:
            raise IdempotencyConflict()
        if existing["status"] in {"ready", "verified"}:
            return provisioning_payload(existing)
        request_id = str(existing["id"])
        attempts = int(existing["attempts"]) + 1
    else:
        request_id = str(uuid.uuid4())
        attempts = 1

    now = _utcnow()
    metadata = json.dumps(
        {
            "phone_number": number,
            "country_code": body.country_code,
            "number_type": body.number_type,
            "attempt": attempts,
        }
    )
    if existing is None:
        start_statement = (
            (
                "INSERT INTO telephony_provisioning_requests"
                " (id, organization_id, business_profile_id, idempotency_key,"
                " country_code, number_type, requested_phone_number, status,"
                " attempts, created_by_user_id, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, 'provisioning', ?, ?, ?, ?)"
            ),
            (
                request_id,
                organization_id,
                profile["id"],
                body.idempotency_key,
                body.country_code,
                body.number_type,
                number,
                attempts,
                admin.id,
                now,
                now,
            ),
        )
    else:
        start_statement = (
            (
                "UPDATE telephony_provisioning_requests"
                " SET status = 'provisioning', attempts = ?, last_error_code = NULL,"
                " last_error_message = NULL, updated_at = ? WHERE id = ?"
            ),
            (attempts, now, request_id),
        )
    store.transaction(
        [
            start_statement,
            (
                (
                    "INSERT INTO audit_logs"
                    " (organization_id, actor_user_id, action, target_type, target_id,"
                    " metadata, ip, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
                ),
                (
                    organization_id,
                    admin.id,
                    AuditAction.TELEPHONY_PROVISIONING_STARTED.value,
                    "telephony_provisioning_request",
                    request_id,
                    metadata,
                    ip,
                    now,
                ),
            ),
        ]
    )

    try:
        result = provider.provision_number(
            number,
            address_sid=body.address_sid,
            bundle_sid=body.bundle_sid,
            identity_sid=body.identity_sid,
            trunk_domain=body.trunk_domain,
        )
    except TelephonyProviderError as exc:
        failed_at = _utcnow()
        store.transaction(
            [
                (
                    (
                        "UPDATE telephony_provisioning_requests"
                        " SET status = 'failed', last_error_code = ?,"
                        " last_error_message = ?, updated_at = ? WHERE id = ?"
                    ),
                    (exc.code, exc.message, failed_at, request_id),
                ),
                (
                    (
                        "INSERT INTO audit_logs"
                        " (organization_id, actor_user_id, action, target_type,"
                        " target_id, metadata, ip, created_at)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
                    ),
                    (
                        organization_id,
                        admin.id,
                        AuditAction.TELEPHONY_PROVISIONING_FAILED.value,
                        "telephony_provisioning_request",
                        request_id,
                        json.dumps({"code": exc.code, "retryable": exc.retryable}),
                        ip,
                        failed_at,
                    ),
                ),
            ]
        )
        raise _provider_error(exc) from exc

    phone_id = str(assigned[0]["id"]) if assigned else _phone_id(number)
    completed_at = _utcnow()
    if assigned:
        phone_statement = (
            (
                "UPDATE phone_numbers SET provider = 'twilio',"
                " provider_account_sid = ?, provider_number_sid = ?,"
                " provider_trunk_sid = ?, country_code = ?, number_type = ?,"
                " updated_at = ? WHERE organization_id = ? AND id = ?"
            ),
            (
                result.account_sid,
                result.phone_number_sid,
                result.trunk_sid,
                body.country_code,
                body.number_type,
                completed_at,
                organization_id,
                phone_id,
            ),
        )
    else:
        phone_statement = (
            (
                "INSERT INTO phone_numbers"
                " (id, organization_id, business_profile_id, e164, status, provider,"
                " provider_account_sid, provider_number_sid, provider_trunk_sid,"
                " country_code, number_type, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, 'inactive', 'twilio', ?, ?, ?, ?, ?, ?, ?)"
            ),
            (
                phone_id,
                organization_id,
                profile["id"],
                result.phone_number,
                result.account_sid,
                result.phone_number_sid,
                result.trunk_sid,
                body.country_code,
                body.number_type,
                completed_at,
                completed_at,
            ),
        )
    store.transaction(
        [
            phone_statement,
            insert_statement(
                usage_event(
                    organization_id=organization_id,
                    event_type="twilio.phone_number.rental",
                    quantity=1,
                    unit="number_month",
                    source="twilio",
                    idempotency_key=(
                        f"phone-number-rental:{result.phone_number_sid}:initial"
                    ),
                    provider_reference=result.phone_number_sid,
                    occurred_at=completed_at,
                    metadata={"phone_number": result.phone_number},
                )
            ),
            (
                (
                    "UPDATE telephony_provisioning_requests"
                    " SET phone_number_id = ?, status = 'ready',"
                    " provider_phone_number_sid = ?, provider_trunk_sid = ?,"
                    " phone_number_e164 = ?, last_error_code = NULL,"
                    " last_error_message = NULL, updated_at = ?, completed_at = ?"
                    " WHERE id = ?"
                ),
                (
                    phone_id,
                    result.phone_number_sid,
                    result.trunk_sid,
                    result.phone_number,
                    completed_at,
                    completed_at,
                    request_id,
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
                    admin.id,
                    AuditAction.TELEPHONY_PROVISIONING_READY.value,
                    "telephony_provisioning_request",
                    request_id,
                    json.dumps(
                        {
                            "phone_number": result.phone_number,
                            "phone_number_sid": result.phone_number_sid,
                            "trunk_sid": result.trunk_sid,
                        }
                    ),
                    ip,
                    completed_at,
                ),
            ),
        ]
    )
    ready = store.provisioning_by_idempotency_key(organization_id, body.idempotency_key)
    if ready is None:
        raise RuntimeError("provisioning request could not be reloaded")
    return provisioning_payload(ready)


def verify_test_call(
    store: Store,
    admin: CurrentUser,
    organization_id: str,
    body: VerifyTestCallRequest,
    *,
    ip: str | None,
) -> dict[str, Any]:
    onboarding = _onboarding(store, organization_id)
    provisioning = store.latest_provisioning(organization_id)
    if provisioning is None:
        raise ProvisioningNotFound()
    if provisioning["status"] == "verified":
        return provisioning_payload(provisioning)
    if provisioning["status"] != "ready" or not provisioning["completed_at"]:
        raise ProvisioningNotReady()
    published = store.query(
        "SELECT 1 FROM agent_versions"
        " WHERE organization_id = ? AND business_profile_id = ?"
        " AND status = 'published' LIMIT 1",
        (organization_id, provisioning["business_profile_id"]),
    )
    if not published:
        raise ProvisioningNotReady()
    call = store.completed_test_call(
        organization_id,
        str(provisioning["phone_number_e164"]),
        provisioning["completed_at"],
        body.call_id,
    )
    if call is None:
        raise TestCallNotFound()

    now = _utcnow()
    statements: list[tuple[str, tuple[Any, ...]]] = [
        (
            (
                "UPDATE telephony_provisioning_requests"
                " SET status = 'verified', tested_at = ?, updated_at = ?"
                " WHERE id = ? AND status = 'ready'"
            ),
            (now, now, provisioning["id"]),
        ),
        (
            (
                "UPDATE onboarding_records SET status = 'active',"
                " activated_by_user_id = ?, activated_at = COALESCE(activated_at, ?),"
                " updated_at = ? WHERE organization_id = ?"
            ),
            (admin.id, now, now, organization_id),
        ),
        (
            (
                "INSERT INTO audit_logs"
                " (organization_id, actor_user_id, action, target_type, target_id,"
                " metadata, ip, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
            ),
            (
                organization_id,
                admin.id,
                AuditAction.TELEPHONY_TEST_CALL_VERIFIED.value,
                "call",
                call["call_id"],
                json.dumps(
                    {
                        "phone_number": provisioning["phone_number_e164"],
                        "provisioning_request_id": provisioning["id"],
                    }
                ),
                ip,
                now,
            ),
        ),
    ]
    if onboarding["status"] != "active":
        statements.append(
            (
                (
                    "INSERT INTO audit_logs"
                    " (organization_id, actor_user_id, action, target_type, target_id,"
                    " metadata, ip, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
                ),
                (
                    organization_id,
                    admin.id,
                    AuditAction.ONBOARDING_ACTIVATED.value,
                    "onboarding",
                    onboarding["id"],
                    json.dumps({"test_call_id": call["call_id"]}),
                    ip,
                    now,
                ),
            )
        )
    store.transaction(statements)
    verified = store.latest_provisioning(organization_id)
    if verified is None:
        raise RuntimeError("verified provisioning request could not be reloaded")
    return provisioning_payload(verified)
