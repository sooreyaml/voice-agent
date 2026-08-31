from __future__ import annotations

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .normalization import normalize_e164


class BusinessIdentity(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str = Field(min_length=1, max_length=200)
    timezone: str = Field(min_length=1, max_length=100)
    phone_numbers: list[str] = Field(min_length=1)

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("business timezone must be a valid IANA timezone") from exc
        return value

    @field_validator("phone_numbers")
    @classmethod
    def valid_phone_numbers(cls, values: list[str]) -> list[str]:
        normalized = [normalize_e164(value) for value in values]
        if len(set(normalized)) != len(normalized):
            raise ValueError("business phone numbers must be unique")
        return normalized


class AgentConfiguration(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str = Field(min_length=1, max_length=100)
    greeting: str = Field(min_length=1, max_length=500)
    voice: str | None = Field(default=None, max_length=50)


class PublishedBusinessConfiguration(BaseModel):
    model_config = ConfigDict(extra="allow")

    business: BusinessIdentity
    agent: AgentConfiguration
