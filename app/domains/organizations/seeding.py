"""Idempotent provisioning for a complete business organization account."""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.business import BusinessProfile
from app.domains.audit.models import AuditAction
from app.domains.auth.passwords import hash_password
from app.domains.auth.schemas import SignupRequest
from app.domains.businesses.repository import BusinessRepository
from app.domains.businesses.schemas import PublishedBusinessConfiguration
from app.store import Store

_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


@dataclass(frozen=True)
class OrganizationSeed:
    organization_name: str
    organization_slug: str
    owner_email: str
    business_name: str
    business_slug: str
    phone_numbers: tuple[str, ...]
    timezone: str = "Europe/London"
    agent_name: str = "Alex"
    business_description: str | None = None
    greeting: str | None = None
    contact_email: str | None = None
    contact_phone: str | None = None
    website: str | None = None
    transfer_number: str | None = None


@dataclass(frozen=True)
class SeedResult:
    organization_id: str
    organization_slug: str
    owner_user_id: str
    owner_email: str
    profile_id: str
    business_slug: str
    version_id: str
    version_number: int
    phone_numbers: tuple[str, ...]
    organization_created: bool
    owner_created: bool
    owner_password_set: bool
    profile_version_created: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "organization": {
                "id": self.organization_id,
                "slug": self.organization_slug,
                "created": self.organization_created,
            },
            "owner": {
                "id": self.owner_user_id,
                "email": self.owner_email,
                "created": self.owner_created,
                "password_set": self.owner_password_set,
                "email_verified": True,
                "role": "owner",
            },
            "business_profile": {
                "id": self.profile_id,
                "slug": self.business_slug,
                "version_id": self.version_id,
                "version": self.version_number,
                "version_created": self.profile_version_created,
                "phone_numbers": list(self.phone_numbers),
                "status": "published",
            },
        }


def _slug(value: str, field: str) -> str:
    normalized = value.strip().lower()
    if len(normalized) > 100 or not _SLUG_RE.fullmatch(normalized):
        raise ValueError(
            f"{field} must contain only lowercase letters, numbers, and single hyphens"
        )
    return normalized


def build_profile(template_path: Path, seed: OrganizationSeed) -> BusinessProfile:
    """Clone the canonical YAML template and apply tenant identity fields."""
    source = BusinessProfile.load(template_path)
    raw = copy.deepcopy(source.raw)
    business = raw.setdefault("business", {})
    agent = raw.setdefault("agent", {})
    contact = raw.setdefault("contact", {})

    business["name"] = seed.business_name.strip()
    business["timezone"] = seed.timezone.strip()
    business["phone_numbers"] = list(seed.phone_numbers)
    if seed.business_description is not None:
        business["what_we_do"] = seed.business_description.strip()

    agent["name"] = seed.agent_name.strip()
    agent["greeting"] = (
        seed.greeting.strip()
        if seed.greeting
        else (
            f"Thanks for calling {seed.business_name.strip()}, this is "
            f"{seed.agent_name.strip()}. How can I help you today?"
        )
    )

    contact["email"] = (seed.contact_email or seed.owner_email).strip().lower()
    contact["phone"] = (seed.contact_phone or seed.phone_numbers[0]).strip()
    contact["transfer_to"] = (seed.transfer_number or "").strip()
    if seed.website:
        contact["website"] = seed.website.strip()
    else:
        contact.pop("website", None)

    slug = _slug(seed.business_slug, "business slug")
    profile = BusinessProfile(raw=raw, slug=slug)
    PublishedBusinessConfiguration.model_validate(profile.raw)
    return profile


def seed_organization(
    store: Store,
    *,
    template_path: Path,
    seed: OrganizationSeed,
    owner_password: str,
) -> SeedResult:
    """Create or reconcile one organization, owner, and published profile.

    Existing owner passwords are intentionally preserved. A password is only
    installed when the user is new or does not yet have one.
    """
    try:
        signup = SignupRequest(
            email=seed.owner_email,
            password=owner_password,
            organization_name=seed.organization_name,
        )
    except ValidationError as exc:
        raise ValueError(str(exc)) from exc

    organization_name = signup.organization_name.strip()
    organization_slug = _slug(seed.organization_slug, "organization slug")
    profile = build_profile(template_path, seed)
    repository = BusinessRepository(store)

    existing_organization_id = store.organization_id_for_slug(organization_slug)
    organization_created = existing_organization_id is None
    existing_organization = (
        store.organization(existing_organization_id)
        if existing_organization_id is not None
        else None
    )
    organization_id = store.ensure_organization(
        organization_slug, organization_name
    )

    existing_user = store.get_user_by_email(signup.email)
    owner_created = existing_user is None
    owner_password_set = owner_created or not (
        existing_user and existing_user.get("password_hash")
    )
    password_hash = hash_password(owner_password) if owner_password_set else None
    owner_user_id = store.create_user(signup.email, password_hash=password_hash)
    if not owner_created and owner_password_set and password_hash is not None:
        store.set_user_password(owner_user_id, password_hash)
    store.mark_email_verified(owner_user_id)

    previous_role = store.membership_role(organization_id, owner_user_id)
    store.add_membership(organization_id, owner_user_id, "owner")

    previous_profile = next(
        (
            item
            for item in repository.list_published()
            if item.organization_id == organization_id and item.slug == profile.slug
        ),
        None,
    )
    published = repository.publish(profile, organization_id=organization_id)
    profile_version_created = (
        previous_profile is None
        or previous_profile.version_id != published.version_id
    )

    if organization_created:
        store.record_audit(
            AuditAction.ORG_CREATED.value,
            organization_id=organization_id,
            actor_user_id=owner_user_id,
            target_type="organization",
            target_id=organization_id,
            metadata={
                "name": organization_name,
                "slug": organization_slug,
                "source": "seed_organization",
            },
        )
    elif existing_organization and existing_organization["name"] != organization_name:
        store.record_audit(
            AuditAction.ORG_UPDATED.value,
            organization_id=organization_id,
            actor_user_id=owner_user_id,
            target_type="organization",
            target_id=organization_id,
            metadata={"name": organization_name, "source": "seed_organization"},
        )

    if previous_role is None:
        store.record_audit(
            AuditAction.MEMBER_JOINED.value,
            organization_id=organization_id,
            actor_user_id=owner_user_id,
            target_type="user",
            target_id=owner_user_id,
            metadata={"role": "owner", "source": "seed_organization"},
        )
    elif previous_role != "owner":
        store.record_audit(
            AuditAction.MEMBER_ROLE_CHANGED.value,
            organization_id=organization_id,
            actor_user_id=owner_user_id,
            target_type="user",
            target_id=owner_user_id,
            metadata={
                "from_role": previous_role,
                "to_role": "owner",
                "source": "seed_organization",
            },
        )

    if profile_version_created:
        store.record_audit(
            AuditAction.PROFILE_PUBLISHED.value,
            organization_id=organization_id,
            actor_user_id=owner_user_id,
            target_type="agent_version",
            target_id=published.version_id,
            metadata={
                "profile_id": published.profile_id,
                "version": published.version_number,
                "phone_numbers": published.phone_numbers,
                "source": "seed_organization",
            },
        )

    return SeedResult(
        organization_id=organization_id,
        organization_slug=organization_slug,
        owner_user_id=owner_user_id,
        owner_email=signup.email,
        profile_id=published.profile_id,
        business_slug=published.slug,
        version_id=published.version_id,
        version_number=published.version_number,
        phone_numbers=tuple(published.phone_numbers),
        organization_created=organization_created,
        owner_created=owner_created,
        owner_password_set=owner_password_set,
        profile_version_created=profile_version_created,
    )
