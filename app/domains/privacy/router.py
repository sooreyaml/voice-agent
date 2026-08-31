from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, status

from app.domains.auth.dependencies import OrgContext, StoreDep, require_org_role

from . import service
from .schemas import (
    CreateDeletionRequest,
    CreateExportRequest,
    DataRequestResponse,
    PrivacySettingsResponse,
    UpdatePrivacySettingsRequest,
)

router = APIRouter(
    prefix="/organizations/{organization_id}", tags=["privacy and data rights"]
)
OrgAdminDep = Annotated[OrgContext, Depends(require_org_role("admin"))]
OrgOwnerDep = Annotated[OrgContext, Depends(require_org_role("owner"))]


def _ip(request: Request) -> str | None:
    return request.client.host if request.client else None


@router.get(
    "/privacy",
    response_model=PrivacySettingsResponse,
    summary="Get transcript retention settings",
)
def get_privacy_settings(context: OrgAdminDep, store: StoreDep) -> dict[str, Any]:
    return service.get_privacy_settings(store, context.organization_id)


@router.patch(
    "/privacy",
    response_model=PrivacySettingsResponse,
    summary="Update transcript retention settings",
)
def update_privacy_settings(
    body: UpdatePrivacySettingsRequest,
    context: OrgAdminDep,
    store: StoreDep,
    request: Request,
) -> dict[str, Any]:
    return service.update_privacy_settings(
        store,
        context.user,
        context.organization_id,
        transcript_retention_days=body.transcript_retention_days,
        ip=_ip(request),
    )


@router.post(
    "/data-requests/exports",
    response_model=DataRequestResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Request a portable account data export",
)
def request_data_export(
    body: CreateExportRequest,
    context: OrgOwnerDep,
    store: StoreDep,
    request: Request,
) -> dict[str, Any]:
    return service.create_export_request(
        store,
        context.user,
        context.organization_id,
        idempotency_key=body.idempotency_key,
        ip=_ip(request),
    )


@router.post(
    "/data-requests/deletion",
    response_model=DataRequestResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Schedule account-data deletion after a seven-day safety window",
    description=(
        "Only the owner can schedule deletion. The exact organization slug is "
        "required as confirmation, and the pending request can be cancelled."
    ),
)
def request_data_deletion(
    body: CreateDeletionRequest,
    context: OrgOwnerDep,
    store: StoreDep,
    request: Request,
) -> dict[str, Any]:
    return service.create_deletion_request(
        store,
        context.user,
        context.organization_id,
        idempotency_key=body.idempotency_key,
        confirm_organization_slug=body.confirm_organization_slug,
        ip=_ip(request),
    )


@router.get(
    "/data-requests",
    response_model=list[DataRequestResponse],
    summary="List account data requests",
)
def list_data_requests(context: OrgOwnerDep, store: StoreDep) -> list[dict[str, Any]]:
    return service.list_data_requests(store, context.organization_id)


@router.get(
    "/data-requests/{request_id}",
    response_model=DataRequestResponse,
    summary="Get one data request and its unexpired export artifact",
)
def get_data_request(
    request_id: str, context: OrgOwnerDep, store: StoreDep
) -> dict[str, Any]:
    return service.get_data_request(store, context.organization_id, request_id)


@router.post(
    "/data-requests/{request_id}/cancel",
    response_model=DataRequestResponse,
    summary="Cancel a pending export or deletion request",
)
def cancel_data_request(
    request_id: str,
    context: OrgOwnerDep,
    store: StoreDep,
    request: Request,
) -> dict[str, Any]:
    return service.cancel_data_request(
        store,
        context.user,
        context.organization_id,
        request_id,
        ip=_ip(request),
    )
