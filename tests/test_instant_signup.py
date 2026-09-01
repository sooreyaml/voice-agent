"""Signup hands the tenant a live number and a working default agent at once."""

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
        openai_project_id="proj_test",
        database_path=tmp_path / "calls.sqlite3",
        database_url="",
        auth_session_secret="unit-test-secret",
        integration_encryption_key="k8jx5ZBLhq5deNjiiCfCrYKexwPaYN8SkNIwN5OEcU0=",
        environment="development",
        businesses_dir=BUSINESSES,
        app_base_url="http://testserver",
        resend_api_key="",
        resend_from_email="",
        # billing not configured -> tenant is created "active" with no subscription
        stripe_price_id="",
    )
    return replace(base, **overrides)


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import app.main

    monkeypatch.setattr(app.main, "settings", _settings(tmp_path))
    with TestClient(app.main.app) as test_client:
        test_client.get("/api/v1/ping")
        yield test_client


def _csrf(client: TestClient) -> dict[str, str]:
    return {"X-CSRF-Token": client.cookies["csrf"]}


def _signup(client: TestClient, *, email="owner@acme.test", organization_name="Acme Co"):
    return client.post(
        "/api/v1/auth/signup",
        json={"email": email, "password": PW, "organization_name": organization_name},
        headers=_csrf(client),
    )


def test_signup_claims_a_pool_number_and_publishes_a_live_default_agent(
    client: TestClient,
):
    store = client.app.state.store
    assert store.add_pool_number("+15550000123", "US") is True
    assert store.available_pool_count() == 1

    body = _signup(client).json()

    assert body["phone_number"] == "+15550000123"
    assert body["subscription"] is None  # billing not configured
    assert body["checkout_url"] is None

    # The number routes to a published agent named after the organization.
    from app.domains.businesses.repository import BusinessRepository

    profile = BusinessRepository(store).find_by_phone_number("+15550000123")
    assert profile is not None
    assert profile.name == "Acme Co"
    assert profile.phone_numbers == ["+15550000123"]

    # The pool row is now assigned, not available.
    assert store.available_pool_count() == 0
    assert store.pool_counts().get("assigned") == 1
    assert store.organization(str(body["organization"]["id"]))["lifecycle"] == "active"


def test_signup_with_an_empty_pool_still_creates_the_account(client: TestClient):
    body = _signup(client).json()
    assert body["phone_number"] is None

    me = client.get("/api/v1/me").json()
    assert me["organizations"][0]["role"] == "owner"


def test_two_signups_cannot_claim_the_same_number(client: TestClient):
    store = client.app.state.store
    store.add_pool_number("+15550000900", "US")

    first = _signup(client, email="a@acme.test", organization_name="A").json()
    second = _signup(
        _fresh(client), email="b@acme.test", organization_name="B"
    ).json()

    numbers = {first["phone_number"], second["phone_number"]}
    assert numbers == {"+15550000900", None}


def _fresh(client: TestClient) -> TestClient:
    other = TestClient(client.app)
    other.get("/api/v1/ping")
    return other
