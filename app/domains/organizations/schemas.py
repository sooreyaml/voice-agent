from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field

from app.domains.auth.schemas import Email

Role = Literal["owner", "admin", "member", "viewer"]
OrgName = Annotated[str, Field(min_length=1, max_length=200)]


class CreateOrganizationRequest(BaseModel):
    name: OrgName


class UpdateOrganizationRequest(BaseModel):
    name: OrgName


class OrganizationResponse(BaseModel):
    id: str
    slug: str
    name: str


class MemberResponse(BaseModel):
    user_id: str
    email: str
    role: Role
    email_verified: bool
    joined_at: datetime | None = None


class UpdateMemberRequest(BaseModel):
    role: Role


class CreateInvitationRequest(BaseModel):
    email: Email
    # Granting owner requires the inviter to be an owner (enforced in the service).
    role: Role


class InvitationResponse(BaseModel):
    id: str
    organization_id: str
    email: str
    role: Role
    invited_by: str | None = None
    created_at: datetime | None = None
    expires_at: datetime | None = None
    accepted_at: datetime | None = None


class InvitationCreatedResponse(InvitationResponse):
    # The raw token is returned once so the caller can hand out the link while
    # real email delivery does not exist yet.
    token: str


class InvitationPreviewResponse(BaseModel):
    organization_name: str
    email: str
    role: Role
    expired: bool


class AcceptInvitationRequest(BaseModel):
    token: str = Field(min_length=1, max_length=512)


class AuditLogEntry(BaseModel):
    id: int
    action: str
    actor_user_id: str | None = None
    target_type: str | None = None
    target_id: str | None = None
    metadata: dict | None = None
    ip: str | None = None
    created_at: datetime | None = None


class AdminOrganizationResponse(BaseModel):
    id: str
    slug: str
    name: str
    member_count: int
    lifecycle: str
    subscription_status: str | None = None
    phone_number: str | None = None
    created_at: datetime | None = None


class AdminOrganizationDetail(AdminOrganizationResponse):
    members: list[MemberResponse]


class PlatformOverviewResponse(BaseModel):
    period_start: datetime
    period_end: datetime
    organizations: dict[str, int]
    subscriptions: dict[str, int]
    payment_failures: int
    period_calls: int
    period_model_cost_usd: float
    period_customer_charge_micros: int
    number_pool: dict[str, int]
