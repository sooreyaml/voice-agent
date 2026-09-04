from __future__ import annotations

from typing import Any

import pytest

from app.domains.telephony.dependencies import get_provisioning_provider
from app.domains.telephony.provider import ProvisioningResult

# -- shared onboarding helpers -------------------------------------------
#
# The gated signup flow is: POST /auth/signup -> (verify email) -> PUT the
# business profile, which provisions the number when billing is off. Tests that
# just need "a working organization" call ``onboard``; tests that exercise a
# single step call the smaller helpers.

DEFAULT_PASSWORD = "correct horse staple 9"

DEFAULT_INTAKE: dict[str, Any] = {
    "legal_name": "Acme Co LLC",
    "address_line1": "500 Main Street",
    "address_line2": None,
    "city": "Austin",
    "region": "TX",
    "postal_code": "78701",
    "country": "US",
    "contact_email": "owner@acme.test",
    "contact_phone": "+15125550100",
    "business_name": "Acme Co",
    "timezone": "America/Chicago",
    "industry": "Professional services",
    "what_you_do": "We take inbound calls and capture leads for the business.",
}


def csrf_headers(client: Any) -> dict[str, str]:
    return {"X-CSRF-Token": client.cookies["csrf"]}


def api_signup(
    client: Any,
    *,
    email: str = "owner@acme.test",
    organization_name: str = "Acme Co",
    password: str = DEFAULT_PASSWORD,
) -> dict[str, Any]:
    response = client.post(
        "/api/v1/auth/signup",
        json={
            "email": email,
            "password": password,
            "organization_name": organization_name,
        },
        headers=csrf_headers(client),
    )
    response.raise_for_status()
    return response.json()


def verify_owner_email(client: Any, *, email: str = "owner@acme.test") -> None:
    """Confirm the owner's email straight through the service layer (no need to
    scrape the code out of the logs)."""
    from app.domains.auth.service import (
        confirm_email_verification,
        issue_email_verification_code,
    )

    store = client.app.state.store
    secret = client.app.state.settings.auth_session_secret
    user = store.get_user_by_email(email)
    assert user is not None
    code = issue_email_verification_code(store, str(user["id"]), secret=secret)
    confirm_email_verification(store, str(user["id"]), code, secret=secret)


def complete_business_profile(
    client: Any, organization_id: str, *, headers: dict[str, str] | None = None, **overrides: Any
) -> Any:
    payload = {**DEFAULT_INTAKE, **overrides}
    return client.put(
        f"/api/v1/organizations/{organization_id}/business-profile",
        json=payload,
        headers=headers or csrf_headers(client),
    )


def onboard(
    client: Any,
    *,
    number: str | None = "+15550000123",
    country: str = "US",
    email: str = "owner@acme.test",
    organization_name: str = "Acme Co",
    verify_email: bool = False,
    **intake_overrides: Any,
) -> str:
    """Full billing-off happy path. Returns the organization id with a live
    number and a published agent."""
    store = client.app.state.store
    if number:
        store.add_pool_number(number, country)
    body = api_signup(client, email=email, organization_name=organization_name)
    organization_id = str(body["organization"]["id"])
    if verify_email:
        verify_owner_email(client, email=email)
    intake_overrides.setdefault("country", country)
    intake_overrides.setdefault("business_name", organization_name)
    intake_overrides.setdefault("contact_email", email)
    response = complete_business_profile(client, organization_id, **intake_overrides)
    response.raise_for_status()
    return organization_id


class FakeProvisioningProvider:
    account_sid = "AC_test"

    def __init__(self) -> None:
        self.available: list[str] = []
        self.purchased: list[str] = []
        self.searches: list[dict[str, Any]] = []

    def add_available(self, *phone_numbers: str) -> None:
        self.available.extend(phone_numbers)

    def search_available_numbers(
        self,
        country_code: str,
        number_type: str,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        self.searches.append(
            {"country": country_code, "number_type": number_type, **kwargs}
        )
        limit = int(kwargs.get("limit", 10))
        return [
            {"phone_number": phone_number} for phone_number in self.available[:limit]
        ]

    def provision_number(self, phone_number: str, **_kwargs: Any) -> ProvisioningResult:
        self.available.remove(phone_number)
        self.purchased.append(phone_number)
        return ProvisioningResult(
            account_sid=self.account_sid,
            phone_number_sid=f"PN{len(self.purchased):032d}",
            trunk_sid="TK_test",
            phone_number=phone_number,
        )


@pytest.fixture(autouse=True)
def fake_provisioning_provider() -> FakeProvisioningProvider:
    """Prevent account-signup tests from ever reaching the real Twilio API."""
    import app.main

    fake = FakeProvisioningProvider()
    previous = app.main.app.dependency_overrides.get(get_provisioning_provider)
    app.main.app.dependency_overrides[get_provisioning_provider] = lambda: fake
    try:
        yield fake
    finally:
        if previous is None:
            app.main.app.dependency_overrides.pop(get_provisioning_provider, None)
        else:
            app.main.app.dependency_overrides[get_provisioning_provider] = previous
