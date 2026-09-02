from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, Response, status

from app.domains.api_keys.dependencies import (
    CallsReadDep,
    LeadsReadDep,
    LeadsWriteDep,
)
from app.domains.audit.models import AuditAction
from app.domains.auth.dependencies import (
    CurrentUserDep,
    OrgContext,
    OrgMemberDep,
    SettingsDep,
    StoreDep,
    request_origin,
    require_org_role,
    require_platform_admin,
)
from app.domains.auth.exceptions import APIError, NotFound
from app.domains.auth.schemas import OrganizationMembershipResponse
from app.domains.calls.schemas import (
    CallDetailResponse,
    CallPage,
    LeadItem,
    LeadPage,
    PageInfo,
    UpdateLeadStatusRequest,
)
from app.pagination import InvalidCursor, decode_cursor, encode_cursor

from . import service
from .notifications import deliver_invitation
from .schemas import (
    AcceptInvitationRequest,
    AdminOrganizationDetail,
    AdminOrganizationResponse,
    AuditLogEntry,
    CreateInvitationRequest,
    CreateOrganizationRequest,
    InvitationCreatedResponse,
    InvitationPreviewResponse,
    InvitationResponse,
    MemberResponse,
    OrganizationResponse,
    PlatformOverviewResponse,
    UpdateMemberRequest,
    UpdateOrganizationRequest,
)

router = APIRouter(tags=["organizations"])

DEFAULT_PAGE = 25
MAX_PAGE = 100

AdminDep = Annotated[object, Depends(require_platform_admin)]
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


def _org_payload(org: dict[str, Any]) -> dict[str, Any]:
    return {"id": str(org["id"]), "slug": str(org["slug"]), "name": str(org["name"])}


def _member_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "user_id": str(row["user_id"]),
        "email": str(row["email"]),
        "role": str(row["role"]),
        "email_verified": row["email_verified_at"] is not None,
        "joined_at": row.get("joined_at"),
    }


def _invitation_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "organization_id": str(row["organization_id"]),
        "email": str(row["email"]),
        "role": str(row["role"]),
        "invited_by": row.get("invited_by"),
        "created_at": row.get("created_at"),
        "expires_at": row.get("expires_at"),
        "accepted_at": row.get("accepted_at"),
    }


# -- organization profile --------------------------------------------


@router.get(
    "/organizations",
    response_model=list[OrganizationMembershipResponse],
    summary="Organizations the user belongs to",
)
def list_organizations(user: CurrentUserDep, store: StoreDep) -> list[dict[str, Any]]:
    return [
        {
            "id": str(r["id"]),
            "slug": str(r["slug"]),
            "name": str(r["name"]),
            "role": str(r["role"]),
        }
        for r in store.organizations_for_user(user.id)
    ]


@router.post(
    "/organizations",
    response_model=OrganizationResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new organization (creator becomes owner)",
)
def create_organization(
    body: CreateOrganizationRequest, user: CurrentUserDep, store: StoreDep
) -> dict[str, Any]:
    return _org_payload(service.create_organization(store, user, body.name))


@router.get(
    "/organizations/{organization_id}",
    response_model=OrganizationResponse,
    summary="One organization's profile",
    responses={404: {"description": "Not a member of this organization"}},
)
def get_organization(context: OrgMemberDep, store: StoreDep) -> dict[str, Any]:
    org = store.organization(context.organization_id)
    if org is None:  # pragma: no cover - membership implies existence
        raise NotFound("Organization not found.")
    return _org_payload(org)


@router.patch(
    "/organizations/{organization_id}",
    response_model=OrganizationResponse,
    summary="Rename an organization (admin or owner)",
)
def update_organization(
    body: UpdateOrganizationRequest,
    context: OrgAdminDep,
    store: StoreDep,
    request: Request,
) -> dict[str, Any]:
    return _org_payload(
        service.rename_organization(store, context, body.name, ip=_ip(request))
    )


# -- members ----------------------------------------------------


@router.get(
    "/organizations/{organization_id}/members",
    response_model=list[MemberResponse],
    summary="List organization members",
)
def list_members(context: OrgMemberDep, store: StoreDep) -> list[dict[str, Any]]:
    return [_member_payload(r) for r in store.list_members(context.organization_id)]


@router.patch(
    "/organizations/{organization_id}/members/{user_id}",
    response_model=list[MemberResponse],
    summary="Change a member's role (owner only)",
    responses={409: {"description": "Last owner, or changing your own role"}},
)
def update_member_role(
    user_id: str,
    body: UpdateMemberRequest,
    context: OrgOwnerDep,
    store: StoreDep,
    request: Request,
) -> list[dict[str, Any]]:
    service.change_member_role(store, context, user_id, body.role, ip=_ip(request))
    return [_member_payload(r) for r in store.list_members(context.organization_id)]


@router.delete(
    "/organizations/{organization_id}/members/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove a member (owner), or leave the organization (self)",
    responses={409: {"description": "Last owner cannot be removed"}},
)
def remove_member(
    user_id: str,
    context: OrgMemberDep,
    store: StoreDep,
    request: Request,
) -> Response:
    service.remove_member(store, context, user_id, ip=_ip(request))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# -- invitations ----------------------------------------------


@router.get(
    "/organizations/{organization_id}/invitations",
    response_model=list[InvitationResponse],
    summary="Pending invitations (admin or owner)",
)
def list_invitations(context: OrgAdminDep, store: StoreDep) -> list[dict[str, Any]]:
    return [
        _invitation_payload(r)
        for r in store.list_pending_invitations(context.organization_id)
    ]


@router.post(
    "/organizations/{organization_id}/invitations",
    response_model=InvitationCreatedResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Invite someone to the organization (admin or owner)",
    responses={
        403: {"description": "Cannot grant a role higher than your own"},
        409: {"description": "Already a member"},
    },
)
def create_invitation(
    body: CreateInvitationRequest,
    context: OrgAdminDep,
    store: StoreDep,
    settings: SettingsDep,
    request: Request,
) -> dict[str, Any]:
    invitation, raw = service.invite_member(
        store,
        context,
        body.email,
        body.role,
        secret=settings.auth_session_secret,
        ip=_ip(request),
    )
    org = store.organization(context.organization_id)
    deliver_invitation(
        email=body.email,
        organization_name=str(org["name"]) if org else "",
        role=body.role,
        raw_token=raw,
        base_url=settings.resolve_base_url(request_origin(request)),
        resend_api_key=settings.resend_api_key,
        resend_from_email=settings.resend_from_email,
    )
    return {**_invitation_payload(invitation), "token": raw}


@router.delete(
    "/organizations/{organization_id}/invitations/{invitation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke a pending invitation (admin or owner)",
)
def delete_invitation(
    invitation_id: str,
    context: OrgAdminDep,
    store: StoreDep,
    request: Request,
) -> Response:
    service.revoke_invitation(store, context, invitation_id, ip=_ip(request))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/invitations/{token}",
    response_model=InvitationPreviewResponse,
    summary="Preview an invitation before signing in",
    responses={400: {"description": "Invitation invalid or already accepted"}},
)
def preview_invitation(
    token: str, store: StoreDep, settings: SettingsDep
) -> dict[str, Any]:
    return service.preview_invitation(
        store, token, secret=settings.auth_session_secret
    )


@router.post(
    "/invitations/{token}/accept",
    summary="Accept an invitation as the signed-in user",
    status_code=status.HTTP_200_OK,
    responses={
        400: {"description": "Invitation invalid or expired"},
        403: {"description": "Invitation was sent to a different address"},
    },
)
def accept_invitation(
    token: str,
    body: AcceptInvitationRequest | None,
    user: CurrentUserDep,
    store: StoreDep,
    settings: SettingsDep,
    request: Request,
) -> dict[str, Any]:
    # Token in the path is canonical; a body token, if sent, must match.
    raw = token
    if body is not None and body.token and body.token != token:
        raise APIError("Token mismatch.", code="invitation_invalid", status_code=400)
    organization_id = service.accept_invitation(
        store, user, raw, secret=settings.auth_session_secret, ip=_ip(request)
    )
    org = store.organization(organization_id)
    role = store.membership_role(organization_id, user.id)
    return {
        "organization": _org_payload(org) if org else None,
        "role": role,
    }


# -- audit log ------------------------------------------------


@router.get(
    "/organizations/{organization_id}/audit-log",
    summary="Recent audit entries for the organization (admin or owner)",
)
def get_audit_log(
    context: OrgAdminDep,
    store: StoreDep,
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = DEFAULT_PAGE,
) -> dict[str, Any]:
    decoded = _cursor(cursor)
    before_id = int(decoded) if decoded and decoded.lstrip("-").isdigit() else None
    rows = store.audit_log_page(context.organization_id, limit + 1, before_id)
    has_more = len(rows) > limit
    page_rows = rows[:limit]
    items = [
        AuditLogEntry(
            id=row["id"],
            action=row["action"],
            actor_user_id=row.get("actor_user_id"),
            target_type=row.get("target_type"),
            target_id=row.get("target_id"),
            metadata=row.get("metadata"),
            ip=row.get("ip"),
            created_at=row.get("created_at"),
        )
        for row in page_rows
    ]
    next_cursor = (
        encode_cursor(str(items[-1].id)) if has_more and items else None
    )
    return {"items": items, "page": PageInfo(next_cursor=next_cursor, has_more=has_more)}


# -- platform administration (read-only operator overview) --------


def _admin_org_item(r: dict[str, Any]) -> AdminOrganizationResponse:
    return AdminOrganizationResponse(
        id=str(r["id"]),
        slug=str(r["slug"]),
        name=str(r["name"]),
        member_count=int(r["member_count"]),
        lifecycle=str(r.get("lifecycle") or "active"),
        subscription_status=r.get("subscription_status"),
        phone_number=r.get("phone_number"),
        created_at=r.get("created_at"),
    )


@router.get(
    "/admin/overview",
    response_model=PlatformOverviewResponse,
    summary="Platform-wide operator metrics (platform administrators only)",
)
def admin_platform_overview(_admin: AdminDep, store: StoreDep) -> dict[str, Any]:
    now = datetime.now(UTC).replace(microsecond=0)
    period_start = now.replace(day=1, hour=0, minute=0, second=0)
    stats = store.platform_stats(period_start)
    return {"period_start": period_start, "period_end": now, **stats}


@router.get(
    "/admin/organizations",
    response_model=dict,
    summary="All organizations with lifecycle and billing state (admins only)",
)
def admin_list_organizations(
    _admin: AdminDep,
    store: StoreDep,
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = DEFAULT_PAGE,
) -> dict[str, Any]:
    decoded = _cursor(cursor)
    before_created_at = before_id = None
    if decoded is not None:
        before_created_at, _, before_id = decoded.partition("|")
    rows = store.organizations_page(limit + 1, before_created_at, before_id)
    has_more = len(rows) > limit
    page_rows = rows[:limit]
    items = [_admin_org_item(r) for r in page_rows]
    next_cursor = None
    if has_more and page_rows:
        last = page_rows[-1]
        next_cursor = encode_cursor(f"{last['created_at']}|{last['id']}")
    return {"items": items, "page": PageInfo(next_cursor=next_cursor, has_more=has_more)}


@router.get(
    "/admin/organizations/{organization_id}",
    response_model=AdminOrganizationDetail,
    summary="One organization with members (platform administrators only)",
    responses={404: {"description": "Organization not found"}},
)
def admin_get_organization(
    organization_id: str, _admin: AdminDep, store: StoreDep
) -> dict[str, Any]:
    org = store.organization(organization_id)
    if org is None:
        raise NotFound("Organization not found.")
    members = [_member_payload(r) for r in store.list_members(organization_id)]
    sub = store.query(
        "SELECT status FROM subscriptions WHERE organization_id = ?",
        (organization_id,),
    )
    number = store.query(
        "SELECT e164 FROM phone_numbers WHERE organization_id = ?"
        " AND status = 'active' LIMIT 1",
        (organization_id,),
    )
    return {
        "id": str(org["id"]),
        "slug": str(org["slug"]),
        "name": str(org["name"]),
        "member_count": len(members),
        "lifecycle": str(org.get("lifecycle") or "active"),
        "subscription_status": sub[0]["status"] if sub else None,
        "phone_number": number[0]["e164"] if number else None,
        "created_at": org.get("created_at"),
        "members": members,
    }


# -- tenant-scoped call history (relocated from the auth router) -----


@router.get(
    "/organizations/{organization_id}/calls",
    response_model=CallPage,
    summary="Recent calls for one organization",
    responses={404: {"description": "Not a member of this organization"}},
)
def list_org_calls(
    context: CallsReadDep,
    store: StoreDep,
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = DEFAULT_PAGE,
) -> dict[str, Any]:
    decoded = _cursor(cursor)
    before_started_at = before_call_id = None
    if decoded is not None:
        before_started_at, _, before_call_id = decoded.partition("|")
    rows = store.calls_page(
        context.organization_id, limit + 1, before_started_at, before_call_id
    )
    has_more = len(rows) > limit
    items = rows[:limit]
    next_cursor = None
    if has_more and items:
        last = items[-1]
        next_cursor = encode_cursor(f"{last['started_at']}|{last['call_id']}")
    return {"items": items, "page": PageInfo(next_cursor=next_cursor, has_more=has_more)}


@router.get(
    "/organizations/{organization_id}/calls/{call_id}",
    response_model=CallDetailResponse,
    summary="One organization call with its transcript",
    responses={404: {"description": "Call not found in this organization"}},
)
def get_org_call(
    call_id: str, context: CallsReadDep, store: StoreDep
) -> dict[str, Any]:
    detail = store.call_detail(context.organization_id, call_id)
    if detail is None:
        raise NotFound("Call not found.")
    return detail


@router.get(
    "/organizations/{organization_id}/leads",
    response_model=LeadPage,
    summary="Captured caller needs for one organization",
    responses={404: {"description": "Not a member of this organization"}},
)
def list_org_leads(
    context: LeadsReadDep,
    store: StoreDep,
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = DEFAULT_PAGE,
) -> dict[str, Any]:
    decoded = _cursor(cursor)
    before_id = int(decoded) if decoded and decoded.isdigit() else None
    rows = store.leads_page(context.organization_id, limit + 1, before_id)
    has_more = len(rows) > limit
    items = [LeadItem(**row) for row in rows[:limit]]
    next_cursor = encode_cursor(str(items[-1].id)) if has_more and items else None
    return {"items": items, "page": PageInfo(next_cursor=next_cursor, has_more=has_more)}


@router.patch(
    "/organizations/{organization_id}/leads/{lead_id}",
    response_model=LeadItem,
    summary="Update a captured lead's follow-up status",
    responses={404: {"description": "Lead not found in this organization"}},
)
def update_org_lead_status(
    lead_id: int,
    body: UpdateLeadStatusRequest,
    context: LeadsWriteDep,
    store: StoreDep,
    request: Request,
) -> dict[str, Any]:
    row = store.update_lead_status(
        context.organization_id,
        lead_id,
        status=body.status,
        note=body.note,
        updated_by=context.principal,
    )
    if row is None:
        raise NotFound("Lead not found.")
    store.record_audit(
        AuditAction.LEAD_STATUS_CHANGED.value,
        organization_id=context.organization_id,
        actor_user_id=context.user.id if context.user else None,
        target_type="lead",
        target_id=str(lead_id),
        metadata={"status": body.status, "principal": context.principal},
        ip=_ip(request),
    )
    return row
