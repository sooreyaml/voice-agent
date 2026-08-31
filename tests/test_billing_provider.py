from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from app.domains.billing.provider import StripeBillingService


def test_stripe_provider_uses_hosted_sessions_and_idempotent_meter_events():
    client = MagicMock()
    client.v1.checkout.sessions.create.return_value = SimpleNamespace(
        id="cs_test_123",
        url="https://checkout.stripe.test/session",
        expires_at=1_800_000_000,
    )
    client.v1.billing_portal.sessions.create.return_value = SimpleNamespace(
        id="bps_test_123",
        url="https://billing.stripe.test/session",
    )
    client.v1.billing.meter_events.create.return_value = SimpleNamespace(
        identifier="usage-event-123"
    )
    factory = MagicMock(return_value=client)
    provider = StripeBillingService(
        "sk_test_secret",
        "whsec_test",
        client_factory=factory,
    )

    checkout = provider.create_checkout_session(
        organization_id="org-123",
        plan_id="plan-123",
        price_id="price_123",
        customer_email="owner@example.com",
        customer_id=None,
        success_url="https://service.example/success",
        cancel_url="https://service.example/cancel",
        idempotency_key="checkout-123",
    )
    portal = provider.create_portal_session(
        customer_id="cus_123",
        return_url="https://service.example/billing",
    )
    identifier = provider.send_meter_event(
        event_name="call_seconds",
        customer_id="cus_123",
        quantity=61,
        identifier="usage-event-123",
        timestamp=1_799_999_000,
    )

    assert checkout.id == "cs_test_123"
    assert portal.id == "bps_test_123"
    assert identifier == "usage-event-123"
    factory.assert_called_once_with("sk_test_secret", max_network_retries=2)
    checkout_params = client.v1.checkout.sessions.create.call_args.args[0]
    assert checkout_params["mode"] == "subscription"
    assert checkout_params["customer_email"] == "owner@example.com"
    assert checkout_params["metadata"]["organization_id"] == "org-123"
    assert client.v1.checkout.sessions.create.call_args.kwargs["options"] == {
        "idempotency_key": "checkout-123"
    }
    client.v1.billing.meter_events.create.assert_called_once_with(
        {
            "event_name": "call_seconds",
            "payload": {"stripe_customer_id": "cus_123", "value": "61"},
            "identifier": "usage-event-123",
            "timestamp": 1_799_999_000,
        },
        options={"idempotency_key": "usage-event-123"},
    )
