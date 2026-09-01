"""Bearer-protected operational provisioning endpoints."""

from __future__ import annotations

import hmac

from fastapi import APIRouter, Header
from pydantic import BaseModel, Field, SecretStr, ValidationError

from app.domains.auth.dependencies import SettingsDep, StoreDep
from app.domains.auth.exceptions import APIError, NotFound
from app.domains.auth.schemas import Email
from app.domains.businesses.repository import PhoneNumberAlreadyAssigned

from .seeding import OrganizationSeed, seed_organization

router = APIRouter(prefix="/operations", tags=["operations"])


class SeedOrganizationRequest(BaseModel):
    organization_name: str = Field(min_length=1, max_length=200)
    organization_slug: str = Field(
        min_length=1, max_length=100, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$"
    )
    owner_email: Email
    owner_password: SecretStr
    business_name: str = Field(min_length=1, max_length=200)
    business_slug: str | None = Field(
        default=None,
        min_length=1,
        max_length=100,
        pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$",
    )
    phone_number: str = Field(min_length=1, max_length=30)
    timezone: str = Field(default="Europe/London", min_length=1, max_length=100)
    agent_name: str = Field(default="Alex", min_length=1, max_length=100)
    business_description: str | None = Field(default=None, max_length=2_000)
    greeting: str | None = Field(default=None, max_length=500)
    contact_email: str | None = Field(default=None, max_length=320)
    website: str | None = Field(default=None, max_length=2_000)
    transfer_number: str | None = Field(default=None, max_length=30)


def _authorize(authorization: str | None, expected: str) -> None:
    if not expected:
        raise NotFound()
    scheme, separator, token = (authorization or "").partition(" ")
    valid = (
        separator == " "
        and scheme.lower() == "bearer"
        and bool(token)
        and hmac.compare_digest(token, expected)
    )
    if not valid:
        raise APIError(
            "The provisioning token is invalid.",
            code="invalid_seed_token",
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
        )


@router.post(
    "/seed-organization",
    summary="Idempotently provision an organization owner and business profile",
)
def provision_organization(
    body: SeedOrganizationRequest,
    store: StoreDep,
    settings: SettingsDep,
    authorization: str | None = Header(default=None),
) -> dict:
    _authorize(authorization, settings.seed_api_token)
    seed = OrganizationSeed(
        organization_name=body.organization_name,
        organization_slug=body.organization_slug,
        owner_email=body.owner_email,
        business_name=body.business_name,
        business_slug=body.business_slug or body.organization_slug,
        phone_numbers=(body.phone_number,),
        timezone=body.timezone,
        agent_name=body.agent_name,
        business_description=body.business_description,
        greeting=body.greeting,
        contact_email=body.contact_email,
        website=body.website,
        transfer_number=body.transfer_number,
    )
    try:
        result = seed_organization(
            store,
            template_path=settings.businesses_dir / "harborview-dental.yaml",
            seed=seed,
            owner_password=body.owner_password.get_secret_value(),
        )
    except PhoneNumberAlreadyAssigned as exc:
        raise APIError(
            str(exc), code="phone_number_conflict", status_code=409
        ) from exc
    except (ValueError, ValidationError) as exc:
        raise APIError(
            "The organization seed data is invalid.",
            code="invalid_seed_data",
            status_code=422,
        ) from exc
    return result.as_dict()
