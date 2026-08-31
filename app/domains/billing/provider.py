from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from stripe import SignatureVerificationError, StripeClient, StripeError


class StripeBillingError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.message = message[:1000]
        self.retryable = retryable


@dataclass(frozen=True)
class HostedSession:
    id: str
    url: str
    expires_at: int | None = None


class StripeBillingService:
    """Stripe boundary for checkout, portal, signed webhooks, and meters."""

    def __init__(
        self,
        secret_key: str,
        webhook_secret: str,
        *,
        client_factory: Callable[..., StripeClient] = StripeClient,
    ) -> None:
        self.secret_key = secret_key.strip()
        self.webhook_secret = webhook_secret.strip()
        self._client_factory = client_factory
        self._client: StripeClient | None = None

    def _stripe(self) -> StripeClient:
        if not self.secret_key:
            raise StripeBillingError("Stripe billing is not configured.")
        if self._client is None:
            self._client = self._client_factory(
                self.secret_key,
                max_network_retries=2,
            )
        return self._client

    @staticmethod
    def _call(operation: Callable[[], Any]) -> Any:
        try:
            return operation()
        except StripeError as exc:
            status = int(getattr(exc, "http_status", 0) or 0)
            retryable = status in {409, 429} or status >= 500
            raise StripeBillingError(str(exc), retryable=retryable) from exc

    def create_checkout_session(
        self,
        *,
        organization_id: str,
        plan_id: str,
        price_id: str,
        customer_email: str,
        customer_id: str | None,
        success_url: str,
        cancel_url: str,
        idempotency_key: str,
    ) -> HostedSession:
        params: dict[str, Any] = {
            "mode": "subscription",
            "line_items": [{"price": price_id, "quantity": 1}],
            "success_url": success_url,
            "cancel_url": cancel_url,
            "client_reference_id": organization_id,
            "metadata": {
                "organization_id": organization_id,
                "billing_plan_id": plan_id,
            },
            "subscription_data": {
                "metadata": {
                    "organization_id": organization_id,
                    "billing_plan_id": plan_id,
                }
            },
        }
        if customer_id:
            params["customer"] = customer_id
        else:
            params["customer_email"] = customer_email
        session = self._call(
            lambda: self._stripe().v1.checkout.sessions.create(
                params,
                options={"idempotency_key": idempotency_key},
            )
        )
        return HostedSession(
            id=str(session.id),
            url=str(session.url),
            expires_at=(int(session.expires_at) if session.expires_at else None),
        )

    def create_portal_session(
        self, *, customer_id: str, return_url: str
    ) -> HostedSession:
        session = self._call(
            lambda: self._stripe().v1.billing_portal.sessions.create(
                {"customer": customer_id, "return_url": return_url}
            )
        )
        return HostedSession(id=str(session.id), url=str(session.url))

    def construct_event(self, payload: bytes, signature: str | None) -> dict[str, Any]:
        if not self.webhook_secret:
            raise StripeBillingError("Stripe webhook verification is not configured.")
        event = self._stripe().construct_event(
            payload,
            signature,
            self.webhook_secret,
        )
        return event.to_dict_recursive()

    def send_meter_event(
        self,
        *,
        event_name: str,
        customer_id: str,
        quantity: int,
        identifier: str,
        timestamp: int,
    ) -> str:
        event = self._call(
            lambda: self._stripe().v1.billing.meter_events.create(
                {
                    "event_name": event_name,
                    "payload": {
                        "stripe_customer_id": customer_id,
                        "value": str(quantity),
                    },
                    "identifier": identifier,
                    "timestamp": timestamp,
                },
                options={"idempotency_key": identifier},
            )
        )
        return str(event.identifier)


__all__ = [
    "HostedSession",
    "SignatureVerificationError",
    "StripeBillingError",
    "StripeBillingService",
]
