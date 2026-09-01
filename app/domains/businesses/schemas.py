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


# -- self-service agent editing --------------------------------------------
#
# The management API lets an owner rewrite everything about their agent except
# the phone number, which is pool-managed. These request models mirror
# ``PublishedBusinessConfiguration`` but drop ``business.phone_numbers`` — the
# router injects the organization's live number before saving.


class DraftBusinessIdentity(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str = Field(min_length=1, max_length=200)
    timezone: str = Field(min_length=1, max_length=100)

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("business timezone must be a valid IANA timezone") from exc
        return value


class AgentDraftRequest(BaseModel):
    """A full replacement configuration for the organization's one agent.

    ``extra="allow"`` keeps the open-ended sections the prompt renderer reads
    (``hours``, ``services``, ``faqs``, ``knowledge``, ``contact``,
    ``guardrails``, ``location``) — send the whole object back on every edit,
    not a partial patch.
    """

    model_config = ConfigDict(extra="allow")

    business: DraftBusinessIdentity
    agent: AgentConfiguration


class AgentVersionView(BaseModel):
    version_number: int
    configuration: dict
    rendered_prompt: str | None = None


class AgentStateResponse(BaseModel):
    provisioned: bool
    lifecycle: str
    editable: bool
    active_phone_numbers: list[str]
    slug: str | None = None
    name: str | None = None
    published: AgentVersionView | None = None
    draft: AgentVersionView | None = None


class AgentDraftResponse(BaseModel):
    version_number: int
    configuration: dict
    rendered_prompt: str
