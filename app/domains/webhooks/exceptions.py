from __future__ import annotations

from app.domains.auth.exceptions import APIError

from .constants import ErrorCode


class WebhookEndpointNotFound(APIError):
    status_code = 404
    code = ErrorCode.ENDPOINT_NOT_FOUND

    def __init__(self) -> None:
        super().__init__("Webhook endpoint not found.")


class WebhookDeliveryNotFound(APIError):
    status_code = 404
    code = ErrorCode.DELIVERY_NOT_FOUND

    def __init__(self) -> None:
        super().__init__("Webhook delivery not found.")


class InvalidEventType(APIError):
    status_code = 422
    code = ErrorCode.INVALID_EVENT_TYPE

    def __init__(self, value: str) -> None:
        super().__init__(
            f"Unknown event type {value!r}.",
            field_errors={"event_types": "Contains an unknown event type."},
        )
