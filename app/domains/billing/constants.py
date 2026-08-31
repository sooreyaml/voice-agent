from __future__ import annotations

USAGE_SOURCE_INTERNAL = "internal"
USAGE_SOURCE_OPENAI = "openai"
USAGE_SOURCE_STRIPE = "stripe"
USAGE_SOURCE_TWILIO = "twilio"


class ErrorCode:
    ACTIVE_SUBSCRIPTION = "billing_active_subscription"
    IDEMPOTENCY_CONFLICT = "billing_idempotency_conflict"
    PLAN_CONFLICT = "billing_plan_conflict"
    PLAN_NOT_FOUND = "billing_plan_not_found"
    PROVIDER_NOT_CONFIGURED = "billing_provider_not_configured"
    PROVIDER_UNAVAILABLE = "billing_provider_unavailable"
    SUBSCRIPTION_NOT_FOUND = "billing_subscription_not_found"
    USAGE_EVENT_NOT_FOUND = "billing_usage_event_not_found"
    WEBHOOK_INVALID = "billing_webhook_invalid"
    SPEND_LIMIT_EXCEEDED = "billing_spend_limit_exceeded"
