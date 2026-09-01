"""Password auth, browser sessions, CSRF, tenant scoping, and the 3b email flows."""

from __future__ import annotations

import importlib
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

BUSINESSES = Path(__file__).resolve().parent.parent / "businesses"

OWNER_EMAIL = "owner@example.com"
OWNER_PASSWORD = "correct horse staple 9"


def _settings(tmp_path: Path, **overrides):
    from app.settings import load_settings

    base = replace(
        load_settings(),
        openai_api_key="sk-test",
        openai_webhook_secret="whsec_test",
        database_path=tmp_path / "calls.sqlite3",
        database_url="",
        auth_session_secret="unit-test-secret",
        integration_encryption_key="k8jx5ZBLhq5deNjiiCfCrYKexwPaYN8SkNIwN5OEcU0=",
        environment="development",
        businesses_dir=BUSINESSES,
        require_email_verification=False,
        app_base_url="http://testserver",
    )
    return replace(base, **overrides)


@pytest.fixture
def outbox(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    from app.domains.auth import router

    captured: list[dict] = []
    monkeypatch.setattr(router, "deliver_email_token", lambda **kw: captured.append(kw))
    return captured


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import app.main

    monkeypatch.setattr(app.main, "settings", _settings(tmp_path))
    with TestClient(app.main.app) as test_client:
        test_client.get("/api/v1/ping")  # seed the CSRF cookie
        yield test_client


def _csrf(client: TestClient) -> dict[str, str]:
    return {"X-CSRF-Token": client.cookies["csrf"]}


def _signup(
    client: TestClient,
    *,
    email: str = OWNER_EMAIL,
    password: str = OWNER_PASSWORD,
    organization_name: str = "Acme Clinic",
):
    return client.post(
        "/api/v1/auth/signup",
        json={
            "email": email,
            "password": password,
            "organization_name": organization_name,
        },
        headers=_csrf(client),
    )


def _login(client: TestClient, *, email: str = OWNER_EMAIL, password: str = OWNER_PASSWORD):
    return client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": password},
        headers=_csrf(client),
    )


def _fresh(client: TestClient) -> TestClient:
    other = TestClient(client.app)
    other.get("/api/v1/ping")
    return other


# -- signup / login / session ------------------------------------------


def test_ping_seeds_csrf_cookie(client: TestClient):
    assert client.cookies.get("csrf")


def test_signup_creates_owner_and_authenticates(client: TestClient):
    response = _signup(client)
    assert response.status_code == 201
    body = response.json()
    assert body["user"]["email"] == OWNER_EMAIL
    assert body["user"]["email_verified"] is False

    me = client.get("/api/v1/me").json()
    assert me["organizations"][0]["role"] == "owner"
    assert me["organizations"][0]["id"] == body["organization"]["id"]


def test_signup_rejects_duplicate_email(client: TestClient):
    _signup(client)
    duplicate = _signup(_fresh(client), organization_name="Another Co")
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "email_taken"


def test_signup_validates_password_length(client: TestClient):
    response = _signup(client, password="short")
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "validation_failed"
    assert "password" in error["field_errors"]


def test_login_rejects_wrong_password(client: TestClient):
    _signup(client)
    response = _login(_fresh(client), password="not the password")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_credentials"


def test_login_starts_a_session(client: TestClient):
    _signup(client)
    guest = _fresh(client)
    assert _login(guest, email=OWNER_EMAIL.upper()).status_code == 200
    assert guest.get("/api/v1/me").json()["user"]["email"] == OWNER_EMAIL


def test_logout_requires_csrf_then_revokes_session(client: TestClient):
    _signup(client)
    assert client.post("/api/v1/auth/logout").status_code == 403
    assert client.post("/api/v1/auth/logout", headers=_csrf(client)).status_code == 204
    assert client.get("/api/v1/me").status_code == 401


def test_me_requires_authentication(client: TestClient):
    response = _fresh(client).get("/api/v1/me")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "not_authenticated"


def test_request_id_header_is_always_present(client: TestClient):
    assert client.get("/api/v1/ping").headers.get("x-request-id")


# -- tenant scoping --------------------------------------------------


def test_calls_require_authentication(client: TestClient):
    org_id = _signup(client).json()["organization"]["id"]
    response = _fresh(client).get(f"/api/v1/organizations/{org_id}/calls")
    assert response.status_code == 401


def test_member_cannot_read_another_organizations_calls(client: TestClient):
    _signup(client)  # owner@example.com in "Acme Clinic", logged in on `client`

    stranger = _fresh(client)
    other_org = _signup(
        stranger, email="other@example.com", organization_name="Rival Ltd"
    ).json()["organization"]["id"]

    response = client.get(f"/api/v1/organizations/{other_org}/calls")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "organization_not_found"


def test_calls_are_cursor_paginated(client: TestClient):
    org_id = _signup(client).json()["organization"]["id"]
    store = client.app.state.store
    for suffix in ("1", "2", "3"):
        store.start_call(org_id, f"call-{suffix}", "Acme", "+15550000001", "+15550000002")

    first = client.get(
        f"/api/v1/organizations/{org_id}/calls", params={"limit": 2}
    ).json()
    assert [c["call_id"] for c in first["items"]] == ["call-3", "call-2"]
    assert first["page"]["has_more"] is True

    second = client.get(
        f"/api/v1/organizations/{org_id}/calls",
        params={"limit": 2, "cursor": first["page"]["next_cursor"]},
    ).json()
    assert [c["call_id"] for c in second["items"]] == ["call-1"]
    assert second["page"]["has_more"] is False
    assert second["page"]["next_cursor"] is None


def test_call_detail_is_scoped_and_404s_for_unknown(client: TestClient):
    org_id = _signup(client).json()["organization"]["id"]
    response = client.get(f"/api/v1/organizations/{org_id}/calls/nope")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


# -- email verification / password reset (slice 3b) ------------------


def test_email_verification_roundtrip(client: TestClient, outbox: list[dict]):
    _signup(client)
    assert client.post(
        "/api/v1/auth/verify-email/request", headers=_csrf(client)
    ).status_code == 202
    token = outbox[-1]["raw_token"]

    confirm = client.post(
        "/api/v1/auth/verify-email/confirm", json={"token": token}, headers=_csrf(client)
    )
    assert confirm.status_code == 200
    assert client.get("/api/v1/me").json()["user"]["email_verified"] is True

    replay = client.post(
        "/api/v1/auth/verify-email/confirm", json={"token": token}, headers=_csrf(client)
    )
    assert replay.status_code == 400
    assert replay.json()["error"]["code"] == "invalid_token"


def test_password_reset_roundtrip_revokes_sessions(client: TestClient, outbox: list[dict]):
    _signup(client)
    assert client.post(
        "/api/v1/auth/password-reset/request",
        json={"email": OWNER_EMAIL},
        headers=_csrf(client),
    ).status_code == 202
    token = outbox[-1]["raw_token"]

    new_password = "a brand new passphrase"
    confirm = client.post(
        "/api/v1/auth/password-reset/confirm",
        json={"token": token, "password": new_password},
        headers=_csrf(client),
    )
    assert confirm.status_code == 200
    # The reset logs every existing session out.
    assert client.get("/api/v1/me").status_code == 401

    assert _login(_fresh(client), password=new_password).status_code == 200
    assert _login(_fresh(client), password=OWNER_PASSWORD).status_code == 401


def test_password_reset_request_does_not_reveal_unknown_accounts(
    client: TestClient, outbox: list[dict]
):
    response = client.post(
        "/api/v1/auth/password-reset/request",
        json={"email": "ghost@example.com"},
        headers=_csrf(client),
    )
    assert response.status_code == 202
    assert outbox == []


# -- docs gating ---------------------------------------------------


def test_interactive_docs_are_hidden_in_production(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("AUTH_SESSION_SECRET", "prod-secret")
    import app.settings as settings_module

    importlib.reload(settings_module)
    import app.main as main_module

    main_module = importlib.reload(main_module)
    try:
        from app.runtime_state import MemoryRuntimeState

        monkeypatch.setattr(
            main_module, "build_runtime_state", lambda _settings: MemoryRuntimeState()
        )
        monkeypatch.setattr(
            main_module,
            "settings",
            _settings(
                tmp_path,
                environment="production",
                redis_url="redis://test.invalid/0",
                resend_api_key="re_test",
                resend_from_email="Call Agent <test@example.com>",
            ),
        )
        with TestClient(main_module.app) as production_client:
            assert production_client.get("/docs").status_code == 404
            assert production_client.get("/openapi.json").status_code == 404
            assert production_client.get("/health").status_code == 200
    finally:
        # Restore the real environment *before* reloading, so the module-level
        # settings singleton is not left reflecting production for later tests.
        monkeypatch.undo()
        importlib.reload(settings_module)
        importlib.reload(main_module)
