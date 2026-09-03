from __future__ import annotations

from app.domains.auth.exceptions import APIError

from .constants import ErrorCode


class ApiKeyNotFound(APIError):
    status_code = 404
    code = ErrorCode.NOT_FOUND

    def __init__(self) -> None:
        super().__init__("We couldn't find that API key.")


class InvalidApiKey(APIError):
    status_code = 401
    code = ErrorCode.INVALID_KEY

    def __init__(self, message: str = "The API key is invalid or has been revoked.") -> None:
        super().__init__(message)


class UnknownScope(APIError):
    status_code = 422
    code = ErrorCode.UNKNOWN_SCOPE

    def __init__(self, scopes: list[str]) -> None:
        super().__init__(
            f"Unknown scope(s): {', '.join(scopes)}.",
            field_errors={"scopes": "Contains an unknown scope."},
        )


class MissingScope(APIError):
    status_code = 403
    code = ErrorCode.MISSING_SCOPE

    def __init__(self, scopes: list[str]) -> None:
        super().__init__(
            f"This API key is missing the required scope(s): {', '.join(scopes)}."
        )


class TooManyApiKeys(APIError):
    status_code = 409
    code = ErrorCode.TOO_MANY

    def __init__(self, limit: int) -> None:
        super().__init__(f"An organization may have at most {limit} active API keys.")


class ApiKeyRateLimited(APIError):
    status_code = 429
    code = ErrorCode.RATE_LIMITED

    def __init__(self, retry_after: int) -> None:
        super().__init__(
            "API rate limit exceeded for this key. Slow down and retry.",
            headers={"Retry-After": str(max(retry_after, 1))},
        )
