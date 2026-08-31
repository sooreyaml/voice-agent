from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

from app.domains.auth.schemas import Email
from app.domains.businesses.schemas import PublishedBusinessConfiguration

ProfileSlug = Annotated[
    str,
    Field(
        min_length=1,
        max_length=100,
        pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$",
    ),
]


class CreateOnboardingRequest(BaseModel):
    organization_name: str = Field(min_length=1, max_length=200)
    owner_email: Email


class SaveOnboardingProfileRequest(BaseModel):
    slug: ProfileSlug
    configuration: PublishedBusinessConfiguration


class OnboardingOrganization(BaseModel):
    id: str
    slug: str
    name: str


class OnboardingSteps(BaseModel):
    owner: Literal["invited", "accepted"]
    business_profile: Literal["not_started", "draft", "published", "changes_pending"]
    phone_number: Literal[
        "not_started",
        "selected",
        "provisioning",
        "ready",
        "routed",
        "failed",
        "verified",
    ]
    activation: Literal["pending", "active"]


class OnboardingProfileResponse(BaseModel):
    id: str
    slug: str
    name: str
    timezone: str
    draft_version: int | None = None
    published_version: int | None = None
    active_phone_numbers: list[str] = Field(default_factory=list)
    configuration: dict[str, Any] | None = None


class OnboardingResponse(BaseModel):
    id: str
    organization: OnboardingOrganization
    owner_email: str
    status: Literal["in_progress", "active"]
    mode: Literal["staff_led", "self_service"]
    steps: OnboardingSteps
    profile: OnboardingProfileResponse | None = None
    created_by_user_id: str | None = None
    activated_by_user_id: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    activated_at: datetime | None = None


class OnboardingCreatedResponse(OnboardingResponse):
    invitation_token: str


class DraftPreviewResponse(BaseModel):
    profile_id: str
    version_id: str
    version_number: int
    rendered_prompt: str
    configuration: dict[str, Any]


class OnboardingPageInfo(BaseModel):
    next_cursor: str | None = None
    has_more: bool = False


class OnboardingPage(BaseModel):
    items: list[OnboardingResponse]
    page: OnboardingPageInfo
