from __future__ import annotations

from app.domains.auth.exceptions import APIError

from .constants import ErrorCode


class ProviderNotConfigured(APIError):
    status_code = 503
    code = ErrorCode.PROVIDER_NOT_CONFIGURED

    def __init__(self) -> None:
        super().__init__("Twilio provisioning is not configured on this deployment.")


class ProviderUnavailable(APIError):
    status_code = 502
    code = ErrorCode.PROVIDER_UNAVAILABLE

    def __init__(self, message: str) -> None:
        super().__init__(message)


class ProfileRequired(APIError):
    status_code = 409
    code = ErrorCode.PROFILE_REQUIRED

    def __init__(self) -> None:
        super().__init__("Save a business profile draft before selecting a number.")


class NumberNotSelected(APIError):
    status_code = 409
    code = ErrorCode.NUMBER_NOT_SELECTED

    def __init__(self) -> None:
        super().__init__(
            "The selected number must be present in the business profile draft."
        )


class PhoneNumberConflict(APIError):
    status_code = 409
    code = ErrorCode.PHONE_NUMBER_CONFLICT

    def __init__(self) -> None:
        super().__init__("That phone number is already assigned to another business.")


class IdempotencyConflict(APIError):
    status_code = 409
    code = ErrorCode.IDEMPOTENCY_CONFLICT

    def __init__(self) -> None:
        super().__init__("That idempotency key was used for a different purchase.")


class ProvisioningNotFound(APIError):
    status_code = 404
    code = ErrorCode.PROVISIONING_NOT_FOUND

    def __init__(self) -> None:
        super().__init__("No telephone-number provisioning request was found.")


class ProvisioningNotReady(APIError):
    status_code = 409
    code = ErrorCode.PROVISIONING_NOT_READY

    def __init__(self) -> None:
        super().__init__("The telephone number has not finished provisioning.")


class TestCallNotFound(APIError):
    status_code = 409
    code = ErrorCode.TEST_CALL_NOT_FOUND

    def __init__(self) -> None:
        super().__init__(
            "No completed inbound test call was found for the provisioned number."
        )
