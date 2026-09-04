from __future__ import annotations

from app.domains.auth.exceptions import APIError

from .constants import ErrorCode


class EmailNotVerified(APIError):
    """The signed-in owner must confirm their email before this step."""

    status_code = 403
    code = ErrorCode.EMAIL_NOT_VERIFIED

    def __init__(self) -> None:
        super().__init__(
            "Verify your email address before setting up your phone number. "
            "Check your inbox for the code, or request a new one."
        )


class BusinessProfileIncomplete(APIError):
    """Number provisioning was requested before the business profile was done."""

    status_code = 409
    code = ErrorCode.BUSINESS_PROFILE_INCOMPLETE

    def __init__(self) -> None:
        super().__init__(
            "Complete your business profile before we can assign a phone number."
        )


class OnboardingAlreadyActive(APIError):
    """The organization already has a live number and agent."""

    status_code = 409
    code = ErrorCode.ONBOARDING_ALREADY_ACTIVE

    def __init__(self) -> None:
        super().__init__("This organization already has a phone number.")


class NumberProvisioningFailed(APIError):
    status_code = 503
    code = ErrorCode.NUMBER_PROVISIONING_FAILED

    def __init__(self) -> None:
        super().__init__(
            "We couldn't assign a phone number right now. Your business profile "
            "is saved — try again in a moment."
        )
