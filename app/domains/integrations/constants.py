from __future__ import annotations

# Providers this deployment knows how to talk to. Keep in sync with
# ``registry._BUILDERS`` and the schema in ``schemas.py``.
PROVIDER_CAL_COM = "cal_com"
PROVIDER_HUBSPOT = "hubspot"
CALENDAR_PROVIDERS: frozenset[str] = frozenset({PROVIDER_CAL_COM})
CRM_PROVIDERS: frozenset[str] = frozenset({PROVIDER_HUBSPOT})
SUPPORTED_PROVIDERS: frozenset[str] = CALENDAR_PROVIDERS | CRM_PROVIDERS

# integration_connections.status
STATUS_ACTIVE = "active"
STATUS_ERROR = "error"
STATUS_REVOKED = "revoked"

# Voice-tool guardrails (roadmap section 8): a slow or unavailable provider must
# never leave the caller in silence, so every provider call is bounded and a
# failure degrades to capturing a follow-up.
TOOL_TIMEOUT_SECONDS = 8.0
AVAILABILITY_LOOKAHEAD_DAYS = 21
MAX_SLOTS_RETURNED = 6
DEFAULT_APPOINTMENT_MINUTES = 30

# Post-call CRM sync (background worker). Same backoff shape as webhook delivery.
CRM_SYNC_MAX_ATTEMPTS = 6
CRM_SYNC_BACKOFF_BASE_SECONDS = 30
CRM_SYNC_BACKOFF_CAP_SECONDS = 3600
CRM_SYNC_STALE_LOCK_SECONDS = 300

# Dev-only fallback so tests and local runs need no key. Any non-development
# environment must supply INTEGRATION_ENCRYPTION_KEY (enforced in lifespan).
DEV_CIPHER_SEED = b"call-agent-dev-integration-cipher"


class ErrorCode:
    UNKNOWN_PROVIDER = "integration_unknown_provider"
    NOT_FOUND = "integration_not_found"
    ENCRYPTION_NOT_CONFIGURED = "integration_encryption_not_configured"
    PROVIDER_REJECTED = "integration_provider_rejected"
    INVALID_CREDENTIALS = "integration_invalid_credentials"
