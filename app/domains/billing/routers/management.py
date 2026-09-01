from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, status

from app.domains.auth.dependencies import (
    CurrentUserDep,
    OrgContext,
    OrgMemberDep,
    StoreDep,
    require_org_role,
)
from app.domains.auth.exceptions import APIError
from app.pagination import InvalidCursor, decode_cursor, encode_cursor

from ..dependencies import BillingEnabledDep, StripeBillingDep
from ..schemas import (
    BillingOverviewResponse,
    BillingPlanResponse,
    CheckoutSessionRequest,
    HostedSessionResponse,
    PortalSessionRequest,
    UsageEventPage,
)
from ..services import management as service

router = APIRouter(tags=["billing"], dependencies=[BillingEnabledDep])
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
    description=(
        "Signup already opens this checkout; this is the owner's retry path if "
        "that never completed."
    ),
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
