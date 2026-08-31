from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response, status

from app.domains.auth.dependencies import (
    OrgContext,
    OrgMemberDep,
    StoreDep,
    require_org_role,
)

from . import service
from .dependencies import CredentialCipherDep
from .schemas import (
    ConnectIntegrationRequest,
    IntegrationResponse,
    IntegrationTestResponse,
)

router = APIRouter(tags=["integrations"])

OrgAdminDep = Annotated[OrgContext, Depends(require_org_role("admin"))]


def _ip(request: Request) -> str | None:
    return request.client.host if request.client else None


@router.get(
    "/organizations/{organization_id}/integrations",
    response_model=list[IntegrationResponse],
    summary="List an organization's integration connections",
)
def list_integrations(
    context: OrgMemberDep, store: StoreDep
) -> list[dict[str, Any]]:
    return service.list_connections(store, context.organization_id)


@router.get(
    "/organizations/{organization_id}/integrations/{provider}",
    response_model=IntegrationResponse,
    summary="One integration connection",
    responses={404: {"description": "Provider unknown or not connected"}},
)
def get_integration(
    provider: str, context: OrgMemberDep, store: StoreDep
) -> dict[str, Any]:
    return service.get_connection(store, context.organization_id, provider)


@router.put(
    "/organizations/{organization_id}/integrations/{provider}",
    response_model=IntegrationResponse,
    summary="Connect or replace an integration (admin or owner)",
    responses={
        404: {"description": "Unknown provider"},
        422: {"description": "The provider rejected the credentials"},
        502: {"description": "The provider could not be reached"},
        503: {"description": "Integration encryption is not configured"},
    },
)
def connect_integration(
    provider: str,
    body: ConnectIntegrationRequest,
    context: OrgAdminDep,
    store: StoreDep,
    cipher: CredentialCipherDep,
    request: Request,
) -> dict[str, Any]:
    return service.connect(
        store,
        cipher,
        context.user,
        context.organization_id,
        provider,
        body,
        ip=_ip(request),
    )


@router.post(
    "/organizations/{organization_id}/integrations/{provider}/test",
    response_model=IntegrationTestResponse,
    summary="Re-verify a connected integration against the provider (admin or owner)",
    responses={404: {"description": "Provider unknown or not connected"}},
)
def test_integration(
    provider: str,
    context: OrgAdminDep,
    store: StoreDep,
    cipher: CredentialCipherDep,
) -> dict[str, Any]:
    return service.test_connection(store, cipher, context.organization_id, provider)


@router.delete(
    "/organizations/{organization_id}/integrations/{provider}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Disconnect an integration (admin or owner)",
    responses={404: {"description": "Provider unknown or not connected"}},
)
def disconnect_integration(
    provider: str,
    context: OrgAdminDep,
    store: StoreDep,
    request: Request,
) -> Response:
    service.disconnect(
        store, context.user, context.organization_id, provider, ip=_ip(request)
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
