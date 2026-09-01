"""Connecting the HubSpot CRM through the integration framework, alongside the
existing Cal.com calendar connector."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.domains.integrations import registry
from app.domains.integrations.base import CrmProviderError

BUSINESSES = Path(__file__).resolve().parent.parent / "businesses"
PW = "correct horse staple 9"
HS_TOKEN = "pat-na1-0000-1111-2222-333344445555"


def _settings(tmp_path: Path):
    from app.settings import load_settings

    return replace(
        load_settings(),
        openai_api_key="sk-test",
        openai_webhook_secret="whsec_test",
        database_path=tmp_path / "calls.sqlite3",
        database_url="",
        auth_session_secret="unit-test-secret",
        integration_encryption_key="",
        environment="development",
        businesses_dir=BUSINESSES,
        app_base_url="http://testserver",
    )


class _FakeProvider:
    def __init__(self, provider: str) -> None:
        self.provider = provider
        self.error: CrmProviderError | None = None

    def verify(self) -> dict:
        if self.error is not None:
            raise self.error
        return {"external_account_id": f"{self.provider}-acct"}


@pytest.fixture
def fake_providers(monkeypatch: pytest.MonkeyPatch) -> dict:
    made: dict[str, _FakeProvider] = {}

    def build_provider(provider, credentials, settings, *, timeout=8.0):
        if provider not in registry._BUILDERS:
            from app.domains.integrations.exceptions import UnknownProvider

            raise UnknownProvider(provider)
        made[provider] = made.get(provider) or _FakeProvider(provider)
        return made[provider]

    monkeypatch.setattr(
        "app.domains.integrations.registry.build_provider", build_provider
    )
    return made


@pytest.fixture
def app_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import app.main

    monkeypatch.setattr(app.main, "settings", _settings(tmp_path))
    with TestClient(app.main.app) as client:
        client.get("/api/v1/ping")
        yield client


def _csrf(c: TestClient) -> dict[str, str]:
    return {"X-CSRF-Token": c.cookies["csrf"]}


def _owner(client: TestClient) -> tuple[TestClient, str]:
    s = TestClient(client.app)
    s.get("/api/v1/ping")
    r = s.post(
        "/api/v1/auth/signup",
        json={"email": "owner@x.com", "password": PW, "organization_name": "Acme"},
        headers=_csrf(s),
    )
    return s, r.json()["organization"]["id"]


def test_connect_hubspot_hides_the_token_and_records_portal(
    app_client: TestClient, fake_providers: dict
):
    owner, org_id = _owner(app_client)
    resp = owner.put(
        f"/api/v1/organizations/{org_id}/integrations/hubspot",
        json={"access_token": HS_TOKEN, "display_name": "Sales CRM"},
        headers=_csrf(owner),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["provider"] == "hubspot" and body["status"] == "active"
    assert body["external_account_id"] == "hubspot-acct"
    assert body["settings"].get("portal_id") == "hubspot-acct"
    assert HS_TOKEN not in resp.text and "access_token" not in resp.text


def test_connect_hubspot_requires_access_token(
    app_client: TestClient, fake_providers: dict
):
    owner, org_id = _owner(app_client)
    resp = owner.put(
        f"/api/v1/organizations/{org_id}/integrations/hubspot",
        json={"display_name": "no token"},
        headers=_csrf(owner),
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "integration_invalid_credentials"


def test_cal_com_still_requires_its_own_fields(
    app_client: TestClient, fake_providers: dict
):
    owner, org_id = _owner(app_client)
    resp = owner.put(
        f"/api/v1/organizations/{org_id}/integrations/cal_com",
        json={"api_key": "cal_live_key_value"},  # missing event_type_id
        headers=_csrf(owner),
    )
    assert resp.status_code == 422


def test_bad_hubspot_token_surfaces_as_422(
    app_client: TestClient, fake_providers: dict
):
    owner, org_id = _owner(app_client)
    fake_providers["hubspot"] = _FakeProvider("hubspot")
    fake_providers["hubspot"].error = CrmProviderError("hubspot_http_401", "bad token")
    resp = owner.put(
        f"/api/v1/organizations/{org_id}/integrations/hubspot",
        json={"access_token": HS_TOKEN},
        headers=_csrf(owner),
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "integration_invalid_credentials"


def test_calendar_and_crm_connections_coexist(
    app_client: TestClient, fake_providers: dict
):
    owner, org_id = _owner(app_client)
    owner.put(
        f"/api/v1/organizations/{org_id}/integrations/hubspot",
        json={"access_token": HS_TOKEN},
        headers=_csrf(owner),
    )
    owner.put(
        f"/api/v1/organizations/{org_id}/integrations/cal_com",
        json={"api_key": "cal_live_key_value", "event_type_id": 42},
        headers=_csrf(owner),
    )
    providers = {
        c["provider"]
        for c in owner.get(f"/api/v1/organizations/{org_id}/integrations").json()
    }
    assert providers == {"hubspot", "cal_com"}

    test = owner.post(
        f"/api/v1/organizations/{org_id}/integrations/hubspot/test",
        headers=_csrf(owner),
    )
    assert test.status_code == 200 and test.json()["ok"] is True
