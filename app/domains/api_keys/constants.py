from __future__ import annotations

# Public-API scopes. A scope is `<resource>:<action>`; cookie/session principals
# implicitly hold every scope (role checks gate them separately).
SCOPE_CALLS_READ = "calls:read"
SCOPE_LEADS_READ = "leads:read"
SCOPE_LEADS_WRITE = "leads:write"

ALL_SCOPES: frozenset[str] = frozenset(
    {SCOPE_CALLS_READ, SCOPE_LEADS_READ, SCOPE_LEADS_WRITE}
)

# `cak_` = "call-agent key". token_urlsafe(32) -> ~43 chars of body.
KEY_PREFIX = "cak_"
KEY_BODY_BYTES = 32
# Stored for display / identification in listings (never the whole key).
DISPLAY_PREFIX_CHARS = 12

MAX_KEYS_PER_ORG = 25
DEFAULT_RATE_LIMIT_PER_MINUTE = 120


class ErrorCode:
    NOT_FOUND = "api_key_not_found"
    INVALID_KEY = "api_key_invalid"
    UNKNOWN_SCOPE = "api_key_unknown_scope"
    MISSING_SCOPE = "api_key_missing_scope"
    TOO_MANY = "api_key_limit_reached"
    RATE_LIMITED = "api_key_rate_limited"
