"""The platform-admin surface is a read-only operator overview and nothing more."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

BUSINESSES = Path(__file__).resolve().parent.parent / "businesses"
PW = "correct horse staple 9"


def _settings(tmp_path: Path):
    from app.settings import load_settings

    return replace(
        load_settings(),
        openai_api_key="sk-test",
        openai_webhook_secret="whsec_test",
        database_path=tmp_path / "calls.sqlite3",
        database_url="",
        auth_session_secret="unit-test-secret",
        environment="development",
        businesses_dir=BUSINESSES,
        resend_api_key="",
        resend_from_email="",
        number_pool_country="US",
        stripe_price_id="",
    )


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import app.main

    monkeypatch.setattr(app.main, "settings", _settings(tmp_path))
    with TestClient(app.main.app) as test_client:
        test_client.get("/api/v1/ping")
        yield test_client


def _csrf(c: TestClient) -> dict[str, str]:
    return {"X-CSRF-Token": c.cookies["csrf"]}


def _account(c: TestClient, email: str, org: str) -> TestClient:
    s = TestClient(c.app)
    s.get("/api/v1/ping")
    r = s.post(
        "/api/v1/auth/signup",
        json={"email": email, "password": PW, "organization_name": org},
        headers=_csrf(s),
    )
    assert r.status_code == 201, r.text
    return s


def _admin(c: TestClient, email: str = "staff@platform.example") -> TestClient:
    a = _account(c, email, "Platform Staff")
    c.app.state.store.set_platform_admin(a.get("/api/v1/me").json()["user"]["id"], True)
    return a


def test_overview_reports_lifecycle_billing_and_pool_health(client: TestClient):
    admin = _admin(client)
    _account(client, "a@x.test", "Alpha Co")
    client.app.state.store.add_pool_number("+15550000111", "US")

    overview = admin.get("/api/v1/admin/overview")
    assert overview.status_code == 200
    body = overview.json()
    assert body["organizations"].get("active", 0) >= 2  # both signed up without billing
    assert body["number_pool"].get("available") == 1
    assert "period_customer_charge_micros" in body
    assert "payment_failures" in body


def test_admin_org_list_shows_lifecycle_and_number(client: TestClient):
    admin = _admin(client)
    store = client.app.state.store
    store.add_pool_number("+15550000222", "US")
    owner = _account(client, "owner@x.test", "Owner Co")
    org_id = owner.get("/api/v1/me").json()["organizations"][0]["id"]

    listing = admin.get("/api/v1/admin/organizations").json()
    row = next(o for o in listing["items"] if o["id"] == org_id)
    assert row["lifecycle"] == "active"
    assert row["phone_number"] == "+15550000222"

    detail = admin.get(f"/api/v1/admin/organizations/{org_id}").json()
    assert detail["lifecycle"] == "active"
    assert detail["phone_number"] == "+15550000222"


def test_overview_requires_platform_admin(client: TestClient):
    nobody = _account(client, "nobody@x.test", "Nobody Co")
    assert nobody.get("/api/v1/admin/overview").status_code == 403


def test_admin_cannot_author_or_onboard(client: TestClient):
    admin = _admin(client)
    # Every write path a "do work for tenants" admin used to have is gone.
    for method, path in (
        ("post", "/api/v1/admin/onboarding"),
        ("post", "/api/v1/admin/billing/plans"),
        ("post", "/api/v1/admin/billing/usage-events"),
    ):
        resp = getattr(admin, method)(path, json={}, headers=_csrf(admin))
        assert resp.status_code == 404, (path, resp.status_code)
