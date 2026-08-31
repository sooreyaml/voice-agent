from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response, status

from app.domains.auth.dependencies import (
    OrgContext,
    SettingsDep,
    StoreDep,
    require_org_role,
)

from . import service
from .schemas import ApiKeyCreatedResponse, ApiKeyResponse, CreateApiKeyRequest

router = APIRouter(tags=["api-keys"])

OrgAdminDep = Annotated[OrgContext, Depends(require_org_role("admin"))]


def _ip(request: Request) -> str | None:
    return request.client.host if request.client else None


@router.get(
    "/organizations/{organization_id}/api-keys",
    response_model=list[ApiKeyResponse],
    summary="List an organization's API keys (admin or owner); secrets never shown",
)
def list_api_keys(context: OrgAdminDep, store: StoreDep) -> list[dict[str, Any]]:
    return service.list_keys(store, context.organization_id)


@router.post(
    "/organizations/{organization_id}/api-keys",
    response_model=ApiKeyCreatedResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a scoped API key (admin or owner); the secret is returned once",
    responses={409: {"description": "The organization has reached its key limit"}},
)
def create_api_key(
    body: CreateApiKeyRequest,
    context: OrgAdminDep,
    store: StoreDep,
    settings: SettingsDep,
    request: Request,
) -> dict[str, Any]:
    return service.create_key(
        store,
        settings,
        context.user,
        context.organization_id,
        name=body.name,
        scopes=body.scopes,
        ip=_ip(request),
    )


@router.post(
    "/organizations/{organization_id}/api-keys/{key_id}/rotate",
    response_model=ApiKeyCreatedResponse,
    summary="Issue a new secret for a key (admin or owner); the old one stops working",
    responses={404: {"description": "Key not found or already revoked"}},
)
def rotate_api_key(
    key_id: str,
    context: OrgAdminDep,
    store: StoreDep,
    settings: SettingsDep,
    request: Request,
) -> dict[str, Any]:
    return service.rotate_key(
        store, settings, context.user, context.organization_id, key_id, ip=_ip(request)
    )


@router.delete(
    "/organizations/{organization_id}/api-keys/{key_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke an API key (admin or owner)",
    responses={404: {"description": "Key not found"}},
)
def revoke_api_key(
    key_id: str,
    context: OrgAdminDep,
    store: StoreDep,
    request: Request,
) -> Response:
    service.revoke_key(
        store, context.user, context.organization_id, key_id, ip=_ip(request)
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
