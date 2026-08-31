from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ConnectIntegrationRequest(BaseModel):
    """Connect (or replace) a provider for an organization.

    Provider-specific fields are optional here and required per provider in
    ``service._credentials_and_settings`` (the path ``provider`` is authoritative).
    """

    model_config = ConfigDict(extra="forbid")

    display_name: str | None = Field(default=None, max_length=200)

    # Cal.com (calendar)
    api_key: str | None = Field(default=None, min_length=8, max_length=500)
    event_type_id: int | None = Field(default=None, gt=0)
    timezone: str = Field(default="UTC", max_length=100)

    # HubSpot (CRM)
    access_token: str | None = Field(default=None, min_length=20, max_length=500)

    @field_validator("timezone")
    @classmethod
    def _valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("timezone must be a valid IANA timezone") from exc
        return value


class IntegrationResponse(BaseModel):
    id: str
    organization_id: str
    provider: str
    status: str
    display_name: str | None = None
    external_account_id: str | None = None
    settings: dict[str, Any] = Field(default_factory=dict)
    last_error: str | None = None
    last_verified_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class IntegrationTestResponse(BaseModel):
    ok: bool
    external_account_id: str | None = None
    checked_at: datetime
    detail: str | None = None
