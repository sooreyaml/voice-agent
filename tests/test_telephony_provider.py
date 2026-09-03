"""Provider-level error mapping for the shared Twilio account."""

from __future__ import annotations

import json

import pytest
from twilio.base.exceptions import TwilioException

from app.domains.telephony.provider import (
    TelephonyProviderError,
    TwilioProvisioningService,
)


class _FakeResponse:
    """Mirror of ``twilio.http.response.Response`` for the bits we read."""

    def __init__(self, status_code: int, text: str) -> None:
        self.status_code = status_code
        self.text = text


class _RaisingContext:
    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    def list(self, **_kwargs: object) -> list:
        raise self._exc


class _AvailableNumbers:
    def __init__(self, exc: BaseException) -> None:
        self.mobile = _RaisingContext(exc)
        self.local = _RaisingContext(exc)


class _FakeClient:
    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    def available_phone_numbers(self, _country: str) -> _AvailableNumbers:
        return _AvailableNumbers(self._exc)


def _service(exc: BaseException) -> TwilioProvisioningService:
    return TwilioProvisioningService(
        "ACtest",
        "token",
        "proj_test",
        client_factory=lambda _sid, _token: _FakeClient(exc),
    )


def _page_exception(status: int, code: int) -> TwilioException:
    """The bare exception the Twilio SDK raises from a non-200 page fetch."""
    body = json.dumps(
        {
            "code": code,
            "message": f"account ACtest with status 4 is not active ({status})",
            "status": status,
        }
    )
    return TwilioException("Unable to fetch page", _FakeResponse(status, body))


def test_paginated_auth_failure_maps_to_provider_error_not_a_raw_exception():
    # A suspended/closed account makes .list() raise the base TwilioException,
    # never TwilioRestException. It must still become a TelephonyProviderError
    # so signup's graceful-degradation path can catch it.
    with pytest.raises(TelephonyProviderError) as excinfo:
        _service(_page_exception(401, 20003)).search_available_numbers(
            "GB", "mobile", limit=5
        )

    err = excinfo.value
    assert err.status == 401
    assert err.retryable is False
    assert "20003" in err.code
    assert "not active" in err.message


def test_paginated_server_error_is_marked_retryable():
    with pytest.raises(TelephonyProviderError) as excinfo:
        _service(_page_exception(503, 20500)).search_available_numbers(
            "GB", "mobile", limit=5
        )

    assert excinfo.value.status == 503
    assert excinfo.value.retryable is True


def test_paginated_failure_without_a_response_still_maps_cleanly():
    with pytest.raises(TelephonyProviderError) as excinfo:
        _service(TwilioException("Unable to fetch page")).search_available_numbers(
            "GB", "mobile", limit=5
        )

    assert excinfo.value.code == "twilio_request_failed"
    assert excinfo.value.status is None
