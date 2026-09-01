"""Scoped public-API keys: creation, bearer auth on calls/leads, scopes,
tenant isolation, revoke/rotate, and per-key rate limiting.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.domains.api_keys import service as api_key_service

BUSINESSES = Path(__file__).resolve().parent.parent / "businesses"
PW = "correct horse staple 9"


def _settings(tmp_path: Path, **overrides):
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
        **overrides,
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


def _csrf(client: TestClient) -> dict[str, str]:
    return {"X-CSRF-Token": client.cookies["csrf"]}


def _account(client: TestClient, email: str, org: str = "Acme") -> tuple[TestClient, str]:
    session = TestClient(client.app)
    session.get("/api/v1/ping")
    resp = session.post(
        "/api/v1/auth/signup",
        json={"email": email, "password": PW, "organization_name": org},
        headers=_csrf(session),
    )
    assert resp.status_code == 201, resp.text
    return session, resp.json()["organization"]["id"]


def _make_key(client: TestClient, org_id: str, scopes: list[str], name: str = "ci"):
    return client.post(
        f"/api/v1/organizations/{org_id}/api-keys",
        json={"name": name, "scopes": scopes},
        headers=_csrf(client),
    )


def _bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


# -- key management -------------------------------------------------


def test_secret_is_shown_once_then_only_the_prefix(app_client: TestClient):
    owner, org_id = _account(app_client, "owner@x.com")
    created = _make_key(owner, org_id, ["calls:read", "leads:read"])
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["key"].startswith("cak_")
    assert body["prefix"] == body["key"][:12]
    assert body["scopes"] == ["calls:read", "leads:read"]

    listed = owner.get(f"/api/v1/organizations/{org_id}/api-keys").json()
    assert len(listed) == 1
    assert "key" not in listed[0] and "token_hash" not in listed[0]
    assert listed[0]["prefix"] == body["prefix"]


def test_create_needs_admin_and_rejects_unknown_scope(app_client: TestClient):
    _owner, org_id = _account(app_client, "owner@x.com")
    member = TestClient(app_client.app)
    member.get("/api/v1/ping")
    member.post(
        "/api/v1/auth/signup",
        json={"email": "m@x.com", "password": PW, "organization_name": "M"},
        headers=_csrf(member),
    )
    app_client.app.state.store.add_membership(
        org_id, member.get("/api/v1/me").json()["user"]["id"], "member"
    )
    assert _make_key(member, org_id, ["calls:read"]).status_code == 403

    owner_client = TestClient(app_client.app)
    owner_client.get("/api/v1/ping")
    bad = owner_client.post(
        "/api/v1/auth/signup",
        json={"email": "o2@x.com", "password": PW, "organization_name": "O2"},
        headers=_csrf(owner_client),
    )
    org2 = bad.json()["organization"]["id"]
    assert _make_key(owner_client, org2, ["calls:write"]).status_code == 422


def test_key_limit_is_enforced(app_client: TestClient, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(api_key_service, "MAX_KEYS_PER_ORG", 2)
    owner, org_id = _account(app_client, "owner@x.com")
    assert _make_key(owner, org_id, ["calls:read"], "a").status_code == 201
    assert _make_key(owner, org_id, ["calls:read"], "b").status_code == 201
    third = _make_key(owner, org_id, ["calls:read"], "c")
    assert third.status_code == 409
    assert third.json()["error"]["code"] == "api_key_limit_reached"


# -- bearer auth on the read endpoints ---------------------------


def test_bearer_key_reads_calls_and_leads_without_a_cookie(app_client: TestClient):
    owner, org_id = _account(app_client, "owner@x.com")
    store = app_client.app.state.store
    store.start_call(org_id, "c1", "Acme", "+1", "+2")
    store.add_lead(org_id, "c1", {"intent": "pricing_question", "details": "how much"})
    store.finish_call(org_id, "c1", "completed", "", 0.0)

    key = _make_key(owner, org_id, ["calls:read", "leads:read"]).json()["key"]
    anon = TestClient(app_client.app)  # no session cookie at all

    calls = anon.get(f"/api/v1/organizations/{org_id}/calls", headers=_bearer(key))
    assert calls.status_code == 200
    assert calls.json()["items"][0]["call_id"] == "c1"

    leads = anon.get(f"/api/v1/organizations/{org_id}/leads", headers=_bearer(key))
    assert leads.status_code == 200
    assert leads.json()["items"][0]["intent"] == "pricing_question"

    one = anon.get(
        f"/api/v1/organizations/{org_id}/calls/c1", headers=_bearer(key)
    )
    assert one.status_code == 200 and one.json()["transcript"] == []


def test_bearer_missing_scope_is_403(app_client: TestClient):
    owner, org_id = _account(app_client, "owner@x.com")
    key = _make_key(owner, org_id, ["calls:read"]).json()["key"]
    anon = TestClient(app_client.app)
    resp = anon.get(f"/api/v1/organizations/{org_id}/leads", headers=_bearer(key))
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "api_key_missing_scope"


def test_bearer_key_is_tenant_scoped(app_client: TestClient):
    owner_a, org_a = _account(app_client, "a@x.com", "A")
    _owner_b, org_b = _account(app_client, "b@x.com", "B")
    key = _make_key(owner_a, org_a, ["calls:read"]).json()["key"]
    anon = TestClient(app_client.app)
    assert anon.get(
        f"/api/v1/organizations/{org_b}/calls", headers=_bearer(key)
    ).status_code == 404


def test_revoked_and_rotated_keys(app_client: TestClient):
    owner, org_id = _account(app_client, "owner@x.com")
    created = _make_key(owner, org_id, ["calls:read"]).json()
    key_id, key = created["id"], created["key"]
    anon = TestClient(app_client.app)
    assert anon.get(
        f"/api/v1/organizations/{org_id}/calls", headers=_bearer(key)
    ).status_code == 200

    rotated = owner.post(
        f"/api/v1/organizations/{org_id}/api-keys/{key_id}/rotate",
        headers=_csrf(owner),
    ).json()
    assert rotated["key"] != key
    assert anon.get(
        f"/api/v1/organizations/{org_id}/calls", headers=_bearer(key)
    ).status_code == 401  # old secret dead
    assert anon.get(
        f"/api/v1/organizations/{org_id}/calls", headers=_bearer(rotated["key"])
    ).status_code == 200

    assert owner.request(
        "DELETE",
        f"/api/v1/organizations/{org_id}/api-keys/{key_id}",
        headers=_csrf(owner),
    ).status_code == 204
    resp = anon.get(
        f"/api/v1/organizations/{org_id}/calls", headers=_bearer(rotated["key"])
    )
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "api_key_invalid"


def test_cookie_access_is_unaffected(app_client: TestClient):
    owner, org_id = _account(app_client, "owner@x.com")
    assert owner.get(f"/api/v1/organizations/{org_id}/calls").status_code == 200

    outsider, _o = _account(app_client, "out@x.com", "Out")
    assert outsider.get(
        f"/api/v1/organizations/{org_id}/calls"
    ).status_code == 404

    anon = TestClient(app_client.app)
    assert anon.get(f"/api/v1/organizations/{org_id}/calls").status_code == 401


def test_per_key_rate_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import app.main

    monkeypatch.setattr(
        app.main, "settings", _settings(tmp_path, api_key_rate_limit_per_minute=2)
    )
    with TestClient(app.main.app) as client:
        client.get("/api/v1/ping")
        owner, org_id = _account(client, "owner@x.com")
        key = _make_key(owner, org_id, ["calls:read"]).json()["key"]
        anon = TestClient(client.app)
        url = f"/api/v1/organizations/{org_id}/calls"
        assert anon.get(url, headers=_bearer(key)).status_code == 200
        assert anon.get(url, headers=_bearer(key)).status_code == 200
        limited = anon.get(url, headers=_bearer(key))
        assert limited.status_code == 429
        assert limited.json()["error"]["code"] == "api_key_rate_limited"
        assert "retry-after" in {k.lower() for k in limited.headers}
