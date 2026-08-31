"""Phase 6 subscription and immutable usage-ledger API tests."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.domains.billing.provider import HostedSession

BUSINESSES = Path(__file__).resolve().parent.parent / "businesses"
PW = "correct horse staple 9"


class FakeStripeBillingService:
    secret_key = "sk_test_fake"

    def __init__(self) -> None:
        self.checkout_calls: list[dict] = []
        self.portal_calls: list[dict] = []
        self.meter_calls: list[dict] = []

    def create_checkout_session(self, **kwargs):
        self.checkout_calls.append(kwargs)
        return HostedSession(
            id="cs_test_checkout",
            url="https://checkout.stripe.test/session",
            expires_at=1_900_000_000,
        )

    def create_portal_session(self, **kwargs):
        self.portal_calls.append(kwargs)
        return HostedSession(
            id="bps_test_portal",
            url="https://billing.stripe.test/session",
        )

    def construct_event(self, payload, signature):
        if signature != "valid-signature":
            raise ValueError("bad signature")
        return json.loads(payload)

    def send_meter_event(self, **kwargs):
        self.meter_calls.append(kwargs)
        return kwargs["identifier"]


def _settings(tmp_path: Path):
    from app.settings import load_settings

    return replace(
        load_settings(),
        openai_api_key="sk-test",
        openai_webhook_secret="whsec_test",
        openai_project_id="proj_test",
        database_path=tmp_path / "calls.sqlite3",
        database_url="",
        auth_session_secret="unit-test-secret",
        environment="development",
        businesses_dir=BUSINESSES,
        business_config_source="yaml",
        app_base_url="http://testserver",
        stripe_secret_key="sk_test_fake",
        stripe_webhook_secret="whsec_test",
    )


@pytest.fixture
def fake_stripe() -> FakeStripeBillingService:
    return FakeStripeBillingService()


@pytest.fixture
def app_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_stripe: FakeStripeBillingService,
):
    import app.main
    from app.domains.billing.dependencies import get_stripe_billing_service

    monkeypatch.setattr(app.main, "settings", _settings(tmp_path))
    app.main.app.dependency_overrides[get_stripe_billing_service] = lambda: fake_stripe
    try:
        with TestClient(app.main.app) as client:
            client.get("/api/v1/ping")
            yield client
    finally:
        app.main.app.dependency_overrides.pop(get_stripe_billing_service, None)


def _csrf(client: TestClient) -> dict[str, str]:
    return {"X-CSRF-Token": client.cookies["csrf"]}


def _account(client: TestClient, email: str, organization: str) -> TestClient:
    session = TestClient(client.app)
    session.get("/api/v1/ping")
    response = session.post(
        "/api/v1/auth/signup",
        json={"email": email, "password": PW, "organization_name": organization},
        headers=_csrf(session),
    )
    assert response.status_code == 201, response.text
    return session


def _platform_admin(client: TestClient) -> TestClient:
    admin = _account(client, "staff@platform.example", "Platform Staff")
    user_id = admin.get("/api/v1/me").json()["user"]["id"]
    client.app.state.store.set_platform_admin(user_id, True)
    return admin


def _organization_id(session: TestClient) -> str:
    return session.get("/api/v1/me").json()["organizations"][0]["id"]


def _plan() -> dict:
    return {
        "code": "starter",
        "name": "Starter",
        "currency": "usd",
        "monthly_amount_minor": 4900,
        "included_seconds": 6000,
        "overage_amount_micros_per_second": 2500,
        "stripe_price_id": "price_starter123",
        "stripe_meter_event_name": "call_seconds",
        "entitlements": {"concurrent_calls": 1},
    }


def _create_plan(admin: TestClient) -> dict:
    response = admin.post(
        "/api/v1/admin/billing/plans",
        json=_plan(),
        headers=_csrf(admin),
    )
    assert response.status_code == 201, response.text
    return response.json()


def _subscription_event(organization_id: str, plan_id: str) -> dict:
    now = datetime.now(UTC).replace(microsecond=0)
    return {
        "id": "evt_subscription_created",
        "type": "customer.subscription.created",
        "data": {
            "object": {
                "id": "sub_123",
                "customer": "cus_123",
                "status": "active",
                "cancel_at_period_end": False,
                "trial_end": None,
                "metadata": {
                    "organization_id": organization_id,
                    "billing_plan_id": plan_id,
                },
                "items": {
                    "data": [
                        {
                            "price": {"id": "price_starter123"},
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


def _activate_subscription(
    client: TestClient, organization_id: str, plan_id: str
) -> None:
    response = client.post(
        "/webhooks/stripe",
        content=json.dumps(_subscription_event(organization_id, plan_id)),
        headers={"Stripe-Signature": "valid-signature"},
    )
    assert response.status_code == 200, response.text


def test_plan_checkout_webhook_and_portal_are_backend_only(
    app_client: TestClient, fake_stripe: FakeStripeBillingService
):
    admin = _platform_admin(app_client)
    plan = _create_plan(admin)
    owner = _account(app_client, "owner@example.com", "Customer Org")
    organization_id = _organization_id(owner)

    plans = owner.get("/api/v1/billing/plans")
    assert plans.status_code == 200
    assert plans.json()[0]["entitlements"] == {"concurrent_calls": 1}

    checkout = owner.post(
        f"/api/v1/organizations/{organization_id}/billing/checkout",
        json={
            "plan_code": "starter",
            "success_url": "https://service.example/billing/success",
            "cancel_url": "https://service.example/billing/cancel",
            "idempotency_key": "checkout-request-001",
        },
        headers=_csrf(owner),
    )
    assert checkout.status_code == 201, checkout.text
    assert checkout.json()["url"].startswith("https://checkout.stripe.test/")
    assert fake_stripe.checkout_calls[0]["price_id"] == "price_starter123"
    assert fake_stripe.checkout_calls[0]["organization_id"] == organization_id

    before_webhook = owner.get(
        f"/api/v1/organizations/{organization_id}/billing"
    ).json()
    assert before_webhook["subscription"]["status"] == "checkout_pending"

    _activate_subscription(app_client, organization_id, plan["id"])
    replay = app_client.post(
        "/webhooks/stripe",
        content=json.dumps(_subscription_event(organization_id, plan["id"])),
        headers={"Stripe-Signature": "valid-signature"},
    )
    assert replay.json()["duplicate"] is True

    overview = owner.get(
        f"/api/v1/organizations/{organization_id}/billing"
    ).json()
    assert overview["subscription"]["status"] == "active"
    assert "provider_customer_id" not in overview["subscription"]

    portal = owner.post(
        f"/api/v1/organizations/{organization_id}/billing/portal",
        json={"return_url": "https://service.example/billing"},
        headers=_csrf(owner),
    )
    assert portal.status_code == 201, portal.text
    assert fake_stripe.portal_calls == [
        {"customer_id": "cus_123", "return_url": "https://service.example/billing"}
    ]


def test_usage_events_are_idempotent_immutable_and_reversed_by_append(
    app_client: TestClient,
):
    admin = _platform_admin(app_client)
    owner = _account(app_client, "ledger@example.com", "Ledger Org")
    organization_id = _organization_id(owner)
    occurred_at = datetime.now(UTC).replace(microsecond=0).isoformat()
    body = {
        "organization_id": organization_id,
        "event_type": "provider.adjustment",
        "quantity": 10,
        "unit": "second",
        "provider_cost_micros": 5000,
        "customer_charge_micros": 7500,
        "currency": "USD",
        "source": "reconciliation",
        "idempotency_key": "reconcile-request-001",
        "metadata": {"reason": "provider statement"},
        "occurred_at": occurred_at,
    }
    first = admin.post(
        "/api/v1/admin/billing/usage-events",
        json=body,
        headers=_csrf(admin),
    )
    assert first.status_code == 201, first.text
    replay = admin.post(
        "/api/v1/admin/billing/usage-events",
        json=body,
        headers=_csrf(admin),
    )
    assert replay.status_code == 201
    assert replay.json()["id"] == first.json()["id"]

    reversal = {
        **body,
        "quantity": -10,
        "provider_cost_micros": -5000,
        "customer_charge_micros": -7500,
        "idempotency_key": "reconcile-reversal-001",
        "reversal_of_event_id": first.json()["id"],
    }
    reversed_response = admin.post(
        "/api/v1/admin/billing/usage-events",
        json=reversal,
        headers=_csrf(admin),
    )
    assert reversed_response.status_code == 201, reversed_response.text

    usage = owner.get(f"/api/v1/organizations/{organization_id}/usage")
    assert usage.status_code == 200
    assert {item["quantity"] for item in usage.json()["items"]} == {-10, 10}
    with pytest.raises(sqlite3.DatabaseError, match="immutable"):
        app_client.app.state.store.execute(
            "UPDATE usage_events SET quantity = 99 WHERE id = ?",
            (first.json()["id"],),
        )
    with pytest.raises(sqlite3.DatabaseError, match="immutable"):
        app_client.app.state.store.execute(
            "DELETE FROM usage_events WHERE id = ?", (first.json()["id"],)
        )


def test_call_duration_exports_once_to_stripe_meter(
    app_client: TestClient, fake_stripe: FakeStripeBillingService
):
    admin = _platform_admin(app_client)
    plan = _create_plan(admin)
    owner = _account(app_client, "meter@example.com", "Meter Org")
    organization_id = _organization_id(owner)
    _activate_subscription(app_client, organization_id, plan["id"])

    usage = admin.post(
        "/api/v1/admin/billing/usage-events",
        json={
            "organization_id": organization_id,
            "event_type": "twilio.call.duration",
            "quantity": 61,
            "unit": "second",
            "source": "twilio",
            "idempotency_key": "call-123-duration",
            "provider_reference": "call-123",
            "occurred_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        },
        headers=_csrf(admin),
    )
    assert usage.status_code == 201, usage.text
    exported = admin.post(
        "/api/v1/admin/billing/usage-exports/stripe",
        json={"limit": 100},
        headers=_csrf(admin),
    )
    assert exported.status_code == 200, exported.text
    assert exported.json() == {"sent": 1, "failed": 0, "remaining": 0}
    assert fake_stripe.meter_calls[0]["quantity"] == 61
    assert fake_stripe.meter_calls[0]["customer_id"] == "cus_123"

    second = admin.post(
        "/api/v1/admin/billing/usage-exports/stripe",
        json={"limit": 100},
        headers=_csrf(admin),
    )
    assert second.json() == {"sent": 0, "failed": 0, "remaining": 0}
    assert len(fake_stripe.meter_calls) == 1


def test_billing_authorization_and_webhook_signature(
    app_client: TestClient,
):
    admin = _platform_admin(app_client)
    plan = _create_plan(admin)
    owner = _account(app_client, "tenant@example.com", "Tenant Org")
    outsider = _account(app_client, "outside@example.com", "Outside Org")
    organization_id = _organization_id(owner)

    assert outsider.get(
        f"/api/v1/organizations/{organization_id}/billing"
    ).status_code == 404
    assert owner.post(
        "/api/v1/admin/billing/plans",
        json={**_plan(), "code": "forbidden"},
        headers=_csrf(owner),
    ).status_code == 403
    invalid = app_client.post(
        "/webhooks/stripe",
        content=json.dumps(_subscription_event(organization_id, plan["id"])),
        headers={"Stripe-Signature": "invalid"},
    )
    assert invalid.status_code == 400
