"""Cal.com calendar connector (API v2, API-key auth).

Docs: https://cal.com/docs/api-reference/v2 . The response envelope is
``{"status": "...", "data": ...}``; slot payloads have varied by API version so
parsing is deliberately permissive.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from ..base import Booking, CalendarProvider, CalendarProviderError, TimeSlot

CAL_API_BASE = "https://api.cal.com/v2"
CAL_API_VERSION = "2024-08-13"
_NON_DIGITS = re.compile(r"\D+")


def _parse_dt(value: Any) -> datetime:
    text = str(value).strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _iso_z(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _synthesise_email(phone: str | None, name: str) -> str:
    digits = _NON_DIGITS.sub("", phone or "")
    handle = digits or _NON_DIGITS.sub("", name.lower()) or "caller"
    return f"{handle}@voice.calls.invalid"


class CalComCalendar(CalendarProvider):
    provider = "cal_com"

    def __init__(
        self,
        api_key: str,
        *,
        event_type_id: int,
        timezone: str = "UTC",
        base_url: str = CAL_API_BASE,
        timeout: float = 10.0,
        client_factory: Callable[..., httpx.Client] = httpx.Client,
    ) -> None:
        self._api_key = api_key.strip()
        self._event_type_id = int(event_type_id)
        self._timezone = timezone or "UTC"
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._client_factory = client_factory
        self._client: httpx.Client | None = None

    # -- transport ----------------------------------------------------------

    def _http(self) -> httpx.Client:
        if not self._api_key:
            raise CalendarProviderError(
                "provider_not_configured", "Cal.com API key is missing."
            )
        if self._client is None:
            self._client = self._client_factory(
                base_url=self._base_url,
                timeout=self._timeout,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "cal-api-version": CAL_API_VERSION,
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
            raise CalendarProviderError(
                "transport_error", f"Could not reach Cal.com: {exc}", retryable=True
            ) from exc
        if response.status_code >= 400:
            raise CalendarProviderError(
                f"cal_com_http_{response.status_code}",
                _error_text(response),
                retryable=response.status_code == 429
                or response.status_code >= 500,
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise CalendarProviderError(
                "bad_response", "Cal.com returned a non-JSON response."
            ) from exc
        if isinstance(body, dict) and "data" in body:
            return body["data"]
        return body

    # -- CalendarProvider -------------------------------------------------

    def verify(self) -> dict[str, Any]:
        data = self._request("GET", "/me")
        account = data if isinstance(data, dict) else {}
        identifier = (
            account.get("username")
            or account.get("email")
            or (str(account.get("id")) if account.get("id") is not None else None)
        )
        if not identifier:
            raise CalendarProviderError(
                "unexpected_response", "Cal.com did not identify the account."
            )
        return {
            "external_account_id": str(identifier),
            "email": account.get("email"),
            "event_type_id": self._event_type_id,
        }

    def available_slots(
        self, *, start: datetime, end: datetime, duration_minutes: int
    ) -> list[TimeSlot]:
        data = self._request(
            "GET",
            "/slots",
            params={
                "eventTypeId": self._event_type_id,
                "start": _iso_z(start),
                "end": _iso_z(end),
                "timeZone": self._timezone,
            },
        )
        slots: list[TimeSlot] = []
        for raw in _iter_slot_times(data):
            try:
                slot_start = _parse_dt(raw)
            except ValueError:
                continue
            slots.append(
                TimeSlot(
                    start=slot_start,
                    end=slot_start + timedelta(minutes=duration_minutes),
                )
            )
        slots.sort(key=lambda item: item.start)
        return slots

    def create_booking(
        self,
        *,
        start: datetime,
        duration_minutes: int,
        name: str,
        phone: str | None = None,
        email: str | None = None,
        notes: str | None = None,
        timezone: str = "UTC",
    ) -> Booking:
        attendee: dict[str, Any] = {
            "name": name,
            "email": email or _synthesise_email(phone, name),
            "timeZone": timezone or self._timezone,
        }
        if phone:
            attendee["phoneNumber"] = phone
        payload: dict[str, Any] = {
            "start": _iso_z(start),
            "eventTypeId": self._event_type_id,
            "attendee": attendee,
        }
        if notes:
            payload["bookingFieldsResponses"] = {"notes": notes}
        data = self._request("POST", "/bookings", json=payload)
        return _booking_from(data, fallback_start=start, duration_minutes=duration_minutes)

    def cancel_booking(self, booking_id: str, *, reason: str | None = None) -> None:
        self._request(
            "POST",
            f"/bookings/{booking_id}/cancel",
            json={"cancellationReason": reason or "Cancelled by phone"},
        )

    def reschedule_booking(
        self, booking_id: str, *, start: datetime
    ) -> Booking:
        data = self._request(
            "POST",
            f"/bookings/{booking_id}/reschedule",
            json={"start": _iso_z(start), "rescheduledBy": "voice-agent"},
        )
        return _booking_from(data, fallback_start=start, duration_minutes=0)


def _error_text(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return f"Cal.com returned HTTP {response.status_code}."
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])[:500]
        if body.get("message"):
            return str(body["message"])[:500]
    return f"Cal.com returned HTTP {response.status_code}."


def _iter_slot_times(data: Any) -> list[Any]:
    """Yield start times across the shapes Cal.com has used for /slots."""
    if isinstance(data, dict) and isinstance(data.get("slots"), dict):
        data = data["slots"]
    times: list[Any] = []
    if isinstance(data, dict):
        for value in data.values():
            if isinstance(value, list):
                for entry in value:
                    if isinstance(entry, dict):
                        times.append(entry.get("start") or entry.get("time"))
                    else:
                        times.append(entry)
    elif isinstance(data, list):
        for entry in data:
            if isinstance(entry, dict):
                times.append(entry.get("start") or entry.get("time"))
            else:
                times.append(entry)
    return [t for t in times if t]


def _booking_from(
    data: Any, *, fallback_start: datetime, duration_minutes: int
) -> Booking:
    record = data if isinstance(data, dict) else {}
    uid = record.get("uid") or record.get("id")
    if not uid:
        raise CalendarProviderError(
            "unexpected_response", "Cal.com did not return a booking id."
        )
    try:
        start = _parse_dt(record["start"]) if record.get("start") else fallback_start
    except ValueError:
        start = fallback_start
    try:
        end = (
            _parse_dt(record["end"])
            if record.get("end")
            else start + timedelta(minutes=max(duration_minutes, 0))
        )
    except ValueError:
        end = start
    location = record.get("location") or record.get("meetingUrl")
    return Booking(
        id=str(uid),
        start=start,
        end=end,
        location=str(location) if location else None,
        reschedule_url=(
            str(record["rescheduleUrl"]) if record.get("rescheduleUrl") else None
        ),
        cancel_url=str(record["cancelUrl"]) if record.get("cancelUrl") else None,
    )
