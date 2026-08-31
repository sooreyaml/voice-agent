"""The Cal.com calendar connector against a mocked transport."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from app.domains.integrations.base import CalendarProviderError
from app.domains.integrations.providers.cal_com import CAL_API_BASE, CalComCalendar


def _calendar(handler, **kwargs) -> CalComCalendar:
    def factory(**client_kwargs):
        client_kwargs.pop("transport", None)
        return httpx.Client(transport=httpx.MockTransport(handler), **client_kwargs)

    return CalComCalendar(
        "cal_live_secret_key",
        event_type_id=kwargs.pop("event_type_id", 55),
        timezone=kwargs.pop("timezone", "UTC"),
        client_factory=factory,
        **kwargs,
    )


def test_verify_identifies_the_account():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v2/me"
        assert request.headers["Authorization"] == "Bearer cal_live_secret_key"
        assert request.headers["cal-api-version"]
        return httpx.Response(
            200,
            json={"status": "success", "data": {"username": "acme", "id": 9}},
        )

    assert _calendar(handler).verify()["external_account_id"] == "acme"


def test_available_slots_v2_shape():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v2/slots"
        assert request.url.params["eventTypeId"] == "55"
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "2026-09-01": [
                        {"start": "2026-09-01T13:30:00.000Z"},
                        {"start": "2026-09-01T09:00:00.000Z"},
                    ]
                },
            },
        )

    slots = _calendar(handler).available_slots(
        start=datetime(2026, 9, 1, tzinfo=UTC),
        end=datetime(2026, 9, 2, tzinfo=UTC),
        duration_minutes=30,
    )
    assert [s.start.hour for s in slots] == [9, 13]  # sorted ascending
    assert slots[0].start.tzinfo is not None
    assert (slots[0].end - slots[0].start).total_seconds() == 1800


def test_available_slots_legacy_slots_wrapper():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": {
                    "slots": {"2026-09-01": [{"time": "2026-09-01T09:00:00Z"}]}
                }
            },
        )

    slots = _calendar(handler).available_slots(
        start=datetime(2026, 9, 1, tzinfo=UTC),
        end=datetime(2026, 9, 2, tzinfo=UTC),
        duration_minutes=15,
    )
    assert len(slots) == 1 and slots[0].start.hour == 9


def test_create_booking_sends_expected_body_and_synthesises_email():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST" and request.url.path == "/v2/bookings"
        seen["body"] = request.read().decode()
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "uid": "bk_123",
                    "start": "2026-09-01T09:00:00.000Z",
                    "end": "2026-09-01T09:30:00.000Z",
                    "location": "https://meet.example/x",
                },
            },
        )

    booking = _calendar(handler).create_booking(
        start=datetime(2026, 9, 1, 9, 0, tzinfo=UTC),
        duration_minutes=30,
        name="Dana Scully",
        phone="+16175550188",
        notes="cleaning",
    )
    assert booking.id == "bk_123"
    assert booking.location == "https://meet.example/x"
    body = seen["body"]
    assert '"eventTypeId":55' in body.replace(" ", "")
    assert "16175550188@voice.calls.invalid" in body
    assert "+16175550188" in body


def test_http_5xx_is_retryable_and_4xx_is_not():
    calendar = _calendar(lambda r: httpx.Response(503, json={"error": {"message": "down"}}))
    with pytest.raises(CalendarProviderError) as excinfo:
        calendar.verify()
    assert excinfo.value.retryable is True

    calendar = _calendar(lambda r: httpx.Response(401, json={"error": {"message": "nope"}}))
    with pytest.raises(CalendarProviderError) as excinfo:
        calendar.verify()
    assert excinfo.value.retryable is False
    assert excinfo.value.code == "cal_com_http_401"


def test_transport_failure_becomes_retryable_provider_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    with pytest.raises(CalendarProviderError) as excinfo:
        _calendar(handler).verify()
    assert excinfo.value.code == "transport_error" and excinfo.value.retryable


def test_cancel_and_reschedule_hit_the_right_paths():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path}")
        if request.url.path.endswith("/reschedule"):
            return httpx.Response(
                200,
                json={"data": {"uid": "bk_new", "start": "2026-09-02T10:00:00.000Z"}},
            )
        return httpx.Response(200, json={"status": "success", "data": {}})

    calendar = _calendar(handler)
    calendar.cancel_booking("bk_123", reason="caller changed mind")
    moved = calendar.reschedule_booking(
        "bk_123", start=datetime(2026, 9, 2, 10, 0, tzinfo=UTC)
    )
    assert moved.id == "bk_new"
    assert "POST /v2/bookings/bk_123/cancel" in calls
    assert "POST /v2/bookings/bk_123/reschedule" in calls


def test_base_url_default_is_v2():
    assert CAL_API_BASE.endswith("/v2")
