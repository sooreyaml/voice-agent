from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, Field

from app.domains.businesses.normalization import normalize_e164

CountryCode = Annotated[
    str,
    BeforeValidator(lambda value: str(value).strip().upper()),
    Field(pattern=r"^[A-Z]{2}$"),
]
E164 = Annotated[str, BeforeValidator(normalize_e164)]
NumberType = Literal["local", "mobile", "national", "toll_free"]
ProviderSid = Annotated[
    str,
    Field(pattern=r"^[A-Z]{2}[0-9a-fA-F]{32}$"),
]


class AvailablePhoneNumberResponse(BaseModel):
    phone_number: str
    friendly_name: str
    country_code: str
    locality: str | None = None
    region: str | None = None
    postal_code: str | None = None
    address_requirements: Literal["none", "any", "local", "foreign"]
    beta: bool
    capabilities: dict[str, Any]


class AvailablePhoneNumbersResponse(BaseModel):
    items: list[AvailablePhoneNumberResponse]


class RegulatoryRequirementResponse(BaseModel):
    sid: str
    friendly_name: str
    country_code: str
    number_type: str
    end_user_type: str
    requirements: dict[str, Any]


class RegulatoryRequirementsResponse(BaseModel):
    items: list[RegulatoryRequirementResponse]


class ProvisionPhoneNumberRequest(BaseModel):
    idempotency_key: str = Field(
        min_length=8,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    country_code: CountryCode
    number_type: NumberType
    phone_number: E164
    purchase_approved: Literal[True]
    address_sid: ProviderSid | None = None
    bundle_sid: ProviderSid | None = None
    identity_sid: ProviderSid | None = None
    trunk_domain: str | None = Field(
        default=None,
        max_length=255,
        pattern=r"^[A-Za-z0-9-]+\.pstn\.twilio\.com$",
    )


class ProvisioningResponse(BaseModel):
    id: str
    organization_id: str
    business_profile_id: str
    idempotency_key: str
    country_code: str
    number_type: str
    requested_phone_number: str
    status: Literal["provisioning", "ready", "failed", "verified"]
    attempts: int
    provider_phone_number_sid: str | None = None
    provider_trunk_sid: str | None = None
    phone_number: str | None = None
    last_error_code: str | None = None
    last_error_message: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    completed_at: datetime | None = None
    tested_at: datetime | None = None


class VerifyTestCallRequest(BaseModel):
    call_id: str | None = Field(default=None, min_length=1, max_length=255)
