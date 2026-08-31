from __future__ import annotations

from app.domains.auth.exceptions import APIError

from .constants import ErrorCode


class UnknownProvider(APIError):
    status_code = 404
    code = ErrorCode.UNKNOWN_PROVIDER

    def __init__(self, provider: str) -> None:
        super().__init__(f"Unknown integration provider {provider!r}.")


class IntegrationNotFound(APIError):
    status_code = 404
    code = ErrorCode.NOT_FOUND

    def __init__(self) -> None:
        super().__init__("This organization has no such integration connected.")


class IntegrationEncryptionNotConfigured(APIError):
    status_code = 503
    code = ErrorCode.ENCRYPTION_NOT_CONFIGURED

    def __init__(self, message: str = "Integration encryption is not configured.") -> None:
        super().__init__(message)


class IntegrationProviderRejected(APIError):
    status_code = 502
    code = ErrorCode.PROVIDER_REJECTED

    def __init__(self, message: str) -> None:
        super().__init__(message)


class IntegrationInvalidCredentials(APIError):
    status_code = 422
    code = ErrorCode.INVALID_CREDENTIALS

    def __init__(
        self, message: str = "The provider rejected those credentials."
    ) -> None:
        super().__init__(message, field_errors={"api_key": "Rejected by the provider."})
