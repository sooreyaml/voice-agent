"""Every /api/v1 failure comes back as the shared JSON envelope, never a bare 500."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

BUSINESSES = Path(__file__).resolve().parent.parent / "businesses"
PW = "correct horse staple 9"


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
        resend_api_key="",
        resend_from_email="",
        stripe_price_id="",
    )
    return replace(base, **overrides)


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import app.main

    monkeypatch.setattr(app.main, "settings", _settings(tmp_path))
    # raise_server_exceptions=False so the test sees the 500 *response* the
    # middleware produced, not the re-raised exception.
    with TestClient(app.main.app, raise_server_exceptions=False) as test_client:
        test_client.get("/api/v1/ping")
        yield test_client


def _csrf(client: TestClient) -> dict[str, str]:
    return {"X-CSRF-Token": client.cookies["csrf"]}


def test_unhandled_exception_returns_the_envelope_not_a_bare_500(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    from app.domains.auth import router

    def _boom(*_a, **_kw):
        raise RuntimeError("something deep broke")

    monkeypatch.setattr(router, "authenticate", _boom)

    response = client.post(
        "/api/v1/auth/login",
        json={"email": "a@b.test", "password": PW},
        headers=_csrf(client),
    )

    assert response.status_code == 500
    assert response.headers["content-type"].startswith("application/json")
    error = response.json()["error"]
    assert error["code"] == "internal_error"
    assert error["message"] and not error["message"].lower().startswith("internal server")
    assert error["request_id"]
    # The leak check: no stack trace / exception text in the body.
    assert "something deep broke" not in response.text
    assert "Traceback" not in response.text


def test_wrong_method_on_an_api_route_is_friendly(client: TestClient):
    # /api/v1/auth/login is POST-only.
    response = client.get("/api/v1/auth/login")

    assert response.status_code == 405
    error = response.json()["error"]
    assert error["code"] == "method_not_allowed"
    assert error["message"] != "Method Not Allowed"
    assert error["code"] != "internal_error"
    assert "allow" in {h.lower() for h in response.headers}


def test_unknown_api_path_is_a_friendly_404(client: TestClient):
    response = client.get("/api/v1/does-not-exist")

    assert response.status_code == 404
    error = response.json()["error"]
    assert error["code"] == "not_found"
    assert error["message"] != "Not Found"
    assert error["request_id"]


def test_duplicate_signup_still_reports_email_taken(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    from app.domains.auth import router

    monkeypatch.setattr(router, "deliver_email_verification_code", lambda **_kw: None)

    body = {"email": "dupe@acme.test", "password": PW, "organization_name": "Acme"}
    first = client.post("/api/v1/auth/signup", json=body, headers=_csrf(client))
    assert first.status_code == 201

    other = TestClient(client.app, raise_server_exceptions=False)
    other.get("/api/v1/ping")
    dupe = other.post(
        "/api/v1/auth/signup",
        json={**body, "organization_name": "Acme Two"},
        headers={"X-CSRF-Token": other.cookies["csrf"]},
    )
    assert dupe.status_code == 409
    error = dupe.json()["error"]
    assert error["code"] == "email_taken"
    assert error["field_errors"].get("email")


def test_signup_survives_a_verification_email_failure(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    from app.domains.auth import router

    def _mail_down(**_kw):
        raise RuntimeError("Resend is down")

    monkeypatch.setattr(router, "deliver_email_verification_code", _mail_down)

    response = client.post(
        "/api/v1/auth/signup",
        json={
            "email": "mailfail@acme.test",
            "password": PW,
            "organization_name": "Mailfail Co",
        },
        headers=_csrf(client),
    )

    assert response.status_code == 201
    assert response.json()["user"]["email"] == "mailfail@acme.test"
    # The session cookie was still set, so the account is usable.
    assert client.cookies.get("session")
