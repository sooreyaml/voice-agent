from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, status

from app.domains.auth.dependencies import (
    CurrentUser,
    CurrentUserDep,
    OrgContext,
    OrgMemberDep,
    StoreDep,
    require_org_role,
    require_platform_admin,
)
from app.domains.auth.exceptions import APIError
from app.pagination import InvalidCursor, decode_cursor, encode_cursor

from ..dependencies import StripeBillingDep
from ..schemas import (
    BillingOverviewResponse,
    BillingPlanResponse,
    CheckoutSessionRequest,
    CreateBillingPlanRequest,
    ExportUsageRequest,
    ExportUsageResponse,
    HostedSessionResponse,
    PortalSessionRequest,
    RecordUsageEventRequest,
    UpdateBillingPlanRequest,
    UsageEventPage,
    UsageEventResponse,
)
from ..services import management as service

router = APIRouter(tags=["billing"])
AdminDep = Annotated[CurrentUser, Depends(require_platform_admin)]
OrgOwnerDep = Annotated[OrgContext, Depends(require_org_role("owner"))]


def _ip(request: Request) -> str | None:
    return request.client.host if request.client else None


@router.get(
    "/billing/plans",
    response_model=list[BillingPlanResponse],
    summary="List active subscription plans",
)
def list_active_plans(
    _user: CurrentUserDep, store: StoreDep
) -> list[dict[str, Any]]:
    return service.list_plans(store, active_only=True)


@router.get(
    "/organizations/{organization_id}/billing",
    response_model=BillingOverviewResponse,
    summary="Subscription and current-period usage for an organization",
)
def get_billing_overview(
    context: OrgMemberDep, store: StoreDep
) -> dict[str, Any]:
    return service.billing_overview(store, context.organization_id)


@router.post(
    "/organizations/{organization_id}/billing/checkout",
    response_model=HostedSessionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a Stripe-hosted subscription checkout session",
    responses={
        409: {"description": "Organization already has an active subscription"},
        502: {"description": "Stripe rejected the request"},
        503: {"description": "Stripe is not configured"},
    },
)
def create_checkout_session(
    body: CheckoutSessionRequest,
    context: OrgOwnerDep,
    store: StoreDep,
    provider: StripeBillingDep,
    request: Request,
) -> dict[str, Any]:
    return service.create_checkout(
        store, provider, context, body, ip=_ip(request)
    )


@router.post(
    "/organizations/{organization_id}/billing/portal",
    response_model=HostedSessionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a short-lived Stripe-hosted billing portal session",
    responses={
        409: {"description": "No Stripe customer exists for the organization"},
        502: {"description": "Stripe rejected the request"},
        503: {"description": "Stripe is not configured"},
    },
)
def create_portal_session(
    body: PortalSessionRequest,
    context: OrgOwnerDep,
    store: StoreDep,
    provider: StripeBillingDep,
) -> dict[str, Any]:
    return service.create_portal(store, provider, context, body)


@router.get(
    "/organizations/{organization_id}/usage",
    response_model=UsageEventPage,
    summary="Immutable usage ledger for an organization",
)
def list_usage_events(
    context: OrgMemberDep,
    store: StoreDep,
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
) -> dict[str, Any]:
    try:
        decoded = decode_cursor(cursor)
    except InvalidCursor as exc:
        raise APIError(
            "Invalid page cursor.", code="invalid_cursor", status_code=400
        ) from exc
    before_occurred_at = before_id = None
    if decoded is not None:
        before_occurred_at, separator, before_id = decoded.partition("|")
        if not separator or not before_occurred_at or not before_id:
            raise APIError(
                "Invalid page cursor.", code="invalid_cursor", status_code=400
            )
    rows = service.usage_page(
        store,
        context.organization_id,
        limit + 1,
        before_occurred_at,
        before_id,
    )
    has_more = len(rows) > limit
    items = rows[:limit]
    next_cursor = None
    if has_more and items:
        last = items[-1]
        next_cursor = encode_cursor(f"{last['occurred_at']}|{last['id']}")
    return {"items": items, "page": {"next_cursor": next_cursor, "has_more": has_more}}


@router.get(
    "/admin/billing/plans",
    response_model=list[BillingPlanResponse],
    summary="List every billing plan (platform administrators only)",
)
def admin_list_plans(
    _admin: AdminDep, store: StoreDep
) -> list[dict[str, Any]]:
    return service.list_plans(store, active_only=False)


@router.post(
    "/admin/billing/plans",
    response_model=BillingPlanResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a billing plan (platform administrators only)",
    responses={409: {"description": "Plan code or Stripe price already exists"}},
)
def admin_create_plan(
    body: CreateBillingPlanRequest,
    admin: AdminDep,
    store: StoreDep,
    request: Request,
) -> dict[str, Any]:
    return service.create_plan(store, admin, body, ip=_ip(request))


@router.patch(
    "/admin/billing/plans/{plan_id}",
    response_model=BillingPlanResponse,
    summary="Update or archive a billing plan (platform administrators only)",
    responses={404: {"description": "Plan not found"}},
)
def admin_update_plan(
    plan_id: str,
    body: UpdateBillingPlanRequest,
    admin: AdminDep,
    store: StoreDep,
    request: Request,
) -> dict[str, Any]:
    return service.update_plan(store, admin, plan_id, body, ip=_ip(request))


@router.post(
    "/admin/billing/usage-events",
    response_model=UsageEventResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Append a reconciliation or reversal usage event",
    responses={409: {"description": "Idempotency or reversal mismatch"}},
)
def admin_record_usage(
    body: RecordUsageEventRequest,
    admin: AdminDep,
    store: StoreDep,
    request: Request,
) -> dict[str, Any]:
    return service.record_usage(store, admin, body, ip=_ip(request))


@router.post(
    "/admin/billing/usage-exports/stripe",
    response_model=ExportUsageResponse,
    summary="Export pending call duration to the configured Stripe meter",
    responses={503: {"description": "Stripe is not configured"}},
)
def admin_export_usage(
    body: ExportUsageRequest,
    _admin: AdminDep,
    store: StoreDep,
    provider: StripeBillingDep,
) -> dict[str, int]:
    return service.export_usage(store, provider, limit=body.limit)
