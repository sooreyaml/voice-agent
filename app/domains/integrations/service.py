"""Integration connection lifecycle and the runtime provider loaders.

FastAPI-free: raw-SQL ``Store`` access only, so the call runtime and the worker
can reuse ``load_calendar_provider`` / ``load_crm_provider``.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from app.domains.audit.models import AuditAction
from app.domains.auth.dependencies import CurrentUser
from app.store import Store

from . import registry
from .base import CalendarProvider, CrmProvider, ProviderError
from .constants import (
    CALENDAR_PROVIDERS,
    CRM_PROVIDERS,
    STATUS_ACTIVE,
    STATUS_ERROR,
    SUPPORTED_PROVIDERS,
    TOOL_TIMEOUT_SECONDS,
)
from .crypto import CredentialCipher
from .exceptions import (
    IntegrationInvalidCredentials,
    IntegrationNotFound,
    IntegrationProviderRejected,
    UnknownProvider,
)
from .schemas import ConnectIntegrationRequest

logger = logging.getLogger(__name__)

# Admin connect/test calls can afford a longer wait than an in-call tool.
PROBE_TIMEOUT_SECONDS = 15.0


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _load_json(value: Any) -> dict[str, Any]:
    if not value:
        return {}
    try:
        decoded = json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _safe_payload(row: dict[str, Any]) -> dict[str, Any]:
    """Everything a client may see — never the credential blob or raw scopes."""
    return {
        "id": str(row["id"]),
        "organization_id": str(row["organization_id"]),
        "provider": str(row["provider"]),
        "status": str(row["status"]),
        "display_name": row.get("display_name"),
        "external_account_id": row.get("external_account_id"),
        "settings": _load_json(row.get("settings")),
        "last_error": row.get("last_error"),
        "last_verified_at": row.get("last_verified_at"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


def _require_provider(provider: str) -> None:
    if provider not in SUPPORTED_PROVIDERS:
        raise UnknownProvider(provider)


def _is_auth_error(exc: ProviderError) -> bool:
    return exc.code.endswith(("_http_401", "_http_403")) or (
        exc.code == "provider_not_configured"
    )


def _provider_error(exc: ProviderError) -> Exception:
    if _is_auth_error(exc):
        return IntegrationInvalidCredentials(exc.message)
    return IntegrationProviderRejected(
        f"The provider rejected the request: {exc.message}"
    )


def _credentials_and_settings(
    provider: str, body: ConnectIntegrationRequest
) -> tuple[dict[str, Any], dict[str, Any]]:
    if provider in CALENDAR_PROVIDERS:
        if not body.api_key or not body.event_type_id:
            raise IntegrationInvalidCredentials(
                "api_key and event_type_id are required for this provider.",
            )
        return (
            {"api_key": body.api_key},
            {"event_type_id": body.event_type_id, "timezone": body.timezone},
        )
    if provider in CRM_PROVIDERS:
        if not body.access_token:
            raise IntegrationInvalidCredentials(
                "access_token is required for this provider."
            )
        return {"access_token": body.access_token}, {}
    raise UnknownProvider(provider)  # pragma: no cover - _require_provider ran


# -- read ---------------------------------------------------------------


def list_connections(store: Store, organization_id: str) -> list[dict[str, Any]]:
    return [
        _safe_payload(row)
        for row in store.list_integration_connections(organization_id)
    ]


def get_connection(
    store: Store, organization_id: str, provider: str
) -> dict[str, Any]:
    _require_provider(provider)
    row = store.integration_connection(organization_id, provider)
    if row is None:
        raise IntegrationNotFound()
    return _safe_payload(row)


# -- write --------------------------------------------------------------


def connect(
    store: Store,
    cipher: CredentialCipher,
    admin: CurrentUser,
    organization_id: str,
    provider: str,
    body: ConnectIntegrationRequest,
    *,
    ip: str | None,
) -> dict[str, Any]:
    _require_provider(provider)
    credentials, settings = _credentials_and_settings(provider, body)

    client = registry.build_provider(
        provider, credentials, settings, timeout=PROBE_TIMEOUT_SECONDS
    )
    try:
        verified = client.verify()
    except ProviderError as exc:
        raise _provider_error(exc) from exc

    external_account_id = (
        str(verified["external_account_id"])
        if verified.get("external_account_id")
        else None
    )
    if external_account_id and provider in CRM_PROVIDERS:
        settings["portal_id"] = external_account_id

    now = _utcnow()
    store.upsert_integration_connection(
        organization_id,
        provider,
        status=STATUS_ACTIVE,
        display_name=body.display_name,
        encrypted_credentials=cipher.seal(credentials),
        external_account_id=external_account_id,
        scopes=None,
        settings=json.dumps(settings, sort_keys=True),
        last_error=None,
        last_verified_at=now,
    )
    store.record_audit(
        AuditAction.INTEGRATION_CONNECTED.value,
        organization_id=organization_id,
        actor_user_id=admin.id,
        target_type="integration_connection",
        target_id=provider,
        metadata={"provider": provider, "external_account_id": external_account_id},
        ip=ip,
    )
    return get_connection(store, organization_id, provider)


def test_connection(
    store: Store, cipher: CredentialCipher, organization_id: str, provider: str
) -> dict[str, Any]:
    _require_provider(provider)
    row = store.integration_connection(organization_id, provider)
    if row is None:
        raise IntegrationNotFound()

    client = registry.build_provider(
        provider,
        cipher.open(str(row["encrypted_credentials"])),
        _load_json(row.get("settings")),
        timeout=PROBE_TIMEOUT_SECONDS,
    )
    checked_at = _utcnow()
    try:
        verified = client.verify()
    except ProviderError as exc:
        store.set_integration_status(
            organization_id,
            provider,
            status=STATUS_ERROR,
            last_error=exc.message,
            last_verified_at=None,
        )
        return {
            "ok": False,
            "external_account_id": row.get("external_account_id"),
            "checked_at": checked_at,
            "detail": exc.message,
        }
    store.set_integration_status(
        organization_id,
        provider,
        status=STATUS_ACTIVE,
        last_error=None,
        last_verified_at=checked_at,
    )
    return {
        "ok": True,
        "external_account_id": verified.get("external_account_id")
        or row.get("external_account_id"),
        "checked_at": checked_at,
        "detail": None,
    }


def disconnect(
    store: Store,
    admin: CurrentUser,
    organization_id: str,
    provider: str,
    *,
    ip: str | None,
) -> None:
    _require_provider(provider)
    if not store.delete_integration_connection(organization_id, provider):
        raise IntegrationNotFound()
    store.record_audit(
        AuditAction.INTEGRATION_DISCONNECTED.value,
        organization_id=organization_id,
        actor_user_id=admin.id,
        target_type="integration_connection",
        target_id=provider,
        metadata={"provider": provider},
        ip=ip,
    )


# -- runtime ----------------------------------------------------------


def _load_provider(
    store: Store,
    cipher: CredentialCipher,
    organization_id: str,
    providers: frozenset[str],
) -> Any | None:
    for row in store.active_integration_connections(organization_id):
        if str(row["provider"]) not in providers:
            continue
        try:
            return registry.build_provider(
                str(row["provider"]),
                cipher.open(str(row["encrypted_credentials"])),
                _load_json(row.get("settings")),
                timeout=TOOL_TIMEOUT_SECONDS,
            )
        except (RuntimeError, KeyError, ValueError, TypeError):
            logger.exception(
                "integration for org %s could not be loaded", organization_id
            )
            return None
    return None


def load_calendar_provider(
    store: Store, cipher: CredentialCipher, organization_id: str
) -> CalendarProvider | None:
    """The connected calendar for a call, or None. Never raises for 'not
    connected'; a broken stored connection surfaces as a log line and None.
    """
    return _load_provider(store, cipher, organization_id, CALENDAR_PROVIDERS)


def load_crm_provider(
    store: Store, cipher: CredentialCipher, organization_id: str
) -> CrmProvider | None:
    """The connected CRM for an organization, or None (same contract as above)."""
    return _load_provider(store, cipher, organization_id, CRM_PROVIDERS)
