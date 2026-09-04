from __future__ import annotations

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, field_validator

from app.domains.auth.schemas import Email
from app.domains.businesses.normalization import normalize_e164


class BusinessProfileIntakeRequest(BaseModel):
    """The business profile an owner completes before a number is provisioned.

    Every field is required except the two optional address lines: the record is
    only marked complete when the whole form validates, and completion is what
    opens the provisioning gate.
    """

    legal_name: str = Field(min_length=1, max_length=200)
    address_line1: str = Field(min_length=1, max_length=200)
    address_line2: str | None = Field(default=None, max_length=200)
    city: str = Field(min_length=1, max_length=120)
    region: str | None = Field(default=None, max_length=120)
    postal_code: str = Field(min_length=1, max_length=32)
    country: str = Field(min_length=2, max_length=2)
    contact_email: Email
    contact_phone: str = Field(min_length=1, max_length=32)
    business_name: str = Field(min_length=1, max_length=200)
    timezone: str = Field(min_length=1, max_length=100)
    industry: str = Field(min_length=1, max_length=120)
    what_you_do: str = Field(min_length=10, max_length=2000)

    @field_validator("country")
    @classmethod
    def _iso_country(cls, value: str) -> str:
        code = value.strip().upper()
        if not code.isalpha():
            raise ValueError("country must be a two-letter ISO 3166-1 code")
        return code

    @field_validator("contact_phone")
    @classmethod
    def _e164(cls, value: str) -> str:
        try:
            return normalize_e164(value)
        except ValueError as exc:
            raise ValueError("contact phone must be a valid E.164 number") from exc

    @field_validator("timezone")
    @classmethod
    def _iana_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("timezone must be a valid IANA timezone") from exc
        return value

    @field_validator(
        "legal_name",
        "address_line1",
        "address_line2",
        "city",
        "region",
        "postal_code",
        "business_name",
        "industry",
        "what_you_do",
    )
    @classmethod
    def _strip(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip()


class BusinessProfileResponse(BaseModel):
    legal_name: str
    address_line1: str
    address_line2: str | None = None
    city: str
    region: str | None = None
    postal_code: str
    country: str
    contact_email: str
    contact_phone: str
    business_name: str
    timezone: str
    industry: str
    what_you_do: str
    completed: bool


class OnboardingStateResponse(BaseModel):
    lifecycle: str
    email_verified: bool
    profile_complete: bool
    number_provisioned: bool
    phone_number: str | None = None
    # Present only when billing is on and the owner still has to pay: the
    # profile is done but the number waits on a completed checkout.
    checkout_url: str | None = None
    # Machine-readable list of what still stands between here and a live number,
    # e.g. ["email_not_verified", "business_profile_incomplete", "awaiting_payment"].
    blocking_reasons: list[str] = Field(default_factory=list)
    business_profile: BusinessProfileResponse | None = None
