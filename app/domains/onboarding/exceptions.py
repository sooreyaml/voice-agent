from __future__ import annotations

from app.domains.auth.exceptions import APIError

from .constants import ErrorCode


class OnboardingNotFound(APIError):
    status_code = 404
    code = ErrorCode.ONBOARDING_NOT_FOUND

    def __init__(self) -> None:
        super().__init__("Onboarding record not found.")


class OnboardingProfileMissing(APIError):
    status_code = 409
    code = ErrorCode.ONBOARDING_PROFILE_MISSING

    def __init__(self) -> None:
        super().__init__("Save a business profile draft before continuing.")


class ProfileSlugMismatch(APIError):
    status_code = 409
    code = ErrorCode.PROFILE_SLUG_MISMATCH

    def __init__(self) -> None:
        super().__init__(
            "An onboarding organization can only have one business profile."
        )


class PhoneNumberConflict(APIError):
    status_code = 409
    code = ErrorCode.PHONE_NUMBER_CONFLICT

    def __init__(self, message: str) -> None:
        super().__init__(message)
