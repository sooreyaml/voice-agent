from __future__ import annotations

from datetime import timedelta

SESSION_COOKIE = "session"
CSRF_COOKIE = "csrf"
CSRF_HEADER = "x-csrf-token"

SESSION_TTL = timedelta(days=14)
VERIFY_EMAIL_TTL = timedelta(hours=24)
RESET_PASSWORD_TTL = timedelta(hours=1)

# Used to key the at-rest hash of session and email tokens when no
# AUTH_SESSION_SECRET is configured. Only reachable in local development;
# app startup requires a real secret in staging and production.
DEV_TOKEN_KEY = "call-agent-dev-insecure-token-key"

MIN_PASSWORD_LENGTH = 10
MAX_PASSWORD_LENGTH = 200

INVITATION_TTL = timedelta(days=7)

# Membership roles, lowest to highest privilege.
ROLE_RANK = {"viewer": 0, "member": 1, "admin": 2, "owner": 3}


class ErrorCode:
    INVALID_CREDENTIALS = "invalid_credentials"
    NOT_AUTHENTICATED = "not_authenticated"
    CSRF_FAILED = "csrf_failed"
    EMAIL_TAKEN = "email_taken"
    EMAIL_NOT_VERIFIED = "email_not_verified"
    INVALID_TOKEN = "invalid_token"
    FORBIDDEN = "forbidden"
    ORGANIZATION_NOT_FOUND = "organization_not_found"
    NOT_FOUND = "not_found"
    VALIDATION_FAILED = "validation_failed"
    INTERNAL_ERROR = "internal_error"
