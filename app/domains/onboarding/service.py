"""Business rules for platform-admin-led customer onboarding."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

from app.business import BusinessProfile
from app.domains.audit.models import AuditAction
from app.domains.auth.constants import INVITATION_TTL
from app.domains.auth.dependencies import CurrentUser
from app.domains.auth.service import hash_token, new_raw_token, unique_org_slug
from app.domains.businesses.repository import (
    BusinessRepository,
    DraftNotFound,
    PhoneNumberAlreadyAssigned,
)
from app.domains.telephony.exceptions import ProvisioningNotReady
from app.store import Store

from .exceptions import (
    OnboardingNotFound,
    OnboardingProfileMissing,
    PhoneNumberConflict,
    ProfileSlugMismatch,
)
from .schemas import CreateOnboardingRequest, SaveOnboardingProfileRequest


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _row(store: Store, organization_id: str) -> dict[str, Any]:
    row = store.onboarding_record(organization_id)
    if row is None:
        raise OnboardingNotFound()
    return row


def create_onboarding(
    store: Store,
    admin: CurrentUser,
    body: CreateOnboardingRequest,
    *,
    secret: str,
    ip: str | None,
) -> tuple[str, str]:
    """Atomically create the tenant, owner invitation, tracker, and audit row."""
    organization_id = str(uuid.uuid4())
    onboarding_id = str(uuid.uuid4())
    invitation_id = str(uuid.uuid4())
    organization_name = body.organization_name.strip()
    organization_slug = unique_org_slug(store, organization_name)
    owner_email = body.owner_email.strip().lower()
    raw_token = new_raw_token()
    now = _utcnow()
    expires_at = now + INVITATION_TTL
    metadata = json.dumps(
        {
            "organization_name": organization_name,
            "organization_slug": organization_slug,
            "owner_email": owner_email,
            "invitation_id": invitation_id,
        }
    )
    store.transaction(
        [
            (
                (
                    "INSERT INTO organizations"
                    " (id, slug, name, created_at, updated_at) VALUES (?, ?, ?, ?, ?)"
                ),
                (organization_id, organization_slug, organization_name, now, now),
            ),
            (
                (
                    "INSERT INTO invitations"
                    " (id, organization_id, email, role, token_hash, invited_by,"
                    " created_at, expires_at) VALUES (?, ?, ?, 'owner', ?, ?, ?, ?)"
                ),
                (
                    invitation_id,
                    organization_id,
                    owner_email,
                    hash_token(raw_token, secret),
                    admin.id,
                    now,
                    expires_at,
                ),
            ),
            (
                (
                    "INSERT INTO onboarding_records"
                    " (id, organization_id, owner_email, status, created_by_user_id,"
                    " created_at, updated_at)"
                    " VALUES (?, ?, ?, 'in_progress', ?, ?, ?)"
                ),
                (
                    onboarding_id,
                    organization_id,
                    owner_email,
                    admin.id,
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
                    admin.id,
                    AuditAction.ONBOARDING_STARTED.value,
                    "onboarding",
                    onboarding_id,
                    metadata,
                    ip,
                    now,
                ),
            ),
        ]
    )
    return organization_id, raw_token


def start_self_service_onboarding(
    store: Store,
    user: CurrentUser,
    organization_id: str,
    *,
    ip: str | None,
) -> None:
    """Idempotently attach onboarding progress to an existing tenant.

    Signup already creates the organization and owner membership. This operation
    deliberately creates neither a second organization nor an invitation.
    """
    existing = store.onboarding_record(organization_id)
    if existing is not None:
        return
    organization = store.organization(organization_id)
    if organization is None:
        raise OnboardingNotFound()
    onboarding_id = str(uuid.uuid4())
    now = _utcnow()
    store.transaction(
        [
            (
                (
                    "INSERT INTO onboarding_records"
                    " (id, organization_id, owner_email, status, mode,"
                    " created_by_user_id, created_at, updated_at)"
                    " VALUES (?, ?, ?, 'in_progress', 'self_service', ?, ?, ?)"
                ),
                (onboarding_id, organization_id, user.email, user.id, now, now),
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
                    AuditAction.ONBOARDING_STARTED.value,
                    "onboarding",
                    onboarding_id,
                    json.dumps({"mode": "self_service"}),
                    ip,
                    now,
                ),
            ),
        ]
    )


def onboarding_payload(
    store: Store, repository: BusinessRepository, organization_id: str
) -> dict[str, Any]:
    row = _row(store, organization_id)
    profile = repository.profile_state(organization_id)
    has_draft = bool(profile and profile["draft_version"] is not None)
    has_published = bool(profile and profile["published_version"] is not None)
    active_numbers = profile["active_phone_numbers"] if profile else []

    if has_draft and has_published:
        profile_step = "changes_pending"
    elif has_draft:
        profile_step = "draft"
    elif has_published:
        profile_step = "published"
    else:
        profile_step = "not_started"

    configuration = profile.get("configuration") if profile else None
    selected_numbers = (
        (configuration.get("business") or {}).get("phone_numbers") or []
        if configuration
        else []
    )
    provisioning = store.latest_provisioning(organization_id)
    if provisioning and provisioning["status"] == "verified":
        phone_step = "verified"
    elif provisioning and provisioning["status"] == "failed":
        phone_step = "failed"
    elif provisioning and provisioning["status"] == "provisioning":
        phone_step = "provisioning"
    elif provisioning and active_numbers:
        phone_step = "routed"
    elif provisioning and provisioning["status"] == "ready":
        phone_step = "ready"
    elif active_numbers:
        phone_step = "routed"
    elif selected_numbers:
        phone_step = "selected"
    else:
        phone_step = "not_started"
    return {
        "id": str(row["id"]),
        "organization": {
            "id": str(row["organization_id"]),
            "slug": str(row["slug"]),
            "name": str(row["name"]),
        },
        "owner_email": str(row["owner_email"]),
        "status": str(row["status"]),
        "mode": str(row["mode"]),
        "steps": {
            "owner": "accepted" if bool(row["owner_accepted"]) else "invited",
            "business_profile": profile_step,
            "phone_number": phone_step,
            "activation": "active" if row["status"] == "active" else "pending",
        },
        "profile": profile,
        "created_by_user_id": row.get("created_by_user_id"),
        "activated_by_user_id": row.get("activated_by_user_id"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "activated_at": row.get("activated_at"),
    }


def save_profile_draft(
    store: Store,
    repository: BusinessRepository,
    admin: CurrentUser,
    organization_id: str,
    body: SaveOnboardingProfileRequest,
    *,
    ip: str | None,
) -> dict[str, Any]:
    _row(store, organization_id)
    existing = repository.profile_state(organization_id)
    if existing is not None and existing["slug"] != body.slug:
        raise ProfileSlugMismatch()
    profile = BusinessProfile(
        raw=body.configuration.model_dump(mode="python"), slug=body.slug
    )
    try:
        draft = repository.save_draft(organization_id, profile)
    except PhoneNumberAlreadyAssigned as exc:
        raise PhoneNumberConflict(str(exc)) from exc
    store.record_audit(
        AuditAction.PROFILE_DRAFT_SAVED.value,
        organization_id=organization_id,
        actor_user_id=admin.id,
        target_type="agent_version",
        target_id=draft.version_id,
        metadata={"profile_id": draft.profile_id, "version": draft.version_number},
        ip=ip,
    )
    return onboarding_payload(store, repository, organization_id)


def preview_profile(
    store: Store, repository: BusinessRepository, organization_id: str
) -> dict[str, Any]:
    _row(store, organization_id)
    profile = repository.profile_state(organization_id)
    if profile is None or profile["draft_version"] is None:
        raise OnboardingProfileMissing()
    try:
        return repository.draft_preview(organization_id, str(profile["slug"]))
    except DraftNotFound as exc:  # protects against concurrent publication
        raise OnboardingProfileMissing() from exc


def publish_profile(
    store: Store,
    repository: BusinessRepository,
    admin: CurrentUser,
    organization_id: str,
    *,
    ip: str | None,
    require_provisioning: bool = False,
) -> dict[str, Any]:
    record = _row(store, organization_id)
    profile = repository.profile_state(organization_id)
    if profile is None or profile["draft_version"] is None:
        raise OnboardingProfileMissing()
    provisioning = store.latest_provisioning(organization_id)
    if require_provisioning and provisioning is None:
        raise ProvisioningNotReady()
    if provisioning and provisioning["status"] not in {"ready", "verified"}:
        raise ProvisioningNotReady()
    try:
        published = repository.publish_draft(organization_id, str(profile["slug"]))
    except DraftNotFound as exc:
        raise OnboardingProfileMissing() from exc
    except PhoneNumberAlreadyAssigned as exc:
        raise PhoneNumberConflict(str(exc)) from exc

    now = _utcnow()
    should_activate = provisioning is None or provisioning["status"] == "verified"
    onboarding_update = (
        (
            "UPDATE onboarding_records SET status = 'active',"
            " activated_by_user_id = ?, activated_at = COALESCE(activated_at, ?),"
            " updated_at = ? WHERE organization_id = ?"
        ),
        (admin.id, now, now, organization_id),
    )
    if not should_activate:
        onboarding_update = (
            (
                "UPDATE onboarding_records SET status = 'in_progress',"
                " updated_at = ? WHERE organization_id = ?"
            ),
            (now, organization_id),
        )
    statements: list[tuple[str, tuple[Any, ...]]] = [
        onboarding_update,
        (
            (
                "INSERT INTO audit_logs"
                " (organization_id, actor_user_id, action, target_type, target_id,"
                " metadata, ip, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
            ),
            (
                organization_id,
                admin.id,
                AuditAction.PROFILE_PUBLISHED.value,
                "agent_version",
                published.version_id,
                json.dumps(
                    {
                        "profile_id": published.profile_id,
                        "version": published.version_number,
                        "phone_numbers": published.phone_numbers,
                    }
                ),
                ip,
                now,
            ),
        ),
    ]
    if should_activate and record["status"] != "active":
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
                    record["id"],
                    json.dumps({"agent_version_id": published.version_id}),
                    ip,
                    now,
                ),
            )
        )
    store.transaction(statements)
    return onboarding_payload(store, repository, organization_id)
