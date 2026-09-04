"""Business-profile onboarding for an organization owner.

After signing up and verifying their email the owner fills in their business
profile here; completing it provisions the phone number (or, when billing is on,
returns a checkout link and provisions once payment lands).
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, status

from app.domains.auth.dependencies import (
    OrgContext,
    OrgMemberDep,
    SettingsDep,
    StoreDep,
    request_origin,
    require_org_role,
)
from app.domains.billing.dependencies import StripeBillingDep
from app.domains.telephony.dependencies import ProvisioningProviderDep

from . import service
from .schemas import BusinessProfileIntakeRequest, OnboardingStateResponse

router = APIRouter(tags=["onboarding"])

OrgAdminDep = Annotated[OrgContext, Depends(require_org_role("admin"))]


@router.get(
    "/organizations/{organization_id}/onboarding",
    response_model=OnboardingStateResponse,
    summary="Where this organization is between signup and a live phone number",
)
def get_onboarding(context: OrgMemberDep, store: StoreDep, settings: SettingsDep) -> dict[str, Any]:
    return service.onboarding_state(store, settings, context.organization_id)


@router.put(
    "/organizations/{organization_id}/business-profile",
    response_model=OnboardingStateResponse,
    summary="Save the business profile and provision the phone number",
    responses={
        403: {"description": "Owner's email is not verified"},
        503: {"description": "Number could not be provisioned; profile is saved"},
    },
)
def put_business_profile(
    body: BusinessProfileIntakeRequest,
    context: OrgAdminDep,
    store: StoreDep,
    settings: SettingsDep,
    provisioning_provider: ProvisioningProviderDep,
    stripe: StripeBillingDep,
    request: Request,
) -> dict[str, Any]:
    service.save_business_profile(
        store, context.organization_id, body.model_dump()
    )
    return service.activate(
        store,
        settings,
        provisioning_provider,
        context.organization_id,
        owner_email=context.user.email,
        base_url=settings.resolve_base_url(request_origin(request)),
        stripe_provider=stripe,
    )


@router.post(
    "/organizations/{organization_id}/business-profile/provision",
    response_model=OnboardingStateResponse,
    status_code=status.HTTP_200_OK,
    summary="Retry number provisioning for a profile-complete organization",
    responses={
        403: {"description": "Owner's email is not verified"},
        409: {"description": "Business profile is not complete yet"},
        503: {"description": "Number could not be provisioned; try again shortly"},
    },
)
def retry_provision(
    context: OrgAdminDep,
    store: StoreDep,
    settings: SettingsDep,
    provisioning_provider: ProvisioningProviderDep,
    stripe: StripeBillingDep,
    request: Request,
) -> dict[str, Any]:
    return service.activate(
        store,
        settings,
        provisioning_provider,
        context.organization_id,
        owner_email=context.user.email,
        base_url=settings.resolve_base_url(request_origin(request)),
        stripe_provider=stripe,
    )
