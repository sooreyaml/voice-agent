from __future__ import annotations

from fastapi import FastAPI

from app.api import install_api
from app.domains.billing.services import spend
from app.settings import load_settings


def test_billing_can_be_disabled_from_the_environment(monkeypatch) -> None:
    monkeypatch.setenv("BILLING_ENABLED", "false")

    assert load_settings().billing_enabled is False


def test_disabled_billing_routes_are_not_installed() -> None:
    test_app = FastAPI()
    install_api(test_app, billing_enabled=False)
    paths = set(test_app.openapi()["paths"])

    assert "/api/v1/auth/login" in paths
    assert "/api/v1/billing/plans" not in paths
    assert "/api/v1/organizations/{organization_id}/billing/spend-limit" not in paths
    assert "/api/v1/organizations/{organization_id}/usage" not in paths
    assert "/webhooks/stripe" not in paths


def test_disabled_billing_bypasses_spend_checks(monkeypatch) -> None:
    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("spend status must not be queried")

    monkeypatch.setattr(spend, "spend_limit_status", fail_if_called)

    assert spend.call_is_allowed(None, "org_123", billing_enabled=False) is True
