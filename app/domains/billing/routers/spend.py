from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request

from app.domains.auth.dependencies import OrgContext, StoreDep, require_org_role

from ..schemas import SpendLimitResponse, UpdateSpendLimitRequest
from ..services import spend as service

router = APIRouter(
    prefix="/organizations/{organization_id}/billing/spend-limit",
    tags=["billing"],
)
OrgAdminDep = Annotated[OrgContext, Depends(require_org_role("admin"))]
OrgOwnerDep = Annotated[OrgContext, Depends(require_org_role("owner"))]


def _ip(request: Request) -> str | None:
    return request.client.host if request.client else None


@router.get(
    "",
    response_model=SpendLimitResponse,
    summary="Get this month's spend and configured tenant limit",
)
def get_spend_limit(context: OrgAdminDep, store: StoreDep) -> dict[str, Any]:
    return service.spend_limit_status(store, context.organization_id)


@router.put(
    "",
    response_model=SpendLimitResponse,
    summary="Set or disable the organization's monthly spend limit",
)
def update_spend_limit(
    body: UpdateSpendLimitRequest,
    context: OrgOwnerDep,
    store: StoreDep,
    request: Request,
) -> dict[str, Any]:
    return service.update_spend_limit(
        store,
        context.user,
        context.organization_id,
        monthly_limit_micros=body.monthly_limit_micros,
        hard_limit=body.hard_limit,
        warning_threshold_percent=body.warning_threshold_percent,
        ip=_ip(request),
    )
