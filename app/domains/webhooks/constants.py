from __future__ import annotations

# Event types delivered to customer endpoints (roadmap section 7, item 1).
EVENT_CALL_COMPLETED = "call.completed"
EVENT_CALL_TRANSFERRED = "call.transferred"
EVENT_LEAD_CREATED = "lead.created"
# Emitted when the agent books a calendar appointment during a call (Phase 8).
EVENT_APPOINTMENT_BOOKED = "appointment.booked"

EVENT_TYPES: frozenset[str] = frozenset(
    {
        EVENT_CALL_COMPLETED,
        EVENT_CALL_TRANSFERRED,
        EVENT_LEAD_CREATED,
        EVENT_APPOINTMENT_BOOKED,
    }
)

# Delivery lifecycle.
STATUS_PENDING = "pending"
STATUS_DELIVERING = "delivering"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"
STATUS_DEAD = "dead"

RETRYABLE_STATUSES = (STATUS_PENDING, STATUS_FAILED)

DEFAULT_MAX_ATTEMPTS = 6
# Exponential backoff between attempts, in seconds, capped. attempt 1 -> 30s,
# 2 -> 60s, 3 -> 120s, ... 6 -> 960s. A little jitter is added at schedule time.
BACKOFF_BASE_SECONDS = 30
BACKOFF_CAP_SECONDS = 3600

# A delivery claimed by a worker that then died is retried after this long.
STALE_LOCK_SECONDS = 300

DELIVERY_TIMEOUT_SECONDS = 10
RESPONSE_SNIPPET_CHARS = 500

SIGNATURE_HEADER = "X-Callagent-Signature"
EVENT_TYPE_HEADER = "X-Callagent-Event"
EVENT_ID_HEADER = "X-Callagent-Event-Id"
DELIVERY_ID_HEADER = "X-Callagent-Delivery-Id"
ATTEMPT_HEADER = "X-Callagent-Delivery-Attempt"
USER_AGENT = "call-agent-webhooks/1"

SECRET_PREFIX = "whsec_"


class ErrorCode:
    ENDPOINT_NOT_FOUND = "webhook_endpoint_not_found"
    DELIVERY_NOT_FOUND = "webhook_delivery_not_found"
    INVALID_EVENT_TYPE = "invalid_event_type"
    INVALID_URL = "invalid_webhook_url"
