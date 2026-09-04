"""The gated signup flow: register -> verify email -> complete the business
profile -> number provisioned (billing off) or checkout link + deferred number
(billing on).
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.conftest import (
    DEFAULT_INTAKE,
    api_signup,
    complete_business_profile,
    csrf_headers,
    verify_owner_email,
)

BUSINESSES = Path(__file__).resolve().parent.parent / "businesses"
PW = "correct horse staple 9"
PRICE_ID = "price_starter_gated"


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
        number_pool_country="US",
        require_email_verification=False,
        billing_enabled=False,
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


def _fresh(client: TestClient) -> TestClient:
    other = TestClient(client.app)
    other.get("/api/v1/ping")
    return other


# -- signup no longer hands over a number -------------------------------


def test_signup_creates_a_registered_org_and_no_number(client: TestClient):
    store = client.app.state.store
    store.add_pool_number("+15550009000", "US")

    body = api_signup(client)

    assert body["next_step"] == "verify_email"
    assert "phone_number" not in body
    assert "checkout_url" not in body
    org_id = str(body["organization"]["id"])
    assert store.organization(org_id)["lifecycle"] == "registered"
    # The pool number was not touched.
    assert store.available_pool_count() == 1
    # Session is live and the owner is signed in but unverified.
    me = client.get("/api/v1/me").json()
    assert me["user"]["email_verified"] is False


def test_verifying_email_moves_the_org_to_profile_pending(client: TestClient):
    body = api_signup(client)
    org_id = str(body["organization"]["id"])

    verify_owner_email(client)

    assert client.app.state.store.organization(org_id)["lifecycle"] == "profile_pending"


# -- completing the business profile provisions the number -------------


def test_business_profile_provisions_a_real_agent(client: TestClient):
    store = client.app.state.store
    store.add_pool_number("+15550001234", "US")
    org_id = str(api_signup(client)["organization"]["id"])

    state = complete_business_profile(
        client,
        org_id,
        business_name="Bright Smiles Dental",
        what_you_do="A dental practice booking checkups and cleanings by phone.",
    )
    assert state.status_code == 200, state.text
    payload = state.json()
    assert payload["phone_number"] == "+15550001234"
    assert payload["number_provisioned"] is True
    assert payload["blocking_reasons"] == []
    assert store.organization(org_id)["lifecycle"] == "active"

    from app.domains.businesses.repository import BusinessRepository

    profile = BusinessRepository(store).find_by_phone_number("+15550001234")
    assert profile is not None
    assert profile.name == "Bright Smiles Dental"
    assert "dental practice" in json.dumps(profile.raw).lower()

    phone = store.query(
        "SELECT provider, provider_number_sid, country_code, number_type"
        " FROM phone_numbers WHERE organization_id = ?",
        (org_id,),
    )[0]
    assert phone["country_code"] == "US"
    assert phone["provider"] == "twilio"


def test_profile_is_idempotent_and_does_not_buy_a_second_number(client: TestClient):
    store = client.app.state.store
    store.add_pool_number("+15550005555", "US")
    org_id = str(api_signup(client)["organization"]["id"])

    first = complete_business_profile(client, org_id)
    assert first.status_code == 200
    store.add_pool_number("+15550006666", "US")
    again = complete_business_profile(client, org_id)

    assert again.status_code == 200
    assert again.json()["phone_number"] == "+15550005555"
    assert store.available_pool_count() == 1  # the spare was left alone


def test_empty_pool_saves_the_profile_then_a_retry_provisions(
    client: TestClient, fake_provisioning_provider
):
    org_id = str(api_signup(client)["organization"]["id"])

    blocked = complete_business_profile(client, org_id)
    assert blocked.status_code == 503
    assert blocked.json()["error"]["code"] == "number_provisioning_failed"

    onboarding = client.get(f"/api/v1/organizations/{org_id}/onboarding").json()
    assert onboarding["profile_complete"] is True
    assert onboarding["number_provisioned"] is False
    assert onboarding["blocking_reasons"] == []  # nothing the owner can fix

    fake_provisioning_provider.add_available("+15550007777")
    retry = client.post(
        f"/api/v1/organizations/{org_id}/business-profile/provision",
        headers=csrf_headers(client),
    )
    assert retry.status_code == 200
    assert retry.json()["phone_number"] == "+15550007777"


def test_a_pool_number_from_another_country_is_not_used(client: TestClient):
    store = client.app.state.store
    store.add_pool_number("+442071234567", "GB")  # US deployment
    org_id = str(api_signup(client)["organization"]["id"])

    blocked = complete_business_profile(client, org_id)
    assert blocked.status_code == 503
    assert store.available_pool_count() == 1


def test_two_onboardings_cannot_claim_the_same_number(client: TestClient):
    store = client.app.state.store
    store.add_pool_number("+15550008888", "US")

    a = _fresh(client)
    api_signup(a, email="a@acme.test", organization_name="A Co")
    org_a = a.get("/api/v1/me").json()["organizations"][0]["id"]
    b = _fresh(client)
    api_signup(b, email="b@acme.test", organization_name="B Co")
    org_b = b.get("/api/v1/me").json()["organizations"][0]["id"]

    first = complete_business_profile(
        a, org_a, business_name="A Co", contact_email="a@acme.test"
    )
    second = complete_business_profile(
        b, org_b, business_name="B Co", contact_email="b@acme.test"
    )

    outcomes = {first.status_code, second.status_code}
    assert outcomes == {200, 503}
    assert store.pool_counts().get("assigned") == 1


# -- gates -------------------------------------------------------------


def test_provision_is_blocked_until_the_profile_is_complete(client: TestClient):
    client.app.state.store.add_pool_number("+15550001111", "US")
    org_id = str(api_signup(client)["organization"]["id"])

    resp = client.post(
        f"/api/v1/organizations/{org_id}/agent/provision", headers=csrf_headers(client)
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "business_profile_incomplete"


def test_verified_email_is_required_when_the_flag_is_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import app.main

    monkeypatch.setattr(
        app.main, "settings", _settings(tmp_path, require_email_verification=True)
    )
    with TestClient(app.main.app) as client:
        client.get("/api/v1/ping")
        client.app.state.store.add_pool_number("+15550002222", "US")
        org_id = str(api_signup(client)["organization"]["id"])

        blocked = complete_business_profile(client, org_id)
        assert blocked.status_code == 403
        assert blocked.json()["error"]["code"] == "email_not_verified"

        onboarding = client.get(
            f"/api/v1/organizations/{org_id}/onboarding"
        ).json()
        assert "email_not_verified" in onboarding["blocking_reasons"]

        verify_owner_email(client)
        ok = complete_business_profile(client, org_id)
        assert ok.status_code == 200
        assert ok.json()["phone_number"] == "+15550002222"


def test_business_profile_rejects_a_bad_payload(client: TestClient):
    org_id = str(api_signup(client)["organization"]["id"])

    bad = client.put(
        f"/api/v1/organizations/{org_id}/business-profile",
        json={**DEFAULT_INTAKE, "country": "United States", "what_you_do": "hi"},
        headers=csrf_headers(client),
    )
    assert bad.status_code == 422
    fields = bad.json()["error"]["field_errors"]
    assert "body.country" in fields or "country" in str(fields)


def test_business_profile_is_tenant_scoped(client: TestClient):
    store = client.app.state.store
    store.add_pool_number("+15550003333", "US")
    owner_a = _fresh(client)
    api_signup(owner_a, email="a@acme.test", organization_name="A Co")
    org_a = owner_a.get("/api/v1/me").json()["organizations"][0]["id"]

    intruder = _fresh(client)
    api_signup(intruder, email="intruder@evil.test", organization_name="Evil Co")

    resp = complete_business_profile(
        intruder, org_a, business_name="A Co", contact_email="a@acme.test"
    )
    assert resp.status_code in (403, 404)
    assert store.available_pool_count() == 1


# -- legacy backfill ignores the gated flow ---------------------------


def test_backfill_skips_gated_signups_without_a_completed_profile(client: TestClient):
    store = client.app.state.store
    api_signup(client)  # registered, no intake

    from app.domains.telephony.service import provision_pending_organizations

    result = provision_pending_organizations(
        store,
        None,
        default_profile_template=BUSINESSES / "_default.yaml",
        default_timezone="America/New_York",
        country="US",
        number_type="local",
        sms_enabled=False,
        bundle_sid=None,
        address_sid=None,
        limit=10,
    )
    assert result.attempted == 0


# -- billing on: checkout link now, number after payment --------------


class _FakeStripe:
    secret_key = "sk_test_fake"

    def __init__(self) -> None:
        self.checkout_calls: list[dict] = []

    def create_checkout_session(self, **kwargs):
        from app.domains.billing.provider import HostedSession

        self.checkout_calls.append(kwargs)
        return HostedSession(
            id="cs_gated",
            url="https://checkout.stripe.test/gated",
            expires_at=1_900_000_000,
        )

    def construct_event(self, payload, signature):
        if signature != "valid-signature":
            raise ValueError("bad signature")
        return json.loads(payload)


def _seed_plan(store) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    store.execute(
        "INSERT INTO billing_plans (id, code, name, status, currency,"
        " monthly_amount_minor, included_seconds,"
        " overage_amount_micros_per_second, stripe_price_id,"
        " stripe_meter_event_name, entitlements, created_at, updated_at)"
        " VALUES ('plan-gated', 'starter', 'Starter', 'active', 'USD', 0, 0, 0,"
        " ?, 'call_seconds', '{}', ?, ?) ON CONFLICT (code) DO NOTHING",
        (PRICE_ID, now, now),
    )


@pytest.fixture
def billing_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import app.main
    from app.domains.billing.dependencies import get_stripe_billing_service

    monkeypatch.setattr(
        app.main,
        "settings",
        _settings(
            tmp_path,
            billing_enabled=True,
            stripe_secret_key="sk_test_fake",
            stripe_webhook_secret="whsec_test",
            stripe_price_id=PRICE_ID,
        ),
    )
    fake = _FakeStripe()
    app.main.app.dependency_overrides[get_stripe_billing_service] = lambda: fake
    try:
        with TestClient(app.main.app) as client:
            client.get("/api/v1/ping")
            _seed_plan(client.app.state.store)
            client.fake_stripe = fake
            yield client
    finally:
        app.main.app.dependency_overrides.pop(get_stripe_billing_service, None)


def _checkout_completed_event(organization_id: str) -> dict:
    return {
        "id": "evt_checkout_gated",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": "cs_gated",
                "customer": "cus_gated",
                "subscription": "sub_gated",
                "metadata": {"organization_id": organization_id},
            }
        },
    }


def _subscription_active_event(organization_id: str) -> dict:
    now = datetime.now(UTC).replace(microsecond=0)
    return {
        "id": "evt_sub_gated_active",
        "type": "customer.subscription.updated",
        "data": {
            "object": {
                "id": "sub_gated",
                "customer": "cus_gated",
                "status": "active",
                "cancel_at_period_end": False,
                "trial_end": None,
                "metadata": {"organization_id": organization_id},
                "items": {
                    "data": [
                        {
                            "price": {"id": PRICE_ID},
                            "current_period_start": int(now.timestamp()),
                            "current_period_end": int(
                                (now + timedelta(days=30)).timestamp()
                            ),
                        }
                    ]
                },
            }
        },
    }


def _post_event(client: TestClient, event: dict):
    return client.post(
        "/webhooks/stripe",
        content=json.dumps(event),
        headers={"Stripe-Signature": "valid-signature"},
    )


def test_billing_on_defers_the_number_until_checkout_is_paid(
    billing_client: TestClient,
):
    client = billing_client
    store = client.app.state.store
    store.add_pool_number("+15550004444", "US")
    org_id = str(api_signup(client)["organization"]["id"])

    state = complete_business_profile(client, org_id)
    assert state.status_code == 200, state.text
    body = state.json()
    assert body["checkout_url"] == "https://checkout.stripe.test/gated"
    assert body["number_provisioned"] is False
    assert store.organization(org_id)["lifecycle"] == "eligible"
    assert store.active_phone_number(org_id) is None

    assert _post_event(client, _checkout_completed_event(org_id)).status_code == 200
    assert _post_event(client, _subscription_active_event(org_id)).status_code == 200

    assert store.active_phone_number(org_id) == "+15550004444"
    assert store.organization(org_id)["lifecycle"] == "active"


def test_provisioning_sweep_backfills_a_paid_org_the_webhook_missed(
    billing_client: TestClient, fake_provisioning_provider
):
    client = billing_client
    store = client.app.state.store
    org_id = str(api_signup(client)["organization"]["id"])

    # Profile complete, checkout started, but the pool was empty when the
    # webhook fired -> org sits 'eligible'/'provisioning' with a paid sub.
    complete_business_profile(client, org_id)
    store.execute(
        "UPDATE subscriptions SET status = 'active' WHERE organization_id = ?",
        (org_id,),
    )
    store.advance_organization_lifecycle(org_id, "provisioning", ("eligible",))
    fake_provisioning_provider.add_available("+15550009999")

    from app.domains.onboarding import service as onboarding_service

    provisioned = onboarding_service.sweep_awaiting_number(
        store, fake_provisioning_provider, client.app.state.settings, limit=10
    )
    assert provisioned == 1
    assert store.active_phone_number(org_id) == "+15550009999"
    assert store.organization(org_id)["lifecycle"] == "active"


def test_reaper_grace_leaves_a_just_created_eligible_org(billing_client: TestClient):
    from app.domains.billing.services.lifecycle import reap_abandoned_signups

    client = billing_client
    store = client.app.state.store
    org_id = str(api_signup(client)["organization"]["id"])
    complete_business_profile(client, org_id)  # -> eligible, unpaid

    # Inside the grace window: untouched.
    assert reap_abandoned_signups(store, grace_hours=24, quarantine_days=30) == 0
    store.execute(
        "UPDATE organizations SET created_at = ? WHERE id = ?",
        (datetime.now(UTC) - timedelta(hours=48), org_id),
    )
    assert reap_abandoned_signups(store, grace_hours=24, quarantine_days=30) == 1
    assert store.organization(org_id)["lifecycle"] == "closed"
