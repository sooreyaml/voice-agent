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
        number_pool_country="GB",
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


def _signup(
    client: TestClient, *, email="owner@acme.test", organization_name="Acme Co"
):
    return client.post(
        "/api/v1/auth/signup",
        json={"email": email, "password": PW, "organization_name": organization_name},
        headers=_csrf(client),
    )


def test_signup_claims_a_pool_number_and_publishes_a_live_default_agent(
    client: TestClient,
    fake_provisioning_provider,
):
    store = client.app.state.store
    fake_provisioning_provider.add_available("+442071234567")

    body = _signup(client).json()

    assert body["phone_number"] == "+442071234567"
    assert body["subscription"] is None  # billing not configured
    assert body["checkout_url"] is None

    # The number routes to a published agent named after the organization.
    from app.domains.businesses.repository import BusinessRepository

    profile = BusinessRepository(store).find_by_phone_number("+442071234567")
    assert profile is not None
    assert profile.name == "Acme Co"
    assert profile.phone_numbers == ["+442071234567"]

    # The number was bought during signup and recorded as assigned.
    assert fake_provisioning_provider.purchased == ["+442071234567"]
    assert store.available_pool_count() == 0
    assert store.pool_counts().get("assigned") == 1
    phone = store.query(
        "SELECT provider, provider_account_sid, provider_number_sid,"
        " provider_trunk_sid, country_code, number_type FROM phone_numbers"
        " WHERE organization_id = ?",
        (str(body["organization"]["id"]),),
    )[0]
    assert phone == {
        "provider": "twilio",
        "provider_account_sid": "AC_test",
        "provider_number_sid": "PN00000000000000000000000000000001",
        "provider_trunk_sid": "TK_test",
        "country_code": "GB",
        "number_type": "mobile",
    }
    assert store.organization(str(body["organization"]["id"]))["lifecycle"] == "active"


def test_signup_ignores_a_pool_number_from_a_different_country(client: TestClient):
    store = client.app.state.store
    # A leftover US number from before the pool country was switched to GB.
    store.add_pool_number("+15550000123", "US")

    body = _signup(client).json()

    assert body["phone_number"] is None
    # The US number was not handed out and is still available.
    assert store.available_pool_count() == 1


def test_signup_with_an_empty_pool_still_creates_the_account(client: TestClient):
    body = _signup(client).json()
    assert body["phone_number"] is None

    me = client.get("/api/v1/me").json()
    assert me["organizations"][0]["role"] == "owner"


def test_two_signups_cannot_claim_the_same_number(client: TestClient):
    store = client.app.state.store
    store.add_pool_number("+442071230900", "GB")

    first = _signup(client, email="a@acme.test", organization_name="A").json()
    second = _signup(_fresh(client), email="b@acme.test", organization_name="B").json()

    numbers = {first["phone_number"], second["phone_number"]}
    assert numbers == {"+442071230900", None}


def test_deployment_backfill_provisions_pending_signups(
    client: TestClient, fake_provisioning_provider
):
    first = _signup(client, email="a@acme.test", organization_name="A").json()
    second = _signup(_fresh(client), email="b@acme.test", organization_name="B").json()
    fake_provisioning_provider.add_available(
        "+442071230901",
        "+442071230902",
    )

    from app.domains.businesses.repository import BusinessRepository
    from app.domains.telephony.service import provision_pending_organizations

    result = provision_pending_organizations(
        client.app.state.store,
        fake_provisioning_provider,
        default_profile_template=BUSINESSES / "_default.yaml",
        default_timezone="Europe/London",
        country="GB",
        number_type="mobile",
        sms_enabled=True,
        bundle_sid="BU_test",
        address_sid="AD_test",
        limit=10,
    )

    assert result.attempted == 2
    assert len(result.provisioned) == 2
    assert result.failures == []
    repository = BusinessRepository(client.app.state.store)
    assert repository.agent_overview(str(first["organization"]["id"])) is not None
    assert repository.agent_overview(str(second["organization"]["id"])) is not None

    repeated = provision_pending_organizations(
        client.app.state.store,
        fake_provisioning_provider,
        default_profile_template=BUSINESSES / "_default.yaml",
        default_timezone="Europe/London",
        country="GB",
        number_type="mobile",
        sms_enabled=True,
        bundle_sid="BU_test",
        address_sid="AD_test",
        limit=10,
    )
    assert repeated.attempted == 0
    assert repeated.provisioned == []


def _fresh(client: TestClient) -> TestClient:
    other = TestClient(client.app)
    other.get("/api/v1/ping")
    return other
