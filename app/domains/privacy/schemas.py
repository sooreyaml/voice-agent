from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

IdempotencyKey = Annotated[
    str,
    Field(
        min_length=8,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    ),
]


class PrivacySettingsResponse(BaseModel):
    organization_id: str
    transcript_retention_days: int | None
    updated_by_user_id: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class UpdatePrivacySettingsRequest(BaseModel):
    # null means retain transcripts until an explicit deletion request.
    transcript_retention_days: int | None = Field(ge=1, le=3650)


class CreateExportRequest(BaseModel):
    idempotency_key: IdempotencyKey


class CreateDeletionRequest(BaseModel):
    idempotency_key: IdempotencyKey
    confirm_organization_slug: str = Field(min_length=1, max_length=100)


class DataRequestResponse(BaseModel):
    id: str
    organization_id: str
    requested_by_user_id: str | None = None
    kind: Literal["export", "deletion"]
    status: Literal["pending", "processing", "completed", "failed", "dead", "cancelled"]
    attempts: int
    max_attempts: int
    execute_after: datetime
    result: dict[str, Any] | None = None
    result_expires_at: datetime | None = None
    last_error: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    completed_at: datetime | None = None
