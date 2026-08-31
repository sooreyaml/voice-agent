from __future__ import annotations

from app.domains.auth.exceptions import APIError

from .constants import ErrorCode


class BillingProviderNotConfigured(APIError):
    status_code = 503
    code = ErrorCode.PROVIDER_NOT_CONFIGURED

    def __init__(self) -> None:
        super().__init__("Stripe billing is not configured on this deployment.")


class BillingProviderUnavailable(APIError):
    status_code = 502
    code = ErrorCode.PROVIDER_UNAVAILABLE

    def __init__(self) -> None:
        super().__init__("Stripe could not complete the request. Retry shortly.")


class BillingPlanNotFound(APIError):
    status_code = 404
    code = ErrorCode.PLAN_NOT_FOUND

    def __init__(self) -> None:
        super().__init__("Billing plan not found.")


class BillingPlanConflict(APIError):
    status_code = 409
    code = ErrorCode.PLAN_CONFLICT

    def __init__(self) -> None:
        super().__init__("The billing plan code or Stripe price is already in use.")


class ActiveSubscriptionConflict(APIError):
    status_code = 409
    code = ErrorCode.ACTIVE_SUBSCRIPTION

    def __init__(self) -> None:
        super().__init__("This organization already has an active subscription.")


class BillingSubscriptionNotFound(APIError):
    status_code = 409
    code = ErrorCode.SUBSCRIPTION_NOT_FOUND

    def __init__(self) -> None:
        super().__init__("The organization does not have a Stripe customer yet.")


class UsageEventNotFound(APIError):
    status_code = 404
    code = ErrorCode.USAGE_EVENT_NOT_FOUND

    def __init__(self) -> None:
        super().__init__("Usage event not found.")


class BillingIdempotencyConflict(APIError):
    status_code = 409
    code = ErrorCode.IDEMPOTENCY_CONFLICT

    def __init__(self) -> None:
        super().__init__("That idempotency key was used for different usage.")


class SpendLimitExceeded(APIError):
    status_code = 402
    code = ErrorCode.SPEND_LIMIT_EXCEEDED

    def __init__(self) -> None:
        super().__init__(
            "This organization's monthly spend limit has been reached."
        )
