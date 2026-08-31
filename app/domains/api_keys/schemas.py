from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from .constants import ALL_SCOPES


class CreateApiKeyRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    scopes: list[str] = Field(min_length=1)

    @field_validator("scopes")
    @classmethod
    def _known_scopes(cls, value: list[str]) -> list[str]:
        unknown = sorted(set(value) - ALL_SCOPES)
        if unknown:
            raise ValueError(f"unknown scope(s): {', '.join(unknown)}")
        return sorted(set(value))


class ApiKeyResponse(BaseModel):
    id: str
    organization_id: str
    name: str
    prefix: str
    scopes: list[str]
    created_by_user_id: str | None = None
    last_used_at: datetime | None = None
    revoked_at: datetime | None = None
    created_at: datetime | None = None


class ApiKeyCreatedResponse(ApiKeyResponse):
    # The full secret, shown exactly once.
    key: str
