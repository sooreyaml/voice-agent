"""Lead follow-up status: cookie + scoped-key writes, role floor, and the
`leads:write` bearer path (which bypasses CSRF)."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.domains.api_keys import service as api_key_service

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
        integration_encryption_key="",
        environment="development",
        businesses_dir=BUSINESSES,
        app_base_url="http://testserver",
    )


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    api_key_service.rate_limiter.reset()
    yield
    api_key_service.rate_limiter.reset()


@pytest.fixture
def app_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import app.main

    monkeypatch.setattr(app.main, "settings", _settings(tmp_path))
    with TestClient(app.main.app) as client:
        client.get("/api/v1/ping")
        yield client


def _csrf(c: TestClient) -> dict[str, str]:
    return {"X-CSRF-Token": c.cookies["csrf"]}


def _account(client: TestClient, email: str, org: str = "Acme") -> tuple[TestClient, str]:
    s = TestClient(client.app)
    s.get("/api/v1/ping")
    r = s.post(
        "/api/v1/auth/signup",
        json={"email": email, "password": PW, "organization_name": org},
        headers=_csrf(s),
    )
    assert r.status_code == 201, r.text
    return s, r.json()["organization"]["id"]


def _seed_lead(store, org_id: str, call_id: str = "c1") -> int:
    store.start_call(org_id, call_id, "Acme", "+1", "+2")
    lead_id = store.add_lead(
        org_id, call_id, {"intent": "pricing_question", "details": "how much?"}
    )
    store.finish_call(org_id, call_id, "completed", "", 0.0)
    return lead_id


def test_member_updates_status_and_it_shows_everywhere(app_client: TestClient):
    owner, org_id = _account(app_client, "owner@x.com")
    lead_id = _seed_lead(app_client.app.state.store, org_id)

    resp = owner.patch(
        f"/api/v1/organizations/{org_id}/leads/{lead_id}",
        json={"status": "handled", "note": "called back, booked"},
        headers=_csrf(owner),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "handled"
    assert body["status_note"] == "called back, booked"
    assert body["status_updated_at"] is not None

    listed = owner.get(f"/api/v1/organizations/{org_id}/leads").json()["items"][0]
    assert listed["status"] == "handled"

    call = owner.get(f"/api/v1/organizations/{org_id}/calls/c1").json()
    assert call["leads"][0]["status"] == "handled"


def test_viewer_is_forbidden(app_client: TestClient):
    _owner, org_id = _account(app_client, "owner@x.com")
    lead_id = _seed_lead(app_client.app.state.store, org_id)

    viewer = TestClient(app_client.app)
    viewer.get("/api/v1/ping")
    viewer.post(
        "/api/v1/auth/signup",
        json={"email": "v@x.com", "password": PW, "organization_name": "V"},
        headers=_csrf(viewer),
    )
    app_client.app.state.store.add_membership(
        org_id, viewer.get("/api/v1/me").json()["user"]["id"], "viewer"
    )
    resp = viewer.patch(
        f"/api/v1/organizations/{org_id}/leads/{lead_id}",
        json={"status": "handled"},
        headers=_csrf(viewer),
    )
    assert resp.status_code == 403


def test_scoped_key_writes_status_without_csrf(app_client: TestClient):
    owner, org_id = _account(app_client, "owner@x.com")
    lead_id = _seed_lead(app_client.app.state.store, org_id)
    key = owner.post(
        f"/api/v1/organizations/{org_id}/api-keys",
        json={"name": "ci", "scopes": ["leads:read", "leads:write"]},
        headers=_csrf(owner),
    ).json()["key"]

    anon = TestClient(app_client.app)  # no cookie, no CSRF token
    resp = anon.patch(
        f"/api/v1/organizations/{org_id}/leads/{lead_id}",
        json={"status": "dismissed"},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "dismissed"


def test_key_without_leads_write_is_403(app_client: TestClient):
    owner, org_id = _account(app_client, "owner@x.com")
    lead_id = _seed_lead(app_client.app.state.store, org_id)
    key = owner.post(
        f"/api/v1/organizations/{org_id}/api-keys",
        json={"name": "ro", "scopes": ["leads:read"]},
        headers=_csrf(owner),
    ).json()["key"]
    anon = TestClient(app_client.app)
    resp = anon.patch(
        f"/api/v1/organizations/{org_id}/leads/{lead_id}",
        json={"status": "handled"},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "api_key_missing_scope"


def test_unknown_lead_is_404_and_bad_status_is_422(app_client: TestClient):
    owner, org_id = _account(app_client, "owner@x.com")
    assert owner.patch(
        f"/api/v1/organizations/{org_id}/leads/9999",
        json={"status": "handled"},
        headers=_csrf(owner),
    ).status_code == 404

    lead_id = _seed_lead(app_client.app.state.store, org_id)
    assert owner.patch(
        f"/api/v1/organizations/{org_id}/leads/{lead_id}",
        json={"status": "archived"},
        headers=_csrf(owner),
    ).status_code == 422
