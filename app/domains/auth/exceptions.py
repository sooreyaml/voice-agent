from __future__ import annotations

from typing import Any

from .constants import ErrorCode


class APIError(Exception):
    """A failure that should be rendered as the shared JSON error envelope.

    ``code`` is the stable machine-readable string API clients switch on;
    ``message`` is safe fallback copy; ``field_errors`` maps an input field to
    its problem for validation failures.
    """

    status_code: int = 400
    code: str = ErrorCode.VALIDATION_FAILED

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        status_code: int | None = None,
        field_errors: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        if status_code is not None:
            self.status_code = status_code
        self.field_errors: dict[str, str] = field_errors or {}
        self.headers = headers

    def envelope(self, request_id: str) -> dict[str, Any]:
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "field_errors": self.field_errors,
                "request_id": request_id,
            }
        }


class NotAuthenticated(APIError):
    status_code = 401
    code = ErrorCode.NOT_AUTHENTICATED

    def __init__(self, message: str = "Sign in to continue.") -> None:
        super().__init__(message)


class CsrfFailed(APIError):
    status_code = 403
    code = ErrorCode.CSRF_FAILED

    def __init__(self) -> None:
        super().__init__("Your session could not be verified. Reload and retry.")


class InvalidCredentials(APIError):
    status_code = 401
    code = ErrorCode.INVALID_CREDENTIALS

    def __init__(self) -> None:
        super().__init__("That email and password do not match.")


class Forbidden(APIError):
    status_code = 403
    code = ErrorCode.FORBIDDEN

    def __init__(self, message: str = "You do not have access to this.") -> None:
        super().__init__(message)


class OrganizationNotFound(APIError):
    status_code = 404
    code = ErrorCode.ORGANIZATION_NOT_FOUND

    def __init__(self) -> None:
        super().__init__("Organization not found.")


class NotFound(APIError):
    status_code = 404
    code = ErrorCode.NOT_FOUND

    def __init__(self, message: str = "Not found.") -> None:
        super().__init__(message)


class EmailTaken(APIError):
    status_code = 409
    code = ErrorCode.EMAIL_TAKEN

    def __init__(self) -> None:
        super().__init__(
            "An account with that email already exists.",
            field_errors={"email": "Already registered."},
        )


class InvalidToken(APIError):
    status_code = 400
    code = ErrorCode.INVALID_TOKEN

    def __init__(self) -> None:
        super().__init__("This link is invalid or has expired.")
