from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, status

from app.domains.auth.dependencies import (
    CurrentUser,
    OrgContext,
    SettingsDep,
    StoreDep,
    require_org_role,
    require_platform_admin,
)
from app.domains.auth.exceptions import APIError
from app.domains.organizations.notifications import deliver_invitation
from app.pagination import InvalidCursor, decode_cursor, encode_cursor

from . import service
from .dependencies import BusinessRepositoryDep
from .schemas import (
    CreateOnboardingRequest,
    DraftPreviewResponse,
    OnboardingCreatedResponse,
    OnboardingPage,
    OnboardingResponse,
    SaveOnboardingProfileRequest,
)

router = APIRouter(prefix="/admin/onboarding", tags=["admin onboarding"])
self_service_router = APIRouter(
    prefix="/organizations/{organization_id}/onboarding",
    tags=["onboarding"],
)

DEFAULT_PAGE = 25
MAX_PAGE = 100
AdminDep = Annotated[CurrentUser, Depends(require_platform_admin)]
OrgAdminDep = Annotated[OrgContext, Depends(require_org_role("admin"))]
OrgOwnerDep = Annotated[OrgContext, Depends(require_org_role("owner"))]


def _ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _cursor(raw: str | None) -> str | None:
    try:
        return decode_cursor(raw)
    except InvalidCursor as exc:
        raise APIError(
            "Invalid page cursor.", code="invalid_cursor", status_code=400
        ) from exc


@router.get(
    "",
    response_model=OnboardingPage,
    summary="List staff-led customer onboardings",
)
def list_onboardings(
    _admin: AdminDep,
    store: StoreDep,
    repository: BusinessRepositoryDep,
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = DEFAULT_PAGE,
) -> dict[str, Any]:
    decoded = _cursor(cursor)
    before_created_at = before_id = None
    if decoded is not None:
        before_created_at, _, before_id = decoded.partition("|")
    rows = store.onboarding_records_page(limit + 1, before_created_at, before_id)
    has_more = len(rows) > limit
    page_rows = rows[:limit]
    items = [
        service.onboarding_payload(store, repository, str(row["organization_id"]))
        for row in page_rows
    ]
    next_cursor = None
    if has_more and page_rows:
        last = page_rows[-1]
        next_cursor = encode_cursor(f"{last['created_at']}|{last['id']}")
    return {
        "items": items,
        "page": {"next_cursor": next_cursor, "has_more": has_more},
    }


@router.post(
    "",
    response_model=OnboardingCreatedResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Start staff-led onboarding for a customer",
)
def create_onboarding(
    body: CreateOnboardingRequest,
    admin: AdminDep,
    store: StoreDep,
    settings: SettingsDep,
    repository: BusinessRepositoryDep,
    request: Request,
) -> dict[str, Any]:
    organization_id, raw_token = service.create_onboarding(
        store,
        admin,
        body,
        secret=settings.auth_session_secret,
        ip=_ip(request),
    )
    organization = store.organization(organization_id)
    deliver_invitation(
        email=body.owner_email,
        organization_name=str(organization["name"]) if organization else "",
        role="owner",
        raw_token=raw_token,
        base_url=settings.app_base_url,
        resend_api_key=settings.resend_api_key,
        resend_from_email=settings.resend_from_email,
    )
    return {
        **service.onboarding_payload(store, repository, organization_id),
        "invitation_token": raw_token,
    }


@router.get(
    "/{organization_id}",
    response_model=OnboardingResponse,
    summary="Get onboarding progress for one customer",
    responses={404: {"description": "Onboarding record not found"}},
)
def get_onboarding(
    organization_id: str,
    _admin: AdminDep,
    store: StoreDep,
    repository: BusinessRepositoryDep,
) -> dict[str, Any]:
    return service.onboarding_payload(store, repository, organization_id)


@router.put(
    "/{organization_id}/profile",
    response_model=OnboardingResponse,
    summary="Save a validated business and agent profile draft",
    responses={409: {"description": "Phone number or profile conflict"}},
)
def save_profile_draft(
    organization_id: str,
    body: SaveOnboardingProfileRequest,
    admin: AdminDep,
    store: StoreDep,
    repository: BusinessRepositoryDep,
    request: Request,
) -> dict[str, Any]:
    return service.save_profile_draft(
        store,
        repository,
        admin,
        organization_id,
        body,
        ip=_ip(request),
    )


@router.get(
    "/{organization_id}/profile/preview",
    response_model=DraftPreviewResponse,
    summary="Preview the exact prompt generated from the current draft",
    responses={409: {"description": "No profile draft exists"}},
)
def preview_profile(
    organization_id: str,
    _admin: AdminDep,
    store: StoreDep,
    repository: BusinessRepositoryDep,
) -> dict[str, Any]:
    return service.preview_profile(store, repository, organization_id)


@router.post(
    "/{organization_id}/publish",
    response_model=OnboardingResponse,
    summary="Publish the draft, activate routing, and finish onboarding",
    responses={409: {"description": "Draft missing or phone number conflict"}},
)
def publish_profile(
    organization_id: str,
    admin: AdminDep,
    store: StoreDep,
    repository: BusinessRepositoryDep,
    request: Request,
) -> dict[str, Any]:
    return service.publish_profile(
        store,
        repository,
        admin,
        organization_id,
        ip=_ip(request),
    )


# -- tenant self-service -------------------------------------------------


@self_service_router.post(
    "",
    response_model=OnboardingResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Start self-service onboarding for an existing organization",
)
def start_self_service_onboarding(
    context: OrgOwnerDep,
    store: StoreDep,
    repository: BusinessRepositoryDep,
    request: Request,
) -> dict[str, Any]:
    service.start_self_service_onboarding(
        store,
        context.user,
        context.organization_id,
        ip=_ip(request),
    )
    return service.onboarding_payload(store, repository, context.organization_id)


@self_service_router.get(
    "",
    response_model=OnboardingResponse,
    summary="Get this organization's onboarding progress",
    responses={404: {"description": "Self-service onboarding has not started"}},
)
def get_self_service_onboarding(
    context: OrgAdminDep,
    store: StoreDep,
    repository: BusinessRepositoryDep,
) -> dict[str, Any]:
    return service.onboarding_payload(store, repository, context.organization_id)


@self_service_router.put(
    "/profile",
    response_model=OnboardingResponse,
    summary="Save this organization's business and agent draft",
)
def save_self_service_profile(
    body: SaveOnboardingProfileRequest,
    context: OrgAdminDep,
    store: StoreDep,
    repository: BusinessRepositoryDep,
    request: Request,
) -> dict[str, Any]:
    return service.save_profile_draft(
        store,
        repository,
        context.user,
        context.organization_id,
        body,
        ip=_ip(request),
    )


@self_service_router.get(
    "/profile/preview",
    response_model=DraftPreviewResponse,
    summary="Preview the exact prompt generated from this organization's draft",
)
def preview_self_service_profile(
    context: OrgAdminDep,
    store: StoreDep,
    repository: BusinessRepositoryDep,
) -> dict[str, Any]:
    return service.preview_profile(store, repository, context.organization_id)


@self_service_router.post(
    "/publish",
    response_model=OnboardingResponse,
    summary="Publish this organization's draft",
)
def publish_self_service_profile(
    context: OrgAdminDep,
    store: StoreDep,
    repository: BusinessRepositoryDep,
    request: Request,
) -> dict[str, Any]:
    return service.publish_profile(
        store,
        repository,
        context.user,
        context.organization_id,
        ip=_ip(request),
        require_provisioning=True,
    )
