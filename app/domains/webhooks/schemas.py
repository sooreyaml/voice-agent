from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import AnyHttpUrl, BaseModel, Field, field_validator

from .constants import EVENT_TYPES

Description = Annotated[str | None, Field(default=None, max_length=200)]


def _check_event_types(value: list[str] | None) -> list[str] | None:
    if not value:
        return None
    unknown = sorted(set(value) - EVENT_TYPES)
    if unknown:
        raise ValueError(f"unknown event types: {', '.join(unknown)}")
    return sorted(set(value))


class CreateEndpointRequest(BaseModel):
    url: AnyHttpUrl
    description: Description
    event_types: list[str] | None = None
    active: bool = True

    _validate_events = field_validator("event_types")(_check_event_types)

    @field_validator("url")
    @classmethod
    def _https_only(cls, value: AnyHttpUrl) -> AnyHttpUrl:
        if value.scheme != "https":
            raise ValueError("webhook url must use https")
        return value


class UpdateEndpointRequest(BaseModel):
    url: AnyHttpUrl | None = None
    description: Description
    event_types: list[str] | None = None
    active: bool | None = None

    _validate_events = field_validator("event_types")(_check_event_types)

    @field_validator("url")
    @classmethod
    def _https_only(cls, value: AnyHttpUrl | None) -> AnyHttpUrl | None:
        if value is not None and value.scheme != "https":
            raise ValueError("webhook url must use https")
        return value


class EndpointResponse(BaseModel):
    id: str
    organization_id: str
    url: str
    description: str | None = None
    event_types: list[str] | None = None
    active: bool
    created_at: datetime | None = None
    updated_at: datetime | None = None


class EndpointWithSecretResponse(EndpointResponse):
    # Returned only when the secret is first shown (create / rotate).
    secret: str


class SecretResponse(BaseModel):
    secret: str


class DeliveryAttemptResponse(BaseModel):
    attempt: int
    attempted_at: datetime | None = None
    status_code: int | None = None
    error: str | None = None
    duration_ms: int | None = None


class DeliveryResponse(BaseModel):
    id: str
    webhook_event_id: str
    webhook_endpoint_id: str
    event_type: str
    status: str
    attempts: int
    max_attempts: int
    next_attempt_at: datetime | None = None
    last_attempt_at: datetime | None = None
    last_status_code: int | None = None
    last_error: str | None = None
    created_at: datetime | None = None


class DeliveryDetailResponse(DeliveryResponse):
    payload: dict | list | str | None = None
    response_snippet: str | None = None
    history: list[DeliveryAttemptResponse] = Field(default_factory=list)


class PageInfo(BaseModel):
    next_cursor: str | None = None
    has_more: bool = False


class DeliveryPage(BaseModel):
    items: list[DeliveryResponse]
    page: PageInfo
