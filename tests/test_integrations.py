"""Integration connection API: connect, read, test, disconnect, and at-rest
encryption of provider credentials.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.domains.integrations import registry
from app.domains.integrations.base import CalendarProviderError

BUSINESSES = Path(__file__).resolve().parent.parent / "businesses"
PW = "correct horse staple 9"
API_KEY = "cal_live_super_secret_value"


def _settings(tmp_path: Path):
    from app.settings import load_settings

    return replace(
        load_settings(),
        openai_api_key="sk-test",
        openai_webhook_secret="whsec_test",
        database_path=tmp_path / "calls.sqlite3",
        database_url="",
        auth_session_secret="unit-test-secret",
        integration_encryption_key="",  # development derives a throwaway key
        environment="development",
        businesses_dir=BUSINESSES,
        app_base_url="http://testserver",
    )


class FakeCalendar:
    """Stand-in provider whose verify() outcome the test controls."""

    def __init__(self) -> None:
        self.error: CalendarProviderError | None = None
        self.external_account_id = "acme-cal"

    def verify(self) -> dict:
        if self.error is not None:
            raise self.error
        return {"external_account_id": self.external_account_id}


@pytest.fixture
def fake_calendar(monkeypatch: pytest.MonkeyPatch) -> FakeCalendar:
    fake = FakeCalendar()
    seen: dict = {}

    def build_provider(provider, credentials, settings, *, timeout=8.0):
        if provider not in registry._BUILDERS:
            from app.domains.integrations.exceptions import UnknownProvider

            raise UnknownProvider(provider)
        seen["credentials"] = credentials
        seen["settings"] = settings
        return fake

    monkeypatch.setattr(
        "app.domains.integrations.registry.build_provider", build_provider
    )
    fake.seen = seen  # type: ignore[attr-defined]
    return fake


@pytest.fixture
def app_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import app.main

    monkeypatch.setattr(app.main, "settings", _settings(tmp_path))
    with TestClient(app.main.app) as client:
        client.get("/api/v1/ping")
        yield client


def _csrf(client: TestClient) -> dict[str, str]:
    return {"X-CSRF-Token": client.cookies["csrf"]}


def _account(
    client: TestClient, email: str, org: str = "Acme"
) -> tuple[TestClient, str]:
    session = TestClient(client.app)
    session.get("/api/v1/ping")
    resp = session.post(
        "/api/v1/auth/signup",
        json={"email": email, "password": PW, "organization_name": org},
        headers=_csrf(session),
    )
    assert resp.status_code == 201, resp.text
    return session, resp.json()["organization"]["id"]


def _connect(client: TestClient, org_id: str, provider: str = "cal_com", **body):
    payload = {"api_key": API_KEY, "event_type_id": 123, "timezone": "UTC", **body}
    return client.put(
        f"/api/v1/organizations/{org_id}/integrations/{provider}",
        json=payload,
        headers=_csrf(client),
    )


# -- connect + read ---------------------------------------------------


def test_connect_stores_no_secret_and_reads_back(
    app_client: TestClient, fake_calendar: FakeCalendar
):
    owner, org_id = _account(app_client, "owner@x.com")
    resp = _connect(owner, org_id, display_name="Front desk")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["provider"] == "cal_com"
    assert body["status"] == "active"
    assert body["external_account_id"] == "acme-cal"
    assert body["display_name"] == "Front desk"
    assert body["settings"] == {"event_type_id": 123, "timezone": "UTC"}
    assert "api_key" not in resp.text and API_KEY not in resp.text

    listed = owner.get(f"/api/v1/organizations/{org_id}/integrations").json()
    assert [c["provider"] for c in listed] == ["cal_com"]
    one = owner.get(
        f"/api/v1/organizations/{org_id}/integrations/cal_com"
    ).json()
    assert one["id"] == body["id"] and API_KEY not in str(one)


def test_connect_requires_admin_role(
    app_client: TestClient, fake_calendar: FakeCalendar
):
    _owner, org_id = _account(app_client, "owner@x.com")
    member = TestClient(app_client.app)
    member.get("/api/v1/ping")
    member.post(
        "/api/v1/auth/signup",
        json={"email": "m@x.com", "password": PW, "organization_name": "M"},
        headers=_csrf(member),
    )
    member_id = member.get("/api/v1/me").json()["user"]["id"]
    app_client.app.state.store.add_membership(org_id, member_id, "member")
    assert _connect(member, org_id).status_code == 403


def test_unknown_provider_is_404(
    app_client: TestClient, fake_calendar: FakeCalendar
):
    owner, org_id = _account(app_client, "owner@x.com")
    resp = _connect(owner, org_id, provider="salesforce")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "integration_unknown_provider"


def test_bad_credentials_surface_as_422_then_502(
    app_client: TestClient, fake_calendar: FakeCalendar
):
    owner, org_id = _account(app_client, "owner@x.com")

    fake_calendar.error = CalendarProviderError("cal_com_http_401", "bad key")
    resp = _connect(owner, org_id)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "integration_invalid_credentials"

    fake_calendar.error = CalendarProviderError("cal_com_http_500", "kaboom")
    resp = _connect(owner, org_id)
    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "integration_provider_rejected"

    # nothing was persisted by the failed attempts
    assert (
        owner.get(
            f"/api/v1/organizations/{org_id}/integrations/cal_com"
        ).status_code
        == 404
    )


def test_get_absent_connection_is_404(
    app_client: TestClient, fake_calendar: FakeCalendar
):
    owner, org_id = _account(app_client, "owner@x.com")
    resp = owner.get(f"/api/v1/organizations/{org_id}/integrations/cal_com")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "integration_not_found"


def test_integrations_are_tenant_scoped(
    app_client: TestClient, fake_calendar: FakeCalendar
):
    owner, org_id = _account(app_client, "owner@x.com")
    _connect(owner, org_id)
    other, _other = _account(app_client, "other@x.com", "Other")
    assert other.get(
        f"/api/v1/organizations/{org_id}/integrations"
    ).status_code == 404
    assert _connect(other, org_id).status_code == 404


# -- re-verify + disconnect ----------------------------------------


def test_test_endpoint_reports_health_and_flips_status(
    app_client: TestClient, fake_calendar: FakeCalendar
):
    owner, org_id = _account(app_client, "owner@x.com")
    _connect(owner, org_id)

    ok = owner.post(
        f"/api/v1/organizations/{org_id}/integrations/cal_com/test",
        headers=_csrf(owner),
    )
    assert ok.status_code == 200 and ok.json()["ok"] is True
    assert ok.json()["external_account_id"] == "acme-cal"

    fake_calendar.error = CalendarProviderError("cal_com_http_403", "revoked")
    bad = owner.post(
        f"/api/v1/organizations/{org_id}/integrations/cal_com/test",
        headers=_csrf(owner),
    )
    assert bad.status_code == 200 and bad.json()["ok"] is False
    assert "revoked" in bad.json()["detail"]

    after = owner.get(
        f"/api/v1/organizations/{org_id}/integrations/cal_com"
    ).json()
    assert after["status"] == "error" and after["last_error"] == "revoked"


def test_disconnect_removes_the_connection(
    app_client: TestClient, fake_calendar: FakeCalendar
):
    owner, org_id = _account(app_client, "owner@x.com")
    _connect(owner, org_id)

    member = TestClient(app_client.app)
    member.get("/api/v1/ping")
    member.post(
        "/api/v1/auth/signup",
        json={"email": "m@x.com", "password": PW, "organization_name": "M"},
        headers=_csrf(member),
    )
    store = app_client.app.state.store
    store.add_membership(
        org_id, member.get("/api/v1/me").json()["user"]["id"], "member"
    )
    assert member.request(
        "DELETE",
        f"/api/v1/organizations/{org_id}/integrations/cal_com",
        headers=_csrf(member),
    ).status_code == 403

    assert owner.request(
        "DELETE",
        f"/api/v1/organizations/{org_id}/integrations/cal_com",
        headers=_csrf(owner),
    ).status_code == 204
    assert owner.get(
        f"/api/v1/organizations/{org_id}/integrations/cal_com"
    ).status_code == 404
    # disconnecting again is a 404, not a 204
    assert owner.request(
        "DELETE",
        f"/api/v1/organizations/{org_id}/integrations/cal_com",
        headers=_csrf(owner),
    ).status_code == 404


# -- encryption at rest ------------------------------------------


def test_provider_credentials_are_encrypted_at_rest(
    app_client: TestClient, fake_calendar: FakeCalendar
):
    from app.domains.integrations.crypto import build_cipher

    owner, org_id = _account(app_client, "owner@x.com")
    _connect(owner, org_id)

    store = app_client.app.state.store
    row = store.query(
        "SELECT encrypted_credentials FROM integration_connections"
        " WHERE organization_id = ?",
        (org_id,),
    )[0]
    blob = row["encrypted_credentials"]
    assert API_KEY not in blob and "api_key" not in blob

    cipher = build_cipher(app_client.app.state.settings)
    assert cipher.open(blob) == {"api_key": API_KEY}
