"""API-key lifecycle and verification.

FastAPI-free. The raw key is ``cak_<token>``; only ``hash_token(raw, secret)`` is
persisted, so a leaked database row cannot be replayed as a credential.
"""

from __future__ import annotations

import json
import secrets
from datetime import UTC, datetime
from typing import Any

from app.domains.audit.models import AuditAction
from app.domains.auth.dependencies import CurrentUser
from app.domains.auth.service import hash_token
from app.settings import Settings
from app.store import Store

from .constants import (
    DISPLAY_PREFIX_CHARS,
    KEY_BODY_BYTES,
    KEY_PREFIX,
    MAX_KEYS_PER_ORG,
)
from .exceptions import ApiKeyNotFound, TooManyApiKeys


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _new_raw_key() -> tuple[str, str]:
    raw = KEY_PREFIX + secrets.token_urlsafe(KEY_BODY_BYTES)
    return raw, raw[:DISPLAY_PREFIX_CHARS]


def _safe_payload(row: dict[str, Any]) -> dict[str, Any]:
    scopes = row.get("scopes")
    try:
        parsed = json.loads(scopes) if isinstance(scopes, str) else (scopes or [])
    except (TypeError, ValueError):
        parsed = []
    return {
        "id": str(row["id"]),
        "organization_id": str(row["organization_id"]),
        "name": str(row["name"]),
        "prefix": str(row["prefix"]),
        "scopes": sorted(parsed) if isinstance(parsed, list) else [],
        "created_by_user_id": row.get("created_by_user_id"),
        "last_used_at": row.get("last_used_at"),
        "revoked_at": row.get("revoked_at"),
        "created_at": row.get("created_at"),
    }


class _RateLimiterCompatibility:
    """Kept for callers that used the old test-reset hook.

    Rate limiting itself now lives on ``app.state.runtime_state`` so every API
    replica observes the same counters.
    """

    def reset(self) -> None:
        return None


rate_limiter = _RateLimiterCompatibility()


# -- lifecycle ------------------------------------------------------


def create_key(
    store: Store,
    settings: Settings,
    user: CurrentUser,
    organization_id: str,
    *,
    name: str,
    scopes: list[str],
    ip: str | None,
) -> dict[str, Any]:
    active = [
        row
        for row in store.list_api_keys(organization_id)
        if row.get("revoked_at") is None
    ]
    if len(active) >= MAX_KEYS_PER_ORG:
        raise TooManyApiKeys(MAX_KEYS_PER_ORG)

    raw, prefix = _new_raw_key()
    key_id = store.create_api_key(
        organization_id,
        name=name.strip(),
        prefix=prefix,
        token_hash=hash_token(raw, settings.auth_session_secret),
        scopes=json.dumps(sorted(scopes)),
        created_by_user_id=user.id,
    )
    store.record_audit(
        AuditAction.API_KEY_CREATED.value,
        organization_id=organization_id,
        actor_user_id=user.id,
        target_type="api_key",
        target_id=key_id,
        metadata={"name": name.strip(), "scopes": sorted(scopes)},
        ip=ip,
    )
    row = store.api_key(organization_id, key_id)
    assert row is not None
    return {**_safe_payload(row), "key": raw}


def list_keys(store: Store, organization_id: str) -> list[dict[str, Any]]:
    return [_safe_payload(row) for row in store.list_api_keys(organization_id)]


def revoke_key(
    store: Store,
    user: CurrentUser,
    organization_id: str,
    key_id: str,
    *,
    ip: str | None,
) -> None:
    if not store.revoke_api_key(organization_id, key_id):
        raise ApiKeyNotFound()
    store.record_audit(
        AuditAction.API_KEY_REVOKED.value,
        organization_id=organization_id,
        actor_user_id=user.id,
        target_type="api_key",
        target_id=key_id,
        ip=ip,
    )


def rotate_key(
    store: Store,
    settings: Settings,
    user: CurrentUser,
    organization_id: str,
    key_id: str,
    *,
    ip: str | None,
) -> dict[str, Any]:
    row = store.api_key(organization_id, key_id)
    if row is None or row.get("revoked_at") is not None:
        raise ApiKeyNotFound()
    raw, prefix = _new_raw_key()
    store.rotate_api_key(
        organization_id,
        key_id,
        prefix=prefix,
        token_hash=hash_token(raw, settings.auth_session_secret),
    )
    store.record_audit(
        AuditAction.API_KEY_ROTATED.value,
        organization_id=organization_id,
        actor_user_id=user.id,
        target_type="api_key",
        target_id=key_id,
        ip=ip,
    )
    fresh = store.api_key(organization_id, key_id)
    assert fresh is not None
    return {**_safe_payload(fresh), "key": raw}


# -- verification (request path) -----------------------------------


def authenticate(
    store: Store, settings: Settings, raw_key: str
) -> dict[str, Any] | None:
    """Resolve a bearer value to its key row, or None. Touches ``last_used_at``
    on success (at most once per minute to avoid a write per request).
    """
    raw_key = (raw_key or "").strip()
    if not raw_key.startswith(KEY_PREFIX):
        return None
    row = store.api_key_by_hash(hash_token(raw_key, settings.auth_session_secret))
    if row is None or row.get("revoked_at") is not None:
        return None
    last_used = row.get("last_used_at")
    if _stale(last_used):
        store.touch_api_key(str(row["id"]))
    scopes = row.get("scopes")
    try:
        parsed = json.loads(scopes) if isinstance(scopes, str) else (scopes or [])
    except (TypeError, ValueError):
        parsed = []
    row["scope_set"] = frozenset(parsed if isinstance(parsed, list) else [])
    return row


def _stale(last_used: Any) -> bool:
    if last_used is None:
        return True
    if isinstance(last_used, str):
        try:
            last_used = datetime.fromisoformat(last_used)
        except ValueError:
            return True
    if last_used.tzinfo is None:
        last_used = last_used.replace(tzinfo=UTC)
    return (_utcnow() - last_used).total_seconds() > 60
