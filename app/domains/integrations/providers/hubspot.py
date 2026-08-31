"""HubSpot CRM connector (v3 objects API, private-app token auth).

Docs: https://developers.hubspot.com/docs/api/crm/contacts . A private-app access
token is passed as ``Authorization: Bearer``; no OAuth flow is required.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx

from ..base import CrmContact, CrmProvider, CrmProviderError

HUBSPOT_API_BASE = "https://api.hubapi.com"
_NOTE_TO_CONTACT = 202
_TASK_TO_CONTACT = 204


def _epoch_ms(value: datetime | None) -> int:
    moment = value or datetime.now(UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return int(moment.timestamp() * 1000)


def _split_name(name: str | None) -> tuple[str, str]:
    parts = (name or "").strip().split(None, 1)
    if not parts:
        return "", ""
    return (parts[0], parts[1] if len(parts) > 1 else "")


class HubSpotCrm(CrmProvider):
    provider = "hubspot"

    def __init__(
        self,
        access_token: str,
        *,
        base_url: str = HUBSPOT_API_BASE,
        timeout: float = 10.0,
        client_factory: Callable[..., httpx.Client] = httpx.Client,
    ) -> None:
        self._token = access_token.strip()
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._client_factory = client_factory
        self._client: httpx.Client | None = None

    # -- transport --------------------------------------------------------

    def _http(self) -> httpx.Client:
        if not self._token:
            raise CrmProviderError(
                "provider_not_configured", "HubSpot access token is missing."
            )
        if self._client is None:
            self._client = self._client_factory(
                base_url=self._base_url,
                timeout=self._timeout,
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Content-Type": "application/json",
                },
            )
        return self._client

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> Any:
        try:
            response = self._http().request(
                method, path, params=params, json=json
            )
        except httpx.HTTPError as exc:
            raise CrmProviderError(
                "transport_error", f"Could not reach HubSpot: {exc}", retryable=True
            ) from exc
        if response.status_code >= 400:
            raise CrmProviderError(
                f"hubspot_http_{response.status_code}",
                _error_text(response),
                retryable=response.status_code == 429
                or response.status_code >= 500,
            )
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise CrmProviderError(
                "bad_response", "HubSpot returned a non-JSON response."
            ) from exc

    # -- CrmProvider ---------------------------------------------------

    def verify(self) -> dict[str, Any]:
        data = self._request("GET", "/account-info/v3/details")
        portal = data.get("portalId") if isinstance(data, dict) else None
        if portal is None:
            raise CrmProviderError(
                "unexpected_response", "HubSpot did not return an account id."
            )
        return {"external_account_id": str(portal)}

    def find_contact(
        self, *, phone: str | None = None, email: str | None = None
    ) -> CrmContact | None:
        for prop, value in (("email", email), ("phone", phone)):
            if not value:
                continue
            data = self._request(
                "POST",
                "/crm/v3/objects/contacts/search",
                json={
                    "filterGroups": [
                        {
                            "filters": [
                                {
                                    "propertyName": prop,
                                    "operator": "EQ",
                                    "value": value,
                                }
                            ]
                        }
                    ],
                    "properties": ["firstname", "lastname", "email", "phone"],
                    "limit": 1,
                },
            )
            results = data.get("results") if isinstance(data, dict) else None
            if results:
                return _contact_from(results[0])
        return None

    def upsert_contact(
        self,
        *,
        name: str | None = None,
        phone: str | None = None,
        email: str | None = None,
    ) -> CrmContact:
        existing = self.find_contact(phone=phone, email=email)
        if existing is not None:
            return existing
        first, last = _split_name(name)
        properties: dict[str, str] = {}
        if first:
            properties["firstname"] = first
        if last:
            properties["lastname"] = last
        if email:
            properties["email"] = email
        if phone:
            properties["phone"] = phone
        if not properties:
            raise CrmProviderError(
                "insufficient_detail",
                "Need a name, phone, or email to create a contact.",
            )
        data = self._request(
            "POST", "/crm/v3/objects/contacts", json={"properties": properties}
        )
        return _contact_from(data)

    def add_note(self, *, contact_id: str, body: str) -> str:
        data = self._request(
            "POST",
            "/crm/v3/objects/notes",
            json={
                "properties": {
                    "hs_note_body": body[:65000],
                    "hs_timestamp": _epoch_ms(None),
                },
                "associations": _association(contact_id, _NOTE_TO_CONTACT),
            },
        )
        return str(data.get("id", "")) if isinstance(data, dict) else ""

    def create_task(
        self,
        *,
        contact_id: str | None,
        title: str,
        body: str | None = None,
        due_at: datetime | None = None,
    ) -> str:
        properties: dict[str, Any] = {
            "hs_task_subject": title[:255],
            "hs_task_status": "NOT_STARTED",
            "hs_task_priority": "MEDIUM",
            "hs_task_type": "TODO",
            "hs_timestamp": _epoch_ms(due_at),
        }
        if body:
            properties["hs_task_body"] = body[:65000]
        payload: dict[str, Any] = {"properties": properties}
        if contact_id:
            payload["associations"] = _association(contact_id, _TASK_TO_CONTACT)
        data = self._request("POST", "/crm/v3/objects/tasks", json=payload)
        return str(data.get("id", "")) if isinstance(data, dict) else ""


def _association(contact_id: str, type_id: int) -> list[dict[str, Any]]:
    return [
        {
            "to": {"id": str(contact_id)},
            "types": [
                {
                    "associationCategory": "HUBSPOT_DEFINED",
                    "associationTypeId": type_id,
                }
            ],
        }
    ]


def _contact_from(record: Any) -> CrmContact:
    record = record if isinstance(record, dict) else {}
    props = record.get("properties") or {}
    name = " ".join(
        part
        for part in (props.get("firstname"), props.get("lastname"))
        if part
    ).strip()
    return CrmContact(
        id=str(record.get("id", "")),
        name=name or None,
        email=props.get("email"),
        phone=props.get("phone"),
    )


def _error_text(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return f"HubSpot returned HTTP {response.status_code}."
    if isinstance(body, dict) and body.get("message"):
        return str(body["message"])[:500]
    return f"HubSpot returned HTTP {response.status_code}."
