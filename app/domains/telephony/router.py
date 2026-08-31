from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Request, status

from app.domains.auth.dependencies import (
    CurrentUser,
    OrgContext,
    StoreDep,
    require_org_role,
    require_platform_admin,
)

from . import service
from .dependencies import BusinessRepositoryDep, TwilioProvisioningDep
from .schemas import (
    AvailablePhoneNumbersResponse,
    ProvisioningResponse,
    ProvisionPhoneNumberRequest,
    RegulatoryRequirementsResponse,
    VerifyTestCallRequest,
)

router = APIRouter(
    prefix="/admin/onboarding/{organization_id}/telephony",
    tags=["admin telephony"],
)
self_service_router = APIRouter(
    prefix="/organizations/{organization_id}/onboarding/telephony",
    tags=["onboarding telephony"],
)
AdminDep = Annotated[CurrentUser, Depends(require_platform_admin)]
OrgAdminDep = Annotated[OrgContext, Depends(require_org_role("admin"))]
OrgOwnerDep = Annotated[OrgContext, Depends(require_org_role("owner"))]
NumberType = Literal["local", "mobile", "national", "toll_free"]


def _ip(request: Request) -> str | None:
    return request.client.host if request.client else None


@router.get(
    "/requirements",
    response_model=RegulatoryRequirementsResponse,
    summary="Get current Twilio regulatory requirements",
    description=(
        "Returns the provider requirements for a country, number type, and "
        "business or individual end user before a number is purchased."
    ),
    responses={502: {"description": "Twilio rejected the lookup"}},
)
def get_regulatory_requirements(
    organization_id: str,
    _admin: AdminDep,
    store: StoreDep,
    provider: TwilioProvisioningDep,
    country_code: Annotated[
        str, Query(min_length=2, max_length=2, pattern=r"^[A-Za-z]{2}$")
    ],
    number_type: Annotated[NumberType, Query()] = "local",
    end_user_type: Annotated[Literal["business", "individual"], Query()] = "business",
) -> dict:
    return {
        "items": service.regulatory_requirements(
            store,
            provider,
            organization_id,
            country_code.upper(),
            number_type,
            end_user_type,
        )
    }


@router.get(
    "/available-numbers",
    response_model=AvailablePhoneNumbersResponse,
    summary="Search voice-enabled Twilio numbers",
    description=(
        "Searches the platform Twilio account. Area-code filtering is limited "
        "to the United States and Canada; use contains elsewhere."
    ),
    responses={502: {"description": "Twilio rejected the lookup"}},
)
def search_available_numbers(
    organization_id: str,
    _admin: AdminDep,
    store: StoreDep,
    provider: TwilioProvisioningDep,
    country_code: Annotated[
        str, Query(min_length=2, max_length=2, pattern=r"^[A-Za-z]{2}$")
    ],
    number_type: Annotated[NumberType, Query()] = "local",
    area_code: Annotated[int | None, Query(ge=100, le=999)] = None,
    contains: Annotated[str | None, Query(min_length=2, max_length=16)] = None,
    exclude_address_required: Annotated[bool, Query()] = False,
    limit: Annotated[int, Query(ge=1, le=20)] = 10,
) -> dict:
    return {
        "items": service.available_numbers(
            store,
            provider,
            organization_id,
            country_code.upper(),
            number_type,
            area_code=area_code,
            contains=contains,
            exclude_address_required=exclude_address_required,
            limit=limit,
        )
    }


@router.get(
    "",
    response_model=ProvisioningResponse,
    summary="Get the latest telephone provisioning status",
    responses={404: {"description": "No provisioning request exists"}},
)
def get_provisioning_status(
    organization_id: str,
    _admin: AdminDep,
    store: StoreDep,
) -> dict:
    return service.latest_provisioning(store, organization_id)


@router.post(
    "/provision",
    response_model=ProvisioningResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Purchase and connect a selected Twilio number",
    description=(
        "Performs a billable purchase using the platform Twilio account, reuses "
        "the shared OpenAI SIP trunk, and safely replays by idempotency key."
    ),
    responses={
        409: {"description": "Profile, phone-number, or idempotency conflict"},
        502: {"description": "Twilio provisioning failed; retry is safe"},
        503: {"description": "Twilio credentials are not configured"},
    },
)
def provision_phone_number(
    organization_id: str,
    body: ProvisionPhoneNumberRequest,
    admin: AdminDep,
    store: StoreDep,
    repository: BusinessRepositoryDep,
    provider: TwilioProvisioningDep,
    request: Request,
) -> dict:
    return service.provision_number(
        store,
        repository,
        provider,
        admin,
        organization_id,
        body,
        ip=_ip(request),
    )


@router.post(
    "/verify-test-call",
    response_model=ProvisioningResponse,
    summary="Verify a completed inbound test call and activate onboarding",
    responses={409: {"description": "Number or completed test call not ready"}},
)
def verify_test_call(
    organization_id: str,
    body: VerifyTestCallRequest,
    admin: AdminDep,
    store: StoreDep,
    request: Request,
) -> dict:
    return service.verify_test_call(
        store,
        admin,
        organization_id,
        body,
        ip=_ip(request),
    )


# -- tenant self-service -------------------------------------------------


@self_service_router.get(
    "/requirements",
    response_model=RegulatoryRequirementsResponse,
    summary="Get telephone regulatory requirements for self-service onboarding",
    responses={502: {"description": "Twilio rejected the lookup"}},
)
def get_self_service_requirements(
    context: OrgAdminDep,
    store: StoreDep,
    provider: TwilioProvisioningDep,
    country_code: Annotated[
        str, Query(min_length=2, max_length=2, pattern=r"^[A-Za-z]{2}$")
    ],
    number_type: Annotated[NumberType, Query()] = "local",
    end_user_type: Annotated[Literal["business", "individual"], Query()] = "business",
) -> dict:
    return {
        "items": service.regulatory_requirements(
            store,
            provider,
            context.organization_id,
            country_code.upper(),
            number_type,
            end_user_type,
        )
    }


@self_service_router.get(
    "/available-numbers",
    response_model=AvailablePhoneNumbersResponse,
    summary="Search voice-enabled numbers for self-service onboarding",
    responses={502: {"description": "Twilio rejected the lookup"}},
)
def search_self_service_numbers(
    context: OrgAdminDep,
    store: StoreDep,
    provider: TwilioProvisioningDep,
    country_code: Annotated[
        str, Query(min_length=2, max_length=2, pattern=r"^[A-Za-z]{2}$")
    ],
    number_type: Annotated[NumberType, Query()] = "local",
    area_code: Annotated[int | None, Query(ge=100, le=999)] = None,
    contains: Annotated[str | None, Query(min_length=2, max_length=16)] = None,
    exclude_address_required: Annotated[bool, Query()] = False,
    limit: Annotated[int, Query(ge=1, le=20)] = 10,
) -> dict:
    return {
        "items": service.available_numbers(
            store,
            provider,
            context.organization_id,
            country_code.upper(),
            number_type,
            area_code=area_code,
            contains=contains,
            exclude_address_required=exclude_address_required,
            limit=limit,
        )
    }


@self_service_router.get(
    "",
    response_model=ProvisioningResponse,
    summary="Get this organization's telephone provisioning status",
    responses={404: {"description": "No provisioning request exists"}},
)
def get_self_service_provisioning(
    context: OrgAdminDep,
    store: StoreDep,
) -> dict:
    return service.latest_provisioning(store, context.organization_id)


@self_service_router.post(
    "/provision",
    response_model=ProvisioningResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Owner-approved purchase and connection of a selected number",
    description=(
        "This is a billable operation. The organization owner must explicitly "
        "send purchase_approved=true; retries are idempotent."
    ),
    responses={
        409: {"description": "Profile, phone-number, or idempotency conflict"},
        502: {"description": "Twilio provisioning failed; retry is safe"},
        503: {"description": "Twilio credentials are not configured"},
    },
)
def provision_self_service_number(
    body: ProvisionPhoneNumberRequest,
    context: OrgOwnerDep,
    store: StoreDep,
    repository: BusinessRepositoryDep,
    provider: TwilioProvisioningDep,
    request: Request,
) -> dict:
    return service.provision_number(
        store,
        repository,
        provider,
        context.user,
        context.organization_id,
        body,
        ip=_ip(request),
    )


@self_service_router.post(
    "/verify-test-call",
    response_model=ProvisioningResponse,
    summary="Verify a completed inbound test call and activate onboarding",
    responses={409: {"description": "Number or completed test call not ready"}},
)
def verify_self_service_test_call(
    body: VerifyTestCallRequest,
    context: OrgAdminDep,
    store: StoreDep,
    request: Request,
) -> dict:
    return service.verify_test_call(
        store,
        context.user,
        context.organization_id,
        body,
        ip=_ip(request),
    )
