"""Subscription checkout/portal, the immutable usage ledger, and Stripe webhook
lifecycle effects. The single billing plan is seeded from settings at startup
(``ensure_default_plan``) — there is no admin plan-authoring API any more."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.domains.billing.provider import HostedSession
from app.domains.billing.services.management import export_usage
from app.domains.billing.usage import insert_statement, usage_event

BUSINESSES = Path(__file__).resolve().parent.parent / "businesses"
PW = "correct horse staple 9"
PRICE_ID = "price_starter123"


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
        app_base_url="http://testserver",
        billing_enabled=True,
        stripe_secret_key="sk_test_fake",
        stripe_webhook_secret="whsec_test",
        # Signup itself is out of scope here (see test_instant_signup.py /
        # test_lifecycle.py); disable it so these tests own subscription state.
        stripe_price_id="",
        default_billing_plan_code="starter",
        stripe_meter_event_name="call_seconds",
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
            _seed_plan(client.app.state.store)
            yield client
    finally:
        app.main.app.dependency_overrides.pop(get_stripe_billing_service, None)


def _seed_plan(store) -> str:
    """Same shape ``ensure_default_plan`` would seed in production."""
    now = datetime.now(UTC).replace(microsecond=0)
    plan_id = "plan-starter-test"
    store.execute(
        "INSERT INTO billing_plans (id, code, name, status, currency,"
        " monthly_amount_minor, included_seconds,"
        " overage_amount_micros_per_second, stripe_price_id,"
        " stripe_meter_event_name, entitlements, created_at, updated_at)"
        " VALUES (?, 'starter', 'Starter', 'active', 'USD', 4900, 6000, 2500,"
        " ?, 'call_seconds', ?, ?, ?)"
        " ON CONFLICT (code) DO NOTHING",
        (
            plan_id,
            PRICE_ID,
            json.dumps({"concurrent_calls": 1}),
            now,
            now,
        ),
    )
    return plan_id


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


def _organization_id(session: TestClient) -> str:
    return session.get("/api/v1/me").json()["organizations"][0]["id"]


def _plan_id(client: TestClient) -> str:
    rows = client.app.state.store.query(
        "SELECT id FROM billing_plans WHERE code = 'starter'"
    )
    return str(rows[0]["id"])


def _subscription_event(organization_id: str, plan_id: str, *, status: str = "active") -> dict:
    now = datetime.now(UTC).replace(microsecond=0)
    return {
        "id": f"evt_subscription_{status}",
        "type": "customer.subscription.created",
        "data": {
            "object": {
                "id": "sub_123",
                "customer": "cus_123",
                "status": status,
                "cancel_at_period_end": False,
                "trial_end": None,
                "metadata": {
                    "organization_id": organization_id,
                    "billing_plan_id": plan_id,
                },
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


def _post_stripe_event(client: TestClient, event: dict, *, signature: str = "valid-signature"):
    return client.post(
        "/webhooks/stripe",
        content=json.dumps(event),
        headers={"Stripe-Signature": signature},
    )


def test_plan_checkout_webhook_and_portal_are_backend_only(
    app_client: TestClient, fake_stripe: FakeStripeBillingService
):
    owner = _account(app_client, "owner@example.com", "Customer Org")
    organization_id = _organization_id(owner)
    plan_id = _plan_id(app_client)

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
    assert fake_stripe.checkout_calls[0]["price_id"] == PRICE_ID
    assert fake_stripe.checkout_calls[0]["organization_id"] == organization_id

    before_webhook = owner.get(
        f"/api/v1/organizations/{organization_id}/billing"
    ).json()
    assert before_webhook["subscription"]["status"] == "checkout_pending"
    # Signup was not involved, so lifecycle stays at its default until a real
    # subscription event lands (covered separately below and in test_lifecycle.py).

    assert _post_stripe_event(
        app_client, _subscription_event(organization_id, plan_id)
    ).status_code == 200
    replay = _post_stripe_event(
        app_client, _subscription_event(organization_id, plan_id)
    )
    assert replay.json()["duplicate"] is True

    overview = owner.get(
        f"/api/v1/organizations/{organization_id}/billing"
    ).json()
    assert overview["subscription"]["status"] == "active"
    assert "provider_customer_id" not in overview["subscription"]
    assert app_client.app.state.store.organization(organization_id)["lifecycle"] == "active"

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
    owner = _account(app_client, "ledger@example.com", "Ledger Org")
    organization_id = _organization_id(owner)
    store = app_client.app.state.store
    occurred_at = datetime.now(UTC).replace(microsecond=0)

    event = usage_event(
        organization_id=organization_id,
        event_type="provider.adjustment",
        quantity=10,
        unit="second",
        source="reconciliation",
        idempotency_key="reconcile-request-001",
        provider_cost_micros=5000,
        customer_charge_micros=7500,
        occurred_at=occurred_at,
        metadata={"reason": "provider statement"},
    )
    store.execute(*insert_statement(event))
    # Idempotent: the same key does not duplicate.
    store.execute(*insert_statement(event))

    reversal = usage_event(
        organization_id=organization_id,
        event_type="provider.adjustment",
        quantity=-10,
        unit="second",
        source="reconciliation",
        idempotency_key="reconcile-reversal-001",
        provider_cost_micros=-5000,
        customer_charge_micros=-7500,
        occurred_at=occurred_at,
        reversal_of_event_id=event["id"],
    )
    store.execute(*insert_statement(reversal))

    usage = owner.get(f"/api/v1/organizations/{organization_id}/usage")
    assert usage.status_code == 200
    assert {item["quantity"] for item in usage.json()["items"]} == {-10, 10}

    with pytest.raises(sqlite3.DatabaseError, match="immutable"):
        store.execute(
            "UPDATE usage_events SET quantity = 99 WHERE id = ?", (event["id"],)
        )
    with pytest.raises(sqlite3.DatabaseError, match="immutable"):
        store.execute("DELETE FROM usage_events WHERE id = ?", (event["id"],))


def test_call_duration_exports_once_to_stripe_meter(
    app_client: TestClient, fake_stripe: FakeStripeBillingService
):
    owner = _account(app_client, "meter@example.com", "Meter Org")
    organization_id = _organization_id(owner)
    plan_id = _plan_id(app_client)
    store = app_client.app.state.store
    assert _post_stripe_event(
        app_client, _subscription_event(organization_id, plan_id)
    ).status_code == 200

    event = usage_event(
        organization_id=organization_id,
        event_type="twilio.call.duration",
        quantity=61,
        unit="second",
        source="twilio",
        idempotency_key="call-123-duration",
        provider_reference="call-123",
        occurred_at=datetime.now(UTC).replace(microsecond=0),
    )
    store.execute(*insert_statement(event))

    # The worker's usage-export ticker calls exactly this, on a timer.
    exported = export_usage(store, fake_stripe, limit=100)
    assert exported == {"sent": 1, "failed": 0, "remaining": 0}
    assert fake_stripe.meter_calls[0]["quantity"] == 61
    assert fake_stripe.meter_calls[0]["customer_id"] == "cus_123"

    second = export_usage(store, fake_stripe, limit=100)
    assert second == {"sent": 0, "failed": 0, "remaining": 0}
    assert len(fake_stripe.meter_calls) == 1


def test_billing_is_tenant_scoped_and_admin_plan_authoring_is_gone(
    app_client: TestClient,
):
    owner = _account(app_client, "tenant@example.com", "Tenant Org")
    outsider = _account(app_client, "outside@example.com", "Outside Org")
    organization_id = _organization_id(owner)
    plan_id = _plan_id(app_client)

    assert outsider.get(
        f"/api/v1/organizations/{organization_id}/billing"
    ).status_code == 404

    # Admin is a read-only platform overview now; there is nowhere left to
    # author or edit a plan, even for a platform administrator.
    admin = _account(app_client, "staff@platform.example", "Platform Staff")
    app_client.app.state.store.set_platform_admin(
        admin.get("/api/v1/me").json()["user"]["id"], True
    )
    assert admin.post(
        "/api/v1/admin/billing/plans",
        json={"code": "forbidden"},
        headers=_csrf(admin),
    ).status_code == 404
    assert admin.post(
        "/api/v1/admin/billing/usage-events",
        json={},
        headers=_csrf(admin),
    ).status_code == 404

    invalid = _post_stripe_event(
        app_client,
        _subscription_event(organization_id, plan_id),
        signature="invalid",
    )
    assert invalid.status_code == 400
