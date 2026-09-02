from __future__ import annotations

import re
from typing import Annotated

from pydantic import AfterValidator, BaseModel, Field

from .constants import (
    MAX_PASSWORD_LENGTH,
    MIN_PASSWORD_LENGTH,
    VERIFY_EMAIL_CODE_LENGTH,
)

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _valid_email(value: str) -> str:
    cleaned = value.strip().lower()
    if not _EMAIL_RE.match(cleaned) or len(cleaned) > 320:
        raise ValueError("enter a valid email address")
    return cleaned


# email-validator is not a dependency here; this is a deliberately loose check
# that rejects obvious junk and normalises case. Deliverability is proven by the
# verification email, not by the regex.
Email = Annotated[str, Field(max_length=320), AfterValidator(_valid_email)]
Password = Field(min_length=MIN_PASSWORD_LENGTH, max_length=MAX_PASSWORD_LENGTH)


class SignupRequest(BaseModel):
    email: Email
    password: str = Password
    organization_name: str = Field(min_length=1, max_length=200)


class LoginRequest(BaseModel):
    email: Email
    password: str = Field(min_length=1, max_length=MAX_PASSWORD_LENGTH)


class EmailRequest(BaseModel):
    email: Email


def _valid_code(value: str) -> str:
    digits = re.sub(r"\D", "", value)
    if len(digits) != VERIFY_EMAIL_CODE_LENGTH:
        raise ValueError(f"enter the {VERIFY_EMAIL_CODE_LENGTH}-digit code")
    return digits


# Accepts "048213", " 048 213 ", "048-213" -- anything with the right digits.
VerificationCode = Annotated[
    str, Field(max_length=32), AfterValidator(_valid_code)
]


class VerifyEmailRequest(BaseModel):
    code: VerificationCode


class PasswordResetConfirmRequest(BaseModel):
    token: str = Field(min_length=1, max_length=512)
    password: str = Password


class UserResponse(BaseModel):
    id: str
    email: str
    email_verified: bool
    is_platform_admin: bool


class OrganizationMembershipResponse(BaseModel):
    id: str
    slug: str
    name: str
    role: str


class OrganizationResponse(BaseModel):
    id: str
    slug: str
    name: str


class MeResponse(BaseModel):
    user: UserResponse
    organizations: list[OrganizationMembershipResponse]


class SignupSubscriptionResponse(BaseModel):
    status: str


class SignupResponse(BaseModel):
    user: UserResponse
    organization: OrganizationResponse
    # Live the instant signup succeeds; null only if the pool was empty.
    phone_number: str | None = None
    # Present when billing is configured; the tenant must finish checkout_url
    # to keep the number past the signup grace window.
    subscription: SignupSubscriptionResponse | None = None
    checkout_url: str | None = None


class MessageResponse(BaseModel):
    message: str
