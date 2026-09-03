from __future__ import annotations

from typing import Any

import pytest

from app.domains.telephony.dependencies import get_provisioning_provider
from app.domains.telephony.provider import ProvisioningResult


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
